# F1 MONEY ENGINE DESIGN

Reference HEAD: `0b2973d` (post-F0 closure + lease fix).

> Documento de diseño histórico. La implementación posterior de F1.1/F2/F3
> archiva `MonthlyBudget`, convierte `/liberado` en alias de `/disponible`
> y agrega ciclos y calendarios por moneda. Los alcances «futuro» y las
> preguntas de este documento describen aquella referencia, no pendientes
> de la versión actual. Ver `ROADMAP.md` y `REPORTE_F3_CURRENCY_RELEASE.md`.

---

## A. CURRENT MODEL AUDIT

### A.1 MonthlyBudget (`app/fixed_expenses/models.py:151-184`, table `monthly_budget`)
- **Hoy.** Una fila por `(telegram_user_id, month_year)`. Guarda `income` y `extra`. Único consumo real: `FixedExpenseService.month_summary` produce `MonthlyBudgetOut{income, extra, total_fixed_expected, total_fixed_paid, liberado=income+extra-total_fixed_paid}` (`app/fixed_expenses/service.py:490-519`). Lo lee `/liberado` y `/api/month-summary` (`app/bot/handlers.py:1037-1038`, `app/web/routes.py:357`).
- **Qué se puede reutilizar.** El campo `income` es semánticamente la *gross income del ciclo*. `extra` cubre cualquier inflow adicional.
- **Peligro.** El nombre `monthly_budget` y la lógica asumen "1 ciclo = 1 mes calendario". No hay forma de configurar payday o días variables. `liberado` suma `+ extra` (lo cual es un *income* auxiliar, no un gasto) y resta únicamente `total_fixed_paid`. **No descuenta expenses reales variables ni savings reserved.** Es decir: `liberado` actual NO responde "¿cuánto puedo gastar?".
- **Extender.** Reusar la tabla como bridge inicial: por un periodo de transición `MonthlyBudget.income` sigue siendo la fuente para `/liberado`. El Money Engine NO la extiende; introduce su propio modelo `FinancialCycle` y/o `SavingsPlan` y deja `MonthlyBudget` deprecada como legacy. La sección P describe la transición.

### A.2 FixedExpense / FixedExpensePayment (`app/fixed_expenses/models.py:35-148`)
- **Hoy.** `FixedExpense` = template recurrente (monto esperado, día de vencimiento, categoría). `FixedExpensePayment` = occurrence por mes (`month_year` YYYY-MM, `paid_at`, `actual_amount`, `skipped`, `expense_id`). Unique `(fixed_expense_id, month_year)`. F0 garantiza idempotencia entre `mark_paid` y `expenses.expense_id` vía `source_key` y partial unique index.
- **Reutilizable.** La fila template es una *obligación prevista* en el ciclo si su `due_day_of_month` cae dentro del ciclo. La fila payment es un *committed occurrence*: `paid_at IS NOT NULL AND skipped=False AND expense_id IS NOT NULL` ⇒ *realized*.
- **Peligro.** El actual `month_summary` cuenta `total_fixed_paid` pero NO descuenta el `expense` mirror (porque está embebido en `total_fixed_paid` ya como espejo). Si F1 también sumara `expenses` mirror como `realized_expenses` se duplicaría. **El Money Engine debe saber que el pago es el espejo, no agregarlo dos veces.**
- **Extender.** Añadir un flag o un campo derivado `is_committed_in_cycle`. Mantener `paid_at` y `expense_id` como hasta ahora.

### A.3 Expense (`app/expenses/models.py:24-121`)
- **Hoy.** Línea de gasto con `currency`, `expense_date`, `category_id`, `source_type` (`telegram`, `fixed_payment`, `recurring`), `source_key`, `deleted_at` (soft-delete). F0 provee el partial unique index `(telegram_user_id, source_key) WHERE source_key IS NOT NULL`.
- **Reutilizable.** Es la fuente de `realized_expenses` cuando `deleted_at IS NULL`, `expense_date IN cycle`. Excluir explícitamente `source_type='fixed_payment'` y `source_type='recurring'` que ya están representados por sus tablas dueñas. O incluirlo y descontar `fixed_expense_payments.expense_id` para evitar doble conteo. **Decisión F1:** descontar `fixed_expense_payments.expense_id` y `recurring_occurrence_expense_id` (ver A.4) del total de `expenses` cuando se calcula `realized_expenses`. Así un mismo `Expense` row nunca cuenta dos veces.
- **Peligro.** Hard-deleted rows faltan — siempre soft-delete. Idempotente en source_key — el Money Engine DEBE usar el `id` único.
- **Extender.** Ningún cambio de schema. Sólo filtros y joins al consumir.

### A.4 RecurringExpense / RecurringReminder (`app/recurring/models.py:31-136`)
- **Hoy.** `RecurringExpense` = template mensual/anual con `next_due_date`. `RecurringReminder` = state machine de delivery (`pending` → `sending` → `sent`/`resolved`). El expense real se crea vía `RecurringService.register_occurrence` con `source_type='recurring'` y `source_key='recurring:<id>:<occurrence_date>'`. **No existe en HEAD un campo que distinga "obligación financiera" de "recordatorio".**
- **Reutilizable.** El template mismo es una ocurrencia potencial. La lógica de `register_occurrence` ya devuelve `(template, saved_expense)` idempotente.
- **Peligro.** Contar automáticamente cada `RecurringExpense` activo como obligación predicha sería incorrecto: muchos `RecurringExpense` son sólo recordatorios de gastos que el usuario decide caso por caso. Asumirlo mete obligaciones falsas.
- **Extender.** Añadir `is_obligation` (Boolean, default False) en `RecurringExpense`. Si True, el template entra a `pending_recurring_obligations` mientras no esté `register_occurrence` aplicado. La FASE 4 define la semántica exacta.

### A.5 Budget (`app/budgets/models.py:30-78`, table `budgets`)
- **Hoy.** Cap por categoría + moneda. Sirve para alertas al cruzar 80% / 100% del límite mensual calendario. No participa del flujo "available to spend".
- **Reutilizable.** Conceptualmente independiente del Money Engine. **Se conserva tal cual.**
- **Peligro.** Mezclar límites por categoría con available-to-spend sería una mentira financiera: el límite es una guía, el available es estado. No se tocan.
- **Extender.** Ninguno en F1.

### A.6 Servicios y queries existentes
- `ExpenseRepository.sum_total`, `sum_by_period`, `sum_by_period_and_category`, `sum_by_period_for_category`, `sum_by_category`, `sum_by_category_per_currency`, `sum_for_category`, `largest` — todo en `app/expenses/repository.py:157-376`. **Reutilizables como primitivas**, devolviendo `Decimal` por moneda. Todos excluyen `_NOT_DELETED`.
- `FixedExpenseRepository.payments_for_month(user_id, month_year)` — devuelve las occurrences de un mes calendario. **Insuficiente para ciclos no-calendario**: el Money Engine necesita `payments_in_cycle(cycle_start, cycle_end)`. Hay que escribir un query nuevo.
- `ExpenseService.register_many`, `FixedExpenseService.mark_paid`, `RecurringService.register_occurrence` — **se preservan tal cual**. El Money Engine no muta; sólo lee.
- `PeriodWindow` (`app/web/queries.py:64-71`) — conceptualmente alineado con cycle window, pero no incluye `cycle_id`, `payday`, `closed_at`, ni `cycle_index`. El Money Engine introduce `CycleWindow` como dataclass más rica.
- `app/utils/dates.today_in_tz(tz_name)` — utilizado para `business_today`. **Reutilizable** para resolver el "today" del ciclo en la zona de negocio. Respeta el principio F0.

### A.7 `/liberado` (`app/bot/handlers.py:1037-1131`) y `/api/month-summary` (`app/web/routes.py:357`, `app/web/queries.py:782-799`)
- **Hoy.** Devuelven `MonthlyBudgetOut{income, extra, total_fixed_expected, total_fixed_paid, liberado}`. Lo consume el dashboard JS (`app/web/routes.py:1240-1249`) para animar el KPI.
- **Reutilizable.** Compatible hacia atrás mientras la transición esté activa. F1 lo reemplaza por `/api/cycle-summary` que retorna la `CycleSummary` nativa, y mantiene `/api/month-summary` con un adapter que produce el shape viejo desde el nuevo engine. La sección P describe la transición.

---

## B. CHOSEN DOMAIN MODEL

### B.1 Alternativas evaluadas

