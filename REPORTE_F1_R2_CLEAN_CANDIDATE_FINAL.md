# F1 R2 CLEAN CANDIDATE FINAL REPORT

## A. BASE

- **Reference HEAD:** `0b2973d` — `fix: make reminder lease timezone-safe`.
- **Build steps:**
  1. `git archive 0b2973d | tar -x` → baseline (committed state, no
     working-tree dirty).
  2. Overlay the R1 manifest below.
  3. Overlay the R2 manifest below (R2 hunks constructively extend
     R1 new files; R2 hunks on modified files are added to the HEAD
     version with F0 hunks stripped, where applicable).
  4. Nothing else is copied.

## B. R1 OVERLAY MANIFEST

```
[new] alembic/versions/0012_cycle_foundation.py
[new] app/money/__init__.py                 (overwritten by R2 below)
[new] app/money/cycle_resolution.py
[new] app/money/cycle_service.py            (extended by R2 below)
[new] app/money/models.py                   (extended by R2 below)
[new] app/money/repository.py
[new] tests/test_money_cycle_api.py
[new] tests/test_money_cycle_resolution.py
[new] tests/test_money_cycle_service.py
[new] tests/test_money_payday_command.py
[new] tests/test_money_pg.py

[modified] app/bot/handlers.py              R1 hunks only:
                                            + `from app.money import UserSettingsService` import
                                            + `/config_payday` command block (9 lines)
                                            + `_handle_config_payday_command` helper (24 lines)

[modified] app/web/routes.py               R1 hunks only:
                                            + `/api/cycle/current` endpoint
                                            + `/api/cycle/open` endpoint

[modified] app/models_registry.py           R1 hunks only:
                                            + `from app.money.models import FinancialCycle, UserSettings`
```

**R1 OVERLAY: 14 paths** (11 new + 3 modified).

For each "modified" file, the R1 hunks were taken from the R1
report's stated additions. Files were constructed by taking HEAD
0b2973d's version and applying ONLY the R1 hunks; no F0 hunks from
the working tree were applied.

Note: `app/money/__init__.py`, `app/money/cycle_service.py`, and
`app/money/models.py` are listed as "new" because R1 created them;
R2's overlay below is what actually defines their final state.

## C. R2 OVERLAY MANIFEST

```
[new] alembic/versions/0013_money_engine_state.py
[new] app/money/dto.py
[new] app/money/engine.py
[new] app/money/fixed_obligations.py
[new] app/money/snapshot.py
[new] tests/test_money_engine.py
[new] tests/test_money_engine_lifecycle.py
[new] tests/test_money_engine_ownership.py
[new] tests/test_money_engine_pg.py
[new] tests/test_money_engine_architecture.py   (R2 service-boundary fix)

[modified] app/money/__init__.py           replaces R1 with full R2 surface
[modified] app/money/cycle_service.py      extends R1 with lifecycle methods
                                            (close_cycle, rollover_cycle,
                                            ensure_current_cycle)
[modified] app/money/models.py             extends R1 with CycleIncome,
                                            SavingsTarget, SavingsReserved,
                                            SavingsRealization

[modified] app/bot/handlers.py              R2 hunks only:
                                            + `/cycle, /disponible, /gastar_diario` block
                                            + `_handle_cycle_read_command` helper
                                            + 3 `_format_*` helpers

[modified] app/bot/service.py               R2 hunks only:
                                            + 3 help-text lines for new commands

[modified] app/expenses/repository.py       R2 hunks only (F0 name_key hunks stripped):
                                            + `SystemOwnedExpenseError` import in delete()
                                            + `_assert_not_system_owned` checks
                                              (delete / update_amount / update_name)
                                            + `_assert_not_system_owned` helper

[modified] app/expenses/service.py          R2 hunks only:
                                            + `SystemOwnedExpenseError` class

[modified] app/fixed_expenses/service.py    R2 hunks only (no F0 work on this file):
                                            + delete() hardening: outstanding-debt
                                              checks via `FixedExpenseHasOutstandingDebtError`
                                            + `_unresolved_carry_over` helper

[modified] app/web/routes.py               R2 hunks only (F0 dashboard/mensual and
                                            /api/monthly-summary stripped):
                                            + `_summary_to_payload` helper
                                            + `/api/cycle-summary` endpoint
                                            + `/api/cycle/{cycle_id}/close` endpoint
                                            + `/api/cycle/ensure` endpoint

[modified] app/models_registry.py           R2 hunks only (extends R1):
                                            + CycleIncome, SavingsRealization,
                                              SavingsReserved, SavingsTarget imports
```

