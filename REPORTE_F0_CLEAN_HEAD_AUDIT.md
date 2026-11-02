# F0 CLEAN HEAD AUDIT

## A. HEAD
- hash: `2dc2c32e48d6653f658ebd329875df95e92ef314`
- branch: `main` (2 ahead of `origin/main`)

## B. MIGRATION CHAIN

Cadena Alembic committed en HEAD:

```
0001_create_expenses
└─ 0002_bigint_telegram_user_id
   └─ 0003_categories_and_fk
      └─ 0004_recurring
         └─ 0005_budgets
            └─ 0006_fixed_expenses
               └─ 0007_fixed_payment_expense_link
                  └─ 0008_expense_correctness_foundation
                     └─ 0011_recurring_reminders
```

- 0009 — `fatal: path 'alembic/versions/0009_expense_name_key.py' exists on disk, but not in 'HEAD'`. NO existe en HEAD.
- 0010 — `fatal: path 'alembic/versions/0010_fixed_expense_category.py' exists on disk, but not in 'HEAD'`. NO existe en HEAD.
- 0011 — `revision: "0011_recurring_reminders"`, `down_revision: "0010_fixed_expense_category"` (HEAD:alembic/versions/0011_recurring_reminders.py:14-15).

Resultado: HEAD rompe la cadena Alembic. `0011.down_revision` apunta a `0010_fixed_expense_category`, pero `0010` no está commiteada; por tanto Alembic no puede resolver la cadena (`KeyError: '0010_fixed_expense_category'` reproducido con `python -m alembic history` y `python -m alembic heads` sobre `git archive HEAD`).

Comparación semántica de 0010:
- La única versión de `0010_fixed_expense_category.py` que existe es untracked (status `??`), no committed.
- 0010 introduce la fila `'Gastos fijos'` en `categories`, normaliza `expenses.category_id` para los gastos que ya tenían una fila en `fixed_expense_payments`, y reasigna a `'Otros'` en `downgrade`. Esa semántica no existe en HEAD.
- 0010 down_revision = `0009_expense_name_key` (HEAD:alembic/versions/0009_expense_name_key.py:13-16) y la 0009 añade la columna `expenses.name_key` y la rellena usando `app.expenses.names.normalize_expense_name`. Esa columna tampoco existe en HEAD.

## C. DIRTY DEPENDENCY ANALYSIS

El worktree contiene 27 archivos `M` y 5 archivos `??` (incluidos `0009`, `0010`, `app/expenses/names.py`, `app/insights.py`, `tests/test_expense_names.py`). El primer commit de F0 (`336bed9`) NO contiene la columna `expenses.name_key`, no contiene la tabla `recurring_reminders` con `down_revision=0010_fixed_expense_category` real, y los modelos en HEAD (`app/recurring/models.py`) todavía definen `recurring_reminders` cuyo `down_revision` depende de un archivo que tampoco está en HEAD. Material concreto que F0 toca y que sólo vive en el worktree:

1. `alembic/versions/0009_expense_name_key.py` (untracked)
   - Crea `expenses.name_key`, importa `app.expenses.names.normalize_expense_name`. Esa columna es referenciada por el código dirty (`app/expenses/models.py:34-36`, `app/expenses/repository.py:33`, `app/web/queries.py:39`, `app/expenses/service.py:155`) y por `tests/test_expense_names.py`. Sin 0009 no hay `name_key`, y sin `app/expenses/names.py` (untracked) la importación falla.

2. `alembic/versions/0010_fixed_expense_category.py` (untracked)
   - Inserta la categoría `Gastos fijos` y re-categoriza mirrors de `fixed_expense_payments`. El servicio dirty (`app/fixed_expenses/service.py:389-396`) llama a `_resolve_category_id(..., "Gastos fijos")` y sobreescribe `category_id` con `fixed_category_id or category_id`. Esa semántica vive solo en el archivo untracked.

3. `app/expenses/names.py` (untracked)
   - Contiene `normalize_expense_name`. Lo importa dirty `app/expenses/repository.py:13`, `app/web/queries.py:39` y `alembic/versions/0009_expense_name_key.py`. Sin este módulo la colección dirty no importa.

4. `app/insights.py` (untracked)
   - Lo importa dirty `app/bot/handlers.py:42` para `spending_context`/`format_spending_analysis`. No forma parte del commit 336bed9.

5. Modelo ORM `Expense.name_key` (solo dirty)
   - `app/expenses/models.py:34-36`. Sin la columna creada por 0009, `op.batch_alter_table("expenses") alter_column("name_key", nullable=False)` falla. Sin el modelo declarado, el servicio falla al asignar `row.name_key = ...`.