**A. Extender `MonthlyBudget`.**
Pros: 1 tabla, 1 endpoint, baja fricción.
Contras:
- El nombre congela la semántica a mes calendario.
- No soporta `payday ≠ 1` del mes.
- Acopla `income + extra` con `savings_target/reserved/realized` en la misma fila — multi-currency se vuelve tabla ancha.
- El campo `extra` se confunde entre "extra income del mes" y "ahorro extra" / "extra expense".
- El dashboard ya consume `MonthlyBudgetOut`. Cualquier ampliación cambia contrato silenciosamente.
- Migración: alterar columna + nullable + defaults — invasivo para algo que es semánticamente un modelo nuevo.

**B. Crear entidades nuevas: `FinancialCycle`, `SavingsPlan`, `SavingsRealization`.**
Pros:
- Aíslan la semántica del ciclo de la del mes calendario.
- Permiten multi-currency limpio: cada ciclo tiene múltiples `IncomeRecord` por moneda.
- Permiten multi-target de ahorro por ciclo.
- Permite extender sin tocar `FixedExpense` ni `Expense`.
- Coexisten durante la transición con `MonthlyBudget` legacy.
Contras:
- Más tablas.
- Hay que mantener dos fuentes de verdad durante la transición. Se resuelve marcando `MonthlyBudget` deprecated y haciendo que `FinancialCycleService` lea de ambos.

### B.2 Decisión

**Opción B.** Se crean tres entidades nuevas + un servicio `MoneyEngine`:

1. **`FinancialCycle`** (uno por usuario por ciclo). Tabla `financial_cycles`.
2. **`CycleIncome`** (N filas por ciclo, una por moneda + tipo). Tabla `cycle_incomes`.
3. **`SavingsPlan`** (una fila por ciclo, configurable). Tabla `savings_plans`.
4. **`SavingsRealization`** (N filas por ciclo, audit trail de cuánto se apartó efectivamente). Tabla `savings_realizations`.

Se conservan `MonthlyBudget` y `Budget` (este último es independiente). `MonthlyBudget` queda deprecated y su consumo se reemplaza por adapter.

Justificación resumida:
- `MonthlyBudget` mezcla concerns incompatibles (income/extra de un mes calendario + ausencia de savings).
- `Budget` por categoría es una guía, no estado financiero; no debe mezclarse.
- El ciclo no-calendario requiere una entidad que represente "qué payday tengo y cuándo termina". `MonthlyBudget` no modela eso.
- Multi-currency limpio: cada `CycleIncome` tiene su moneda.

---

## C. FINANCIAL DEFINITIONS

Todas las definiciones aplican por moneda. Donde diga "X" se entiende `X_by_currency: dict[str, Decimal]`. Para mantener legibilidad, los ejemplos usan ARS y números redondos.

### C.A `gross_income_c` — Ingreso bruto del ciclo, por moneda
- **Source of truth.** `cycle_incomes` filtrado por `cycle_id`.
- **Componentes.** `income` (sueldo/principal) + `extra` (inflows adicionales) + `adjustment` (positivos o negativos aplicados manualmente).
- **Filtros.** Solo filas del ciclo abierto (o del ciclo en consulta). `currency` particionado.
- **Lifecycle.** Insertadas al crear el ciclo; pueden actualizarse en cualquier momento (cobro real puede diferir del calendarizado).
- **Edge cases.**
  - `gross_income = 0` ⇒ `available` puede ser negativo.
  - `gross_income` registrado tarde no cambia su fecha lógica; cambia la realización del savings_target.
  - Ingreso extra `extra > 0` incrementa `available` desde el momento en que se inserta.

### C.B `savings_target_c`
- **Source of truth.** `savings_plans.target_amount` por `(cycle_id, currency)`.
- **Componentes.** Porcentaje configurable (`target_pct`) o monto absoluto (`target_amount`). Si se definen ambos, gana el absoluto.
- **Default.** `target_pct = 0.20`. Configurable por ciclo.
- **Lifecycle.** Inmutable salvo cambio explícito del usuario. Cambios a mitad de ciclo sólo afectan el cálculo a futuro (ver FASE 5).
- **Currency.** Una `SavingsPlan` por `(cycle_id, currency)`.

### C.C `savings_reserved_c` — Reservado, NO disponible para gastar
- **Source of truth.** **Derivado** = `min(savings_target, max(0, gross_income - realized_expenses - pending_reserved_obligations))`. No es una columna; es una vista derivada.
- **Semántica.** "Lo que YA no se puede gastar porque está comprometido al ahorro del ciclo." Reduce `available_to_spend` inmediatamente.
- **Lifecycle.** Cambia cada vez que cambia `gross_income`, `realized_expenses`, `pending_reserved_obligations` o `savings_target`.
- **Edge case.** Si `savings_target > gross_income`, `savings_reserved = gross_income - realized_expenses - pending_reserved_obligations` (clamp a 0 desde arriba). El usuario habrá reservado más de lo posible.

### C.D `savings_realized_c` — Dinero efectivamente apartado
- **Source of truth.** `savings_realizations.amount` por `(cycle_id, currency)`. **Distinto de reserved.**
- **Semántica.** "Dinero que ya está fuera de la cuenta corriente." Su aumento NO vuelve a reducir `available` porque `available` ya estaba reducido por `savings_reserved`.
- **Lifecycle.** Aumenta con cada `/reserve_savings <amount>` confirmado. Disminuye sólo con un `/release_savings` (unmark explícito).
- **Inferencia.** El usuario puede "guardar" ahorros sin tener el dinero reservado primero (caso C en FASE 5). El engine tolera esto; permite `savings_realized > savings_reserved` pero lo reporta.
- **Edge cases.** `realized > reserved`: permitido; significa que el usuario transfirió dinero a ahorro antes de formalizar el target. `realized < reserved`: lo normal; significa que aún no transfirió todo.

### C.E `realized_expenses_c`
- **Source of truth.** `expenses` rows filtradas por:
  - `telegram_user_id = user_id`,
  - `currency = c`,
  - `deleted_at IS NULL`,
  - `expense_date >= cycle_start AND expense_date <= cycle_end`,
  - **`expense_id NOT IN (SELECT expense_id FROM fixed_expense_payments WHERE expense_id IS NOT NULL)`** ← evita doble conteo.
- **Excluir también.** `expenses.source_key LIKE 'recurring:%'` con `expense_date` dentro del ciclo cuyo `RecurringExpense.is_obligation = true` ⇒ ya está contabilizado vía `pending_recurring_obligations` y, una vez registrado, vía el `Expense` mismo. Pero para no perderlo, **se descuenta si y sólo si está marcado como obligación y dentro del ciclo**, y se descuenta también del bucket `pending_recurring_obligations` al realizarse. Esta regla se afina en FASE 4.
- **Lifecycle.** Crece con cada gasto confirmado. Decrece con soft-delete (que ya borra lógicamente). Se recalcula en cada consulta.

### C.F `pending_fixed_obligations_c`
- **Source of truth.** Suma de `fixed_expenses.expected_amount` (en su moneda) donde:
  - `is_active = true`,
  - `currency = c`,
  - **su ocurrencia cae dentro del ciclo y NO está paid ni skipped** ⇒ `(SELECT paid_at FROM fixed_expense_payments WHERE fixed_expense_id=fixed_expenses.id AND month_year=cycle_month_year) IS NULL AND (SELECT skipped FROM fixed_expense_payments ...) = false`.
- **Componentes por ocurrencia.** `expected_amount` o `actual_amount` si fue marcado con monto real. **Si ya fue pagado pero el ciclo actual sigue abierto**, NO entra aquí (cuenta como realized vía expense mirror).
- **Lifecycle.** Se recalcula al consultar; cambia cuando se paga o se salta.

### C.G `pending_recurring_obligations_c`
- **Source of truth.** `RecurringExpense` filas con `is_obligation = true` y `is_active = true`, donde existe una ocurrencia dentro del ciclo (`next_due_date >= cycle_start AND next_due_date <= cycle_end`) y NO existe `Expense` con `source_key = recurring:<id>:<occurrence_date>` y `deleted_at IS NULL` para esa ocurrencia.
- **Lifecycle.** Disminuye cuando el usuario marca "sí" (register_occurrence) o "no" (skip_occurrence).

### C.H `pending_reserved_obligations_c`
- **Definición.** `pending_fixed_obligations_c + pending_recurring_obligations_c`. Es el total reservado por obligaciones futuras dentro del ciclo.

### C.I `available_to_spend_c`
```
available_to_spend_c =
    gross_income_c
  - savings_reserved_c
  - realized_expenses_c
  - pending_reserved_obligations_c
```
- **Puede ser negativo.** Eso significa "ya gastaste más de lo que podrías".
- **Currency-partitioned.** Nunca se devuelve un valor cross-currency.
- **Por convención, jamás se reduce dos veces por el mismo motivo.** Si una obligación ya estaba en `pending_reserved_obligations` y se paga, el `Expense` mirror queda en `realized_expenses`, pero `pending_reserved_obligations` cae en `0` para esa obligación. **El delta total es 0.**

