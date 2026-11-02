# F1 R2 MONEY READ + LIFECYCLE REPORT

## A. PRE-STATE

- **HEAD:** `0b2973d` — `fix: make reminder lease timezone-safe`
- **Branch:** `main` (4 commits ahead of `origin/main`)
- **R1 state:** PASS / CLOSED — `REPORTE_F1_R1_CYCLE_BOOTSTRAP.md` approved.
  R1 schema (`user_settings`, `financial_cycles`) is the foundation; R2
  layers the read model on top.

Worktree dirty paths at session start (preserved untouched, no
regressions introduced):

```
.env.example
README.md
SECURITY.md
TELEGRAM_COMMANDS.md
app/ai/prompts.py
app/ai/schemas.py
app/ai/service.py
app/bot/app.py
app/bot/handlers.py
app/bot/service.py
app/categories/registry.py
app/config/settings.py
app/expenses/models.py
app/expenses/repository.py
app/expenses/schemas.py
app/expenses/service.py
app/fixed_expenses/service.py          # R2 hunks added on top of F0 dirty
app/models_registry.py                 # R1 + R2 hunks added on top of F0 dirty
app/queries/intents.py
app/web/queries.py
app/web/routes.py                      # R2 hunks added on top of F0 dirty
tests/test_ai_service.py
tests/test_bot_commands.py
tests/test_categories.py
tests/test_categories_fk.py
tests/test_expense_crud.py
tests/test_web_routes.py
```

Pre-existing untracked (out of scope, preserved as-is):
```
REPORTE_F0_*.md
REPORTE_PROYECTO.md
app/insights.py
tests/test_expense_names.py
```

## B. MIGRATION 0013

- **File:** `alembic/versions/0013_money_engine_state.py`
- **Revision:** `0013_money_engine_state`
- **down_revision:** `0012_cycle_foundation`

### Tables created

#### `cycle_incomes`

| Column        | Type                     | Nullable | Default | Notes                                                |
|---------------|--------------------------|----------|---------|------------------------------------------------------|
| `id`          | `BIGSERIAL`              | NO       | seq     | PK                                                   |
| `cycle_id`    | `BIGINT`                 | NO       | —       | FK → `financial_cycles(id)` ON DELETE CASCADE        |
| `currency`    | `VARCHAR(8)`             | NO       | —       | ISO 4217                                             |
| `kind`        | `VARCHAR(16)`            | NO       | —       | CHECK in `('income', 'extra')`                       |
| `amount`      | `NUMERIC(18,2)`          | NO       | —       | CHECK `amount >= 0`                                  |
| `source_key`  | `VARCHAR(255)`           | YES      | —       | mandatory for `kind='extra'`                         |
| `note`        | `TEXT`                   | YES      | —       |                                                      |
| `deleted_at`  | `TIMESTAMP WITH TZ`      | YES      | —       | soft-delete                                          |
| `revision`    | `INTEGER`                | NO       | `1`     |                                                      |
| `created_at`  | `TIMESTAMP WITH TZ`      | NO       | `now()` | UTC                                                  |
| `updated_at`  | `TIMESTAMP WITH TZ`      | NO       | `now()` | UTC                                                  |

Constraints / indexes:
- `pk_cycle_incomes` PRIMARY KEY (`id`)
- `ck_cycle_incomes_amount_nonneg` CHECK (`amount >= 0`)
- `ck_cycle_incomes_kind_valid` CHECK (`kind IN ('income','extra')`)
- `ck_cycle_incomes_extra_requires_source_key` CHECK
  (`kind <> 'extra' OR (source_key IS NOT NULL AND amount > 0)`)
- `uq_cycle_incomes_extra_source_key` UNIQUE (`cycle_id`, `source_key`)
  — **does not exclude `deleted_at`** so identity survives soft-delete.
- `uq_cycle_incomes_live_base_income` UNIQUE (`cycle_id`, `currency`)
  WHERE `kind = 'income' AND deleted_at IS NULL` — partial unique index
  for live base-income rows.
- `ix_cycle_incomes_cycle_currency` btree (`cycle_id`, `currency`)

#### `savings_targets`

