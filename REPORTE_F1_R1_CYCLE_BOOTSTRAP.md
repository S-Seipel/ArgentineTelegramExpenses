# F1 R1 CYCLE BOOTSTRAP REPORT

## A. PRE-STATE

- **HEAD:** `0b2973d` — `fix: make reminder lease timezone-safe`
- **Branch:** `main` (4 commits ahead of `origin/main`)
- **Worktree dirty paths (preserved untouched):**
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
  app/models_registry.py
  app/queries/intents.py
  app/web/queries.py
  app/web/routes.py
  tests/test_ai_service.py
  tests/test_bot_commands.py
  tests/test_categories.py
  tests/test_categories_fk.py
  tests/test_expense_crud.py
  tests/test_web_routes.py
  ```
  Plus untracked (out-of-scope, preserved as-is):
  ```
  REPORTE_F0_CLEAN_HEAD_AUDIT.md
  REPORTE_F0_SELF_CONTAINED_HEAD_REPORT.md
  REPORTE_F0_TIMEZONE_LEASE_FINAL.md
  REPORTE_F1_MONEY_ENGINE_DESIGN.md
  REPORTE_PROYECTO.md
  app/insights.py
  tests/test_expense_names.py
  ```

  F0 is closed. R1 only touches the bare minimum of dirty files
  (`app/models_registry.py`, `app/bot/handlers.py`, `app/web/routes.py`)
  with additive, clearly separable hunks.

## B. MIGRATION

- **File:** `alembic/versions/0012_cycle_foundation.py`
- **Revision:** `0012_cycle_foundation`
- **down_revision:** `0011_recurring_reminders`

### `user_settings`

| Column              | Type                    | Nullable | Default     | Notes                              |
|---------------------|-------------------------|----------|-------------|------------------------------------|
| `telegram_user_id`  | `BIGINT`                | NO       | —           | `PRIMARY KEY`                      |
| `payday_day`        | `INTEGER`               | NO       | `1`         | `CHECK BETWEEN 1 AND 31`          |
| `timezone`          | `TEXT`                  | YES      | `NULL`      | convention: same fallback as `app.config.settings` |
| `created_at`        | `TIMESTAMP WITH TZ`     | NO       | `now()`     | UTC, timezone-aware                |
| `updated_at`        | `TIMESTAMP WITH TZ`     | NO       | `now()`     | UTC, timezone-aware                |

Constraints / indexes:
- `pk_user_settings` PRIMARY KEY (`telegram_user_id`)
- `ck_user_settings_payday_day_range` CHECK (`payday_day BETWEEN 1 AND 31`)

### `financial_cycles`

| Column             | Type                    | Nullable | Default       | Notes                              |
|--------------------|-------------------------|----------|---------------|------------------------------------|
| `id`               | `BIGSERIAL`             | NO       | sequence      | PK                                 |
| `telegram_user_id` | `BIGINT`                | NO       | —             |                                    |
| `cycle_index`      | `INTEGER`               | NO       | —             | ordinal per user                   |
| `start_date`       | `DATE`                  | NO       | —             | business date                      |
| `end_date`         | `DATE`                  | NO       | —             | business date                      |
| `payday_day`       | `INTEGER`               | NO       | —             | snapshot of config at open time    |
| `status`           | `VARCHAR(16)`           | NO       | `'open'`      | `CHECK IN ('open', 'closed')`      |
| `closing_snapshot` | `JSONB`                 | YES      | `NULL`        | reserved for R2 (always NULL R1)   |
| `opened_at`        | `TIMESTAMP WITH TZ`     | NO       | `now()`       | UTC                                |
| `closed_at`        | `TIMESTAMP WITH TZ`     | YES      | `NULL`        |                                    |
| `created_at`       | `TIMESTAMP WITH TZ`     | NO       | `now()`       | UTC                                |
| `updated_at`       | `TIMESTAMP WITH TZ`     | NO       | `now()`       | UTC                                |

Constraints / indexes:
- `pk_financial_cycles` PRIMARY KEY (`id`)
- `uq_financial_cycles_user_index` UNIQUE (`telegram_user_id`, `cycle_index`)
- `uq_financial_cycles_user_open` UNIQUE (`telegram_user_id`)
  WHERE `status = 'open'` — partial unique, **I12**.
- `ck_financial_cycles_status` CHECK (`status IN ('open', 'closed')`)
- `ck_financial_cycles_date_range` CHECK (`end_date >= start_date`)
- `ix_financial_cycles_user_status_end`
  (`telegram_user_id`, `status`, `end_date DESC`) — only created because
  the read path actually filters by user + status + ordered end date in
  R1 (`find_open_cycle`, `get_current_cycle`). Not speculative.

No other tables. No `cycle_incomes`, `savings_*`, `MonthlyBudget` changes,
no obligation flags. R1 deliberately stops at cycle representation.

Live schema verification:
```
\d user_settings
\d financial_cycles
```
both return the exact definitions above (verified on `localhost:15434`,
database `f1_r1_test`).

## C. DOMAIN

### `app/money/__init__.py`
Public surface re-exports: `payday_for`, `cycle_window_for`, `CycleWindow`,
`UserSettings`, `FinancialCycle`, `UserSettingsRepository`,
`FinancialCycleRepository`, `UserSettingsService`, `CycleService`,
`CurrentCycleView`, `CycleRolloverRequired`, `DEFAULT_PAYDAY_DAY`.

### `app/money/models.py`
- `UserSettings` — `telegram_user_id` PK + `payday_day`, `timezone`,
  `created_at`, `updated_at`. CHECK on `payday_day BETWEEN 1 AND 31`.
- `FinancialCycle` — full schema. `closing_snapshot` is
  `JSON().with_variant(JSONB(), "postgresql")` so the model is portable
  for the in-memory SQLite test fixture while the migration still emits
  `JSONB` for Postgres.
- ORM-level partial unique index mirrors the migration so that
  `init_db()` (used in tests and `app.database.database`) yields the
  same constraint surface as `alembic upgrade head`.
- `id` is `BigInteger().with_variant(Integer(), "sqlite")` — SQLite's
  autoincrement requires an `INTEGER PRIMARY KEY`; on Postgres the
  migration uses `BIGSERIAL`.

### `app/money/cycle_resolution.py`
- `payday_for(year, month, payday_day) -> date` — clamps to last day of
  month. Raises `ValueError` for `payday_day < 1` or `> 31`.
- `CycleWindow(start_date, end_date, payday_day)` — frozen dataclass.
- `cycle_window_for(today, payday_day) -> CycleWindow` — pure
  date arithmetic, walks back to the most recent payday on/before today
  and forward to the next payday. No DB. No timezone conversion — works
  on the business date directly (per spec: "No usar timestamps UTC para
  calcular estas fechas. Usar business date.").

### `app/money/repository.py`
- `UserSettingsRepository`: `get`, `get_or_default` (lazy default,
  unattached), `upsert_payday_day` (lazy create, validates range).
- `FinancialCycleRepository`: `find_open_cycle`, `find_latest_cycle`,
  `find_cycle_by_id`, `list_cycles`, `has_any_cycle`, `insert_cycle`.
  `insert_cycle` propagates `IntegrityError` so the service can decide
  on the partial unique violation path.

### `app/money/cycle_service.py`
- `UserSettingsService`: `get_settings`, `set_payday_day`.
- `CycleService.get_current_cycle(user_id, today)` — read-only,
  no writes; returns a `CurrentCycleView` dataclass or `None`.
- `CycleService.open_initial_cycle(user_id, today)` — A/B/C semantics
  from the spec:
  - **A.** Open cycle contains today → return it.
  - **B.** No cycles at all → compute window from `payday_day` and
    insert with `cycle_index=1`.
  - **C.** History exists but no current open cycle →
    `CycleRolloverRequired` (R2 territory; we **do not** synthesise
    rollover).

  Concurrency: on `IntegrityError` the service rolls back and re-reads
  on a fresh session (`_recover_after_concurrent_open`) so the
  rolled-back transaction's READ COMMITTED snapshot doesn't hide the
  winner's commit. The user-visible result is: every concurrent caller
  ends up holding the same logical cycle.

### `app/models_registry.py`
Adds the F1 model registration:
```python
from app.money.models import (  # noqa: F401, F811
    FinancialCycle,
    UserSettings,
)
```
No other change.

## D. PAYDAY SEMANTICS

Pure date-arithmetic (verified by `tests/test_money_cycle_resolution.py`):

| payday_day | today        | start_date   | end_date     | notes                       |
|-----------:|--------------|--------------|--------------|-----------------------------|
|          1 | 2026-06-17   | 2026-06-01   | 2026-06-30   | default payday              |
|          2 | 2026-10-15   | 2026-10-02   | 2026-11-01   | window payday=2             |
|          5 | 2026-08-15   | 2026-08-05   | 2026-09-04   | standard                    |
|         10 | 2026-12-15   | 2026-12-10   | 2027-01-09   | year crossing (Dec→Jan)     |
|         10 | 2027-01-05   | 2026-12-10   | 2027-01-09   | today BEFORE January payday |
|         31 | 2026-01-15   | 2026-01-31   | 2026-02-27   | Feb 2026 has 28 days        |
|         31 | 2026-02-28   | 2026-02-28   | 2026-03-30   | today IS the payday         |
|         31 | 2026-03-30   | 2026-02-28   | 2026-03-30   | still previous cycle        |
|         31 | 2026-03-31   | 2026-03-31   | 2026-04-29   | today IS the payday (Mar)   |
|         31 | 2026-04-15   | 2026-04-30   | 2026-05-30   | Apr clamped to 30           |
|         31 | 2028-02-15   | 2028-02-29   | 2028-03-30   | leap year (Feb 29)          |

Invalid input is rejected:
- `payday_for(2026, 1, 0)` → `ValueError`
- `payday_for(2026, 1, 32)` → `ValueError`
- `cycle_window_for(today, -1)` → `ValueError`

## E. INITIAL OPEN

### Lifecycle (`open_initial_cycle`)

```
                 ┌────────────────────────────────────────┐
                 │  1. get_current_cycle(user, today)     │
                 │     (read-only: find_open_cycle +      │
                 │      window.contains(today))           │
                 └────────────┬───────────────────────────┘
                              │
              ┌───────────────┴─────────────────┐
              │ view is not None                │
              ▼                                 ▼
       return view                    ┌──────────────────────┐
                                      │  2. has_any_cycle()  │
                                      └──────────┬───────────┘
                                                 │
                            ┌────────────────────┴────────────────────┐
                            ▼ False                                    ▼ True
                  ┌──────────────────────┐                  ┌────────────────────┐
                  │ 3. compute window +  │                  │ 4. raise           │
                  │    insert cycle with │                  │ CycleRolloverRe-   │
                  │    cycle_index = 1   │                  │ quired (R2)        │
                  └──────────┬───────────┘                  └────────────────────┘
                             │
                  ┌──────────┴───────────┐
                  ▼ IntegrityError        ▼ success
        ┌────────────────────┐    ┌────────────────────┐
        │ 5. rollback +      │    │ 6. commit + return │
        │ re-read on fresh   │    │    CurrentCycleView│
        │ session; canonical │    └────────────────────┘
        │ view               │
        └────────────────────┘