### C.J `daily_safe_to_spend_c`
- **Fórmula.** `available_to_spend_c / remaining_days_in_cycle_c` donde `remaining_days` cuenta **días restantes incluyendo hoy**.
- **`remaining_days` se calcula en la zona de negocio.** `today_business = today_in_tz(settings.timezone)`. `remaining_days = (cycle_end - today_business).days + 1`.
- **Edge cases.** Ver FASE 7.

---

## D. CYCLE MODEL

### D.1 Entidad `FinancialCycle`
- Una fila por `(telegram_user_id, cycle_index)` autoincremental por usuario.
- Campos:
  - `id, telegram_user_id, cycle_index, start_date, end_date, payday_day, status ('open'|'closed'), opened_at, closed_at`.
  - `opened_at`, `closed_at` son timestamps UTC tz-aware.
  - `start_date`, `end_date` son fechas en zona de negocio.
- `cycle_index` permite ordenar y resolver "ciclo actual" sin depender de mes calendario.
- Un solo ciclo puede estar `open` por usuario (constraint). F1 cierra el ciclo anterior al abrir el nuevo (transición automática o manual).

### D.2 Resolución del ciclo actual
`get_current_cycle(today_business)`:
1. Si existe `FinancialCycle WHERE telegram_user_id = u AND status='open'` y `today_business BETWEEN start_date AND end_date` ⇒ ése.
2. Si no, calcular el ciclo a partir de la configuración de payday del usuario (ver D.3).
3. Si el ciclo calculado ya pasó y no se cerró, devolverlo como `closed`.

### D.3 Configuración de payday
- Por usuario, no por ciclo. Una sola configuración: `payday_day: int (1..31)`. Si `payday_day=2` y `today_business = 2026-10-15`, el ciclo activo es `2026-10-02 .. 2026-11-01`.
- Manejo de fin de mes: si `payday_day=31` y el mes tiene 30 días, el cobro cae el último día del mes (clamp, no se desplaza al mes siguiente).
- Cobro temprano/tarde: si el ingreso real se registra el 5 pero el payday era 2, NO se reabre el ciclo. El cycle `start_date` no cambia; `gross_income` refleja la realidad.
- Configuración persistida en una nueva tabla `user_settings(telegram_user_id PK, payday_day)`. Default `payday_day = 1`.

### D.4 CycleWindow dataclass
```python
@dataclass
class CycleWindow:
    cycle_id: int
    cycle_index: int
    label: str                  # ej "Ciclo 2 Sep → 1 Oct"
    start: date                 # inclusive
    end: date                   # inclusive
    today: date                 # business today
    days_elapsed: int           # (today - start).days + 1, min 1
    days_in_period: int         # (end - start).days + 1
    remaining_days: int         # (end - today).days + 1, max 1
    is_full_period: bool        # today == end (último día)
    status: Literal["open", "closed"]
```

### D.5 FINANCIAL CYCLE vs CALENDAR REPORTING PERIOD
- **`FinancialCycle`** = unidad de planificación financiera (income, savings, obligations).
- **`CalendarReportingPeriod`** = unidad de reporte histórico (puede ser día, semana, mes calendario, año, custom). Ya existe conceptualmente vía `PeriodWindow` (`app/web/queries.py:64-71`).
- `CycleWindow` es la ventana del ciclo de planificación.
- `PeriodWindow` es la ventana de reporte calendario.
- **No se mezclan.** `/api/cycle-summary` devuelve datos por `CycleWindow`. `/api/summary?period=month` devuelve datos por `PeriodWindow` calendario.
- Consultar "septiembre calendario" estando en ciclo `2 Sep → 1 Oct`: el usuario recibe DOS respuestas distintas: el reporte de septiembre calendario (vía `PeriodWindow`) y el ciclo activo (vía `CycleWindow`).

### D.6 Edge cases de ciclo
- **Meses con distinto número de días.** El cycle_start/cycle_end se resuelve caso por caso. Si payday=31 y se arranca en enero (31 días), el primer cycle_end es 28 de febrero (clamp). El siguiente cycle_start es 1 de marzo si el siguiente payday es 1, o se cae el último día de marzo si payday=31 otra vez. **Nunca se solapan dos ciclos abiertos.**
- **Primer ciclo parcial.** Si el usuario configura payday=15 el día 20, el primer ciclo va del 15 del mes actual al 14 del siguiente. `days_in_period = 31`. Se registra como cycle normal.
- **Cerrar un ciclo.** Al cerrarlo (`status='closed'`, `closed_at = now_utc`), las realized_expenses dentro del window siguen consultables históricamente, pero dejan de participar del cálculo `available_to_spend` del ciclo siguiente.

---

## E. SAVINGS MODEL

### E.1 Estados internos
- `target_amount` y/o `target_pct`. Si ambos: gana absoluto.
- `reserved` (derivado, no columna).
- `realized` (suma de `savings_realizations.amount`).
- Estado explícito no se guarda: `reserved` se calcula on-read, `realized` es suma de filas.

### E.2 Reglas por caso

#### Caso A. target=220k, reserved=220k, realized=0
- `savings_reserved_c = 220000`. Reduce `available` en 220000.
- `savings_realized_c = 0`. No hay realization rows.
- `available = gross - 0 - realized_expenses - pending_reserved - 220000`.

#### Caso B. reserved=220k, realized=150k
- `savings_reserved_c = 220000`. Sigue restando 220000.
- `savings_realized_c = 150000`. NO vuelve a restar; ya está restado vía reserved.
- `available` no se reduce dos veces. El usuario "ya descuenta" 220k, de los cuales 70k todavía están en cuenta corriente y 150k ya están transferidos.

#### Caso C. realized > reserved (caso raro, ej. usuario transfiere antes de fijar target)
- `savings_reserved_c` se calcula como si el target fuera 0 o el configured_pct (clamp a gross - otros).
- `savings_realized_c = 250000`, `reserved = 220000`. El usuario "sobre-realizó".
- `available` ya está reducido por reserved. NO se vuelve a reducir.
- Reporte: `savings_realized > savings_reserved`. Estado informativo.

#### Caso D. usuario reduce target a mitad de ciclo (de 220k a 100k)
- `savings_reserved` cae de 220000 a 100000 a partir del momento del cambio.
- `available` AUMENTA en 120000.
- `savings_realized` no cambia (es histórico).
- Si el usuario ya había realizado 150k, ahora `realized > reserved`. Eso es OK; el engine lo reporta.

#### Caso E. usuario libera parte del reserved (unmark explícito)
- Acción: reducir `target_amount` (caso D) o marcar `savings_plans.is_locked_until_close = true` para congelar el cálculo.
- Decisión F1: **`savings_reserved` es derivado, no se "libera" manualmente**. Si el usuario quiere liberar, baja el target. Una nueva acción `/release_savings <amount>` reduce `target_amount` en `min(amount, current_target)`.

#### Caso F. ingreso cambia durante el ciclo
- Si `gross_income` aumenta, `savings_reserved` puede recalcularse hacia arriba (clamp a gross - realized - pending).
- Si `gross_income` baja, `savings_reserved` puede recalcularse hacia abajo (clamp a 0).
- **`savings_reserved` se recalcula on-read**. Cambios en `gross_income` impactan `available` en la próxima consulta.

### E.3 Tabla `savings_realizations`
- `id, cycle_id, currency, amount (>=0), note, created_at (UTC tz-aware)`.
- `cycle_id + currency` particiona.
- `savings_realized_c = SUM(amount) WHERE cycle_id=? AND currency=?`.

---

## F. OBLIGATION MODEL

### F.1 State machine

```
expected (template activo, occurrence dentro del ciclo)
    │
    ▼
reserved (cycle_start <= occurrence_date <= cycle_end, payment aún no realizado)
    │
    ▼
paid/realized (FixedExpensePayment.paid_at IS NOT NULL, skipped=False, expense_id IS NOT NULL)
```

### F.2 Numerales del invariante crítico

**Estado inicial ARS, ciclo abierto, income=1.100.000, savings_target=220.000 (20%).**

| Bucket | Valor |
|---|---|
| gross_income | 1.100.000 |
| savings_reserved (derivado) | 220.000 |
| realized_expenses | 400.000 |
| pending_fixed_obligations (NETFLIX 50.000 + EDESUR 80.000 + GIMNASIO 30.000) | 160.000 |
| pending_recurring_obligations | 40.000 |
| pending_reserved_obligations | 200.000 |
| **available_to_spend** | **1.100.000 − 220.000 − 400.000 − 200.000 = 280.000** |

**Pago de NETFLIX (50.000).**

