# F1 R3 CLEAN CANDIDATE FINAL REPORT

## A. BASE

- **Base hash:** `f7aff39` — `feat: add financial cycle and money read engine`
  (R1+R2 closure commit, frozen by the previous human gate).
- **Candidate build:** `git archive f7aff39 | tar -x` into
  `/tmp/r3_clean`, then the R3 overlay was applied path-by-path
  (5 new files copied verbatim, 5 modified files rebuilt by taking
  f7aff39 as the base and surgically inserting only the R3 hunks
  identified in §B). No other paths from the dirty worktree were
  copied. The dirty worktree was used only as a reference for what
  the R3 hunks look like — never as the source of any file.

## B. R3 OVERLAY MANIFEST

### NEW (5 paths)

```
A  app/money/money_state_service.py
   The F1 R3 mutation service. ~510 lines. Imports
   `app.money.models` only (CycleIncome, FinancialCycle,
   SavingsRealization, SavingsReserved, SavingsTarget). Grep
   confirms ZERO imports of MoneyEngine, CycleService,
   FixedExpenseService, or RecurringService.

A  tests/test_money_state_service.py
   58 unit / service cases (spec §21 matrix).

A  tests/test_money_state_api.py
   16 FastAPI TestClient cases (status codes + payload shapes).

A  tests/test_money_state_telegram.py
   14 handle_command cases covering the 10 R3 commands.

A  tests/test_money_state_pg.py
   Standalone PG runner for unique-constraint checks, concurrency,
   NUMERIC precision.
```

### MODIFIED (5 paths)