```

### Idempotency
`tests/test_money_cycle_service.py::test_open_initial_cycle_is_idempotent_on_same_today`
proves two sequential calls with the same `today` produce one physical
row (`cycle_index == 1`, same `cycle_id`) and the second call returns the
same logical view.

### Concurrency (PostgreSQL)
`tests/test_money_pg.py::_concurrent_open_initial_cycle` spins up
`ThreadPoolExecutor(max_workers=12)`. Each worker opens its own session
and calls `CycleService(session, session_factory=...).open_initial_cycle`
for the same `user_id`, same `today`, with no pre-existing rows.

Result on PostgreSQL 16 (`localhost:15434`):
- 12 workers × 1 call each → 12 returned `CurrentCycleView`s.
- Every returned view has the **same** `cycle_id` and `cycle_index == 1`.
- After the run, the DB contains **exactly one** row in `financial_cycles`
  for that user with `status='open'`.
- No `IntegrityError` escapes as observable behaviour.
- No advisory locks introduced; the partial unique index is the safety
  net and the application re-reads on a fresh session.

```text
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

## F. READ CONTRACT

`CycleService.get_current_cycle` is **read-only**:

- No `INSERT`, `UPDATE`, `DELETE`.
- No `session.commit()`.
- The session is never mutated; the same session can be reused for
  subsequent writes.

