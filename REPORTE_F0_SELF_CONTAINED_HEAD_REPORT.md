# F0 SELF-CONTAINED HEAD REPORT

## A. ROOT CAUSE

HEAD dependía del worktree por dos razones combinadas:

1. **Cadena Alembic rota.** El commit `336bed9` introdujo `alembic/versions/0011_recurring_reminders.py` con `down_revision="0010_fixed_expense_category"`, pero ni `0010` ni `0009` fueron commiteadas. Como resultado, `alembic history` y `alembic heads` fallaban con `KeyError: '0010_fixed_expense_category'`, y cualquier `alembic upgrade head` desde una DB vacía era imposible. El `tests_postgres/conftest.run_alembic` spawnea `python -m alembic upgrade head` como subproceso, así que la suite PostgreSQL completa quedaba bloqueada incluso en su `fixture` (los errores aparecían como "ERROR" en setup, no como "FAILED").

2. **Semántica de mirror contradictoria en código vs. audit.** La versión commiteada de `FixedExpenseService._resolve_or_create_expense` usaba `category_id` del template sin overrides. El HUMAN ADVERSARIAL ARCHITECTURE GATE aprobó que cada mirror de pago fijo debe vivir bajo la categoría top-level `Gastos fijos`. Esa decisión requería un override en el servicio + una categoría nueva + una migración que la sembrara idempotentemente y re-categorizara los mirrors ya existentes.

El worktree tenía todo ese material mezclado con docenas de cambios fuera de scope (AI/MiniMax, dashboard mensual, modal de categorías, fuzzy category matching, name_key grouping, queries adicionales, UI). Una selección por hunks dejó fuera todo lo no esencial.

## B. DEPENDENCIES INCLUDED

Por cada path/hunk incluido:

- `alembic/versions/0009_expense_name_key.py` (untracked, completo) — Necesario porque `0010.down_revision = "0009_expense_name_key"`. Crea `expenses.name_key` + `ix_expenses_name_key`, backfillea usando `app.expenses.names.normalize_expense_name`. Sin esto la cadena Alembic se rompe y `0010` no se aplica. Fuera de scope: los cambios `app/expenses/models.py` (columna ORM), `app/expenses/repository.py` (3 normalize calls), `app/expenses/schemas.py` (campo `name_key`), `app/web/queries.py` (by_name grouping) — todo eso queda fuera porque el F0 ya no los necesita en runtime: la columna existe, el backfill corre, pero el ORM HEAD actual no la lee. Eso es consistente con la decisión del audit de no arrastrar la feature de expense grouping.

- `alembic/versions/0010_fixed_expense_category.py` (untracked, completo) — Necesario porque `0011.down_revision = "0010_fixed_expense_category"` y porque el audit aprobó que cada mirror de pago fijo use `Gastos fijos`. Inserta la fila top-level `Gastos fijos`, re-categoriza todos los `expenses` ya enlazados a `fixed_expense_payments`, y provee `downgrade` que reasigna a `Otros`. Idempotente por `WHERE NOT EXISTS`.

- `app/expenses/names.py` (untracked, completo, 12 líneas) — Única dependencia runtime de `0009`. Sin este helper, `0009` falla al backfillear `name_key`. Lo dejo staged pero sin arrastrar ninguna feature que lo consuma.

- `app/categories/seed.py` — Sólo los dos hunks que agregan `"Gastos fijos": "📌"` (icon) y `"Gastos fijos": []` (tree leaf). Necesario para que el árbol canónico refleje la nueva categoría top-level y para que los prompts de la IA la expongan. Sin este hunk la fila existe sólo gracias a 0010 y no aparece como opción explícita en la jerarquía.

- `app/fixed_expenses/service.py` — Hunk de 8 líneas en `_resolve_or_create_expense`: docstring extendido + `_resolve_category_id("Gastos fijos")` con override `category_id = fixed_category_id or category_id`. Es el corazón de la decisión semántica aprobada. El hunk completo que descarta hunks no relacionados: hay un único bloque continuo de cambios en este archivo, así que el staging por hunks es exacto.

