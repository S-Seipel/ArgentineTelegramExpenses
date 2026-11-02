# F1 R2 SERVICE BOUNDARY FIX REPORT

## A. ROOT CAUSE

The previous R2 report ("MoneyEngine is read-only on the data side but
owns lifecycle") placed the close/rollover/catch-up methods inside
`MoneyEngine`. The HUMAN ADVERSARIAL ARCHITECTURE GATE rejected that:
the F1 DESIGN v5 FINAL froze `MoneyEngine` as **read-only** and
`CycleService` as the **lifecycle owner**.

Why the lifecycle landed there originally:

- The closing math (recompute summary inside the same lock + serialize
  snapshot) is a small surface, so it felt natural to colocate it
  with the read engine.
- The first iteration wrote the engine before the lifecycle split was
  carved out, and the lifecycle helpers grew out of the engine's
  session-boundary primitives (`_lock_cycle`, `_latest_open_cycle`,
  `_try_insert_open_cycle`).

The fix is a clean ownership swap: lifecycle moves to
`CycleService`, the engine is left with only its four read endpoints,
and the snapshot builder becomes a pure module function. The
dependency arrow is one-way (`CycleService` → `MoneyEngine` →
snapshot module). No circular import.

## B. FINAL OWNERSHIP

### `MoneyEngine` (read-only, four public methods)

```python
class MoneyEngine:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_cycle_summary(
        self, user_id: int, cycle_id: int, *, today: date | None = None
    ) -> CycleSummary: ...

    def get_available_to_spend(
        self, user_id: int, cycle_id: int, *, today: date | None = None
    ) -> MoneyByCurrency: ...

    def get_daily_safe_to_spend(
        self, user_id: int, cycle_id: int, *, today: date | None = None
    ) -> MoneyByCurrency | None: ...

    def detect_invariant_violations(
        self, user_id: int
    ) -> list[InvariantViolation]: ...
```

Architecturally enforced:

- No `INSERT`, `UPDATE`, `DELETE` in any method.
- No `session.commit()`, `session.flush()`, `session.rollback()`.
- No `SELECT … FOR UPDATE`. (The engine doesn't lock anything; it
  reads whatever the caller has already locked.)
- No transaction lifecycle ownership — the caller owns the session.

### `CycleService` (lifecycle owner)

```python
class CycleService:
    # R1 (read + bootstrap)
    def get_current_cycle(self, user_id: int, today: date) -> CurrentCycleView | None: ...
    def open_initial_cycle(self, user_id: int, today: date) -> CurrentCycleView: ...

    # R2 (lifecycle)
    def close_cycle(
        self, user_id: int, cycle_id: int, *,
        force: bool = False, closed_by: str = "manual",
    ) -> CycleSummary: ...

    def rollover_cycle(self, user_id: int, today: date) -> FinancialCycle: ...

    def ensure_current_cycle(self, user_id: int, today: date) -> FinancialCycle: ...
```

`CycleService` owns:

- `SELECT … FOR UPDATE` on the cycle row.
- The transaction boundary (`session.commit()` inside `close_cycle`,
  no other commit elsewhere).
- Retry on `IntegrityError` for the partial-unique concurrent-open
  race.
- Status transitions (`status='closed'`, `closed_at=now_utc`).
- Snapshot persistence (`cycle.closing_snapshot = ...`).
- Catch-up defensive cap (`_CATCH_UP_LIMIT = 12`).

### `MoneyStateService`

Not implemented yet — R3 owns the write APIs for income/savings
mutation (the read APIs are already exposed via `MoneyEngine`).

### `FixedExpenseService`

Responsibility is unchanged from R2 first pass:
template CRUD + `mark_paid` / `mark_skipped` / `unmark` / `delete`.
The `delete` path now also walks every closed cycle's
`carried_over_fixed` snapshot and refuses the delete with
`FixedExpenseHasOutstandingDebtError` if any unresolved historical
obligation exists.

## C. DEPENDENCY DIRECTION

```
                  ┌──────────────────────┐
                  │  app.money.snapshot  │
                  │  (pure functions)    │
                  │  - serialize_snapshot │
                  │  - build_closing_…    │
                  │  - ensure_version    │
                  └──────────┬───────────┘
                             │
   ┌─────────────────────────┼─────────────────────────┐
   │                                                  │
   ▼                                                  ▼
┌──────────────────────┐                  ┌──────────────────────┐
│   MoneyEngine        │                  │   CycleService       │
│   (read-only)        │ ◀─── consumes ───│   (lifecycle owner)  │
│                      │                  │                      │
│ - get_cycle_summary  │                  │ - close_cycle        │
│ - get_available…     │                  │ - rollover_cycle     │
│ - get_daily…         │                  │ - ensure_current…    │
│ - detect_invariants  │                  │                      │
└──────────────────────┘                  └──────────────────────┘
```

One-way arrow: `CycleService` constructs a `MoneyEngine(session)` and
calls `get_cycle_summary` inside the same transaction. The engine never
imports anything from `app.money.cycle_service`; the architecture test
`test_money_engine_does_not_import_cycle_service` reads the engine
source and asserts no `from app.money.cycle_service` line appears.

## D. TRANSACTION BOUNDARY

`CycleService.close_cycle` is the canonical transaction. Single
SQLAlchemy session, single transaction, in this exact order:

```
1. cycle = self._lock_cycle_for_update(user_id, cycle_id)
   # SELECT ... FROM financial_cycles WHERE ... FOR UPDATE

2. summary = MoneyEngine(self.session).get_cycle_summary(user_id, cycle_id)
   # MoneyEngine re-fetches the same row (own FOR UPDATE lock is
   # already held by this transaction, so the second SELECT sees
   # the locked data without blocking).

3. if summary.pending_fixed_obligations non-empty AND not force:
       raise CycleHasPendingError(...)        # session still open

4. carried = resolve_carry_over_for_close(self.session, user_id, cycle,
                                          previous_carried_over=…)

5. snapshot = build_closing_snapshot(summary, carried,
                                      closed_at=now_utc, closed_by=…)

6. cycle.closing_snapshot = snapshot
   cycle.status = "closed"
   cycle.closed_at = closed_at

7. self.session.commit()
```

Steps 1-2 share the lock. Step 3 raises before any mutation. Steps 4-6
are pure (snapshot building) + ORM attribute writes on the already-locked
cycle row. Step 7 commits.

If any step raises after step 1, the surrounding `with session_scope()`
catches the exception, calls `session.rollback()`, and the cycle stays
`open` with no `closing_snapshot`. The architecture test
`test_cycle_service_close_cycle_rolls_back_on_failure` monkeypatches
`build_closing_snapshot` to raise and asserts:

- The cycle row's `status` is still `'open'`.
- `closing_snapshot` is still `None`.
- `closed_at` is still `None`.

## E. ROLLOVER

`CycleService.rollover_cycle(user_id, today)` and
`ensure_current_cycle(user_id, today)` moved verbatim into
`CycleService._rollover_once(...)`. Same semantics, same concurrency
guarantees, same defensive cap:

- Latest open cycle contains `today` → no-op return.
- No open cycle at all → defer to `open_initial_cycle` (bootstrap).
- Expired open cycle → close with `force=True, closed_by='rollover'`
  (pending obligations do NOT block the natural rollover) then open
  the next natural cycle using `cycle_window_for(end_date + 1 day,
  payday_day)` so the walk advances one cycle at a time (Sep → Oct →
  Nov → Dec) instead of jumping from Sep straight to Dec.
- The partial unique index `uq_financial_cycles_user_open` is the DB
  safety net; concurrent winners lose the insert race and recover
  the canonical row on a fresh session.
- Defensive cap: `_CATCH_UP_LIMIT = 12` recursive steps.
  `CycleCatchUpLimitExceeded` is raised when the cap is exceeded.

Concurrency verified by `tests/test_money_engine_pg.py
::_concurrent_ensure_current_cycle`: 12 threads × same user / same
`today=Oct-02` all return the same logical cycle id, DB ends with
exactly 1 closed cycle + 1 open cycle, no Nov rows.

## F. TESTS

### Architecture (`tests/test_money_engine_architecture.py`) — 11 cases

1. `test_money_engine_methods_do_not_mutate_cycle` — after 3× hammer
   of every read method, `status` / `closing_snapshot` /
   `closed_at` / `updated_at` / cycle count are all unchanged.
2. `test_money_engine_does_not_commit_session` — pre/post state
   matches; engine never commits.
3. `test_money_engine_close_cycle_method_does_not_exist` —
   `MoneyEngine` only exposes the four read endpoints; explicitly
   asserts `close_cycle`, `rollover_cycle`, `ensure_current_cycle`,
   `open_cycle`, `open_initial_cycle` are not attributes.
4. `test_cycle_service_close_cycle_persists_snapshot_atomically` —
   after close, the row carries `status='closed'`,
   `closing_snapshot` non-null, version `1`.
5. `test_cycle_service_close_cycle_uses_money_engine_summary` —
   the persisted snapshot equals the live read at close time.
6. `test_cycle_service_close_cycle_rolls_back_on_failure` —
   monkeypatched snapshot builder raises → row stays open, no
   snapshot, no closed_at.
7. `test_cycle_service_close_cycle_returns_snapshot_via_money_engine`
   — re-reading the closed cycle on a new session serves the
   snapshot, not a recompute.
8. `test_cycle_service_rollover_is_idempotent_when_today_inside_open`
   — no-op returns the same row.
9. `test_cycle_service_rollover_closes_expired_and_opens_next` —
   expired cycle closed with `closed_by='rollover'`, next cycle
   inserted.
10. `test_cycle_service_ensure_current_cycle_walks_multiple` —
    Sep → Oct → Nov → Dec, four cycles, three closed + one open.
11. `test_money_engine_does_not_import_cycle_service` — the engine
    source contains no `from app.money.cycle_service` line.

### Existing R2 tests still pass

After routing lifecycle calls through `CycleService`:

- `tests/test_money_engine.py` — 18 cases (read model + formula).
- `tests/test_money_engine_lifecycle.py` — 20 cases (close /
  rollover / catch-up / snapshot / daily / carry-over chain /
  template delete with debt).
- `tests/test_money_engine_ownership.py` — 12 cases (system-owned
  Expense guard + 5 invariant violation cases in isolation).
- `tests/test_money_cycle_api.py`, `test_money_cycle_resolution.py`,
  `test_money_cycle_service.py`, `test_money_payday_command.py` —
  R1 suite, unchanged.

### PostgreSQL integration

`tests/test_money_engine_pg.py` (12-thread concurrent
`ensure_current_cycle` + 5 schema CHECK/UNIQUE assertions +
downgrade/re-upgrade round-trip):

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

`tests/test_money_pg.py` (R1) unchanged, still passes:

```
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
======================== 471 passed, 1 warning in 9.87s ========================
```

(460 R2 + 11 new architecture tests, all green.)

### Alembic

```
$ .venv/bin/alembic history
0012_cycle_foundation -> 0013_money_engine_state (head), money engine state: …

$ .venv/bin/alembic heads
0013_money_engine_state (head)

$ DATABASE_URL=postgresql+psycopg://…/empty \
  TELEGRAM_BOT_TOKEN=test TELEGRAM_ALLOWED_USER_ID=1 \
  .venv/bin/alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade 0011_recurring_reminders -> 0012_cycle_foundation, cycle foundation: user_settings + financial_cycles (F1 R1)
INFO  [alembic.runtime.migration] Running upgrade 0012_cycle_foundation -> 0013_money_engine_state, money engine state: cycle_incomes, savings_targets, savings_reserved, savings_realizations (F1 R2)
```

### `compileall`

```
$ .venv/bin/python -m compileall -q app/ alembic/ \
    tests/test_money_engine.py tests/test_money_engine_lifecycle.py \
    tests/test_money_engine_ownership.py tests/test_money_engine_architecture.py \
    tests/test_money_engine_pg.py
(clean exit)
```

### `git diff --check`

```
$ git diff --check
(clean exit)
```

## G. CLEAN CANDIDATE

Per spec, the candidate was assembled in `/tmp/r2sb_clean/`:

1. `git archive HEAD | tar -x` → baseline (committed state).
2. Overlay R1 untracked files (`alembic/versions/0012_cycle_foundation.py`,
   `app/money/{cycle_resolution,cycle_service,repository}.py`,
   the 5 R1 test files).
3. Overlay R2 untracked files (`alembic/versions/0013_money_engine_state.py`,
   `app/money/{__init__,models,dto,engine,fixed_obligations,snapshot}.py`,
   the 4 R2 test files).
4. Overlay the new R2 architecture test (`tests/test_money_engine_architecture.py`).
5. Apply F0 dirty hunks verbatim so the existing test suite can
   collect.

Results in the isolated candidate:
- `pytest tests/`: **471 passed** in 12.44 s — identical to dirty
  worktree.
- `pytest tests/test_money_engine_pg.py`: PG integration **all checks
  passed**.
- `pytest tests/test_money_pg.py` (R1): unchanged, **all checks passed**.
- `alembic upgrade head` against an empty DB: chain ran end-to-end.
- `compileall`: clean.
- `alembic heads`: `0013_money_engine_state (head)`.

Difference between dirty worktree and isolated candidate: **zero
tests, zero failures**. Both environments agree on every assertion,
including the new architecture tests.

## H. EXACT PATHS

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
tests/test_money_engine_architecture.py    # NEW for service-boundary fix
```

### Modified files

```
app/money/__init__.py             # re-export MoneyEngine + CycleService + new models
app/money/cycle_service.py        # + close_cycle, rollover_cycle, ensure_current_cycle, _rollover_once
                                  #   lifecycle now lives here (was on MoneyEngine in R2 first pass)
app/money/engine.py               # STRIPPED: now only 4 read methods, no session_factory,
                                  #   no _lock_cycle, no _try_insert_open_cycle, no commit/rollback
app/money/snapshot.py             # + build_closing_snapshot pure helper
app/expenses/repository.py        # system-owned guard (unchanged from R2 first pass)
app/expenses/service.py           # + SystemOwnedExpenseError
app/fixed_expenses/service.py     # outstanding-debt check on delete (unchanged from R2 first pass)
app/web/routes.py                 # /api/cycle-summary, /api/cycle/{id}/close, /api/cycle/ensure
                                  #   now use CycleService for lifecycle; MoneyEngine for the read model
app/bot/handlers.py               # /cycle, /disponible, /gastar_diario
                                  #   same split: CycleService for lifecycle, MoneyEngine for read
app/bot/service.py                # /help text (unchanged from R2 first pass)
app/models_registry.py            # R1 + R2 model imports (unchanged from R2 first pass)
tests/test_money_engine_lifecycle.py  # routes lifecycle calls through CycleService
tests/test_money_engine_pg.py     # routes concurrent ensure_current_cycle through CycleService
```

### Behaviour delta (R2 first pass → R2 service-boundary fix)

| Concern                                | R2 first pass               | R2 SB fix                              |
|----------------------------------------|-----------------------------|----------------------------------------|
| `MoneyEngine` public methods            | 7 (4 reads + 3 lifecycle)   | 4 (reads only)                         |
| `CycleService` public methods           | 2 (R1 bootstrap)            | 5 (R1 + close/rollover/ensure)         |
| `MoneyEngine` calls `session.commit()`  | yes (in `close_cycle`)      | never                                  |
| `MoneyEngine` calls `SELECT FOR UPDATE`| yes (in `close_cycle`)      | never                                  |
| `CycleService` calls `SELECT FOR UPDATE`| never                      | yes (in `close_cycle`)                 |
| Snapshot building                      | method on `MoneyEngine`     | pure module function `build_closing_snapshot` |
| Dependency arrow                       | bidirectional               | one-way: `CycleService` → `MoneyEngine` → snapshot |

All other behaviours (formula, carry-over, snapshot v1, ownership
guards, invariant detection, multi-currency, daily safe-to-spend,
catch-up, concurrency) are unchanged.

## I. DIRTY PRESERVATION

| Dirty path                              | Pre-SB-fix hunks preserved? |
|-----------------------------------------|------------------------------|
| `.env.example`                          | yes (untouched)              |
| `README.md`                             | yes (untouched)              |
| `SECURITY.md`                           | yes (untouched)              |
| `TELEGRAM_COMMANDS.md`                  | yes (untouched)              |
| `app/ai/prompts.py`                     | yes (untouched)              |
| `app/ai/schemas.py`                     | yes (untouched)              |
| `app/ai/service.py`                     | yes (untouched)              |
| `app/bot/app.py`                        | yes (untouched)              |
| `app/bot/handlers.py`                   | F0 + R1 + R2 preserved; SB-fix hunks are the new CycleService wiring |
| `app/bot/service.py`                    | F0 + R2 preserved; SB-fix did not touch the help text |
| `app/categories/registry.py`            | yes (untouched)              |
| `app/config/settings.py`                | yes (untouched)              |
| `app/expenses/models.py`                | yes (untouched)              |
| `app/expenses/repository.py`            | F0 + R2 preserved; SB-fix did not touch the system-owned guard |
| `app/expenses/schemas.py`               | yes (untouched)              |
| `app/expenses/service.py`               | F0 + R2 preserved; SB-fix did not touch the SystemOwnedExpenseError |
| `app/fixed_expenses/service.py`         | F0 + R2 preserved; SB-fix did not touch the outstanding-debt check |
| `app/models_registry.py`                | R1 + R2 preserved; SB-fix did not touch the imports |
| `app/queries/intents.py`                | yes (untouched)              |
| `app/web/queries.py`                    | yes (untouched)              |
| `app/web/routes.py`                     | F0 + R2 preserved; SB-fix only changed which service object builds the cycle/lifecycle in the endpoints |
| `tests/test_ai_service.py`              | yes (untouched)              |
| `tests/test_bot_commands.py`            | yes (untouched)              |
| `tests/test_categories.py`              | yes (untouched)              |
| `tests/test_categories_fk.py`           | yes (untouched)              |
| `tests/test_expense_crud.py`            | yes (untouched)              |
| `tests/test_web_routes.py`              | yes (untouched)              |

The pre-existing `test_phase0_correctness.py::test_generic_delete_rejects_fixed_payment_mirror`
test passes unchanged. The legacy `LinkedFixedPaymentError` path fires
first when the FK link is intact, so the existing assertion is
satisfied. The new `SystemOwnedExpenseError` is the fallback for
`recurring` mirrors and broken FK links.

## J. GIT

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
 M app/bot/handlers.py              # R2 + SB-fix hunks
 M app/bot/service.py
 M app/categories/registry.py
 M app/config/settings.py
 M app/expenses/models.py
 M app/expenses/repository.py
 M app/expenses/schemas.py
 M app/expenses/service.py
 M app/fixed_expenses/service.py
 M app/models_registry.py
 M app/queries/intents.py
 M app/web/queries.py
 M app/web/routes.py                # SB-fix routes lifecycle through CycleService
 M tests/test_ai_service.py
 M tests/test_bot_commands.py
 M tests/test_categories.py
 M tests/test_categories_fk.py
 M tests/test_expense_crud.py
 M tests/test_web_routes.py
?? REPORTE_F0_*.md
?? REPORTE_F1_*.md
?? REPORTE_PROYECTO.md
?? alembic/versions/0012_cycle_foundation.py
?? alembic/versions/0013_money_engine_state.py
?? app/insights.py
?? app/money/
?? tests/test_expense_names.py
?? tests/test_money_*.py            # R1 + R2 + SB-fix
```

## K. PROGRESO

```
R1 = 100%
R2 = 100%
R3 = 0%
R4 = 0%

PROGRESO F1 TOTAL APROX: 50%
```

Frenar.
Esperar HUMAN ADVERSARIAL ARCHITECTURE GATE.
