# F1 R3 MONEY STATE REPORT

## A. R1/R2 FREEZE

- **Pre-commit HEAD:** `0b2973d` — `fix: make reminder lease timezone-safe`.
- **Closure commit:** `f7aff39` — `feat: add financial cycle and money read engine`.
- **Closure subject (single commit):** `feat: add financial cycle and money read engine`.
- **Exact staged paths (28 total, 7009 insertions, 3 deletions):**

```
A  alembic/versions/0012_cycle_foundation.py          (143 lines)
A  alembic/versions/0013_money_engine_state.py        (285 lines)
A  app/money/__init__.py                              (60 lines)
A  app/money/cycle_resolution.py                      (70 lines)
A  app/money/cycle_service.py                         (642 lines)
A  app/money/dto.py                                   (239 lines)
A  app/money/engine.py                                (607 lines)
A  app/money/fixed_obligations.py                     (398 lines)
A  app/money/models.py                                (418 lines)
A  app/money/repository.py                            (167 lines)
A  app/money/snapshot.py                              (199 lines)
A  tests/test_money_cycle_api.py                      (90 lines)
A  tests/test_money_cycle_resolution.py               (112 lines)
A  tests/test_money_cycle_service.py                  (285 lines)
A  tests/test_money_engine.py                         (566 lines)
A  tests/test_money_engine_architecture.py            (359 lines)
A  tests/test_money_engine_lifecycle.py               (506 lines)
A  tests/test_money_engine_ownership.py               (437 lines)
A  tests/test_money_engine_pg.py                      (416 lines)
A  tests/test_money_payday_command.py                 (91 lines)
A  tests/test_money_pg.py                             (326 lines)
M  app/bot/handlers.py                                (174 lines, R1+R2 additive only)
M  app/bot/service.py                                 (6 lines, R2 help-text add)
M  app/expenses/repository.py                         (38 lines, R2 system-owned guard)
M  app/expenses/service.py                            (8 lines, R2 SystemOwnedExpenseError class)
M  app/fixed_expenses/service.py                      (120 lines, R2 delete hardening)
M  app/models_registry.py                             (8 lines, R1+R2 model imports)
M  app/web/routes.py                                  (242 lines, R1+R2 cycle endpoints)
```

- **Verified before commit:** `pytest tests/` on the staged tree = **459 passed**,
  matching the approved R1+R2 candidate.
- **Post-commit dirty paths (43 entries, all F0):**

```
M  .env.example
M  README.md
M  SECURITY.md
M  TELEGRAM_COMMANDS.md
M  app/ai/prompts.py
M  app/ai/schemas.py
M  app/ai/service.py
M  app/bot/app.py
M  app/bot/handlers.py                              (F0 /analisis block + AI handler)
M  app/bot/service.py                               (F0 /analisis help line)
M  app/categories/registry.py
M  app/config/settings.py
M  app/expenses/models.py
M  app/expenses/repository.py                       (F0 name_key column)
M  app/expenses/schemas.py
M  app/expenses/service.py
M  app/money/__init__.py                            (now also carries R3 re-exports; no F0 hunks)
M  app/queries/intents.py
M  app/web/queries.py
M  app/web/routes.py                                (F0 dashboard/mensual + monthly-summary)
M  tests/test_ai_service.py
M  tests/test_bot_commands.py
M  tests/test_categories.py
M  tests/test_categories_fk.py
M  tests/test_expense_crud.py
M  tests/test_fixed_expense_commands.py             (R3 update — see §J)
M  tests/test_web_routes.py
?? REPORTE_F0_*.md
?? REPORTE_F1_*.md
?? REPORTE_PROYECTO.md
?? app/insights.py
?? app/money/money_state_service.py                 (R3)
?? tests/test_expense_names.py
?? tests/test_money_state_api.py                    (R3)
?? tests/test_money_state_pg.py                     (R3)
?? tests/test_money_state_service.py                (R3)
?? tests/test_money_state_telegram.py               (R3)
```

## B. ARCHITECTURE