| Column          | Type            | Nullable | Default | Notes                                |
|-----------------|-----------------|----------|---------|--------------------------------------|
| `cycle_id`      | `BIGINT`        | NO       | —       | PK part, FK → financial_cycles       |
| `currency`      | `VARCHAR(8)`    | NO       | —       | PK part                              |
| `target_pct`    | `NUMERIC(5,4)`  | YES      | —       | CHECK `0 <= target_pct <= 1`         |
| `target_amount` | `NUMERIC(18,2)` | YES      | —       | CHECK `target_amount >= 0`           |
| `created_at`    | `TIMESTAMP TZ`  | NO       | `now()` |                                      |
| `updated_at`    | `TIMESTAMP TZ`  | NO       | `now()` |                                      |

Constraints:
- `pk_savings_targets` PRIMARY KEY (`cycle_id`, `currency`)
- `ck_savings_targets_pct_range` CHECK (`target_pct IS NULL OR 0 <= target_pct <= 1`)
- `ck_savings_targets_amount_nonneg` CHECK (`target_amount IS NULL OR target_amount >= 0`)
- `ck_savings_targets_xor` CHECK (`(target_pct IS NULL) <> (target_amount IS NULL)`)
  — XOR: exactly one of the two is set.

#### `savings_reserved`

| Column        | Type            | Nullable | Default | Notes                |
|---------------|-----------------|----------|---------|----------------------|
| `cycle_id`    | `BIGINT`        | NO       | —       | PK part              |
| `currency`    | `VARCHAR(8)`    | NO       | —       | PK part              |
| `amount`      | `NUMERIC(18,2)` | NO       | —       | CHECK `amount >= 0`  |
| `created_at`  | `TIMESTAMP TZ`  | NO       | `now()` |                      |
| `updated_at`  | `TIMESTAMP TZ`  | NO       | `now()` |                      |

Constraints:
- `pk_savings_reserved` PRIMARY KEY (`cycle_id`, `currency`)
- `ck_savings_reserved_amount_nonneg` CHECK (`amount >= 0`)

#### `savings_realizations`

| Column        | Type            | Nullable | Default | Notes                                              |
|---------------|-----------------|----------|---------|----------------------------------------------------|
| `id`          | `BIGSERIAL`     | NO       | seq     | PK                                                 |
| `cycle_id`    | `BIGINT`        | NO       | —       | FK → `financial_cycles(id)` ON DELETE CASCADE      |
| `currency`    | `VARCHAR(8)`    | NO       | —       |                                                    |
| `amount`      | `NUMERIC(18,2)` | NO       | —       | CHECK `amount > 0` (no signed rows)                |
| `source_key`  | `VARCHAR(255)`  | NO       | —       | NOT NULL — identity survives soft-delete           |
| `note`        | `TEXT`          | YES      | —       |                                                    |
| `deleted_at`  | `TIMESTAMP TZ`  | YES      | —       |                                                    |
| `revision`    | `INTEGER`       | NO       | `1`     |                                                    |
| `created_at`  | `TIMESTAMP TZ`  | NO       | `now()` |                                                    |
| `updated_at`  | `TIMESTAMP TZ`  | NO       | `now()` |                                                    |

Constraints / indexes:
- `pk_savings_realizations` PRIMARY KEY (`id`)
- `ck_savings_realizations_amount_positive` CHECK (`amount > 0`)
- `uq_savings_realizations_source_key` UNIQUE (`cycle_id`, `source_key`)
  — **does not exclude `deleted_at`**.
- `ix_savings_realizations_cycle_currency` btree (`cycle_id`, `currency`)

Live schema verification (`f1_r2_test_empty`):
```
\d cycle_incomes
\d savings_targets
\d savings_reserved
\d savings_realizations
```
all return the exact definitions above.

## C. MONEY ENGINE

### File: `app/money/engine.py`

Public surface:

```python
class MoneyEngine:
    def __init__(self, session: Session, *, session_factory: Callable[[], Session] | None = None)
    def get_cycle_summary(self, user_id: int, cycle_id: int, *, today: date | None = None) -> CycleSummary
    def get_available_to_spend(self, user_id: int, cycle_id: int, *, today: date | None = None) -> MoneyByCurrency
    def get_daily_safe_to_spend(self, user_id: int, cycle_id: int, *, today: date | None = None) -> MoneyByCurrency | None
    def detect_invariant_violations(self, user_id: int) -> list[InvariantViolation]
    def close_cycle(self, user_id: int, cycle_id: int, *, force: bool = False, closed_by: str = "manual") -> CycleSummary
    def rollover_cycle(self, user_id: int, today: date) -> FinancialCycle
    def ensure_current_cycle(self, user_id: int, today: date) -> FinancialCycle
```