`FixedExpenseService.mark_paid(user, NETFLIX.id, cycle_month_year, 50000)`:
- Crea/actualiza `FixedExpensePayment.paid_at`.
- Crea o restaura el `Expense` mirror `fixed-payment:NETFLIX.id:cycle_month_year`, `amount=50000`, `source_type='fixed_payment'`.
- `realized_expenses` ahora incluye este `Expense` ⇒ +50.000.
- `pending_fixed_obligations` ya no incluye NETFLIX ⇒ −50.000.
- **delta available = +50.000 (realized) − 50.000 (ya no pending) = 0.**
- **`available_to_spend` sigue en 280.000.**

```
realized_expenses = 450.000
pending_reserved_obligations = 150.000
available = 1.100.000 − 220.000 − 450.000 − 150.000 = 280.000 ✓
```

### F.3 Numerales del invariante para un recurrente marcado como obligación

**Estado:** cycle_id activo, `RecurringExpense{name='Spotify', amount=5.000, is_obligation=true}` con `next_due_date` dentro del ciclo, sin `Expense` registrado.

| Bucket | Valor |
|---|---|
| pending_recurring_obligations | 5.000 |
| available | … − 5.000 |

**Confirmación "sí":**

`RecurringService.register_occurrence(user, spotify.id, today)`:
- Crea `Expense` con `source_key='recurring:<id>:<occurrence_date>'`.
- `realized_expenses` aumenta 5.000.
- `pending_recurring_obligations` cae a 0 (la occurrence está registrada).
- **delta available = 0.**
- `RecurringReminder` delivery state sigue igual.

**Skip "no":**

`RecurringService.skip_occurrence(user, spotify.id, today)`:
- NO crea `Expense`. Avanza `next_due_date`.
- `pending_recurring_obligations` cae a 0 (la occurrence ya pasó).
- **`available` aumenta 5.000**.

**Postpone "posponer":**

`RecurringService.postpone_occurrence(user, spotify.id)`:
- `next_due_date += 1`. La occurrence sigue pendiente.
- Si el nuevo `next_due_date` cae fuera del ciclo, deja de contar para este ciclo.
- `pending_recurring_obligations` se recalcula on-read.

---

## G. AVAILABLE-TO-SPEND FORMULA

```
available_to_spend[c] = gross_income[c]
                     - savings_reserved[c]   (derivado, ver C.C)
                     - realized_expenses[c]
                     - pending_reserved_obligations[c]
```

`pending_reserved_obligations[c] = pending_fixed_obligations[c] + pending_recurring_obligations[c]`

`pending_fixed_obligations[c]`:
- Para cada `FixedExpense` activo en moneda `c`:
  - Si existe `FixedExpensePayment` para esa `(fixed_expense_id, cycle_month_year)` con `paid_at IS NOT NULL AND skipped = false` ⇒ ya realizado, NO entra.
  - Si existe con `skipped = true` ⇒ NO entra.
  - Si no existe, entonces comparar `due_day_of_month` con el ciclo: si el día cae dentro de `[cycle_start, cycle_end]`, agregar `expected_amount` (o `actual_amount` si la fila de pago existe con `paid_at IS NULL` pero hay `actual_amount`).

`pending_recurring_obligations[c]`:
- Para cada `RecurringExpense` activo en moneda `c` con `is_obligation=true`:
  - Iterar `occurrence_dates` que caen dentro del ciclo (función `occurrences_in_range(next_due_date, frequency, start, end)`).
  - Para cada occurrence, si NO existe `Expense` con `source_key='recurring:<id>:<occurrence_date>'` y `deleted_at IS NULL` ⇒ agregar `amount`.

`savings_reserved[c]`:
- `min(savings_target[c], max(0, gross_income[c] - realized_expenses[c] - pending_reserved_obligations[c]))`.

`savings_target[c]`:
- Si `savings_plans.target_amount IS NOT NULL` ⇒ ese.
- Si `savings_plans.target_pct IS NOT NULL` ⇒ `gross_income[c] * pct`.
- Si ambos ⇒ absoluto.
- Si ninguno ⇒ `gross_income[c] * 0.20` (default 20%).

`savings_realized[c]`:
- `SUM(savings_realizations.amount) WHERE cycle_id = ? AND currency = ?`.

---

## H. DAILY SAFE-TO-SPEND

### H.1 Fórmula
```
daily_safe_to_spend[c] = available_to_spend[c] / remaining_days_in_cycle[c]
```
`remaining_days = (cycle_end - today_business).days + 1`

### H.2 Casos
- **Hoy con remaining_days = 1 (último día).** `daily = available / 1 = available`. Se renderiza "podés gastar hasta X hoy".
- **Hoy con remaining_days = 0 (ciclo cerrado).** No se calcula `daily`. El endpoint devuelve `null` con `cycle_status = 'closed'`.
- **`available < 0`.** `daily = available / remaining_days` también es negativo. **No se devuelve 0.** El sistema reporta que el dinero ya fue sobregirado. (I8)
- **Ciclo futuro.** No calculable desde `today`. `cycle_status = 'future'`, `available = null`, `daily = null`.
- **`remaining_days <= 0`.** `daily = null`. Se interpreta "ciclo cerrado".
- **No se incluyen las obligaciones ya reservadas en el divisor** pero sí en el numerador (vía la fórmula `available = gross - reserved - realized - pending`).
- **`available = 0`.** `daily = 0`. No es error.

### H.3 Determinismo
- Sin AI.
- Sin randomness.
- Sin lookup asincrónico.
- Una sola pasada SQL por bucket; sin N+1.
- Orden de buckets estable: gross, savings_reserved, realized_expenses, pending_reserved_obligations.

### H.4 Determinación de `today_business`
- `today_business = today_in_tz(settings.timezone)` (existente en `app/utils/dates.py:70-76`).
- Ese valor es `date` puro. La fórmula `cycle_end - today_business` opera en días, sin DST drama.

---

## I. MULTI-CURRENCY CONTRACT

### I.1 Principio
**ARS y USD SIEMPRE separados.** Ningún cálculo agrega monedas distintas. Ningún endpoint devuelve un `available` global si hay ingresos o gastos en más de una moneda.

### I.2 Tipos Pydantic explícitos

```python
class CurrencyAmount(BaseModel):
    currency: str  # ISO 4217, validated set
    amount: Decimal
```

```python
class MoneyByCurrency(BaseModel):
    amounts: dict[str, Decimal]  # keys = currency codes, values = Decimal

    def get(self, currency: str) -> Decimal: ...
    def currencies(self) -> list[str]: ...
    def add(self, currency: str, amount: Decimal) -> "MoneyByCurrency": ...
    def total(self) -> Decimal | None:
        """Return single-currency total. None if multiple currencies present."""
        ...
```

### I.3 Outputs nativos
Todos los resultados financieros se devuelven como `MoneyByCurrency`:

```json
{
  "income": {"ARS": 1100000.00},
  "extra": {"ARS": 75000.00},
  "savings_target": {"ARS": 220000.00},
  "savings_reserved": {"ARS": 220000.00},
  "savings_realized": {"ARS": 150000.00},
  "realized_expenses": {"ARS": 400000.00},
  "pending_fixed_obligations": {"ARS": 200000.00},
  "pending_recurring_obligations": {"ARS": 0.00},
  "pending_reserved_obligations": {"ARS": 200000.00},
  "available_to_spend": {"ARS": 280000.00},
  "daily_safe_to_spend": {"ARS": 35000.00}
}
```

Si hubiera gastos en USD:

```json
{
  "income": {"ARS": 1100000.00},
  "realized_expenses": {"ARS": 400000.00, "USD": 50.00},
  "available_to_spend": {"ARS": 280000.00, "USD": -50.00},
  ...
}
```

El frontend **no puede** accidentalmente sumar `ARS + USD`: tendría que ignorar `MoneyByCurrency.total()` o iterar explícitamente.

### I.4 Conversión FX
**No se implementa.** Si el usuario tiene ARS y USD, los totales viven en `MoneyByCurrency`. La API nunca devuelve un equivalente en USD implícito.

---

## J. SERVICE BOUNDARY

### J.1 Una sola fuente de verdad: `MoneyEngine`

`app/money/engine.py` expone:

```python
class MoneyEngine:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        ...

    # --- ciclo ---
    def get_current_cycle(self, user_id: int, *, today: date | None = None) -> CycleWindow: ...
    def list_cycles(self, user_id: int, *, limit: int = 12) -> list[CycleWindow]: ...

    # --- resumen ---
    def get_cycle_summary(self, user_id: int, cycle_id: int | None = None) -> CycleSummary: ...
    def get_available_to_spend(self, user_id: int, cycle_id: int | None = None) -> MoneyByCurrency: ...
    def get_daily_safe_to_spend(self, user_id: int, cycle_id: int | None = None) -> MoneyByCurrency | None: ...

    # --- income ---
    def set_income(self, user_id: int, cycle_id: int, *, currency: str, amount: Decimal, kind: Literal["income", "extra", "adjustment"] = "income", note: str | None = None) -> CycleIncome: ...
    def add_extra_income(self, user_id: int, cycle_id: int, *, currency: str, amount: Decimal, note: str | None = None) -> CycleIncome: ...

    # --- savings ---
    def set_savings_target(self, user_id: int, cycle_id: int, *, currency: str, target_pct: Decimal | None = None, target_amount: Decimal | None = None) -> SavingsPlan: ...
    def reserve_savings(self, user_id: int, cycle_id: int, *, currency: str, amount: Decimal, note: str | None = None) -> SavingsRealization: ...
    def release_savings(self, user_id: int, cycle_id: int, *, currency: str, amount: Decimal) -> SavingsPlan: ...  # reduces target_amount

    # --- ciclo lifecycle ---
    def open_cycle(self, user_id: int, *, today: date, payday_day: int | None = None) -> FinancialCycle: ...
    def close_cycle(self, user_id: int, cycle_id: int, *, today: date | None = None) -> FinancialCycle: ...
```

### J.2 Inputs y outputs
- **Inputs.** `user_id`, `cycle_id` (o implícito = current), `today` opcional (default = `business_today`).
- **Outputs.** Dataclasses tipadas `CycleSummary`, `MoneyByCurrency`, `CycleWindow`. Sin `dict[str, Any]` filtrado al frontend.
- **Dependencias.** `Session`, `BusinessTimeProvider` (interfaz inyectable para tests). `Settings` para timezone.
- **No calcula:** qué categoría lidera, comparativas con ciclos anteriores, projections futuras (eso es dashboard analytics, scope post-F1).

### J.3 Lo que NO hace el Money Engine
- **No muta `Expense`, `FixedExpense`, `RecurringExpense`.** Lee. La creación/edición pasa por sus servicios actuales.
- **No maneja Budget por categoría.** Esos viven en `BudgetService`.
- **No decide payday automáticamente.** Lee `user_settings.payday_day`.
- **No implementa FX.** Outputs son siempre por moneda.

### J.4 Frontend/Telegram NO recalculan
- Telegram: lee `MoneyEngine.get_daily_safe_to_spend()` y formatea.
- Dashboard: lee `/api/cycle-summary` y renderiza buckets. **JS nunca suma valores cross-currency.**

---

## K. API CONTRACTS

> **Convención:** todas las respuestas envuelven `{"cycle": {...}, "money": {...}}` para que el frontend siempre tenga el contexto del ciclo.

### K.1 `GET /api/cycle/current?user_id=...`
```json
{
  "cycle": {
    "id": 42,
    "cycle_index": 7,
    "label": "Ciclo 2 Oct → 1 Nov",
    "start": "2026-10-02",
    "end": "2026-11-01",
    "today": "2026-10-15",
    "days_elapsed": 14,
    "days_in_period": 31,
    "remaining_days": 18,
    "is_full_period": false,
    "status": "open",
    "currency_summary": ["ARS"]
  }
}
```

### K.2 `GET /api/cycle-summary?user_id=...&cycle_id=...`
```json
{
  "cycle": { ... },
  "money": {
    "income":            {"ARS": "1100000.00"},
    "extra":             {"ARS": "75000.00"},
    "gross_income":      {"ARS": "1175000.00"},
    "savings_target":    {"ARS": "220000.00"},
    "savings_reserved":  {"ARS": "220000.00"},
    "savings_realized":  {"ARS": "150000.00"},
    "realized_expenses": {"ARS": "450000.00"},
    "pending_fixed_obligations":     {"ARS": "150000.00"},
    "pending_recurring_obligations": {"ARS": "0.00"},
    "pending_reserved_obligations":  {"ARS": "150000.00"},
    "available_to_spend":            {"ARS": "355000.00"},
    "daily_safe_to_spend":           {"ARS": "19722.22"},
    "remaining_days": 18,
    "warnings": []
  }
}
```

Warnings posibles: `"savings_realized_exceeds_reserved"`, `"available_negative"`, `"no_income_recorded"`.

### K.3 `GET /api/available-to-spend`
Devuelve sólo `available_to_spend` (subconjunto para uso rápido por Telegram).

### K.4 `GET /api/daily-safe-to-spend`
Devuelve sólo `daily_safe_to_spend` (subconjunto). `null` si ciclo cerrado o futuro.

### K.5 `POST /api/cycles/income`
```json
// Request
{"cycle_id": 42, "currency": "ARS", "amount": "1100000", "kind": "income"}
// Response
{"cycle": {...}, "money": {...}}
```

### K.6 `POST /api/cycles/savings/target`
```json
// Request
{"cycle_id": 42, "currency": "ARS", "target_pct": "0.20"}   // o target_amount
```

### K.7 `POST /api/cycles/savings/reserve`
```json
// Request
{"cycle_id": 42, "currency": "ARS", "amount": "150000", "note": "transferencia BROU"}
```
Crea una `SavingsRealization`. NO cambia `available_to_spend` (ya estaba reducido por `savings_reserved`).

### K.8 `POST /api/cycles/close`
```json
// Request
{"cycle_id": 42, "force": false}
```
Cierra el ciclo. Rechaza si quedan obligations pendientes (a menos que `force=true`).

### K.9 Endpoints legacy (transición)
- `GET /api/month-summary` sigue existiendo. Devuelve `MonthlyBudgetOut` mapeado desde `MoneyEngine.get_cycle_summary`.
- `/liberado` (Telegram) sigue funcionando, mostrando `available_to_spend` con etiqueta legacy.

---

## L. INVARIANTS

### L.1 Currency safety
- **I1.** `MoneyByCurrency.amounts` nunca contiene sumas cross-currency. Toda operación aritmética ocurre dentro de `currency`. **Test property:** `∀ op, ∀ c: sum(amounts_by_op[c]) == expected_amounts_for_currency_c`.

### L.2 Obligation correctness
- **I2.** Pagar una obligación ya reservada (`mark_paid`) no cambia `available_to_spend`. **Test exacto:** `available_after_pay == available_before_pay`.
- **I3.** Soft-deleted expenses (`deleted_at IS NOT NULL`) no entran en `realized_expenses`. **Test:** un `Expense` con `deleted_at` se restaura ⇒ `realized_expenses` lo incluye de nuevo (válido).
- **I4.** Un `Expense` con `source_key` de tipo `fixed-payment:*` o `recurring:*` cuenta exactamente una vez. Nunca vía `realized_expenses` directo, siempre vía su payment/occurrence dueña. **Test:** `mark_paid` dos veces produce 1 realized_expense con monto actualizado.
- **I5.** `savings_reserved` reduce `available_to_spend` **inmediatamente**. **Test:** cambiar `gross_income` sin tocar savings ⇒ `available` cambia.
- **I6.** `savings_realized` no vuelve a reducir `available_to_spend`. **Test:** `reserve_savings(150000)` después de `target=220k, reserved=220k, realized=0` ⇒ `available` NO cambia; `savings_realized` pasa a 150k.
- **I7.** `available_to_spend` puede ser negativo. **Test:** con `gross=0`, `realized_expenses=100`, `pending=50` ⇒ `available = -150`. La UI debe mostrar el signo negativo explícitamente.
- **I8.** `daily_safe_to_spend` nunca se reporta como `0` para esconder `available` negativo. **Test:** `daily` hereda el signo de `available`. Si `available < 0`, `daily < 0`.

### L.3 Savings state
- **I9.** `savings_reserved` es siempre `>= 0` y `<= savings_target`. **Test:** clamped.
- **I10.** `savings_realized >= 0`. Negativos sólo vía `release_savings` que reduce `target_amount`.
- **I11.** Cambiar `target` afecta `savings_reserved` retroactivamente al siguiente cálculo. NO afecta `savings_realized`.

### L.4 Cycle safety
- **I12.** Un usuario tiene a lo sumo un `FinancialCycle.status='open'` en cualquier momento. **Constraint:** `UNIQUE (telegram_user_id) WHERE status='open'`. (Partial unique index.)
- **I13.** `cycle.end_date >= cycle.start_date`. CHECK constraint.
- **I14.** `payday_day` ∈ [1, 31]. CHECK constraint.
- **I15.** Cerrar un ciclo es atómico: en la misma transacción, marca `closed_at` y bloquea nuevas operaciones contra ese cycle_id.