```
                       ┌───────────────────────┐
                       │ app.money.snapshot    │
                       │ (pure module)         │
                       └─────────┬─────────────┘
                                 │
            ┌────────────────────┼─────────────────────┐
            ▼                                          ▼
   ┌──────────────────────┐               ┌─────────────────────────┐
   │   MoneyEngine        │               │   CycleService          │
   │   (READ-ONLY)        │ ◀──consumes── │   (lifecycle owner)     │
   │                      │               │                         │
   │  - get_cycle_summary │               │  - close_cycle          │
   │  - get_available     │               │  - rollover_cycle       │
   │  - get_daily_safe    │               │  - ensure_current_cycle │
   │  - detect_invariants │               │  - open_initial_cycle   │
   └──────────┬───────────┘               └─────────────────────────┘
              ▲                                          ▲
              │ reads                                    │ rolls over
              │                                          │
   ┌──────────┴──────────────┐               ┌──────────┴──────────────┐
   │   MoneyStateService     │               │   MoneyStateService     │
   │   (R3 MUTATIONS)        │               │   (R3 cycle gate)       │
   │                         │               │                         │
   │  - set_income           │               │  _require_open_cycle    │
   │  - record_extra_income  │               │   (shared by every      │
   │  - update_extra_income  │               │    mutation method)     │
   │  - soft_delete_extra    │               │                         │
   │  - set_savings_target   │               │                         │
   │  - set_savings_reserved │               │                         │
   │  - release_savings      │               │                         │
   │  - record_savings_      │               │                         │
   │     realized            │               │                         │
   │  - update_savings_      │               │                         │
   │     realized            │               │                         │
   │  - soft_delete_         │               │                         │
   │     savings_realized    │               │                         │
   └─────────────────────────┘               └─────────────────────────┘
              │                                          ▲
              ▼                                          │
   ┌────────────────────────────────────────────────────────────────────┐
   │  app.money.models.{CycleIncome, SavingsTarget, SavingsReserved,    │
   │                       SavingsRealization}                          │
   │  app.money.repository.FinancialCycleRepository                     │
   └────────────────────────────────────────────────────────────────────┘
```

Dependency direction:

- `MoneyStateService` → SQLAlchemy `Session` → `models.py` only.
- `MoneyStateService` does **not** import `MoneyEngine` (read engine) or
  `CycleService` (lifecycle) — the cycle-gate helper
  `_require_open_cycle` reads the `FinancialCycle` row directly.
- `MoneyEngine` does **not** import `MoneyStateService` (the
  architecture test `test_money_engine_does_not_import_cycle_service`
  is unchanged and still passes).
- `CycleService` does **not** import `MoneyStateService` (it already
  consumed `MoneyEngine` for the snapshot builder; adding R3 would
  invert the arrow).

## C. BASE INCOME

`MoneyStateService.set_income(user_id, cycle_id, currency, amount)`:

- Validates cycle exists, owned by `user_id`, and `status='open'`.
- Validates `amount` is a finite `Decimal` ≥ 0; `0` is accepted.
- UPSERT on `(cycle_id, currency) WHERE kind='income' AND
  deleted_at IS NULL` (the partial unique index from migration
  0013). The select-then-insert-or-update path catches
  `IntegrityError` from concurrent inserts and re-reads the canonical
  row.
- Returns the live `CycleIncome` ORM row.
- One mutation = one currency. ARS/USD never mix.

```text
set_income(1, 10, ARS, 1_100_000)  →  row id=1 amount=1100000.00
set_income(1, 10, ARS, 1_100_000)  →  same row, revision unchanged
set_income(1, 10, ARS, 1_200_000)  →  same row, amount=1200000.00 revision=2
```

## D. EXTRA INCOME

Source-key identity (`(cycle_id, source_key)`) survives soft-delete so a
re-issued `/extra` with the same key restores the previous row instead of
creating a duplicate. The Telegram source key is deterministic:

```text
telegram:extra:<chat_id>:<message_id>
```

| Operation | Semantics |
|-----------|-----------|
| `record_extra_income` | UPSERT on `(cycle_id, source_key)`. If a soft-deleted row exists for the key, the new payload restores it. |
| `update_extra_income(user_id, extra_id, amount=None, note=None)` | Updates by id with ownership check. `revision` bumps only on actual change. |
| `soft_delete_extra_income(user_id, extra_id)` | Idempotent: a second call on an already-deleted row bumps the revision by 1 with no other effect. |