```
M  app/money/__init__.py
   R3 hunks only:
     • Docstring: R1+R2 narrative → R1+R2+R3 narrative + dependency
       arrow diagram.
     • Imports: +10 names from app.money.money_state_service
       (10 error classes + MoneyStateService).
     • __all__: +10 entries.
   No F0 contamination (verified by inspecting the diff vs HEAD).

M  app/web/routes.py
   R3 hunks only (2 hunks out of 21):
     • Hunk @ -14: +1 import line `from app.money import MoneyEngine`.
     • Hunk @ -605: +525 lines = R3 endpoints block (10 router
       decorators + shared helpers `_resolve_open_cycle`,
       `_parse_amount`, `_state_error_to_http`,
       `_income_to_payload`, `_target_to_payload`,
       `_reserved_to_payload`, `_realization_to_payload`) inserted
       immediately before `DASHBOARD_HTML = """<!DOCTYPE html>`.
   F0 hunks EXCLUDED (19 hunks skipped):
     • `resolve_month,` import.
     • `/dashboard/mensual` endpoint.
     • `/api/monthly-summary` endpoint.
     • `category` query param.
     • Monthly dashboard HTML/JS changes (15 hunks).

M  app/bot/handlers.py
   R3 hunks only (6 hunks out of 7):
     • Hunk @ -33: changed `from app.money import UserSettingsService`
       → `from app.money import MoneyEngine, UserSettingsService`.
       (The `app.insights` and `app.web.queries` lines added in the
       same dirty hunk are F0 and were NOT applied.)
     • Hunk @ -149: removed `"/ingreso", "/extra",` from the
       fixed-expense dispatch list (these are now R3 commands
       targeting `MoneyStateService`, not `MonthlyBudget`).
     • Hunk @ -182: +116 lines = 10 new R3 dispatcher blocks
       inserted between the R2 `/cycle,/disponible,/gastar_diario`
       block and the R2 `/start,/help,/ayuda` block.
     • Hunk @ -1058: removed the legacy `/ingreso` and `/extra`
       handlers from `_handle_fixed_expense_command` (they used to
       write to `MonthlyBudget`).
     • Hunk @ -1198: whitespace + dropped the redundant inner
       `MoneyEngine` import from `_handle_cycle_read_command` (now
       module-level).
     • Hunk @ -1308: +512 lines = R3 handler functions appended at
       end of file (`_handle_ingreso_command`, `_handle_extra_command`,
       `_handle_extra_editar_command`, `_handle_extra_borrar_command`,
       `_handle_target_command`, `_handle_reservar_command`,
       `_handle_liberar_command`, `_handle_ahorro_realizado_command`,
       `_handle_ahorro_editar_command`, `_handle_ahorro_borrar_command`,
       `_resolve_current_cycle_id_or_message`,
       `_resolve_cycle_id_for_extra`,
       `_resolve_cycle_id_for_realization`, `_tg_source_key`,
       `_parse_tg_amount`, `_parse_tg_currency`,
       `_parse_target_value`, `_format_mutation_ack`).
   F0 hunks EXCLUDED (1 hunk skipped):
     • Hunk @ -61: `/analisis,/analisis_mes,/consejos` AI block.

M  app/bot/service.py
   R3 hunks only (1 hunk out of 3):
     • Hunk @ -333: in `_help_text()`, removed the legacy
       `/ingreso <monto>` and `/extra <monto>` lines; added the
       10 new R3 command lines under the existing "*Ciclo:*" block.
   F0 hunks EXCLUDED (2 hunks skipped):
     • Hunk @ -75: MiniMax AIUnavailable branch.
     • Hunk @ -319: `/analisis` help line.

M  tests/test_fixed_expense_commands.py
   R3 hunks only (1 hunk out of 1):
     • `test_ingreso_and_liberado` updated: seeds `MonthlyBudget`
       directly via `FixedExpenseService.set_budget` and exercises
       `/liberado` (which still reads `MonthlyBudget` per spec §20).
       The previous behaviour — drive `/ingreso` and `/extra` and
       verify MonthlyBudget got populated — is replaced by direct
       seeding because R3 redirects `/ingreso` and `/extra` to
       `CycleIncome` and explicitly does NOT synchronize the two
       models.
   F0 hunks: NONE in this file's dirty diff.
```

### Overlay purity check

```text
$ diff -rq /tmp/baseline /tmp/r3_clean --exclude=.git --exclude=__pycache --exclude='*.pyc'

Files /tmp/baseline/app/bot/handlers.py and /tmp/r3_clean/app/bot/handlers.py differ
Files /tmp/baseline/app/bot/service.py and /tmp/r3_clean/app/bot/service.py differ
Files /tmp/baseline/app/money/__init__.py and /tmp/r3_clean/app/money/__init__.py differ
Only in /tmp/r3_clean/app/money: money_state_service.py
Files /tmp/baseline/app/web/routes.py and /tmp/r3_clean/app/web/routes.py differ
Files /tmp/baseline/tests/test_fixed_expense_commands.py and /tmp/r3_clean/tests/test_fixed_expense_commands.py differ
Only in /tmp/r3_clean/tests: test_money_state_api.py
Only in /tmp/r3_clean/tests: test_money_state_pg.py
Only in /tmp/r3_clean/tests: test_money_state_service.py
Only in /tmp/r3_clean/tests: test_money_state_telegram.py
```

**10 paths total — exactly the R3 overlay.** No hidden dependencies,
no F0 leakage, no incidental paths.

## C. FROZEN MIGRATIONS

```text
$ diff /tmp/baseline/alembic/versions/0012_cycle_foundation.py \
        /tmp/r3_clean/alembic/versions/0012_cycle_foundation.py
(zero output)

$ diff /tmp/baseline/alembic/versions/0013_money_engine_state.py \
        /tmp/r3_clean/alembic/versions/0013_money_engine_state.py
(zero output)
```

| Migration | Expected diff vs base | Actual diff vs base |
|-----------|-----------------------|---------------------|
| `0012_cycle_foundation.py` (R1)   | ZERO | **ZERO** |
| `0013_money_engine_state.py` (R2) | ZERO | **ZERO** |

R3 does not add a new migration either — the four tables it mutates
(`cycle_incomes`, `savings_targets`, `savings_reserved`,
`savings_realizations`) were already created by migration 0013 in R2
with the schema constraints R3 relies on (UNIQUE surviving
soft-delete, XOR, positive amount, source-key NOT NULL). The
mutation layer just rides on top.

## D. ARCHITECTURE

### `MoneyEngine` — READ-ONLY

- Public methods (unchanged from R2): `get_cycle_summary`,
  `get_available_to_spend`, `get_daily_safe_to_spend`,
  `detect_invariant_violations`.
- Grep of `app/money/engine.py` confirms: "Pure SELECTs. No
  INSERT/UPDATE/DELETE. No transaction boundary."
- All 11 architecture tests in
  `tests/test_money_engine_architecture.py` PASS on the candidate:

```text
test_money_engine_methods_do_not_mutate_cycle           PASSED
test_money_engine_does_not_commit_session               PASSED
test_money_engine_close_cycle_method_does_not_exist     PASSED
test_cycle_service_close_cycle_persists_snapshot_atomically PASSED
test_cycle_service_close_cycle_uses_money_engine_summary    PASSED
test_cycle_service_close_cycle_rolls_back_on_failure        PASSED
test_cycle_service_close_cycle_returns_snapshot_via_money_engine PASSED
test_cycle_service_rollover_is_idempotent_when_today_inside_open PASSED
test_cycle_service_rollover_closes_expired_and_opens_next  PASSED
test_cycle_service_ensure_current_cycle_walks_multiple     PASSED
test_money_engine_does_not_import_cycle_service            PASSED
```

- R3 adds **zero writes** to `MoneyEngine`. The new R3 endpoints
  call `MoneyEngine(s).get_cycle_summary(...)` AFTER the mutation
  commits, only to return the updated summary — a read, never a
  write.

### `MoneyStateService` — R3 MUTATIONS

- Single class, no interface, no factory, no per-method config.
- Imports only `app.money.models` (CycleIncome, FinancialCycle,
  SavingsRealization, SavingsReserved, SavingsTarget).
- Grep confirms: zero imports of `MoneyEngine`, `CycleService`,
  `FixedExpenseService`, `RecurringService`.
- Does NOT call `close_cycle` / `open_initial_cycle` /
  `ensure_current_cycle` (other than via
  `_resolve_current_cycle_id_or_message` in the Telegram layer,
  which is the same single read-only bootstrap already exposed by
  R2). The cycle gate is a direct `session.get(FinancialCycle,
  cycle_id)` followed by attribute checks
  (`cycle.status == "open"`, `cycle.telegram_user_id == user_id`).
- Does NOT mutate `Expense`, `FixedExpense`, or `RecurringExpense`
  rows (grep: zero references to those tables in
  `money_state_service.py`).

### `CycleService` — LIFECYCLE OWNER (unchanged)

- Still owns `open_initial_cycle`, `close_cycle`, `rollover_cycle`,
  `ensure_current_cycle`. R3 endpoints only call
  `ensure_current_cycle` to bootstrap the open cycle for `today`;
  the other three are untouched.

### `FixedExpenseService` — FIXED DOMAIN (unchanged)

- R3 does not touch any `FixedExpensePayment` row or
  `FixedExpenseRepository` method.

## E. INCOME

### BASE INCOME — `MoneyStateService.set_income`

| Spec invariant | Test | Result |
|---|---|---|
| one logical row per `(cycle_id, currency)` | `test_set_income_creates_row`, `test_set_income_same_physical_position` | PASSED |
| set is UPSERT | `test_set_income_update_same_row_updates_amount` | PASSED |
| zero is valid | `test_set_income_zero_valid` | PASSED |
| negative is rejected | `test_set_income_negative_rejected` | PASSED |
| cross-currency isolation | `test_set_income_cross_currency_isolation` | PASSED |
| closed-cycle rejection | `test_set_income_closed_cycle_rejected` | PASSED |
| ownership mismatch | `test_set_income_ownership_mismatch` | PASSED |
| not-found | `test_set_income_cycle_not_found` | PASSED |
| concurrent same identity = one row | PG: `_concurrent_set_income_one_row` (12 threads × same `(cycle, currency)` × same amount) | PASSED |

### EXTRA INCOME — `record_extra_income` / `update_extra_income` / `soft_delete_extra_income`

| Spec invariant | Test | Result |
|---|---|---|
| `source_key` required / non-empty | `test_record_extra_income_missing_source_rejected`, `test_record_extra_income_empty_source_rejected` | PASSED |
| same `source_key` = same physical row | `test_record_extra_income_same_source_same_row` | PASSED |
| same `source_key` updates amount | `test_record_extra_income_same_source_updates_amount` | PASSED |
| soft-delete + same key = RESTORE same row | `test_record_extra_income_soft_delete_then_restore` | PASSED |
| `amount > 0` (zero rejected) | `test_record_extra_income_zero_rejected` | PASSED |
| `amount > 0` (negative rejected) | `test_record_extra_income_negative_rejected` | PASSED |
| `revision` bumps on real change | `test_update_extra_income_revision_bump` | PASSED |
| `revision` does NOT bump when value unchanged | `test_update_extra_income_no_change_no_bump` | PASSED |
| soft-delete is idempotent | `test_soft_delete_extra_income_idempotent` | PASSED |
| cross-user edit rejected | `test_update_extra_income_cross_user_rejected` | PASSED |
| cross-user delete rejected | `test_soft_delete_extra_income_cross_user_rejected` | PASSED |
| concurrent same key = one row | PG: `_concurrent_record_extra_one_row` (12 threads × same `source_key`) | PASSED |

## F. SAVINGS

### TARGET — `set_savings_target`

| Spec invariant | Test | Result |
|---|---|---|
| pct works | `test_set_savings_target_pct` | PASSED |
| fixed amount works | `test_set_savings_target_amount` | PASSED |
| both rejected (XOR) | `test_set_savings_target_both_rejected` | PASSED |
| neither rejected (XOR) | `test_set_savings_target_neither_rejected` | PASSED |
| pct < 0 rejected | `test_set_savings_target_pct_negative_rejected` | PASSED |
| pct > 1 rejected | `test_set_savings_target_pct_above_one_rejected` | PASSED |
| amount < 0 rejected | `test_set_savings_target_amount_negative_rejected` | PASSED |
| soft default 20% stays unpersisted | `test_set_savings_target_default_soft_unpersisted` (DB row count = 0; engine still surfaces 20% via the soft default) | PASSED |
| target change does not touch reserved | `test_set_savings_target_change_does_not_touch_reserved` | PASSED |

### RESERVED — `set_savings_reserved`

| Spec invariant | Test | Result |
|---|---|---|
| zero valid | `test_set_savings_reserved_zero` | PASSED |
| positive valid | `test_set_savings_reserved_positive` | PASSED |
| absolute UPSERT (replaces, not increments) | `test_set_savings_reserved_replaces_value` | PASSED |
| negative rejected | `test_set_savings_reserved_negative_rejected` | PASSED |
| may be below realized (engine emits warning, not error) | `test_set_savings_reserved_can_go_below_realized` — asserts `summary.warnings` contains `savings_realized_exceeds_reserved` | PASSED |
| independent from target | `test_set_savings_target_change_does_not_touch_reserved` | PASSED |

### REALIZED — `record_savings_realized` / `update_savings_realized` / `soft_delete_savings_realized`

| Spec invariant | Test | Result |
|---|---|---|
| `source_key` required | `test_record_savings_realized_source_required` | PASSED |
| same key updates same row | `test_record_savings_realized_same_source_updates` | PASSED |
| restore after soft-delete | `test_record_savings_realized_soft_delete_then_restore` | PASSED |
| `amount > 0` (zero rejected) | `test_record_savings_realized_amount_zero_rejected` | PASSED |
| `amount > 0` (negative rejected) | `test_record_savings_realized_amount_negative_rejected` | PASSED |
| `revision` bumps on real change | `test_update_savings_realized_revision_bump` | PASSED |
| soft-delete is idempotent | `test_soft_delete_savings_realized_idempotent` | PASSED |
| cross-user edit rejected | `test_update_savings_realized_cross_user_rejected` | PASSED |
| cross-user delete rejected | `test_soft_delete_savings_realized_cross_user_rejected` | PASSED |
| concurrent same identity = one row | PG: `_concurrent_record_realized_one_row` (12 threads × same `source_key`) | PASSED |

### RELEASE — `release_savings`

| Spec invariant | Test | Result |
|---|---|---|
| partial release | `test_release_savings_partial` | PASSED |
| clamps at zero | `test_release_savings_clamps_to_zero` | PASSED |
| never mutates realized | `test_release_savings_does_not_mutate_realized` | PASSED |

## G. DELTA CASES

Exact Decimal values produced by `MoneyEngine._compute_open_summary`
(committed = `max_by_currency(reserved, realized)`):

```text
setup: gross_income = 1_000_000.00 ARS
       reserved     =   220_000.00 ARS

CASE 1: realized 150_000 → 200_000
        committed: max(220_000, 200_000) = 220_000
        delta available = 0   (Decimal('0'))

CASE 2: realized 150_000 → 300_000
        committed: max(220_000, 300_000) = 300_000
        delta available = -80_000   (Decimal('-80000'))

CASE 3: realized 300_000 → 180_000 (via soft-delete of one occurrence)
        committed: max(220_000, 180_000) = 220_000
        delta available = +80_000   (Decimal('+80000'))
```

Test coverage:

```text
test_committed_delta_zero_reserved_above_realized    PASSED
test_committed_delta_negative_reserved_below_realized PASSED
test_committed_delta_positive_realized_drop          PASSED
```

Decimal throughout — no `float` in `MoneyStateService`,
`MoneyEngine`, or the route payload serializers.

## H. OWNERSHIP + CLOSED CYCLE

### Ownership

`_require_open_cycle` (in `MoneyStateService`) is called by every
mutation method. It enforces:

1. `cycle = self.session.get(FinancialCycle, cycle_id)`
2. `cycle is None` → `CycleNotFoundError`
3. `cycle.telegram_user_id != user_id` → `CycleOwnershipMismatchError`
4. `cycle.status != "open"` → `CycleClosedError`

Id-based update / delete paths (`update_extra_income`,
`soft_delete_extra_income`, `update_savings_realized`,
`soft_delete_savings_realized`) additionally enforce ownership via
their own `_find_*_by_id` helpers (which JOIN through
`FinancialCycle.telegram_user_id == user_id`).

API layer maps `CycleNotFoundError` / `CycleOwnershipMismatchError` /
`ExtraIncomeNotFoundError` / `SavingsRealizationNotFoundError` to
**HTTP 404** so cross-user probes return the same shape as a real
missing cycle — no id-existence oracle.

| Test | Result |
|---|---|
| `test_set_income_ownership_mismatch` | PASSED |
| `test_set_income_cycle_not_found` | PASSED |
| `test_update_extra_income_cross_user_rejected` | PASSED |
| `test_soft_delete_extra_income_cross_user_rejected` | PASSED |
| `test_update_savings_realized_cross_user_rejected` | PASSED |
| `test_soft_delete_savings_realized_cross_user_rejected` | PASSED |

### Closed-cycle immutability

`test_closed_cycle_rejects_every_mutation` opens a cycle, seeds an
extra + a realization so the update/delete paths have rows to
target, then calls `CycleService.close_cycle(user_id, cycle_id,
force=True)` and attempts every R3 mutation. Each one raises
`CycleClosedError`.

```text
set_income               → CycleClosedError  ✓
record_extra_income      → CycleClosedError  ✓
update_extra_income      → CycleClosedError  ✓
soft_delete_extra        → CycleClosedError  ✓
set_savings_target       → CycleClosedError  ✓
set_savings_reserved     → CycleClosedError  ✓
record_savings_realized  → CycleClosedError  ✓
update_savings_realized  → CycleClosedError  ✓
soft_delete_realized     → CycleClosedError  ✓
release_savings          → CycleClosedError  ✓
```

After each rejection, the enclosing transaction rolls back before
any row mutates, so the cycle's `closing_snapshot` (a JSON dict
produced by `CycleService.close_cycle` and persisted on
`FinancialCycle.closing_snapshot`) remains byte-identical.

## I. API

The R3 endpoints (exactly 10, mounted under `/api/cycles/*`):

| Method | Path | Service method |
|---|---|---|
| POST   | `/api/cycles/income`                              | `MoneyStateService.set_income` |
| POST   | `/api/cycles/extra`                               | `MoneyStateService.record_extra_income` |
| PATCH  | `/api/cycles/extra/<extra_id>`                    | `MoneyStateService.update_extra_income` |
| DELETE | `/api/cycles/extra/<extra_id>`                    | `MoneyStateService.soft_delete_extra_income` |
| POST   | `/api/cycles/savings/target`                      | `MoneyStateService.set_savings_target` |
| POST   | `/api/cycles/savings/reserved`                     | `MoneyStateService.set_savings_reserved` |
| POST   | `/api/cycles/savings/release`                      | `MoneyStateService.release_savings` |
| POST   | `/api/cycles/savings/realized`                    | `MoneyStateService.record_savings_realized` |
| PATCH  | `/api/cycles/savings/realized/<realization_id>`   | `MoneyStateService.update_savings_realized` |
| DELETE | `/api/cycles/savings/realized/<realization_id>`   | `MoneyStateService.soft_delete_savings_realized` |

Note: the human-gate template asked for "exact 11 R3 endpoints" but
spec §13 lists exactly 10 paths. R3 ships the 10 paths verbatim —
the spec is the contract.

### Float safety

- `_parse_amount(raw)` accepts `Decimal` / `int` / `str` and coerces
  through `Decimal(str(raw))`. It never goes through `float`.
- Service signatures are typed as `Decimal`.
- DB columns are `NUMERIC(18, 2)` (R2 migration 0013).
- `Decimal('1234.56')` and `Decimal('0.01')` round-trip through the
  full API → service → DB → read-model chain (verified by
  `test_decimal_roundtrip_*` and PG `_check_decimal_precision`).

### No MonthlyBudget mutation

```text
$ grep -E 'MonthlyBudget|set_budget' app/money/money_state_service.py
(zero matches)

$ grep -E 'MonthlyBudget|set_budget' app/web/routes.py | grep -v '/api/month-summary'
(zero matches)

$ grep -E 'MonthlyBudget|set_budget' app/bot/handlers.py | grep -v '_handle_fixed_expense'
(zero matches)
```

R3 never writes to `MonthlyBudget`. The only R1+R2 endpoint that
does (`/api/month-summary`) was untouched.

## J. TELEGRAM

Ten R3 commands wired in `handle_command`:

| Command | Behaviour |
|---|---|
| `/ingreso <amount>`              | `set_income` (UPSERT base income) |
| `/extra <amount> [nota]`         | `record_extra_income` — `source_key = telegram:extra:<chat_id>:<message_id>` |
| `/extra_editar <id> <amount> [nota]` | `update_extra_income` |
| `/extra_borrar <id>`            | `soft_delete_extra_income` |
| `/target 20%`                   | `set_savings_target(target_pct=0.20)` |
| `/target 220000`                | `set_savings_target(target_amount=220000)` |
| `/target 20`                    | `set_savings_target(target_amount=20)` — bare numbers are amounts, **never** silently pct; this keeps the user-facing rule documented and unambiguous |
| `/reservar <amount>`            | `set_savings_reserved` |
| `/liberar <amount>`              | `release_savings` |
| `/ahorro_realizado <amount> [nota]` | `record_savings_realized` — `source_key = telegram:savings:<chat_id>:<message_id>` |
| `/ahorro_editar <id> <amount> [nota]` | `update_savings_realized` |
| `/ahorro_borrar <id>`           | `soft_delete_savings_realized` |

### Source-key determinism

```python
def _tg_source_key(prefix: str, chat_id: int, message_id: int) -> str:
    return f"telegram:{prefix}:{chat_id}:{message_id}"
```

Re-issuing the same Telegram update (same chat_id + message_id)
yields the same `source_key`; the service UPSERTs onto the same
physical row. Verified by `test_tg_extra_idempotent_on_retry`.

### Cycle bootstrap

Each handler resolves the target cycle via
`_resolve_current_cycle_id_or_message(user_id, today)`, which calls
`CycleService.ensure_current_cycle` and walks the rollover chain
when needed. Telegram UX never exposes cycle ids.

### `/target` parser

```python
def _parse_target_value(args):
    raw = args[0]
    if raw.endswith("%"):
        return Decimal(raw[:-1]) / Decimal("100"), None  # pct path
    # Bare number: amount path; we never silently coerce to pct.
    return None, Decimal(raw)
```

This is the only honest interpretation per spec §15: a bare integer
could be either pct or amount, so we treat it as amount. The user
who wants 20% writes `20%`.

Test coverage:

```text
test_tg_target_pct                  PASSED   /target 20%     → pct=0.20
test_tg_target_amount               PASSED   /target 220000  → amount=220000
test_tg_target_bare_number_is_amount PASSED  /target 20      → amount=20
test_tg_extra_idempotent_on_retry   PASSED   retry yields same row
```

## K. DECIMAL

| Spec invariant | Test | Result |
|---|---|---|
| `0.01` round-trip exact | `test_decimal_roundtrip_one_cent` + PG `_check_decimal_precision` | PASSED |
| `1234.56` round-trip exact | `test_decimal_roundtrip_arbitrary` + PG `_check_decimal_precision` | PASSED |
| `target_pct` Decimal-safe | `test_set_savings_target_pct` (asserts `Decimal('0.2000)'` stored) | PASSED |
| int coercion safe | `test_set_income_int_coerced_to_decimal` | PASSED |
| string coercion safe | `test_set_income_string_coerced_to_decimal`, `test_set_income_string_rejected_if_garbage` | PASSED |
| No `float` coercion in service path | grep over `money_state_service.py`: zero `float(` occurrences | verified |
| API request amounts stay Decimal | `_parse_amount` returns `Decimal(str(raw))` (no `float`) | verified |

PG roundtrip (full API → service → PG → read-model chain):

```text
$ python -m tests.test_money_state_pg
[pg] check NUMERIC precision (1234.56, 0.01)
[pg] all checks passed
```

## L. TEST RESULTS

### Normal suite

```text
$ cd /tmp/r3_clean && pytest tests/ --tb=no -q
547 passed, 1 warning in 13.32s
```

### Test breakdown

| Layer | Tests |
|---|---|
| R1+R2 baseline | 459 |
| R3 service (`test_money_state_service.py`) | 58 |
| R3 API (`test_money_state_api.py`) | 16 |
| R3 Telegram (`test_money_state_telegram.py`) | 14 |
| **Total on clean candidate** | **547** |

### Architecture tests

```text
$ pytest tests/test_money_engine_architecture.py
11 passed in 0.72s
```

### PostgreSQL — R3 (`test_money_state_pg.py`)

```text
$ DATABASE_URL=postgresql+psycopg://agent_os:agent_os@localhost:15434/f1_r3_clean_r3 \
  F1_PG_DB=f1_r3_clean_r3 python -m tests.test_money_state_pg
[pg] resetting schema
[pg] alembic upgrade head
[pg] check cycle_incomes UNIQUE (cycle_id, source_key) survives soft-delete
[pg] check savings_realizations UNIQUE (cycle_id, source_key) survives soft-delete
[pg] check savings_targets XOR
[pg] check extra rows require source_key
[pg] concurrent set_income (same amount → 1 row)
[pg] concurrent record_extra_income (same key → 1 row)
[pg] concurrent record_savings_realized (same key → 1 row)
[pg] check NUMERIC precision (1234.56, 0.01)
[pg] all checks passed
```

### PostgreSQL — R2 regression (`test_money_engine_pg.py`)

```text
$ DATABASE_URL=...f1_r3_clean_r2 ... python -m tests.test_money_engine_pg
[pg] resetting schema
[pg] alembic upgrade head
[pg] check cycle_incomes UNIQUE (cycle_id, source_key) survives soft-delete
[pg] check savings_realizations UNIQUE (cycle_id, source_key) survives soft-delete
[pg] check savings_targets XOR
[pg] check cycle_incomes amount >= 0
[pg] check extra rows require source_key
[pg] alembic downgrade 0012_cycle_foundation
[pg] alembic upgrade head (re-upgrade)
[pg] concurrent ensure_current_cycle
[pg] all checks passed
```

### PostgreSQL — R1 regression (`test_money_pg.py`)

```text
$ DATABASE_URL=...f1_r3_clean_r1 ... python -m tests.test_money_pg
[pg] resetting schema
[pg] alembic upgrade head
[pg] check payday_day range CHECK
[pg] check financial_cycles end_date >= start_date CHECK
[pg] check UNIQUE (user, cycle_index)
[pg] check partial UNIQUE one open per user
[pg] alembic downgrade 0011_recurring_reminders
[pg] alembic upgrade head (re-upgrade)
[pg] concurrent open_initial_cycle
[pg] pre-test cycles for user 500: 0
[pg] all checks passed
```

### Alembic

```text
$ cd /tmp/r3_clean && alembic history | tail -2
0011_recurring_reminders -> 0012_cycle_foundation, cycle foundation: user_settings + financial_cycles (F1 R1)
0012_cycle_foundation -> 0013_money_engine_state, money engine state: cycle_incomes, savings_targets, savings_reserved, savings_realizations (F1 R2)

$ cd /tmp/r3_clean && alembic heads
0013_money_engine_state (head)
```

Empty → head:

```text
$ DATABASE_URL=...f1_r3_clean_empty alembic upgrade head | tail -2
INFO  [alembic.runtime.migration] Running upgrade 0011_recurring_reminders -> 0012_cycle_foundation, ...
INFO  [alembic.runtime.migration] Running upgrade 0012_cycle_foundation -> 0013_money_engine_state, ...
```

### `compileall`

```text
$ cd /tmp/r3_clean && python -m compileall -q .
(clean exit, rc=0)
```

### `git diff --check`

```text
$ cd /tmp/r3_clean && git --no-pager diff --check $(git rev-list --max-parents=0 HEAD)
(zero output)
```

### No skips / deselects / xfail

```text
$ grep -rn "skip\|deselect\|@pytest.mark.skip\|xfail" tests/test_money_state_*.py
(zero output)
```

## M. CLEAN CANDIDATE

| Property | Value |
|---|---|
| Base hash | `f7aff39` (R1+R2 closure) |
| Overlay | 10 paths (5 new + 5 modified) |
| Tests | 547 passed |
| Architecture tests | 11 passed |
| PG R3 | all checks passed |
| PG R2 regression | all checks passed |
| PG R1 regression | all checks passed |
| Alembic head | `0013_money_engine_state` |
| Empty DB → head | ok |
| `compileall` | clean |
| `git diff --check` | clean |
| skip / deselect / xfail | none |

## N. HIDDEN DEPENDENCIES

**NONE.**

The R3 overlay is exactly the 10 paths listed in §B. No file
outside the money domain is modified, and the only test file
modified (`test_fixed_expense_commands.py`) had its single dependent
test re-seeded directly through `FixedExpenseService.set_budget` —
which is the API the pre-R3 test already used. No transitive
breakage, no implicit coupling through `__init__.py` re-exports
beyond the explicit R3 additions.

## O. DIRTY PRESERVATION

The dirty worktree still has all its F0 / R1+R2 paths untouched. R3
work happens entirely on top:

```text
$ cd /Volumes/1TB/Main/Proyectos/dev/telegram-expenses
$ git status --short | wc -l
45

$ git log --oneline -2
f7aff39 feat: add financial cycle and money read engine
0b2973d fix: make reminder lease timezone-safe

$ pytest tests/ --collect-only -q | tail -1
559 tests collected   # 547 R3 + 12 F0-dirty tests
```

The 12-test delta between dirty (559) and clean candidate (547) is
**exactly** the F0-dirty additions across `test_expense_names.py`,
`test_ai_service.py`, `test_bot_commands.py`, `test_categories.py`,
`test_categories_fk.py`, `test_expense_crud.py`, `test_web_routes.py`.
None of those F0-dirty test files were copied into the candidate.

The R3 test count is **identical** on dirty and clean candidate:

```text
dirty:      pytest tests/test_money_state_*.py --collect-only → 88
candidate:  pytest tests/test_money_state_*.py --collect-only → 88
```

## P. GIT

```text
$ git diff --cached --name-only
(zero output — nothing staged)

$ git log --oneline -1
f7aff39 feat: add financial cycle and money read engine

$ git status --short | grep "^??.*money_state"
?? app/money/money_state_service.py
?? tests/test_money_state_api.py
?? tests/test_money_state_pg.py
?? tests/test_money_state_service.py
?? tests/test_money_state_telegram.py
```

- R3 unstaged.
- R3 uncommitted.
- R3 unpushed.
- Last commit is the R1+R2 closure (`f7aff39`); it stays the
  frozen base per the previous gate.

## Q. PROGRESS

```
R1 = 100%
R2 = 100%
R3 = 100% (clean candidate validated, evidence above)
R4 =   0%

F1 TOTAL ≈ 75%
```

Frenar.
Esperar HUMAN ADVERSARIAL ARCHITECTURE GATE.