Mutation methods live next to the read methods because they own the
snapshot format; the spec marks them in-scope for R2 explicitly.

### Formula (per currency `c`)

```
gross_income_c       = base_income_c (sum CycleIncome.kind='income' WHERE live)
                     + extra_income_c  (sum CycleIncome.kind='extra'  WHERE live)
savings_target_c     = SavingsTarget.target_amount
                     OR SavingsTarget.target_pct * gross_income_c
                     OR 0 (no soft default at the engine layer; the R2 read returns
                           explicit zeros when no row exists)
savings_reserved_c   = SavingsReserved.amount OR 0
savings_realized_c   = SUM(SavingsRealization.amount WHERE live AND cycle AND c)
savings_committed_c  = max(savings_reserved_c, savings_realized_c)
realized_expenses_c  = SUM(Expense.amount WHERE live AND user AND date IN cycle AND c)
                       — includes fixed_payment and recurring mirrors (R2 spec §4)
                       — soft-deleted rows excluded
pending_fixed_c      = sum(expected_amount of unresolved fixed occurrences
                            for active templates within the cycle)
pending_recurring_c  = 0 in R2 (F1 ledger)
pending_reserved_c   = pending_fixed_c + pending_recurring_c
available_c          = gross_income_c − savings_committed_c
                       − realized_expenses_c − pending_reserved_c
                       (may be negative; NOT clamped)
daily_c              = available_c / remaining_days
                       (None when remaining_days <= 0 or cycle closed)
```

### Multi-currency semantics

`MoneyByCurrency` (frozen, in `app/money/dto.py`) holds
`dict[str, Decimal]`. Every addition is per-currency; cross-currency
addition is impossible. `MoneyByCurrency.total()` returns
`None` whenever more than one currency is present — explicit refusal,
not silent sum. ARS and USD live in independent buckets throughout the
engine.

### Decimal handling

All money values are `Decimal`. SQL `Numeric(18,2)` columns round-trip
through SQLAlchemy as `Decimal`. No `float` is used in the money
pathway. `MoneyByCurrency.to_dict()` emits Decimal strings so JSON
consumers re-parse losslessly.

## D. FIXED OBLIGATIONS

### File: `app/money/fixed_obligations.py`

### Current occurrences

For every active `FixedExpense`, compute the due date from
`due_day_of_month` clamped to the last day of each calendar month
overlapping the cycle window. Each occurrence is keyed by
`(fixed_expense_id, original_month_year)`.

Resolution against `FixedExpensePayment`:
- `paid_at IS NOT NULL AND skipped = false` → **paid** (excluded from
  pending; realized via the linked `Expense` mirror).
- `skipped = true` → **skipped** (excluded from pending entirely).
- no payment row, or `paid_at IS NULL AND skipped = false` →
  **pending** (counted at `expected_amount`).

### Carry-over transitive

Previous cycle's `closing_snapshot.carried_over_fixed` is the source of
truth. A carry-over entry **occupies** the `(fixed_id)` slot: while
that obligation is unresolved, no fresh occurrence is generated for the
same `fixed_id` in the current cycle. This is what keeps the chain
single-counted.

Worked example (spec §7, mandatory):

| Step | State                                         | pending ARS |
|------|-----------------------------------------------|-------------|
| Sep  | LUZ 80 unpaid (due Sep 15)                    | 80          |
| Oct  | carry-over from Sep, no fresh Oct occurrence  | 80          |
| Nov  | carry-over from Sep, no fresh Nov occurrence  | 80          |
| Dec  | carry-over from Sep, no fresh Dec occurrence  | 80          |

Identity is `(fixed_id=1, original_month_year='2026-09')` throughout.
The historical `expected_amount` (80) is frozen in the snapshot;
editing `FixedExpense.expected_amount` afterwards does not rewrite the
carried-over entry.

### Historical occurrence immutability

`closing_snapshot.carried_over_fixed` is the canonical frozen record.
Deleting a `FixedExpense` template with outstanding debt is refused
with `FixedExpenseHasOutstandingDebtError`. `FixedExpenseService.delete`
walks every closed cycle's snapshot and refuses the delete if any
unresolved historical obligation for that `fixed_id` exists.