Tests cover: create / same-source-update / soft-delete / restore /
amount=0 reject / negative reject / missing-source reject /
empty-source reject / revision bump / cross-user reject.

## E. SAVINGS TARGET

`MoneyStateService.set_savings_target(user_id, cycle_id, currency,
*, target_pct=None, target_amount=None)`:

- XOR enforced: exactly one of `target_pct` / `target_amount`.
  Both / neither → `TargetXorViolationError` (HTTP 400).
- `target_pct` quantized to 4 decimals, range `[0, 1]`.
- `target_amount` quantized to 2 decimals, `>= 0`.
- UPSERT on `(cycle_id, currency)` (composite PK).
- Default 20% is a **soft** default in `MoneyEngine`; R3 never
  auto-inserts a row on read (verified by
  `test_set_savings_target_default_soft_unpersisted`).
- Changing the target does not touch `savings_reserved` (verified by
  `test_set_savings_target_change_does_not_touch_reserved`).

## F. SAVINGS RESERVED + RELEASE

`set_savings_reserved(user_id, cycle_id, currency, amount)`:

- Absolute replace (NOT increment). `amount` may be `0`; negatives are
  rejected with `InvalidAmountError`.
- UPSERT on `(cycle_id, currency)` (composite PK).
- `reserved` may legally sit BELOW `realized` — the engine emits
  `savings_realized_exceeds_reserved` in `summary.warnings` but never
  raises.

`release_savings(user_id, cycle_id, currency, amount)`:

- Decrements `reserved` by `amount`, clamped at `0`.
- **Never** inserts a negative `SavingsRealization`.
- The committed formula `max(reserved, realized)` lives in
  `MoneyEngine._compute_open_summary`; release does not invent any
  bookkeeping transfer.

```text
reserved=220   realized=150   committed=220   available = gross-220-expenses-pending
release 100    reserved=120   realized=150   committed=150 (engine picks max)
release 500    reserved=0     realized=150   committed=150
```

## G. SAVINGS REALIZED

Source-key identity `(cycle_id, source_key)` (UNIQUE survives
soft-delete). Telegram source key:

```text
telegram:savings:<chat_id>:<message_id>
```

| Operation | Semantics |
|-----------|-----------|
| `record_savings_realized` | UPSERT. A re-issued command with the same key restores a soft-deleted row. |
| `update_savings_realized(user_id, realization_id, amount=None, note=None)` | Updates by id with ownership check. |
| `soft_delete_savings_realized(user_id, realization_id)` | Idempotent; a second call bumps the revision by 1 with no other effect. |

Tests cover: create / same-source-update / soft-delete / restore /
amount≤0 reject / source required / cross-user reject / revision bump.

## H. AVAILABLE DELTAS

The committed formula is computed by `MoneyEngine._compute_open_summary`:

```python
savings_committed = max_by_currency(savings_reserved, savings_realized)
```

Exact spec §9 cases:

| Step | Reserved | Realized | Committed | Δ Available |
|------|---------:|---------:|----------:|------------:|
| start | 220 000 | 150 000 | **220 000** | 0 |
| realized 150→200 | 220 000 | 200 000 | **220 000** | 0 |
| reserved 220→220 (Δ0) — baseline above; same | | | | |
| reserved=220 realized 150→300 | 220 000 | 300 000 | **300 000** | **-80 000** |
| realized 300→180 (soft-delete one) | 220 000 | 180 000 | **220 000** | **+80 000** |

Verified end-to-end by `test_committed_delta_zero_reserved_above_realized`,
`test_committed_delta_negative_reserved_below_realized`, and
`test_committed_delta_positive_realized_drop`.

## I. API

Eleven new JSON endpoints, all under `/api/cycles/*`. They share a small
helper surface (`_resolve_open_cycle`, `_parse_amount`,
`_state_error_to_http`) that maps domain errors to HTTP status codes
consistently:

| Status | When |
|-------:|------|
| 200    | mutation succeeded; returns the entity + the updated `CycleSummary` JSON payload |
| 400    | invalid_argument (amount / source_key / XOR violation) |
| 404    | cycle_not_found OR entity_not_found (cross-user invisible) |
| 409    | cycle_closed (rejected at the gate) |
| 500    | internal_error (defensive catch-all) |

Endpoint catalog:

```
POST   /api/cycles/income
POST   /api/cycles/extra
PATCH  /api/cycles/extra/<extra_id>
DELETE /api/cycles/extra/<extra_id>
POST   /api/cycles/savings/target
POST   /api/cycles/savings/reserved
POST   /api/cycles/savings/release
POST   /api/cycles/savings/realized
PATCH  /api/cycles/savings/realized/<realization_id>
DELETE /api/cycles/savings/realized/<realization_id>
```

The pre-existing `/api/cycle-summary` (R2 read) and
`/api/cycle/{cycle_id}/close` (R2 lifecycle) are untouched.

## J. TELEGRAM

Eleven new commands wired in `app/bot/handlers.py`. All routes the
Telegram user through `CycleService.ensure_current_cycle` (R2 rollover
walk), so the user never has to think about cycle ids; the source keys
are deterministic `(chat_id, message_id)` pairs:

```text
extra:    telegram:extra:<chat_id>:<message_id>
savings:  telegram:savings:<chat_id>:<message_id>
```

| Command | Action |
|---------|--------|
| `/ingreso <amount>` | `set_income` (UPSERT base income) |
| `/extra <amount> [nota]` | `record_extra_income` (source_key = chat+message) |
| `/extra_editar <id> <amount> [nota]` | `update_extra_income` |
| `/extra_borrar <id>` | `soft_delete_extra_income` |
| `/target 20%` / `/target 220000` | `set_savings_target` (pct XOR amount; bare numbers are treated as amounts, never silently as pct — see §15) |
| `/reservar <amount>` | `set_savings_reserved` |
| `/liberar <amount>` | `release_savings` |
| `/ahorro_realizado <amount> [nota]` | `record_savings_realized` |
| `/ahorro_editar <id> <amount> [nota]` | `update_savings_realized` |
| `/ahorro_borrar <id>` | `soft_delete_savings_realized` |

The legacy `/ingreso` and `/extra` handlers (which wrote to
`MonthlyBudget.income` / `MonthlyBudget.extra`) are removed from
`_handle_fixed_expense_command`. `/liberado` still reads the legacy
`MonthlyBudget` row, so the pre-existing test
`test_ingreso_and_liberado` was updated to seed `MonthlyBudget` via
`FixedExpenseService.set_budget` directly. `MonthlyBudget` and
`/api/month-summary` stay untouched per spec §20.

Help text (`_help_text`) lists the new commands under a "Ciclo:" section.

## K. OWNERSHIP + CLOSED CYCLE

Every mutation enforces three invariants through
`MoneyStateService._require_open_cycle`:

1. **Cycle exists.** `CycleNotFoundError` → HTTP 404 / Telegram
   "Ciclo no encontrado".
2. **Cycle owned by `user_id`.** `CycleOwnershipMismatchError` → HTTP
   404 / Telegram refusal. Cross-user probes return the same `404`
   shape as a real missing cycle, so a malicious caller cannot probe
   for id existence.
3. **Cycle is `status='open'`.** `CycleClosedError` → HTTP 409 /
   Telegram refusal. Historical state is immutable.

For `update_extra_income` / `soft_delete_extra_income` /
`update_savings_realized` / `soft_delete_savings_realized` the gate
runs after the id-based ownership check, so a malicious user cannot
update another user's entity even by knowing the id.

Verified by:

- `test_set_income_ownership_mismatch`
- `test_update_extra_income_cross_user_rejected`
- `test_soft_delete_extra_income_cross_user_rejected`
- `test_update_savings_realized_cross_user_rejected`
- `test_soft_delete_savings_realized_cross_user_rejected`
- `test_closed_cycle_rejects_every_mutation` (one assertion per
  mutation; closes the cycle and confirms each one raises
  `CycleClosedError`)

## L. TESTS

### L.1 Unit / service (tests/test_money_state_service.py)