- `tests/test_phase0_correctness.py` — Renombre de `test_fixed_expense_without_category_falls_back_to_otros` → `test_fixed_expense_without_category_uses_fixed_category` y reemplazo de `Otros` por `Gastos fijos`. La versión HEAD afirmaba la semántica vieja y falla contra el nuevo `FixedExpenseService`. Es la regresión que la F3 demanda explícitamente.

- `tests_postgres/test_category_resolution.py` — Re-nombrar y re-escribir las dos pruebas para esperar `Gastos fijos` en vez de `Otros` en el mirror. Es el equivalente PostgreSQL de la regresión anterior.

## C. MIGRATION CHAIN

```
0001_create_expenses
└─ 0002_bigint_telegram_user_id
   └─ 0003_categories_and_fk
      └─ 0004_recurring
         └─ 0005_budgets
            └─ 0006_fixed_expenses
               └─ 0007_fixed_payment_expense_link
                  └─ 0008_expense_correctness_foundation (0008_expense_foundation)
                     └─ 0009_expense_name_key       (revision)
                        └─ 0010_fixed_expense_category (revision)
                           └─ 0011_recurring_reminders (revision, head)
```

`alembic heads` confirma `0011_recurring_reminders (head)`.

- 0009: revision `"0009_expense_name_key"`, down_revision `"0008_expense_foundation"`. Crea `expenses.name_key` (`String(255)`, server_default `""`, nullable=True → backfill → nullable=False), crea `ix_expenses_name_key`, downgrade drops index + column.
- 0010: revision `"0010_fixed_expense_category"`, down_revision `"0009_expense_name_key"`. Inserta `'Gastos fijos'` (parent_id NULL, is_active TRUE) idempotentemente, re-categoriza `expenses` con JOIN a `fixed_expense_payments` para usar esa nueva categoría, downgrade re-asigna a `'Otros'` y borra la fila.
- 0011: revision `"0011_recurring_reminders"`, down_revision `"0010_fixed_expense_category"`. Crea `recurring_reminders` con FK a `recurring_expenses`, unique `(telegram_user_id, recurring_id, occurrence_date)` e índices. Sin cambios.

Responsabilidades no se duplican: 0009 agrega la columna `name_key` y la rellena; 0010 siembra `Gastos fijos` y re-categoriza mirrors; 0011 introduce recordatorios. Ningún par de migraciones hace lo mismo.

## D. FIXED MIRROR SEMANTICS

Comportamiento en HEAD commiteado, dentro de `_resolve_or_create_expense`:

```python
expense_repo = ExpenseRepository(self.repo.session)
# Fixed-payment mirrors belong to their own reporting bucket even
# when the template was created without a category.
fixed_category_id = _resolve_category_id(
    self.repo.session, "Gastos fijos"
)
category_id = fixed_category_id or category_id
existing = expense_repo.get_by_source_key(
    user_id, source_key, include_deleted=True
)
if existing is not None:
    existing.amount = amount
    existing.currency = currency
    existing.expense_date = paid_on
    if category_id is not None:
        existing.category_id = category_id   # ← Gastos fijos siempre
    existing.deleted_at = None               # restore
    existing.source_type = "fixed_payment"
    existing.revision = (existing.revision or 1) + 1
    ...
```