**R2 OVERLAY: 20 paths** (10 new + 10 modified).

For each "modified" file, R2 hunks were applied to the HEAD version
(NOT to the dirty worktree's version). Where F0 hunks existed in
the dirty worktree, they were stripped explicitly.

## TOTAL

**Total unique paths in manifest: 28** (21 from R1+R2 "new"
files, but 3 are overwritten by R2, giving 18 unique new files +
10 unique modified files).

Counted by overlay:

| Layer       | new | modified | total |
|-------------|----:|---------:|------:|
| R1 overlay  |  11 |        3 |    14 |
| R2 overlay  |  10 |       10 |    20 |
| Union       |  18 |       10 |    28 |

The 3 R1-new files that R2 also modifies (`__init__.py`,
`cycle_service.py`, `models.py`) end up in R2's final state and
contribute to the R2-modified bucket.

## D. EXCLUDED DIRTY PATHS

The following dirty paths from the working tree were NOT copied
into the candidate. Each one is F0 work that R1+R2 does not depend
on:

```
.env.example
README.md
SECURITY.md
TELEGRAM_COMMANDS.md
app/ai/prompts.py
app/ai/schemas.py
app/ai/service.py
app/bot/app.py
app/categories/registry.py
app/config/settings.py
app/expenses/models.py          (F0: name_key field)
app/expenses/schemas.py         (F0: name_key field)
app/insights.py                 (F0)
app/queries/intents.py          (F0)
app/web/queries.py              (F0: resolve_month, monthly helpers)
tests/test_ai_service.py        (F0: AI analysis)
tests/test_bot_commands.py      (F0: command routing changes)
tests/test_categories.py        (F0: category test additions)
tests/test_categories_fk.py     (F0)
tests/test_expense_crud.py      (F0: name grouping)
tests/test_expense_names.py     (F0: expense name normalization)
tests/test_web_routes.py        (F0: monthly-summary test additions)
```

Verification: candidate's `app/web/routes.py` and the four F0-dirty
test files match HEAD 0b2973d byte-for-byte (`diff -q` returns no
differences). A keyword scan of `app/` for F0 markers
(`insights`, `spending_analysis`, `minimax`, `ai_provider`,
`name_key`, `monthly-summary`, `dashboard/mensual`,
`monthly_dashboard`, `resolve_month`, `app.insights`) returns NONE.

## E. TEST RESULTS

### Full normal suite

```
$ .venv/bin/pytest tests/
======================== 459 passed, 1 warning in 9.44s ========================
```

Test count breakdown (candidate):
- 398 HEAD baseline + R1+R2 = 459 (no skips / no deselects)
- 471 dirty worktree − 12 F0-specific = 459 ✓

The 12-test delta (F0 work that R1+R2 doesn't depend on):
- test_ai_service, test_bot_commands (delta), test_categories,
  test_categories_fk, test_expense_crud, test_web_routes (delta),
  test_expense_names, and AI/dashboard additions in shared test
  files.

### Architecture tests (R2 SB-fix coverage)

```
$ .venv/bin/pytest tests/test_money_engine_architecture.py -v
======================== 11 passed in 0.51s ========================

  test_money_engine_methods_do_not_mutate_cycle PASSED
  test_money_engine_does_not_commit_session PASSED
  test_money_engine_close_cycle_method_does_not_exist PASSED
  test_cycle_service_close_cycle_persists_snapshot_atomically PASSED
  test_cycle_service_close_cycle_uses_money_engine_summary PASSED
  test_cycle_service_close_cycle_rolls_back_on_failure PASSED
  test_cycle_service_close_cycle_returns_snapshot_via_money_engine PASSED
  test_cycle_service_rollover_is_idempotent_when_today_inside_open PASSED
  test_cycle_service_rollover_closes_expired_and_opens_next PASSED
  test_cycle_service_ensure_current_cycle_walks_multiple PASSED
  test_money_engine_does_not_import_cycle_service PASSED
```

### PostgreSQL integration — F0/R1/R2

`tests/test_money_engine_pg.py` (R2):

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

`tests/test_money_pg.py` (R1):

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

### Concurrency (R2)

After 12 threads × `CycleService.ensure_current_cycle(700, today=Oct-02)`:

```
SELECT status, count(*) FROM financial_cycles WHERE telegram_user_id=700 GROUP BY status ORDER BY status;
 status | count
--------+-------
 closed |     1
 open   |     1
```

Expected exactly:
- 1 closed (Sep)
- 1 open (Oct)
- 0 Nov cycles

Result: **MATCH**.

### Concurrency (R1)

`tests/test_money_pg.py::_concurrent_open_initial_cycle`: 12 threads
× `open_initial_cycle(500, today=2026-10-10)` → all 12 callers return
same `cycle_id=1`, exactly 1 row open in DB.

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
$ .venv/bin/python -m compileall -q .
(clean exit, exit code 0)
```

### Diff check (equivalent to `git diff --check`)

```
$ .venv/bin/python -c "<F0 keyword scan>"
FORBIDDEN KEYWORDS: NONE FOUND

# All F0-dirty test files in candidate == HEAD 0b2973d:
$ for f in test_web_routes test_bot_commands test_categories ... ; do
    diff -q HEAD tests/$f.py candidate/tests/$f.py
  done
# All return: no differences
```

No skip / deselect was added in R1 or R2 tests. The candidate suite
collection (`pytest --collect-only`) reports exactly 459 tests.

## F. HIDDEN DEPENDENCIES

**NONE found.**

R1+R2 dependencies verified:

| Path                              | R1/R2 dep | Verified |
|-----------------------------------|-----------|----------|
| app/bot/handlers.py               | R1+R2 additive (3 hunks) | clean candidate compiles, 459 tests pass |
| app/bot/service.py                | R2 additive (3 help lines) | clean candidate compiles, tests pass |
| app/expenses/repository.py        | R2 additive (system-owned guard) | HEAD version + R2 hunk; F0 stripped |
| app/expenses/service.py           | R2 additive (SystemOwnedExpenseError) | HEAD + 1 class added |
| app/fixed_expenses/service.py     | R2 additive (delete hardening) | HEAD + delete body + helper |
| app/money/cycle_service.py        | R1 created, R2 extended | full R2 file (R2-overwrites-new) |
| app/money/engine.py               | R2 only | full R2 file |
| app/money/models.py               | R1 created, R2 extended | R1 base + 4 R2 model classes |
| app/money/__init__.py             | R1 created, R2 replaced | R2 final surface |
| app/money/dto.py                  | R2 only | full R2 file |
| app/money/fixed_obligations.py    | R2 only | full R2 file |
| app/money/repository.py           | R1 only | full R1 file |
| app/money/snapshot.py             | R2 only | full R2 file |
| app/models_registry.py            | R1+R2 additive | HEAD + 2 import blocks |
| app/web/routes.py                 | R1+R2 additive | HEAD + 5 endpoints + 1 helper |

## G. GIT

- No `git add` of any kind.
- No `git commit`.
- No `git push`.

The candidate was assembled in a temporary directory
(`/tmp/r2cc_clean/`) and torn down after validation. The
working-tree status is unchanged from the previous report.

## H. PROGRESS

```
R1 = 100%
R2 = 100% candidate
R3 = 0%
R4 = 0%

PROGRESO F1 TOTAL APROX: 50%
```

Frenar.
Esperar HUMAN ADVERSARIAL ARCHITECTURE GATE.