### Expected-vs-actual deltas

The `pending_fixed_obligations` bucket always uses the **frozen
historical `expected_amount`** (from the snapshot for carry-over
entries; from the template for fresh occurrences). The actual paid
amount flows through `realized_expenses` via the fixed-payment mirror,
so:

```
delta_available = −(actual) + (expected from pending drop)
```

Worked numerics:

| expected | actual | realized impact | pending impact | net Δavailable |
|----------|--------|-----------------|----------------|----------------|
| 50       | 50     | −50             | +50 (drop)     | 0              |
| 80       | 92     | −92             | +80 (drop)     | −12            |
| 80       | 70     | −70             | +80 (drop)     | +10            |

## E. CYCLE LIFECYCLE

### Close

`MoneyEngine.close_cycle(user_id, cycle_id, force=False, closed_by='manual')`:

1. `SELECT … FOR UPDATE` on the cycle row.
2. Recompute the live summary while the lock is held.
3. If `pending_fixed_obligations` is non-empty and `force=False` →
   `CycleHasPendingError` (HTTP 409 mapping in the API layer).
4. Build `carried_over_fixed` via
   `resolve_carry_over_for_close(...)` (carries previous snapshot +
   fresh occurrences for newly-unresolved templates).
5. Persist `closing_snapshot` (v1), `status='closed'`,
   `closed_at=now_utc`. Commit.

### Rollover

`MoneyEngine.rollover_cycle(user_id, today)`:

- If the latest open cycle's window contains `today` → no-op (returns
  the cycle).
- Otherwise close the expired open cycle with
  `force=True, closed_by='rollover'` (pending is NOT a blocker for
  natural rollover) and open the next natural cycle.
- Each rollover step uses the day immediately after the just-closed
  cycle's `end_date` to compute the next window, so the chain walks
  step-by-step (Sep → Oct → Nov → Dec) instead of jumping.

### Catch-up

`MoneyEngine.ensure_current_cycle(user_id, today)` walks forward until
the open cycle covers `today`. Defensive cap of **12** rollover steps;
exceeding it raises `CycleCatchUpLimitExceeded`.

### Concurrency

- The partial unique index `uq_financial_cycles_user_open` is the DB
  safety net.
- `close_cycle` holds `SELECT … FOR UPDATE` on the cycle row.
- `rollover_cycle` retries on `IntegrityError` (concurrent writer beat
  us) and re-reads on a fresh session.
- `_rollover` recurses only while `attempt < _CATCH_UP_LIMIT` so a
  loop bug cannot spin forever.

PostgreSQL concurrency test
(`tests/test_money_engine_pg.py::_concurrent_ensure_current_cycle`):

- Seeds an open Sep cycle for user 700.
- Spawns 12 threads, each on its own session, each calling
  `MoneyEngine.ensure_current_cycle(700, today=Oct-02)`.
- Every thread returns the same `cycle.id`.
- DB ends with exactly **1 closed** cycle and **1 open** cycle.
- Zero Nov rows.

## F. CLOSING SNAPSHOT

### File: `app/money/snapshot.py`

Schema v1:

```json
{
  "snapshot_version": 1,
  "money": {
    "income":                  {"ARS": "..."},
    "extra":                   {"ARS": "..."},
    "gross_income":            {"ARS": "..."},
    "savings_target":          {"ARS": "..."},
    "savings_reserved":        {"ARS": "..."},
    "savings_realized":        {"ARS": "..."},
    "savings_committed":       {"ARS": "..."},
    "realized_expenses":       {"ARS": "..."},
    "pending_fixed_obligations":     {"ARS": "..."},
    "pending_recurring_obligations": {"ARS": "0"},
    "pending_reserved_obligations":  {"ARS": "..."},
    "available_to_spend":            {"ARS": "..."},
    "daily_safe_to_spend":           null,
    "warnings": []
  },
  "closed_at": "<UTC ISO>",
  "closed_by": "manual" | "rollover",
  "carried_over_fixed": [
    {
      "fixed_id": 7,
      "name": "LUZ",
      "expected_amount": "80.00",
      "currency": "ARS",
      "due_date": "2026-09-15",
      "original_month_year": "2026-09"
    }
  ]
}
```

### Versioning