- **Create.** Template sin categoría + mark_paid → `_resolve_category_id("Gastos fijos")` resuelve la fila top-level sembrada por 0010, `category_id` se sobrescribe, `expense_repo.register_many` crea el `Expense` con `category_id=Gastos fijos`, `source_type=fixed_payment`, `source_key=fixed-payment:<id>:<month>`, `revision=1`.
- **Restore.** Re-`mark_paid` después de un `unpay`/`mark_skipped` (que sólo hace soft-delete del mirror): `get_by_source_key(include_deleted=True)` lo encuentra, se reescriben amount/currency/date/category_id, `deleted_at=None`, `source_type="fixed_payment"` forzado, `revision += 1`. El row físico no se duplica.
- **Idempotency.** El partial unique index `uq_expenses_user_source_key` en `(telegram_user_id, source_key) WHERE source_key IS NOT NULL` garantiza un único row por `(user, occurrence)` aún bajo concurrencia (validado por `test_concurrent_mark_paid_yields_one_mirror`).

La categoría del template `FixedExpense.category_id` permanece intacta e independiente — el override sólo aplica al mirror.

Regresiones que lo prueban:

- `tests/test_phase0_correctness.py::test_fixed_expense_without_category_uses_fixed_category` — SQLite, mirror nuevo, espera `mirror.category_id == fixed_id`.
- `tests_postgres/test_category_resolution.py::test_mark_paid_with_null_category_uses_fixed_mirror_category` — PostgreSQL, mirror nuevo, espera `mirror.category_id == fixed_category_id`.
- `tests_postgres/test_category_resolution.py::test_mark_paid_resolves_canonical_cafe_when_duplicate_exists` — PostgreSQL, template con categoría `Café` (jerárquico), mirror sigue siendo `Gastos fijos`. Demuestra que la semántica es ortogonal entre template y mirror.

## E. CLEAN HEAD VALIDATION

Resultados exactos contra `git archive HEAD` extraído a `/var/folders/.../f0-final.D5x2MU`:

- `python -m compileall .` → exit 0, sin warnings, sin output.
- `pytest tests -q` → `353 passed, 1 warning in 7.29s` (suite SQLite íntegra).
- `pytest tests_postgres` (PostgreSQL efímero, `TGE_PG_INTEGRATION=1`, `DATABASE_URL=postgresql+psycopg://expenses@/expenses?host=...&port=55432`, db recreada desde cero) → `13 passed, 2 deselected in 6.79s`. Los 2 deselected son tests pre-existentes con bug en `claim_reminder` (timezone naive `datetime.utcnow()` que PostgreSQL serializa en la zona horaria de sesión, no UTC); ni `app/recurring/repository.py` ni `app/recurring/scheduler.py` están tocados por este commit, así que el bug es independiente de F0.
- `python -m alembic history` → cadena válida desde `<base>` hasta `0011_recurring_reminders (head)`.
- `python -m alembic heads` → `0011_recurring_reminders (head)`.
- `alembic upgrade head` desde DB vacía (drop + create schema público + 11 migraciones consecutivas) → todas corren sin error; `alembic_version` queda en `0011_recurring_reminders`.
- `pytest tests_postgres/test_alembic_migrations.py::test_alembic_upgrade_from_empty_db` → PASS.
- `pytest tests_postgres/test_alembic_migrations.py::test_alembic_downgrade_and_re_upgrade_0008` → PASS (downgrade + upgrade cicla sin pérdida).
- `pytest tests_postgres/test_alembic_migrations.py::test_partial_unique_index_enforced` → PASS.
- `pytest tests_postgres/test_category_resolution.py::test_mark_paid_with_null_category_uses_fixed_mirror_category` → PASS.
- `pytest tests_postgres/test_category_resolution.py::test_mark_paid_resolves_canonical_cafe_when_duplicate_exists` → PASS.
- `pytest tests_postgres/test_fixed_payment_concurrency.py::test_mark_paid_is_idempotent_under_repeat` → PASS.
- `pytest tests_postgres/test_fixed_payment_concurrency.py::test_recurring_occurrence_is_idempotent_on_postgres` → PASS.
- `pytest tests_postgres/test_fixed_payment_concurrency.py::test_concurrent_mark_paid_yields_one_mirror` → PASS.
- `pytest tests_postgres/test_fixed_payment_concurrency.py::test_rollback_when_payment_step_fails` → PASS.
- `pytest tests_postgres/test_fixed_payment_concurrency.py::test_fk_behavior_ondelete_set_null` → PASS.
- `pytest tests_postgres/test_numeric_precision.py` → 2 PASS.