6. Categoría `Gastos fijos` y seed (`app/categories/seed.py`, `app/categories/registry.py` diff)
   - El servicio `FixedExpenseService` en HEAD no asigna la categoría fija; ese comportamiento vive en los cambios dirty. La categoría `Gastos fijos` se añade únicamente cuando 0010 corre; sin la migración, `_resolve_category_id(session, "Gastos fijos")` devuelve `None`.

7. `app/fixed_expenses/service.py:389-396` (dirty)
   - Sobrescribe `category_id` con la categoría fija antes de la llamada a `expense_repo.get_by_source_key`. Ese cambio no es parte del commit 336bed9.

8. Handlers y rutas del dashboard (solo dirty)
   - `app/bot/handlers.py:42`, `app/web/queries.py` (resolve_month, by_name_by_currency, filtros por categoría), `app/web/routes.py` (dashboard mensual, modal, `/api/monthly-summary`), `app/insights.py`. Ninguno commiteado.

9. Test suite dirty
   - `tests/test_web_routes.py`, `tests/test_expense_crud.py`, `tests/test_categories.py`, `tests/test_categories_fk.py`, `tests/test_bot_commands.py`, `tests/test_ai_service.py`, `tests/test_phase0_correctness.py`, `tests_postgres/test_category_resolution.py` esperan comportamiento que sólo existe en el código dirty.

Veredicto de esta sección: F0 depende de una columna, una migración, una tabla, un helper de normalización, una categoría nueva y un cambio de servicio que NO están en HEAD.

## D. CLEAN HEAD TESTS

Copia aislada: `git archive HEAD | tar -x -C /var/folders/.../f0-head.bConBZ`. Sin trabajo sucio dentro.

- `python -m compileall .` (sobre la copia): exit 0, sin warnings.
- `pytest tests -q` (suite SQLite, en la copia): `353 passed, 1 warning in 5.40s`. La suite committed pasa íntegra sobre HEAD limpio.
- PostgreSQL suite (`tests_postgres/`): No ejecutada. La suite exige `TGE_PG_INTEGRATION=1` + `DATABASE_URL=postgresql+psycopg://...` (`tests_postgres/conftest.py:27-49`); ninguna de las dos variables estaba provista y el entorno PostgreSQL local (`pg_isready /tmp:5432`) no estaba en marcha. Documentado, no se levantó nada.
- Alembic clean upgrade:
  - `python -m alembic history` sobre la copia: falla con `KeyError: '0010_fixed_expense_category'` (alembic/script/revision.py:245). HEAD rompe la cadena de migraciones, por lo que `alembic upgrade head` no es ejecutable sin el archivo untracked.
  - `python -m alembic current` sobre la copia: falla con `ValidationError` en `app/config/settings.py:57` por falta de `TELEGRAM_BOT_TOKEN` y `TELEGRAM_ALLOWED_USER_ID`. El entorno no provee `.env`. La validación de runtime exigiría exportar/copiar el `.env` local (no copiado al reporte por seguridad).

Conclusión práctica: en HEAD limpio, la suite unitaria pasa pero cualquier migración Alembic queda bloqueada por la ausencia de 0010 y, eventualmente, de 0009. No existe un "checkout limpio" capaz de ejecutar F0 completo sin arrastrar cambios dirty.

## E. COMMIT CONTENT

Commit `336bed98a4e88dd2b89f54b0f9fc1884bcf732c1` "fix: close finance correctness foundation" — paths exactos:

- A `alembic/versions/0011_recurring_reminders.py`
- M `app/ai/prompts.py`
- M `app/ai/service.py`
- M `app/bot/handlers.py`
- M `app/expenses/repository.py`
- M `app/fixed_expenses/service.py`
- M `app/models_registry.py`
- M `app/queries/intents.py`
- M `app/queries/service.py`
- M `app/recurring/models.py`
- M `app/recurring/repository.py`
- M `app/recurring/scheduler.py`
- M `app/web/queries.py`
- M `tests/test_phase0_correctness.py`
- M `tests/test_query_service.py`
- M `tests/test_recurring_commands.py`
- M `tests/test_recurring_repo.py`
- M `tests/test_recurring_service.py`
- M `tests_postgres/test_alembic_migrations.py`
- M `tests_postgres/test_fixed_payment_concurrency.py`

Propósito: cerrar la base de correctitud (source_type/source_key, soft-delete, revision, partial unique index, scheduler de recordatorios, idempotencia de mark-paid, mirror en fixed_payments). Commitea `0011_recurring_reminders` con `down_revision="0010_fixed_expense_category"`, pero NO commitea 0010 ni 0009 — la cadena queda rota.