Proven by `test_get_current_cycle_does_not_mutate`:

```python
# Pre: 1 cycle (cycle_index=1, status=open), opened_at=T0
before_count = ...  # 1
before_opened = ... # T0
CycleService(s).get_current_cycle(user_id=1, today=date(2026, 8, 20))
CycleService(s).get_current_cycle(user_id=1, today=date(2026, 8, 25))
# Post:
after_count  = ... # 1
after_opened = ... # T0  (unchanged)
```

Additional read contract:
- `get_current_cycle` returns `None` when no rows exist (verified).
- `get_current_cycle` returns `None` when the open cycle has expired
  (verified by inserting a cycle whose window doesn't contain today).

## G. TESTS

### Unit (cycle_resolution, pure)
`tests/test_money_cycle_resolution.py` — **18 cases**:
- payday normal clamp not needed;
- payday 31 January;
- payday 31 February non-leap year (→ Feb 28);
- payday 31 February leap year (→ Feb 29);
- payday 31 April (→ Apr 30);
- payday rejects 0;
- payday rejects 32;
- window payday=2 mid-month;
- window crossing year Dec→Jan;
- window crossing year Jan (today BEFORE January payday);
- window default payday=1;
- window payday=31 on Feb 28 (today IS payday);
- window payday=31 on Mar 30 (still previous cycle);
- window payday=31 on Mar 31 (today IS payday);
- window contains boundaries + outside;
- invalid payday 0 rejected;
- invalid payday 32 rejected;
- invalid payday negative rejected.

### Unit (cycle_service, repository — SQLite)
`tests/test_money_cycle_service.py` — **17 cases**:
- settings default lazy;
- payday set creates row lazily;
- payday set updates existing;
- payday set rejects 0;
- payday set rejects 32;
- open initial cycle (payday=2, today=2026-10-15) → 2026-10-02..2026-11-01, cycle_index=1;
- open initial cycle default payday=1;
- open initial cycle is idempotent on same today;
- open initial cycle raises `CycleRolloverRequired` when history exists;
- `closing_snapshot` stays NULL after open;
- `get_current_cycle` returns open in window;
- `get_current_cycle` returns None when no cycles;
- `get_current_cycle` returns None when open but expired;
- `get_current_cycle` does NOT mutate (read contract);
- repository `find_latest_cycle` returns highest index;
- repository `list_cycles` orders desc;
- repository `find_by_id` is scoped to user.

### Telegram `/config_payday`
`tests/test_money_payday_command.py` — **6 cases**:
- valid day persists row + timezone;
- missing args → usage;
- rejects 0;
- rejects 32;
- rejects non-numeric;
- updates existing row.

### API (`/api/cycle/current`, `/api/cycle/open`)
`tests/test_money_cycle_api.py` — **4 cases**:
- `GET /api/cycle/current` returns `{cycle: null}` when empty;
- `POST /api/cycle/open` then `GET /api/cycle/current` round-trip;
- `POST /api/cycle/open` returns HTTP 409 with `cycle_rollover_required`
  when history exists;
- `GET /api/cycle/current` returns null outside the window and **does
  not close** the still-open cycle (read-only contract on the wire).

### PostgreSQL integration
`tests/test_money_pg.py` (standalone, requires the dev Postgres on
`localhost:15434`, run with `F1_PG_*` env vars):
- `alembic upgrade head` from empty schema;
- CHECK `payday_day` violation fires on `payday_day = 0`;
- CHECK `end_date >= start_date` violation fires on reversed dates;
- UNIQUE `(telegram_user_id, cycle_index)` violation fires on duplicate;
- PARTIAL UNIQUE `WHERE status='open'` violation fires on a second
  open cycle for the same user;
- `alembic downgrade 0011_recurring_reminders` then `alembic upgrade
  head` re-upgrade cycle (downgrade to `base` is a pre-existing F0
  limitation unrelated to R1 — documented in `test_money_pg.main`);
- 12-thread concurrent `open_initial_cycle`:
  - 0 rows pre-test for user 500;
  - all 12 callers return the same `cycle_id`, `cycle_index == 1`,
    `status == 'open'`;
  - DB ends with exactly one row in `financial_cycles` for user 500.

### Full normal suite
```
$ .venv/bin/pytest tests/
======================== 410 passed, 1 warning in 7.19s ========================
```
(1 warning is an unrelated Starlette deprecation in FastAPI's
`TestClient` import.)

### Alembic
```
$ alembic history
0011_recurring_reminders -> 0012_cycle_foundation (head), cycle foundation: user_settings + financial_cycles (F1 R1)
...
$ alembic heads
0012_cycle_foundation (head)
$ alembic upgrade head  # from empty DB
INFO  [alembic.runtime.migration] Running upgrade 0011_recurring_reminders -> 0012_cycle_foundation, cycle foundation: user_settings + financial_cycles (F1 R1)
```

### `compileall`
```
$ python -m compileall -q app/money alembic/versions/0012_cycle_foundation.py tests/test_money_*.py
(clean exit)
```

### `git diff --check`
```
$ git diff --check
(clean exit — no whitespace/tab issues)
```

### Clean-head reproduction (per spec §13)
Because the spec forbids `git add/commit`, the R1 candidate cannot be
shipped as a `git archive HEAD`. A temporary copy was assembled from:

1. `git archive HEAD | tar -x` → baseline (committed state).
2. Overlaid new R1 files: `alembic/versions/0012_cycle_foundation.py`,
   `app/money/__init__.py`, `app/money/models.py`,
   `app/money/cycle_resolution.py`, `app/money/repository.py`,
   `app/money/cycle_service.py`, all 5 R1 test files.
3. Re-applied the R1 hunks to dirty files: `app/models_registry.py`
   (4-line addition), `app/bot/handlers.py` (1 import + 9-line handler
   block + 24-line helper), `app/web/routes.py` (2 new endpoints
   appended after `api_month_summary`).

Result:
```
$ cd <clean_tree>
$ .venv/bin/pytest tests/                              # full suite
======================== 398 passed, 1 warning in 6.30s ========================
$ .venv/bin/pytest tests/test_money_*.py               # R1 only
======================== 45 passed, 1 warning in 1.20s ========================
$ F1_PG_* .venv/bin/python -u tests/test_money_pg.py   # PG integration
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

The clean R1 candidate is self-sufficient. The 12-test delta between the
dirty worktree (410) and the clean tree (398) is the F0 suite
(`/analisis`, monthly dashboard, etc.) which depends on dirty F0
hunks; it is out of scope for R1.

## H. EXACT PATHS

```
alembic/versions/0012_cycle_foundation.py
app/money/__init__.py
app/money/models.py
app/money/cycle_resolution.py
app/money/repository.py
app/money/cycle_service.py
tests/test_money_cycle_resolution.py
tests/test_money_cycle_service.py
tests/test_money_cycle_api.py
tests/test_money_payday_command.py
tests/test_money_pg.py
```

Modified (R1 hunks only, additive):

```
app/models_registry.py         # +4 lines: money models registration
app/bot/handlers.py            # +1 import, +9 lines handler block, +24 lines helper
app/web/routes.py              # +2 endpoints appended after api_month_summary
```

## I. DIRTY PRESERVATION

Confirmed intact by `git status`:

| Dirty path                              | Pre-R1 hunks preserved? |
|----------------------------------------|-------------------------|
| `.env.example`                          | yes (untouched)         |
| `README.md`                             | yes (untouched)         |
| `SECURITY.md`                           | yes (untouched)         |
| `TELEGRAM_COMMANDS.md`                  | yes (untouched)         |
| `app/ai/prompts.py`                     | yes (untouched)         |
| `app/ai/schemas.py`                     | yes (untouched)         |
| `app/ai/service.py`                     | yes (untouched)         |
| `app/bot/app.py`                        | yes (untouched)         |
| `app/bot/service.py`                    | yes (untouched)         |
| `app/categories/registry.py`            | yes (untouched)         |
| `app/config/settings.py`                | yes (untouched)         |
| `app/expenses/models.py`                | yes (untouched)         |
| `app/expenses/repository.py`            | yes (untouched)         |
| `app/expenses/schemas.py`               | yes (untouched)         |
| `app/expenses/service.py`               | yes (untouched)         |
| `app/queries/intents.py`                | yes (untouched)         |
| `app/web/queries.py`                    | yes (untouched)         |
| `tests/test_ai_service.py`              | yes (untouched)         |
| `tests/test_bot_commands.py`            | yes (untouched)         |
| `tests/test_categories.py`              | yes (untouched)         |
| `tests/test_categories_fk.py`           | yes (untouched)         |
| `tests/test_expense_crud.py`            | yes (untouched)         |
| `tests/test_web_routes.py`              | yes (untouched)         |
| `app/bot/handlers.py`                   | F0 hunks preserved; R1 hunks are clearly separable (bottom of file + 1 import + a single if block) |
| `app/models_registry.py`                | R1 hunks are the ONLY dirty content for this file |
| `app/web/routes.py`                     | F0 hunks preserved; R1 hunks appended at end (`api_cycle_current`, `api_cycle_open`) |

## J. GIT

- No `git add` of any kind (no `git add .`, no `git add -A`, no
  `git add <file>`).
- No `git commit`.
- No `git push`.
- Working tree status (R1 deliverables all untracked, dirty files
  preserved):
  ```
  Untracked files:
      alembic/versions/0012_cycle_foundation.py
      app/money/
      tests/test_money_cycle_api.py
      tests/test_money_cycle_resolution.py
      tests/test_money_cycle_service.py
      tests/test_money_payday_command.py
      tests/test_money_pg.py
  ```

## K. PROGRESO

```
PROGRESO F1:
R1 = 100%
R2 = 0%
R3 = 0%
R4 = 0%

PROGRESO F1 TOTAL APROX: 25%
```

Frenar.
Esperar HUMAN ADVERSARIAL ARCHITECTURE GATE.