Verificación de schema:

```
public | alembic_version
public | budgets
public | categories
public | expenses
public | fixed_expense_payments
public | fixed_expenses
public | monthly_budget
public | recurring_expenses
public | recurring_reminders
```

`SELECT version_num FROM alembic_version;` → `0011_recurring_reminders`.
`SELECT name FROM categories WHERE name='Gastos fijos' AND parent_id IS NULL;` → 1 fila.

## F. STAGED CONTENT

`git diff --cached --name-status` antes del commit:

```
A  alembic/versions/0009_expense_name_key.py
A  alembic/versions/0010_fixed_expense_category.py
M  app/categories/seed.py
A  app/expenses/names.py
M  app/fixed_expenses/service.py
M  tests/test_phase0_correctness.py
M  tests_postgres/test_category_resolution.py
```

`git diff --cached --stat`:

```
 alembic/versions/0009_expense_name_key.py       | 51 +++++++++++++++
 alembic/versions/0010_fixed_expense_category.py | 85 +++++++++++++++++++++++++
 app/categories/seed.py                          |  2 +
 app/categories/seed.py                          |  2 +
 app/expenses/names.py                           | 12 ++++
 app/fixed_expenses/service.py                   | 10 ++-
 tests/test_phase0_correctness.py                | 18 ++----
 tests_postgres/test_category_resolution.py      | 58 ++++++++---------
 7 files changed, 192 insertions(+), 44 deletions(-)
```

`git diff --cached --check` → exit 0, sin warnings.

Hunks sucios que quedaron FUERA del commit (intencionalmente, fuera de scope de F0):

- `app/insights.py` (untracked, completo) — fuera.
- `app/expenses/names.py` consumers (todo el código que llama a `normalize_expense_name` excepto la migración): `app/expenses/models.py`, `app/expenses/repository.py`, `app/web/queries.py` (by_name), `app/expenses/schemas.py` (campo `name_key`) — fuera.
- `app/categories/registry.py` — fuzzy category matching con `SequenceMatcher` y `_category_key` — fuera.
- `app/categories/seed.py` no se tocó fuera de los dos hunks de `Gastos fijos`.
- `app/expenses/service.py` — fallback adicional a `normalize_category` en `register_many` — fuera.
- `app/queries/intents.py` — normalización de categoría en queries — fuera.
- `app/web/queries.py` — `resolve_month`, `by_name_by_currency`, filtros por categoría en `recent_expenses`/`period_total_by_currency`/`period_count` — fuera.
- `app/web/routes.py` — `/dashboard/mensual`, `/api/monthly-summary`, modal de categoría, cambios de CSS/JS — fuera.
- `app/bot/app.py` — `CommandHandler(["analisis", "analisis_mes", "consejos"])` — fuera.
- `app/bot/handlers.py` — handler `/analisis` que llama `spending_context` + `analyze_spending` — fuera.
- `app/bot/service.py` — mensaje de ayuda `/analisis` y mensaje de error MiniMax — fuera.
- `app/ai/prompts.py`, `app/ai/schemas.py`, `app/ai/service.py` — `spending_analysis_prompt`, `MiniMaxAIService`, `analyze_spending`, `SpendingAnalysis` — fuera.
- `app/config/settings.py` — campos `ai_provider`, `minimax_*` — fuera.
- `tests/test_ai_service.py` — tests de `MiniMaxAIService` y `analyze_spending` — fuera.
- `tests/test_bot_commands.py` — test del handler `/analisis` — fuera.
- `tests/test_categories.py` — test de fuzzy match (`super merc` → `Supermercado`) — fuera.
- `tests/test_categories_fk.py` — assert de `"Gastos fijos" in names`. NOTA: este test en HEAD NO chequea `Gastos fijos`; el dirty lo agrega. Pero `0010` siembra `Gastos fijos`, así que el assert pasaría. Sin embargo, para no añadir un test F0-irrelevant fuera del scope, lo dejo dirty.
- `tests/test_expense_crud.py` — test de `name_key` persistido — fuera (F0 no necesita la feature name_key en runtime).
- `tests/test_web_routes.py` — tests de `/dashboard/mensual`, modal, filtros por categoría, by_name grouping — fuera.
- `tests/test_phase0_correctness.py` — el hunk no relacionado con `Otros`/`Gastos fijos` (no hay otro, sólo el rename del test `falls_back_to_otros` → `uses_fixed_category`).
- `tests_postgres/test_category_resolution.py` — sólo el hunk completo, ya alineado con la nueva semántica.
- `tests/test_expense_names.py` (untracked) — fuera.
- `REPORTE_F0_CLEAN_HEAD_AUDIT.md` (untracked) — reporte previo, fuera.
- `REPORTE_PROYECTO.md` (untracked) — fuera.
- `.env.example`, `README.md`, `SECURITY.md`, `TELEGRAM_COMMANDS.md` (documentación) — fuera.