### L.5 Expense correctness (cross-cuts)
- **I16.** `realized_expenses` nunca incluye `Expense` cuyo `id` está en `fixed_expense_payments.expense_id` (los fixed payments cuentan como realized vía la payment, no vía el expense). **Test SQL:** un `Expense` con `source_key='fixed-payment:*'` aparece 0 veces en `realized_expenses`.
- **I17.** `realized_expenses` no incluye `Expense` cuyo `RecurringExpense.is_obligation=true` ya marcado como "ocurrencia registrada" en el ciclo. Si se quiere incluir, va por separado como `recurring_realized_this_cycle` (no implementado en F1).

### L.6 Time semantics
- **I18.** Los timestamps técnicos (`created_at`, `closed_at`, etc.) son UTC tz-aware. Las fechas (`start_date`, `end_date`, `today`) son `date` puros en zona de negocio.
- **I19.** El cálculo de `remaining_days` usa la zona de negocio: `today = today_in_tz(settings.timezone)`. **Test:** con `timezone=Asia/Tokyo`, el cálculo refleja medianoche Tokio.

### L.7 Determinism
- **I20.** El `MoneyEngine` no hace lookups asincrónicos ni AI. Cada método es una transacción SQL determinística.

---

## M. EDGE CASE MATRIX

| Caso | Resultado esperado |
|---|---|
| No `gross_income` registrada | `gross_income = 0`. `available = -realized - pending - reserved(0)`. UI muestra "Sin ingreso registrado este ciclo." |
| `gross_income = 0` | `savings_reserved = 0`. `available = -(realized + pending)`. Negativo, se reporta. |
| Income registrado tarde | `gross_income` actualizado. `savings_reserved` se recalcula. `available` cambia. |
| Expense con `expense_date < cycle_start` | No entra en `realized_expenses`. Reportable via `PeriodWindow`. |
| Expense con `expense_date > cycle_end` (futuro en mismo ciclo) | No entra. Si el ciclo se cerró, se mueve al siguiente. |
| `savings_target > gross_income` | `savings_reserved = max(0, gross - realized - pending)`. UI: "Target excede ingreso." |
| `obligation > available` | `available` se vuelve negativo. UI muestra rojo. |
| Fijo sin categoría | Funciona igual: el mirror usa `Gastos fijos` (F0). El `pending_fixed_obligations` lo cuenta por `expected_amount`. |
| Fixed payment skipped | `FixedExpensePayment.skipped=true`. No entra en `pending_fixed_obligations`. NO crea `Expense`. |
| Fixed payment unmarked (unmark) | `FixedExpensePayment.paid_at=NULL, expense_id preserved, expense soft-deleted`. Vuelve a `pending_fixed_obligations`. |
| Recurring `is_obligation=false` | No entra en `pending_recurring_obligations`. Si se registra vía `register_occurrence`, entra en `realized_expenses` como cualquier gasto normal. |
| Recurring `is_obligation=true` registrado | `Expense` con `source_key='recurring:<id>:<date>'` se crea. NO entra en `realized_expenses` (cuenta vía payment dueña). Baja `pending_recurring_obligations`. |
| Moneda USD sin income USD | No hay fila en `cycle_incomes` con USD. `gross_income_USD = 0`. Gastos USD suman a `realized_expenses_USD`. `available_USD = -realized_USD`. UI: "Sin ingreso USD este ciclo." |
| Cycle boundary: `today == cycle_end` | `remaining_days = 1`. `daily = available / 1 = available`. |
| Cycle boundary: `today == cycle_start` | `remaining_days = days_in_period`. `daily = available / days_in_period`. |
| Cycle boundary: `today > cycle_end` | `cycle.status = 'closed'` (auto). `daily = null`. UI muestra "Ciclo cerrado." |
| Timezone boundary | `today = today_in_tz(settings.timezone)`. Cambiar `settings.timezone` no cambia `cycle_start/end`. **No se recomputa histórico.** |
| Soft-deleted expense | `deleted_at IS NOT NULL`. Sale de `realized_expenses`. `available` aumenta. |
| Restored expense | `deleted_at = NULL`. Vuelve a `realized_expenses`. `available` baja. |
| Extra income posterior al cálculo | Al consultar de nuevo, `extra` aparece y `gross_income` se recalcula. `savings_reserved` se recalcula. `available` cambia. |
| Mark paid de un fijo sin payment row previa | Crea `FixedExpensePayment` + `Expense` mirror. `pending_fixed_obligations` cae, `realized_expenses` sube. `available` sin cambio neto. |
| Mark paid después de unmark | Restaura el `Expense` previo vía `source_key`. Misma fila física. NO duplica. |
| Mark paid con `actual_amount != expected_amount` | `realized_expenses` usa `actual_amount`. `pending_fixed_obligations` usa `expected_amount` cuando no estaba pagado. |
| Cycle cerrado con obligations no pagadas | El siguiente ciclo las recoge: el `due_day_of_month` del template cae dentro del nuevo ciclo ⇒ entra en `pending_fixed_obligations` del nuevo ciclo. |
| Primer ciclo parcial | `cycle_index=1`, `days_in_period = days reales`. `daily = available / remaining_days`. |
| Usuario reduce target de 220k a 100k | `savings_reserved` recalcula a 100k. `available` aumenta 120k. `savings_realized` intacto. |
| Usuario marca "sí" a un recurring obligation con dinero insuficiente | Se crea el `Expense`. `available` se vuelve negativo. UI: alerta. |

---

## N. MIGRATION PLAN

### N.1 Tablas nuevas

#### `user_settings`
```sql
telegram_user_id BIGINT PRIMARY KEY,
payday_day INT NOT NULL DEFAULT 1 CHECK (payday_day BETWEEN 1 AND 31),
timezone TEXT,  -- opcional override; default = settings.timezone
created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
```
Sin índices adicionales: PK es por user.

#### `financial_cycles`
```sql
id BIGSERIAL PRIMARY KEY,
telegram_user_id BIGINT NOT NULL,
cycle_index INT NOT NULL,           -- 1, 2, 3, ... por usuario
start_date DATE NOT NULL,
end_date DATE NOT NULL,
payday_day INT NOT NULL,             -- snapshot del payday al abrir
status TEXT NOT NULL CHECK (status IN ('open', 'closed', 'future')),
opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
closed_at TIMESTAMPTZ,               -- NULL cuando open
created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
CHECK (end_date >= start_date),
CHECK (payday_day BETWEEN 1 AND 31)
```
Índices:
- `idx_financial_cycles_user_open UNIQUE (telegram_user_id) WHERE status='open'` ← **I12**.
- `idx_financial_cycles_user_index UNIQUE (telegram_user_id, cycle_index)` ← ordinal.

#### `cycle_incomes`
```sql
id BIGSERIAL PRIMARY KEY,
cycle_id BIGINT NOT NULL REFERENCES financial_cycles(id) ON DELETE CASCADE,
currency TEXT NOT NULL,
amount NUMERIC(18, 2) NOT NULL,
kind TEXT NOT NULL CHECK (kind IN ('income', 'extra', 'adjustment')),
note TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT now()
```
Índice: `idx_cycle_incomes_cycle_currency (cycle_id, currency)`.

#### `savings_plans`
```sql
cycle_id BIGINT NOT NULL REFERENCES financial_cycles(id) ON DELETE CASCADE,
currency TEXT NOT NULL,
target_pct NUMERIC(5, 4),            -- ej 0.2000 = 20%
target_amount NUMERIC(18, 2),
created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
PRIMARY KEY (cycle_id, currency),
CHECK (target_pct IS NULL OR (target_pct >= 0 AND target_pct <= 1)),
CHECK (target_amount IS NULL OR target_amount >= 0),
CHECK (target_pct IS NOT NULL OR target_amount IS NOT NULL)
```

#### `savings_realizations`
```sql
id BIGSERIAL PRIMARY KEY,
cycle_id BIGINT NOT NULL REFERENCES financial_cycles(id) ON DELETE CASCADE,
currency TEXT NOT NULL,
amount NUMERIC(18, 2) NOT NULL CHECK (amount >= 0),
note TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT now()
```
Índice: `idx_savings_realizations_cycle_currency (cycle_id, currency)`.

### N.2 Modificaciones a tablas existentes

#### `recurring_expenses`
- Añadir columna `is_obligation BOOLEAN NOT NULL DEFAULT false`.
- Sin índices nuevos. La tabla ya tiene `idx_recurring_user_next_due`.

#### `fixed_expenses`
- Sin cambios de schema. La semántica de obligación ya viene de "el template está activo y la occurrence cae en el ciclo".

### N.3 Orden de migración

1. `0012_user_settings.py` — nueva tabla.
2. `0013_recurring_expense_is_obligation.py` — añade columna.
3. `0014_financial_cycles.py` — nueva tabla.
4. `0015_cycle_incomes.py` — nueva tabla.
5. `0016_savings_plans.py` — nueva tabla.
6. `0017_savings_realizations.py` — nueva tabla.