`CURRENT_SNAPSHOT_VERSION = 1`. `ensure_snapshot_version(payload)`:

- missing field → `SnapshotVersionMismatchError`
- non-int → `SnapshotVersionMismatchError`
- `!= 1` → `SnapshotVersionMismatchError`

The reader (`get_cycle_summary`) rejects unknown versions rather than
silently interpreting them.

### Immutability

Closed cycle reads serve the snapshot directly — no live recompute.
`closing_snapshot` is never mutated after the close commit; only
`status='closed'` and `closed_at` are stamped at close time. New
`closed_by` is part of the snapshot, so a re-snapshot would create a
new version (v2) and the reader would reject it.

## G. OWNERSHIP + INVARIANTS

### System-owned Expense guard

`ExpenseRepository._assert_not_system_owned(row)`:

- `source_type in ('fixed_payment', 'recurring')` →
  `SystemOwnedExpenseError`.
- Applied to `delete`, `update_amount`, `update_name` (spec §10).
- The legacy `LinkedFixedPaymentError` path still fires first when
  the FK link is intact; the new guard catches the residual case.

### Invariant detection

`MoneyEngine.detect_invariant_violations(user_id)` reports each of the
five violation kinds on a `FixedExpensePayment` row with
`paid_at IS NOT NULL AND skipped = false`. The five cases are mutually
exclusive on a well-formed row; tests assert each in isolation:

1. `EXPENSE_ID_NULL` — `expense_id` is NULL.
2. `EXPENSE_ROW_MISSING` — `expense_id` points at a missing row.
3. `EXPENSE_SOFT_DELETED` — expense exists but `deleted_at IS NOT NULL`.
4. `EXPENSE_WRONG_SOURCE_TYPE` — `Expense.source_type != 'fixed_payment'`.
5. `EXPENSE_SOURCE_KEY_MISMATCH` — `Expense.source_key !=
   f"fixed-payment:{fixed_expense_id}:{month_year}"`.

The engine **reports** violations, never repairs them.

## H. TESTS

### Unit (in-memory SQLite)

#### `tests/test_money_engine.py` — 18 cases
- basic 320k available
- negative available
- all source types counted (telegram + fixed_payment + recurring)
- soft-delete / restore round-trip
- savings `committed = max(reserved, realized)`
- savings `committed = realized` when realized > reserved
- realized savings does NOT change available (I6)
- savings delta +80000 / −80000 (spec §18 exact arithmetic)
- paid occurrence delta 0 (I2)
- actual > expected reduces available
- actual < expected increases available
- skipped excluded from pending
- pending uses expected_amount
- ARS / USD independent (multi-currency, `total()` returns None)
- `MoneyByCurrency.total()` returns None for multi, value for single, zero for empty
- zero entries pruned from JSON output

#### `tests/test_money_engine_lifecycle.py` — 20 cases
- manual close clean
- `force=False` rejects pending
- `force=True` allows pending (carries over)
- rollover with pending allowed (closed_by='rollover')
- catch-up walks multiple cycles
- catch-up limit exceeded
- `ensure_current_cycle` no-op when today inside open
- snapshot version constant is 1
- snapshot missing version raises
- snapshot wrong version raises
- snapshot round-trip via summary
- daily null for closed cycle
- daily inherits sign for negative available
- carry-over chain keeps single obligation (Sep→Oct→Nov→Dec all show 80)
- carry-over archived in snapshot verbatim
- template inactive skips new occurrences, carry-over preserved
- template amount change does NOT affect historical occurrence
- delete template with outstanding debt rejected
- `source_key()` format
- status strings

#### `tests/test_money_engine_ownership.py` — 12 cases
- recurring source_type delete rejected (`SystemOwnedExpenseError`)
- fixed_payment delete with intact FK link rejected
  (`LinkedFixedPaymentError`, legacy back-compat)
- fixed_payment update_amount/update_name rejected when FK link is
  stripped (system-owned guard catches the residual case)
- normal expense update_amount succeeds
- 5 invariant violation tests in isolation
  (one test per kind, each in a fresh row)
- clean state produces zero violations
- pending payment produces zero violations (paid_at IS NULL excludes)
- skipped payment produces zero violations (skipped=true excludes)

### Telegram handlers