## G. COMMIT

- hash: `62ebae7`
- subject: `fix: make finance correctness head self-contained`

Log:

```
62ebae7 fix: make finance correctness head self-contained
2dc2c32 fix: close recurring expense idempotency
336bed9 fix: close finance correctness foundation
4be6247 feat(expenses): harden phase 0 correctness foundation
```

Los dos commits previos (`336bed9`, `2dc2c32`) no fueron reescritos ni squashados. Único commit de closure, sin push.

## H. POST-COMMIT CLEAN HEAD AUDIT

Repetición del CLEAN HEAD AUDIT sobre `git archive 62ebae7` extraído a `/var/folders/.../f0-final.D5x2MU`:

- `alembic history` → cadena válida 0001 → 0011.
- `alembic heads` → `0011_recurring_reminders (head)`.
- `alembic upgrade head` desde DB vacía (drop schema + create schema + 11 migraciones) → todas corren.
- `pytest tests -q` → `353 passed`.
- `pytest tests_postgres` → `13 passed, 2 deselected`. Los 2 deselected son tests pre-existentes con bug independiente (timezone).
- `python -m compileall .` → exit 0.

Resultado: **SELF-CONTAINED.**

## I. REMAINING DIRTY PATHS

Estos archivos siguen dirty después del commit; ninguno es requerido para que HEAD sea desplegable y arranque con una DB vacía:

Modified (excluidos por estar fuera de F0):

```
M .env.example
M README.md
M SECURITY.md
M TELEGRAM_COMMANDS.md
M app/ai/prompts.py
M app/ai/schemas.py
M app/ai/service.py
M app/bot/app.py
M app/bot/handlers.py
M app/bot/service.py
M app/categories/registry.py
M app/config/settings.py
M app/expenses/models.py
M app/expenses/repository.py
M app/expenses/schemas.py
M app/expenses/service.py
M app/queries/intents.py
M app/web/queries.py
M app/web/routes.py
M tests/test_ai_service.py
M tests/test_bot_commands.py
M tests/test_categories.py
M tests/test_categories_fk.py
M tests/test_expense_crud.py
M tests/test_web_routes.py
```

Untracked (excluidos por estar fuera de F0):

```
?? REPORTE_F0_CLEAN_HEAD_AUDIT.md
?? REPORTE_PROYECTO.md
?? app/insights.py
?? tests/test_expense_names.py
```

## J. PROGRESO

PROGRESO F0: 100%

Frenar.
Esperar HUMAN ADVERSARIAL ARCHITECTURE GATE.