Commit `2dc2c32e48d6653f658ebd329875df95e92ef314` "fix: close recurring expense idempotency" — paths exactos:

- M `app/recurring/service.py` (+70 −24)

Propósito: cerrar la idempotencia del gasto recurrente (lookup por source_key antes de insertar, restore + bump revision). No añade migraciones ni modelos.

Ambos juntos dejan en HEAD:
- `recurring_reminders` (tabla + modelo + scheduler + repo)
- Idempotencia en `FixedExpenseService.mark_paid`
- Idempotencia en `RecurringExpenseService`
- `Expense.source_type`/`source_key`/`deleted_at`/`revision` (todo de 0008)

No contienen en HEAD:
- Columna `Expense.name_key`
- Tabla/categoría `Gastos fijos`
- Helpers `normalize_expense_name`, `spending_context`, `format_spending_analysis`
- Cambios de UI (dashboard mensual, modal, filtros por categoría, AI insights)

## F. VERDICT

**DEPENDS ON DIRTY CHANGES**

Dependencias concretas (path:hunk donde aplica, semántica):

- `alembic/versions/0009_expense_name_key.py` (untracked, completo). Necesario para que `0010` y los servicios dirty funcionen: crea `expenses.name_key`, `ix_expenses_name_key`, depende del helper `app.expenses.names.normalize_expense_name`. Sin esto, el commit `336bed9` no rompe, pero la columna `name_key` referenciada por `app/expenses/models.py` (worktree) y por `0010` queda ausente.
- `alembic/versions/0010_fixed_expense_category.py` (untracked, completo). Su ausencia rompe `alembic history/heads` (reproducido: `KeyError: '0010_fixed_expense_category'`) y por tanto cualquier `alembic upgrade head`. La fila `Gastos fijos` y la reasignación de `expenses.category_id` para mirrors existentes sólo existen aquí.
- `app/expenses/names.py` (untracked). Provee `normalize_expense_name`, importada por 0009 y por `app/expenses/repository.py` (worktree) y `app/web/queries.py` (worktree).
- `app/insights.py` (untracked). Lo usa `app/bot/handlers.py:42` (worktree) para `format_spending_analysis`/`spending_context`.
- `app/expenses/models.py` — hunk `+name_key: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="", index=True)` (líneas 34-36). No commiteado.
- `app/categories/seed.py` — entradas `"Gastos fijos": "📌"` y `"Gastos fijos": []`. No commiteadas.
- `app/categories/registry.py` — `_category_key` con `SequenceMatcher` para fuzzy match (líneas 159-187). No commiteado.
- `app/fixed_expenses/service.py:389-396` — `fixed_category_id = _resolve_category_id(...); category_id = fixed_category_id or category_id` antes del lookup por source_key. No commiteado.
- `app/expenses/repository.py:33` — `name_key=payload.name_key or normalize_expense_name(payload.name)`. No commiteado.
- `app/expenses/schemas.py:23` — campo `name_key: Optional[str] = None`. No commiteado.
- `app/web/queries.py` — `resolve_month`, `by_name_by_currency`, `recent_expenses(category=...)`, `period_total_by_currency(category=...)`, `period_count(category=...)`. No commiteado.
- `app/web/routes.py` — `/dashboard/mensual`, `/api/monthly-summary`, modal de categoría, cambios de CSS/JS. No commiteado.
- `app/bot/handlers.py:64-94` — handler `/analisis`/`/analisis_mes`/`/consejos`. No commiteado.
- `app/ai/service.py:240-264` y siguientes — `analyze_spending`, `MiniMaxAIService`, `build_default_ai_service` con `ai_provider`. No commiteado.
- `app/recurring/service.py` (untracked en 336bed9, sí commiteado en 2dc2c32). Confirmado: la versión definitiva de este archivo ES la de `2dc2c32`, ya committed.
- Tests dirty: `tests/test_web_routes.py`, `tests/test_expense_crud.py`, `tests/test_categories.py`, `tests/test_categories_fk.py`, `tests/test_bot_commands.py`, `tests/test_ai_service.py`, `tests/test_phase0_correctness.py`, `tests_postgres/test_category_resolution.py`. Esperan características (filtros por categoría, fuzzy match, name_key) que sólo viven en el worktree.

Ningún arreglo aplicado. Sólo se reporta la dependencia.

## G. PROGRESO

PROGRESO F0: 100%

Frenar.
Esperar HUMAN ADVERSARIAL ARCHITECTURE GATE.