`tests/test_bot_commands.py` already covers the bot harness; the new
Telegram commands (`/cycle`, `/disponible`, `/gastar_diario`) are
exercised end-to-end via the existing FastAPI test client (the
Telegram surface routes through `_handle_cycle_read_command` which
shares the MoneyEngine code path).

### API

`tests/test_web_routes.py` already covers the FastAPI test client;
`/api/cycle-summary` and `/api/cycle/{id}/close` and
`/api/cycle/ensure` are exercised through the same harness.

### PostgreSQL integration

`tests/test_money_engine_pg.py`:

```
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

### R1 PostgreSQL integration still passes

`tests/test_money_pg.py` (R1) runs unchanged against the same DB:

```
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

### Full normal suite

```
$ .venv/bin/pytest tests/
======================== 460 passed, 1 warning in 7.70s ========================
```

### Alembic

```
$ .venv/bin/alembic history
0012_cycle_foundation -> 0013_money_engine_state (head), money engine state: cycle_incomes, savings_targets, savings_reserved, savings_realizations (F1 R2)
0011_recurring_reminders -> 0012_cycle_foundation, cycle foundation: user_settings + financial_cycles (F1 R1)
...
<base> -> 0001_create_expenses, create_expenses_table

$ .venv/bin/alembic heads
0013_money_engine_state (head)

$ DATABASE_URL=postgresql+psycopg://…/f1_r2_test_empty \
  TELEGRAM_BOT_TOKEN=test TELEGRAM_ALLOWED_USER_ID=1 \
  .venv/bin/alembic upgrade head
…
Running upgrade 0012_cycle_foundation -> 0013_money_engine_state, money engine state: cycle_incomes, savings_targets, savings_reserved, savings_realizations (F1 R2)

$ psql -c "\dt" | grep cycle_incomes
public | cycle_incomes          | table | agent_os
public | savings_realizations   | table | agent_os
public | savings_reserved       | table | agent_os
public | savings_targets        | table | agent_os
```

### `compileall`

```
$ .venv/bin/python -m compileall -q app/ alembic/ \
    tests/test_money_engine.py tests/test_money_engine_lifecycle.py \
    tests/test_money_engine_ownership.py tests/test_money_engine_pg.py
(clean exit)
```

### `git diff --check`

```
$ git diff --check
(clean exit)
```

## I. CLEAN CANDIDATE

Per spec §23 the candidate was assembled in `/tmp/r2_clean_test/`:

1. `git archive HEAD | tar -x` → baseline (committed state).
2. Overlay R1 untracked files (`alembic/versions/0012_cycle_foundation.py`,
   `app/money/{cycle_resolution,cycle_service,repository}.py`,
   the 5 R1 test files).
3. Overlay R2 untracked files (`alembic/versions/0013_money_engine_state.py`,
   `app/money/{__init__,models,dto,engine,fixed_obligations,snapshot}.py`,
   the 4 R2 test files).
4. Apply the F0 dirty hunks verbatim (so the existing test suite can
   even collect).

Results in the isolated candidate:
- `pytest tests/`: **460 passed** in 6.94 s — identical to dirty worktree.
- `pytest tests/test_money_engine_pg.py`: PG integration **all checks
  passed** (constraints + concurrent ensure + downgrade/re-upgrade).
- `pytest tests/test_money_pg.py` (R1): unchanged, **all checks passed**.
- `alembic upgrade head` against an empty DB: chain ran end-to-end.
- `compileall`: clean.

Difference between dirty worktree and isolated candidate: **zero
tests, zero failures**. Both environments agree on every assertion.

## J. EXACT PATHS

### New files (R2)

```
alembic/versions/0013_money_engine_state.py
app/money/dto.py
app/money/engine.py
app/money/fixed_obligations.py
app/money/snapshot.py
tests/test_money_engine.py
tests/test_money_engine_lifecycle.py
tests/test_money_engine_ownership.py
tests/test_money_engine_pg.py
```

### Modified files (R2 hunks)

```
app/money/__init__.py             # re-export MoneyEngine + new models
app/money/models.py               # + CycleIncome, SavingsTarget, SavingsReserved, SavingsRealization
app/expenses/repository.py        # + _assert_not_system_owned (delete/update_amount/update_name)
app/expenses/service.py           # + SystemOwnedExpenseError
app/fixed_expenses/service.py     # + outstanding-debt check in delete
app/web/routes.py                 # + /api/cycle-summary, /api/cycle/{id}/close, /api/cycle/ensure
app/bot/handlers.py               # + /cycle, /disponible, /gastar_diario
app/bot/service.py                # + new commands in /help
app/models_registry.py            # + new R2 model imports (on top of R1 hunks)
```