### N.4 Backfill

- **Ningún backfill automático.** Los datos existentes en `MonthlyBudget` se preservan tal cual.
- El Money Engine sólo actúa cuando el usuario explícitamente abre su primer `FinancialCycle` (vía `/start` o durante el primer `/ingreso` post-migración).
- En el adapter de `/liberado`, si el usuario nunca abrió un `FinancialCycle`, el adapter responde "Aún no iniciaste el sistema de ciclos. Hacé `/start` para configurar tu payday."

### N.5 Constraints / unique keys
- `UNIQUE (telegram_user_id) WHERE status='open'` en `financial_cycles`.
- `UNIQUE (telegram_user_id, cycle_index)` en `financial_cycles`.
- `PRIMARY KEY (cycle_id, currency)` en `savings_plans`.

### N.6 Índices "SOLO si son necesarios"
- `idx_financial_cycles_user_index` necesario para `get_current_cycle()` que consulta `ORDER BY cycle_index DESC LIMIT 1`.
- `idx_cycle_incomes_cycle_currency` necesario para el JOIN de bucket `gross_income`.
- `idx_savings_realizations_cycle_currency` necesario para `SUM(...) GROUP BY currency`.
- NO se añade índice en `fixed_expenses.currency` (la tabla ya tiene `ix_fixed_expense_user_active`).

---

## O. TEST PLAN

### O.1 UNIT (SQLite in-memory, sin red)

#### MoneyEngine — savings
- `test_savings_default_target_is_twenty_percent`: ciclo sin `savings_plans` ⇒ `target_pct=0.20`.
- `test_savings_target_pct_overrides_default`.
- `test_savings_target_amount_overrides_pct`.
- `test_savings_reserved_clamps_to_zero_when_income_insufficient`: target=1000, income=500, realized=0, pending=0 ⇒ reserved=500.
- `test_savings_reserved_clamps_to_target_when_target_smaller`: target=100, income=1000, realized=0, pending=0 ⇒ reserved=100.
- `test_savings_reserved_never_negative`.
- `test_reserve_savings_creates_realization_row_no_change_to_available`.
- `test_release_savings_reduces_target_not_realized`.

#### MoneyEngine — obligations
- `test_pending_fixed_includes_unpaid_active_in_cycle`.
- `test_pending_fixed_excludes_paid`.
- `test_pending_fixed_excludes_skipped`.
- `test_pending_fixed_excludes_out_of_cycle_due_day`.
- `test_pending_fixed_uses_actual_amount_when_marked_with_real`: simulamos payment con `actual_amount`, luego `unmark`, luego `pending` = `actual_amount`.
- `test_pending_recurring_includes_active_obligation_within_cycle`.
- `test_pending_recurring_excludes_registered_occurrence`.
- `test_pending_recurring_excludes_non_obligation_recurring`.

#### MoneyEngine — income / cycle
- `test_set_income_creates_cycle_income_row`.
- `test_add_extra_income_appends_to_gross`.
- `test_set_income_negative_rejected`.
- `test_gross_income_partitioned_by_currency`.

#### MoneyEngine — available
- `test_available_formula_basic`: income=1000, reserved=200, realized=300, pending=100 ⇒ available=400.
- `test_available_handles_negative`: income=100, reserved=0, realized=200, pending=50 ⇒ available=-150.
- `test_paying_pending_obligation_does_not_change_available`: pre = post.
- `test_reserve_savings_does_not_change_available`.
- `test_release_savings_increases_available`.
- `test_income_increase_increases_available_after_reserved_clamp`.
- `test_realized_expense_excludes_fixed_payment_mirror_via_join`.
- `test_realized_expense_excludes_recurring_obligation_mirror`.

#### MoneyEngine — cycle window
- `test_payday_day_default_is_one`.
- `test_payday_day_clamps_to_month_end_for_31`.
- `test_payday_day_resolves_cycle_boundaries`.
- `test_payday_day_february_leap_year_handled`.
- `test_only_one_open_cycle_per_user_constraint`.

#### MoneyEngine — daily safe to spend
- `test_daily_simple_division`.
- `test_daily_equals_available_on_last_day`.
- `test_daily_null_when_cycle_closed`.
- `test_daily_null_when_cycle_future`.
- `test_daily_negative_when_available_negative` (I8).
- `test_daily_zero_when_available_zero`.
- `test_daily_uses_business_timezone_for_today`.

#### Multi-currency
- `test_ars_and_usd_partitioned_in_all_buckets`.
- `test_no_cross_currency_sum_in_any_bucket` (property).
- `test_empty_currency_dict_in_one_currency_does_not_affect_another`.

### O.2 INTEGRATION (PostgreSQL efímero)

- `test_money_engine_after_alembic_upgrade_from_empty_db`.
- `test_money_engine_unique_open_cycle_constraint_via_db`.
- `test_money_engine_savings_plan_target_pct_check_constraint`.
- `test_money_engine_concurrent_set_income_serializable` (dos transactions).
- `test_money_engine_pay_obligation_idempotent_under_concurrent_mark_paid`.
- `test_money_engine_realized_expenses_excludes_fixed_payment_mirror_at_db_level`.

### O.3 PROPERTY / INVARIANT

- **I1.** Property: para cada `(user_id, cycle_id)`, la suma de cualquier bucket por moneda == suma esperada. Test con Hypothesis o for-loop sobre 100 casos aleatorios con seeds determinísticos.
- **I2.** Invariant: `available_to_spend después de mark_paid == available_to_spend antes`. Test con 50 fixtures variando monto, número de obligations, currency.
- **I4.** Property: `mark_paid(paid)` repetido 10 veces produce exactamente 1 `Expense` físico con monto igual al último.
- **I8.** Property: `sign(daily) == sign(available)` salvo cuando `remaining_days <= 0` (`daily=null`).
- **I16.** Property: cualquier `Expense` con `source_key LIKE 'fixed-payment:%'` cuenta 0 veces en `realized_expenses`.

### O.4 Test exacto del invariant-crítico del HUMAN

```python
def test_available_unchanged_when_paid_obligation(in_memory_db):
    # Setup
    set_income(1100000)
    reserve_savings_target(pct=0.20)  # target = 220k
    add_expense(amount=400000)         # realized_expenses = 400k
    add_fixed_expense("NETFLIX", 50000)  # pending_fixed += 50k
    add_fixed_expense("EDESUR", 80000)   # pending_fixed += 80k
    add_recurring_obligation("GYM", 30000)  # pending_recurring += 30k
    # Wait — 30k? OK pending_recurring = 30k; pending_fixed = 130k
    # total pending = 160k. available = 1100 - 220 - 400 - 160 = 320k

    # Sanity
    assert available() == 320000

    # Pay NETFLIX
    mark_paid("NETFLIX", actual=50000)

    # Assertions
    assert realized_expenses() == 450000   # 400k + 50k
    assert pending_fixed() == 80000         # solo EDESUR
    assert available() == 320000            # INVARIANT: igual
```

Variantes con `/reserve_savings 150000`:
```python
def test_reserve_savings_does_not_change_available(in_memory_db):
    set_income(1100000)
    set_savings_target(pct=0.20)
    add_expense(amount=400000)
    add_fixed_expense("NETFLIX", 50000)
    add_fixed_expense("EDESUR", 80000)
    add_recurring_obligation("GYM", 30000)

    available_before = available()
    assert available_before == 320000

    reserve_savings(150000)

    assert savings_realized() == 150000
    assert savings_reserved() == 220000
    assert available() == 320000   # I6
```

### O.5 Test exacto de `daily_safe_to_spend`

```python
def test_daily_simple(in_memory_db):
    set_income(1100000)
    set_savings_target(pct=0.20)
    # ciclo arranca día 2, hoy día 15 (14 días elapsed, 17 remaining)
    open_cycle(today=date(2026, 10, 2), payday_day=2)
    # ... business mock returns date(2026, 10, 15)
    # available = 1100 - 220 = 880 (sin expenses, sin obligations)
    # daily = 880 / 17 = 51.76
    assert daily_safe_to_spend() == Decimal("51.76").quantize(...)
```

---

## P. TRANSITION FROM MONTHLYBUDGET / LIBERADO

### P.1 Política de coexistencia

`MonthlyBudget` y `/liberado` **se preservan** durante la transición. No se borran tablas. No se cambia el comportamiento de `/liberado` salvo que el usuario abra su primer `FinancialCycle`.

### P.2 Estados del usuario

| Estado | Comportamiento |
|---|---|
| Pre-F1 (no abrió cycle) | `/liberado` responde con `MonthlyBudgetOut` legacy. Sin `/api/cycle-summary`. |
| Post-`/start` o primer `/ingreso` post-F1 | `MoneyEngine` activa el primer `FinancialCycle`. `/liberado` ahora delega al Money Engine. |
| Híbrido | `MonthlyBudget` y `FinancialCycle` coexisten. El dashboard decide cuál mostrar basado en `user_settings` o flag de transición. |