58 cases covering:

- **BASE** — create / same-position update / zero valid / negative
  reject / cross-currency isolation / closed-cycle reject / ownership
  reject / cycle-not-found reject.
- **EXTRA** — create / same-source update / soft-delete / restore /
  amount-zero reject / negative reject / missing-source reject /
  empty-source reject / revision bump / cross-user reject.
- **TARGET** — pct / amount / both reject / neither reject / pct<0
  reject / pct>1 reject / amount negative reject / default-soft
  unpersisted / change does not touch reserved.
- **RESERVED** — zero / positive / replace / negative reject /
  below-realized allowed (warning) / no automatic relation to target.
- **REALIZED** — create / same-source update / soft-delete / restore /
  amount<=0 reject / source required / cross-user reject.
- **COMMITTED** — 150→200 = delta 0 / 150→300 = -80 000 / 300→180 =
  +80 000 (the three exact spec §9 cases).
- **CLOSED** — every mutation rejected after `close_cycle`.
- **DECIMAL** — 0.01 / 1234.56 roundtrip exact / int coercion /
  string coercion / garbage string reject.

### L.2 Web API (tests/test_money_state_api.py)

16 cases covering each endpoint's success and failure paths plus the
closed-cycle rejection on the income endpoint.

### L.3 Telegram (tests/test_money_state_telegram.py)

14 cases covering each command's success path and the explicit
`/target 20%` vs `/target 220000` vs `/target 20` (bare number
treated as amount, never silently as pct).

### L.4 PostgreSQL (tests/test_money_state_pg.py)

Standalone runner (`python -m tests.test_money_state_pg`) that
verifies:

- `cycle_incomes` UNIQUE `(cycle_id, source_key)` survives
  soft-delete.
- `savings_realizations` UNIQUE `(cycle_id, source_key)` survives
  soft-delete.
- `savings_targets` XOR CHECK rejects both-NULL inserts.
- `cycle_incomes` CHECK rejects `kind='extra'` without `source_key`.
- 12 threads × `set_income` same `(cycle, currency)` → exactly 1
  base row.
- 12 threads × `record_extra_income` same key → exactly 1 extra row.
- 12 threads × `record_savings_realized` same key → exactly 1
  realization row.
- NUMERIC precision: 1234.56 and 0.01 round-trip exact.

Result on `f1_r3_candidate_test`:

```
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

### L.5 R1+R2 regression on PostgreSQL

`tests/test_money_engine_pg` (R2) on `f1_r3_cand_r2_test`: all checks
passed (downgrade / re-upgrade round-trip + concurrent
`ensure_current_cycle`).

`tests/test_money_pg` (R1) on `f1_r3_cand_r1_test`: all checks passed
(concurrent `open_initial_cycle`).

### L.6 Summary

| Suite | Count |
|-------|------:|
| Clean candidate (R1+R2 baseline) | **459** |
| R3 service tests (added) | +58 |
| R3 API tests (added) | +16 |
| R3 Telegram tests (added) | +14 |
| **Clean candidate total** | **547** |
| F0 dirty (excluded from R3) | +12 |
| Dirty worktree (full pre-R3) | **559** |

`compileall`: clean.
`git diff --check`: clean.
`alembic upgrade head` against an empty PostgreSQL DB: chain runs
end-to-end (`… -> 0012_cycle_foundation -> 0013_money_engine_state
(head)`); R3 adds no new migration because the schema was complete in
R2.

## M. CLEAN CANDIDATE

- **Base hash:** `f7aff39` — R1+R2 closure commit.
- **Exact R3 overlay (paths the human gate should review):**

```
A  app/money/money_state_service.py
M  app/money/__init__.py                     (adds R3 re-exports + docstring)
M  app/web/routes.py                         (adds 11 endpoints + helpers)
M  app/bot/handlers.py                        (adds R3 dispatcher + handlers)
M  app/bot/service.py                         (extends help text; drops legacy
                                              /ingreso + /extra write paths)