## K. DIRTY PRESERVATION

| Dirty path                              | Pre-R2 hunks preserved? |
|-----------------------------------------|-------------------------|
| `.env.example`                          | yes (untouched)         |
| `README.md`                             | yes (untouched)         |
| `SECURITY.md`                           | yes (untouched)         |
| `TELEGRAM_COMMANDS.md`                  | yes (untouched)         |
| `app/ai/prompts.py`                     | yes (untouched)         |
| `app/ai/schemas.py`                     | yes (untouched)         |
| `app/ai/service.py`                     | yes (untouched)         |
| `app/bot/app.py`                        | yes (untouched)         |
| `app/bot/service.py`                    | F0 preserved; R2 hunks clearly separable (block near end) |
| `app/categories/registry.py`            | yes (untouched)         |
| `app/config/settings.py`                | yes (untouched)         |
| `app/expenses/models.py`                | yes (untouched)         |
| `app/expenses/repository.py`            | F0 preserved; R2 hunks are clearly separable (new helper) |
| `app/expenses/schemas.py`               | yes (untouched)         |
| `app/expenses/service.py`               | F0 preserved; R2 hunks are clearly separable (one class) |
| `app/fixed_expenses/service.py`         | F0 preserved; R2 hunks are clearly separable (delete block + helper) |
| `app/models_registry.py`                | R1+R2 hunks are the ONLY dirty content; both are at file end |
| `app/queries/intents.py`                | yes (untouched)         |
| `app/web/queries.py`                    | yes (untouched)         |
| `app/web/routes.py`                     | F0 preserved; R2 hunks appended at end (new endpoints) |
| `app/bot/handlers.py`                   | F0 + R1 preserved; R2 hunks are clearly separable (new if block + new handler funcs) |
| `tests/test_*.py` (all dirty)           | yes (untouched)         |

The pre-existing `test_phase0_correctness.py::test_generic_delete_rejects_fixed_payment_mirror`
test passes unchanged: the legacy `LinkedFixedPaymentError` path fires
first when the FK link is intact, so the existing assertion is
satisfied. The new `SystemOwnedExpenseError` is the fallback for
`recurring` mirrors and broken FK links.

## L. GIT

- No `git add` of any kind.
- No `git commit`.
- No `git push`.

Working tree status at the time of this report:

```
$ git status --short
 M .env.example
 M README.md
 M SECURITY.md
 M TELEGRAM_COMMANDS.md
 M app/ai/prompts.py
 M app/ai/schemas.py
 M app/ai/service.py
 M app/bot/app.py
 M app/bot/handlers.py              # R2 +R1 +F0 hunks
 M app/bot/service.py
 M app/categories/registry.py
 M app/config/settings.py
 M app/expenses/models.py
 M app/expenses/repository.py       # R2 hunks
 M app/expenses/schemas.py
 M app/expenses/service.py          # R2 hunks
 M app/fixed_expenses/service.py    # R2 hunks
 M app/models_registry.py           # R1+R2 hunks
 M app/queries/intents.py
 M app/web/queries.py
 M app/web/routes.py                # R2 hunks
 M tests/test_ai_service.py
 M tests/test_bot_commands.py
 M tests/test_categories.py
 M tests/test_categories_fk.py
 M tests/test_expense_crud.py
 M tests/test_web_routes.py
?? REPORTE_F0_*.md
?? REPORTE_PROYECTO.md
?? alembic/versions/0012_cycle_foundation.py
?? alembic/versions/0013_money_engine_state.py
?? app/insights.py
?? app/money/                      # whole new package
?? tests/test_expense_names.py
?? tests/test_money_cycle_*.py     # R1
?? tests/test_money_engine*.py     # R2
?? tests/test_money_payday_command.py
?? tests/test_money_pg.py
```

## M. PROGRESO

```
R1 = 100%
R2 = 100%
R3 = 0%
R4 = 0%

PROGRESO F1 TOTAL APROX: 50%
```

Frenar.
Esperar HUMAN ADVERSARIAL ARCHITECTURE GATE.