### P.3 Adapter

```python
def legacy_liberado(session, user_id, month_year) -> MonthlyBudgetOut:
    cycle = money_engine.get_current_cycle(user_id)
    if cycle is None:
        # Pre-F1: usar MonthlyBudget
        return fixed_expenses.month_summary(user_id, month_year)

    # Post-F1: derivar MonthlyBudgetOut del MoneyEngine
    summary = money_engine.get_cycle_summary(user_id, cycle.cycle_id)
    return MonthlyBudgetOut(
        income=summary.income.ARS,           # tomar ARS por convención legacy
        extra=summary.extra.ARS,
        total_fixed_expected=summary.pending_fixed_obligations.ARS + total_paid_ARS,
        total_fixed_paid=realized_fixed_ARS,
        liberado=summary.available_to_spend.ARS,
    )
```

### P.4 Deprecation timeline
- F1.0: `MoneyEngine` GA. `MonthlyBudget` permanece. Adapter activo.
- F1.1 (futuro, fuera de scope): `MonthlyBudget` se mueve a tabla histórica (`monthly_budget_legacy`). El adapter lee de allí para `/liberado`.
- F2+: `/liberado` se reemplaza por `/disponible` con el shape nuevo. `MonthlyBudget` queda en `monthly_budget_legacy` sin escrituras.

### P.5 Compatibilidad del dashboard
- `app/web/routes.py:357-...` ya consume `MonthlyBudgetOut`. F1 NO cambia el shape del endpoint. Sólo cambia la fuente de datos.
- El JS (`app/web/routes.py:1240-1249`) sigue funcionando sin cambios.

---

## Q. RISKS / OPEN QUESTIONS

> **Nota:** sólo preguntas que requieren decisión humana real y bloquean la implementación.

### Q.1 — ¿Qué hacer con `extra` (ingresos adicionales no recurrentes)?
**Bloqueante.** Hoy `MonthlyBudget.extra` es cualquier inflow no-sueldo. En F1 modelé `cycle_incomes.kind='extra'` con la misma semántica. **¿OK o preferís un modelo distinto?** Alternativa: `extra` se trackea en `CycleIncome(kind='extra')` y nada más. Otra: `extra` se modela como un ajuste al `gross_income` sin fila propia. **Requiere decisión.**

### Q.2 — ¿Qué hacer cuando un recurrente tiene `is_obligation=true` pero el usuario registra el gasto FUERA de Telegram (no entra al flujo `register_occurrence`)?
**Bloqueante.** El template queda "pendiente" para siempre. Tres opciones:
- (a) El engine exige que la "realización" pase por `register_occurrence`. Cualquier gasto manual con el mismo `source_key` se ignora si la template está activa.
- (b) El engine acepta matching flexible: si el usuario crea un `Expense` con `source_type='recurring'` que coincide por nombre/monto/fecha, lo vincula.
- (c) El engine expone `/recurring_mark_paid <id>` como acción explícita.

**Mi recomendación: (c).** Es explícita, idempotente y testeable. Pero requiere UI/Telegram.

### Q.3 — ¿Cómo manejar `payday_day=31` en febrero?
**Bloqueante de coherencia.** Mi diseño: clamp al último día del mes (`calendar.monthrange(year, month)[1]`). **¿OK o preferís desplazamiento al primer día del mes siguiente?** (Típico problema de recurrencias.)

### Q.4 — ¿El usuario debe poder tener más de un `FinancialCycle` "abierto" simultáneamente?
**Bloqueante de constraint.** Mi diseño: uno solo, vía `UNIQUE (telegram_user_id) WHERE status='open'`. Pero si el usuario tiene dos monedas independientes (ARS y USD), ¿necesita dos ciclos? **Mi propuesta: no. Un solo ciclo contiene todas las monedas. Si quiere operar con monedas en schedules distintos, eso es scope F3+, no F1.** Confirmar.

### Q.5 — ¿El dashboard debe mostrar histórico de ciclos cerrados?
**Bloqueante de UI scope.** Mi diseño: `MoneyEngine.list_cycles(limit=12)` retorna los últimos 12 ciclos con su `CycleSummary`. El dashboard histórico calendario (`/api/summary?period=month`) sigue mostrando reportes por mes calendario (independiente del ciclo). **¿OK?**

### Q.6 — Categoría de los savings_realizations
**Bloqueante de taxonomía.** ¿Tienen categoría propia? ¿Entran en una categoría `Ahorro` del seed? Hoy no existe. **Mi propuesta:** añadir `Ahorro` como categoría top-level en una migración del seed. ¿OK o lo mantenemos sin categoría?

### Q.7 — `daily_safe_to_spend` con multi-currency
**Bloqueante de output.** Mi diseño: un `MoneyByCurrency` con un `daily` por moneda. Cada moneda tiene su `daily` independientemente. **¿El usuario quiere un sólo `daily` en ARS implícito (asumiendo que USD es marginal)?** Mi recomendación: NO. Mostrar ambos.

### Q.8 — ¿Quién cierra los ciclos automáticamente?
**Bloqueante de lifecycle.** Mi diseño: cuando `today > cycle_end`, `get_current_cycle` lo devuelve como `closed` con `closed_at = null` (auto-close). Pero el ciclo no se cierra formalmente hasta que el usuario lo confirma. **¿OK o el sistema debe cerrarlo automáticamente?** (Auto-close parece mejor para no acumular ciclos fantasma.)

### Q.9 — Fuente de verdad para `gross_income` cuando ya hay `MonthlyBudget.income`
**Bloqueante de transición.** ¿Migro datos de `MonthlyBudget` al nuevo `FinancialCycle` en F1.0, o sólo cuando el usuario lo pida explícitamente? Mi recomendación: **on-demand**. La migración automática sería invasiva y podría romper al usuario.

---

## R. IMPLEMENTATION SLICES

> Las slices son secuenciales. Cada una deja HEAD verde y con cierre arquitectónico antes de pasar a la siguiente. Máximo 4 slices.

### R.1 Slice 1 — Schema + Money Engine read-only
- Migraciones `0012..0017` + columna `recurring_expenses.is_obligation`.
- `app/money/__init__.py` + `app/money/models.py` + `app/money/repository.py` + `app/money/engine.py` con sólo lectura (`get_current_cycle`, `list_cycles`, `get_cycle_summary`, `get_available_to_spend`, `get_daily_safe_to_spend`).
- Tests unit + integration del engine sin endpoints nuevos.
- Adapter `/liberado` que prefiere el engine si hay cycle, fallback a `MonthlyBudget` legacy.
- **Cierre:** `pytest tests` verde, `pytest tests_postgres` verde. Adapter en producción para usuarios nuevos.

### R.2 Slice 2 — Write APIs + Telegram commands
- `MoneyEngine.set_income`, `add_extra_income`, `set_savings_target`, `reserve_savings`, `release_savings`.
- Endpoints `POST /api/cycles/income`, `POST /api/cycles/savings/target`, `POST /api/cycles/savings/reserve`.
- Comandos Telegram: `/start` (abre cycle), `/cycle` (resumen), `/disponible`, `/gastar_diario`, `/reserve`, `/target`, `/extra`.
- Migración de `recurring_expenses` permite `is_obligation` vía `/recurring_obligation on|off`.
- **Cierre:** invariantes I2, I5, I6, I7, I8 probadas con tests exactos. Cycle activo visible en Telegram.

### R.3 Slice 3 — Cycle lifecycle + dashboard
- `MoneyEngine.open_cycle`, `close_cycle`.
- Lógica auto-close en `get_current_cycle` cuando `today > end`.
- `GET /api/cycle-summary` con buckets full + currency-partitioned.
- `GET /api/cycle-history?limit=12` para histórico.
- Dashboard: nueva sección "Ciclo activo" con los buckets nativos (sin tocar `MonthlyBudgetOut` legacy).
- **Cierre:** dashboard muestra ARS y USD sin sumar. Tests de transición sin doble fuente de verdad.

### R.4 Slice 4 — Migración asistida + deprecation
- Comando `/migrate_cycle` opcional: copia `MonthlyBudget.income`/`extra` del mes actual al `FinancialCycle` activo.
- `MonthlyBudget` marcada deprecated en docstring.
- Documentación de la transición en `README.md`.
- **Cierre:** todo F1 verde. Post-F1 se eliminará `MonthlyBudget`.

---

## S. PROGRESS

PROGRESO F1 PLANNING: 100%

Frenar.
Esperar HUMAN ADVERSARIAL ARCHITECTURE GATE.