M  tests/test_fixed_expense_commands.py      (seeds MonthlyBudget directly)
A  tests/test_money_state_service.py
A  tests/test_money_state_api.py
A  tests/test_money_state_telegram.py
A  tests/test_money_state_pg.py
```

- **Results on the isolated candidate (`/tmp/r3_clean`):**

```
pytest tests/                                →  547 passed, 1 warning
compileall .                                 →  clean exit (rc=0)
git diff --check                              →  clean exit
alembic history                              →  0013_money_engine_state (head)
test_money_pg (PG)                           →  all checks passed
test_money_engine_pg (PG)                    →  all checks passed
test_money_state_pg (PG)                     →  all checks passed
```

## N. EXACT R3 PATHS

```
A  app/money/money_state_service.py                      (~510 lines, service)
A  tests/test_money_state_service.py                    (~820 lines, 58 tests)
A  tests/test_money_state_api.py                        (~330 lines, 16 tests)
A  tests/test_money_state_telegram.py                   (~310 lines, 14 tests)
A  tests/test_money_state_pg.py                         (~430 lines, PG runner)
M  app/money/__init__.py                                (49 lines diff: doc + R3 exports)
M  app/web/routes.py                                    (~520 lines diff: 11 endpoints + helpers)
M  app/bot/handlers.py                                  (~520 lines diff: dispatcher + handlers)
M  app/bot/service.py                                   (~20 lines diff: help text + legacy handler removal)
M  tests/test_fixed_expense_commands.py                 (~25 lines diff: seeds MonthlyBudget via set_budget)
```

## O. DIRTY PRESERVATION

| Dirty path | Status |
|------------|--------|
| `.env.example` / `README.md` / `SECURITY.md` / `TELEGRAM_COMMANDS.md` | unchanged F0 work |
| `app/ai/{prompts,schemas,service}.py` | unchanged F0 work |
| `app/bot/app.py` | unchanged F0 work |
| `app/bot/handlers.py` | F0 `/analisis` block + AI handler preserved; R3 dispatcher and handlers added; legacy `/ingreso`/`/extra` removed |
| `app/bot/service.py` | F0 `/analisis` help line preserved; R3 cycle command lines added; legacy `/ingreso`/`/extra` lines removed |
| `app/categories/registry.py` | unchanged F0 work |
| `app/config/settings.py` | unchanged F0 work |
| `app/expenses/{models,repository,schemas,service}.py` | F0 `name_key` work preserved (R1+R2 commit did not strip these from the worktree; they remain unstaged) |
| `app/money/__init__.py` | R3 re-exports added on top of the dirty version |
| `app/queries/intents.py` | unchanged F0 work |
| `app/web/queries.py` | unchanged F0 work |
| `app/web/routes.py` | F0 `/dashboard/mensual`, `/api/monthly-summary`, `category` query, monthly nav preserved; R3 endpoints added |
| `tests/test_*.py` (the F0 ones) | unchanged F0 work |
| `tests/test_fixed_expense_commands.py` | R3 update: seeds MonthlyBudget via `set_budget` |
| All `?? REPORTE_*.md`, `app/insights.py`, `tests/test_expense_names.py` | F0 / R1+R2 reports preserved as untracked |
| `tests/test_money_state_*.py`, `app/money/money_state_service.py` | R3 NEW files (untracked) |

## P. GIT

- R3 is **NOT** staged.
- R3 is **NOT** committed.
- R3 is **NOT** pushed.
- Working tree status (after the R1+R2 closure commit + R3 work):

```
$ git status --short | wc -l
43

$ git diff --stat | tail -1
 27 files changed, 2173 insertions(+), 101 deletions(-)
```

- New untracked R3 paths:

```
?? app/money/money_state_service.py
?? tests/test_money_state_service.py
?? tests/test_money_state_api.py
?? tests/test_money_state_telegram.py
?? tests/test_money_state_pg.py
```

- Last commit (R1+R2 closure):

```
$ git log --oneline -2
f7aff39 feat: add financial cycle and money read engine
0b2973d fix: make reminder lease timezone-safe
```

## Q. PROGRESS

```
R1 = 100%
R2 = 100%
R3 = 100% (candidate)
R4 =   0%

F1 TOTAL ≈ 75%
```

Frenar.
Esperar HUMAN ADVERSARIAL ARCHITECTURE GATE.
