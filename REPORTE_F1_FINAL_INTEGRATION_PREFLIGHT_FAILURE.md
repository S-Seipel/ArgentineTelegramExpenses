# F1 FINAL INTEGRATION PREFLIGHT FAILURE

**Date:** 2026-09-25
**Builder:** BUILDER / sequential / new session
**R4 base:** `fdcfca4` (R3 closure — `feat: add financial money state mutations`)

## Verdict

**STOP.** R4 implementation **MUST NOT** proceed.

Preflight **C** (source_key currency immutability) FAILED in both
mutations (`record_extra_income` and `record_savings_realized`).
Preflight A and Preflight B PASSED.

| Preflight | Domain | Result |
|-----------|--------|--------|
| **A** | monthly occurrence across calendar months | **PASS** |
| **B** | paid carry-over + template delete | **PASS** |
| **C** | source_key currency immutability | **FAIL** |

The new test artifact is `tests/test_f1_preflight.py` (4 tests,
uncommitted — see §N). The two failing tests are:

- `test_preflight_c_extra_currency_immutable_sqlite`
- `test_preflight_c_savings_realized_currency_immutable_sqlite`

## A. R3 FREEZE

```
$ git add <exact 10 R3 paths>
$ git diff --cached --name-status
M	app/bot/handlers.py
M	app/bot/service.py
M	app/money/__init__.py
A	app/money/money_state_service.py
M	app/web/routes.py
M	tests/test_fixed_expense_commands.py
A	tests/test_money_state_api.py
A	tests/test_money_state_pg.py
A	tests/test_money_state_service.py
A	tests/test_money_state_telegram.py

$ git commit -m "feat: add financial money state mutations"
[main fdcfca4] feat: add financial money state mutations
 10 files changed, 4652 insertions(+), 77 deletions(-)
```

- **pre-head:** `f7aff39 feat: add financial cycle and money read engine`
- **staged candidate:** exactly the 10 R3 overlay paths
- **commit hash:** `fdcfca4`
- **subject:** `feat: add financial money state mutations`
- **post-commit dirty paths (untouched, unrelated):**
  ```
   M .env.example
   M README.md
   M SECURITY.md
   M TELEGRAM_COMMANDS.md
   M app/ai/prompts.py
   M app/ai/schemas.py
   M app/ai/service.py
   M app/bot/app.py
   M app/categories/registry.py
   M app/config/settings.py
   M app/expenses/models.py
   M app/expenses/repository.py
   M app/expenses/schemas.py
   M app/expenses/service.py
   M app/queries/intents.py
   M app/web/queries.py
   M tests/test_ai_service.py
   M tests/test_bot_commands.py
   M tests/test_categories.py
   M tests/test_categories_fk.py
   M tests/test_expense_crud.py
   M tests/test_web_routes.py
  ?? REPORTE_F0_*.md  (3 files)
  ?? REPORTE_F1_R1_*.md
  ?? REPORTE_F1_R2_*.md  (3 files)
  ?? REPORTE_F1_R3_*.md  (2 files)
  ?? REPORTE_PROYECTO.md
  ?? app/insights.py
  ?? tests/test_expense_names.py
  ?? tests/test_f1_preflight.py   (NEW — preflight artifact, this gate)
  ```
- **R4 BASE = `fdcfca4`** (new R3 closure commit).

## B. FINAL PREFLIGHT

### B.1 — Preflight A: monthly occurrence across calendar months

**PASS.**

Test: `test_preflight_a_two_occurrences_across_calendar_months`
(`tests/test_f1_preflight.py:82`)

Setup:
- `payday_day = 31`
- cycle `2026-02-28 → 2026-03-30`
- template `due_day = 30`, amount `80000`, currency `ARS`

Verification:
- `_due_dates_in_window` (app/money/fixed_obligations.py:330) enumerates
  both `February` (clamped to `2026-02-28`) and `March`
  (`2026-03-30`).
- Both occurrences appear as distinct identities
  `(fixed_id, "2026-02")` and `(fixed_id, "2026-03")`.
- `pending_by_currency["ARS"] == Decimal("160000")` (80000 × 2).
- Paying only February leaves March pending independently
  (`pending_fixed["ARS"] == 80000` after the Feb `mark_paid`).
- The Mar occurrence has NO payment row, confirming identity independence.

### B.2 — Preflight B: paid carry-over + template delete

**PASS.**

Test: `test_preflight_b_paid_carry_over_then_template_delete`
(`tests/test_f1_preflight.py:212`)

Setup:
- Cycle A: `payday_day = 15`, dates `2026-09-15 → 2026-10-14`.
  LUZ template `due_day = 30` → Sep 30 occurrence in cycle A.
- `rollover_cycle(user_id, today=2026-10-15)` closes A with
  `force=True` (carries over the unpaid LUZ Sep obligation) and opens
  cycle B in the same transaction.
- `mark_paid(LUZ, "2026-09", 80000)` creates the payment row with
  `paid_at IS NOT NULL` and the canonical expense mirror.

Verifications:
- Cycle A's `closing_snapshot["carried_over_fixed"]` contains an entry
  with `original_month_year == "2026-09"` (frozen at 80000 ARS).
- The payment row exists with `paid_at IS NOT NULL`.
- The engine reports the LUZ Sep carry-over as `paid=True` in the
  resolved obligations.
- After `delete_fixed_expense(LUZ)`:
  - cycle A `closing_snapshot` is **byte-identical** to the
    pre-delete snapshot (immutable, never touched).
  - the LUZ Sep carry-over entry survives in the snapshot
    (`expected_amount == Decimal("80000")`).
  - pending_fixed for the historical Sep identity **does NOT
    increase**. The engine does not resurrect the paid Sep
    obligation as a new pending entry.

The current `FixedExpenseService.delete` + carry-over interaction is
sound: the `_unresolved_carry_over` check skips resolved entries, the
delete proceeds, and the immutable snapshot guarantees the historical
identity stays frozen.

### B.3 — Preflight C: source_key currency immutability

**FAIL.**

Tests (both fail):

- `test_preflight_c_extra_currency_immutable_sqlite`
  (`tests/test_f1_preflight.py:380`)
- `test_preflight_c_savings_realization_currency_immutable_sqlite`
  (`tests/test_f1_preflight.py:432`)

#### C.1 — extra income

```python
svc.record_extra_income(
    user_id, cycle_id, currency="ARS", amount=Decimal("1000"),
    source_key="extra-x",
)
svc.record_extra_income(
    user_id, cycle_id, currency="USD", amount=Decimal("1000"),
    source_key="extra-x",
)
# ACTUAL: row.currency silently mutated from "ARS" → "USD".
# EXPECTED: MoneyStateError (or equivalent domain error) raised.
```

Source of the bug — `app/money/money_state_service.py:309-329`:

```python
existing = self._find_extra(user_id, cycle_id, source_key)
if existing is not None:
    changed = False
    ...
    if existing.currency != currency:
        existing.currency = currency       # ← silent mutation
        changed = True
```

The IntegrityError fallback at line 355-356 repeats the same bug:

```python
canonical = self._find_extra(user_id, cycle_id, source_key)
...
if canonical.currency != currency:
    canonical.currency = currency           # ← silent mutation
```

#### C.2 — savings realization

Same bug in `record_savings_realized` at
`app/money/money_state_service.py:672-708`:

```python
existing = self._find_savings_realized(user_id, cycle_id, source_key)
if existing is not None:
    ...
    if existing.currency != currency:
        existing.currency = currency       # ← silent mutation
```

#### C.3 — why this is a hard FAIL

Per the user spec for F1 PHASE 1 / PREFLIGHT C:

> Currency asociada a una existing source_key:
> IMMUTABLE.
> Amount/note: pueden corregirse.
> Currency: NO.
> Esperado:
> explicit identity/currency conflict error
> o equivalente domain error.

The current behavior silently moves the operation from ARS to USD
when a retry with a different currency lands on the same
`source_key`. This corrupts the audit trail and the
currency-partitioned read model — the very invariant the F1 engine
is built around.

#### C.4 — required fix (NOT applied — HUMAN gate)

Per the user protocol:

> Si la arquitectura actual no puede garantizar esto:
> STOP y reportar.
> NO mutar snapshots cerrados.
> NO introducir ledger/table nueva sin HUMAN gate.

The required fix is:

1. Add new domain errors
   `ExtraIncomeCurrencyMismatchError` /
   `SavingsRealizationCurrencyMismatchError` (subclass of
   `MoneyStateError`).
2. In `record_extra_income` and `record_savings_realized`, when an
   existing row is found for the same `source_key` and the currency
   differs, raise the new error instead of mutating
   `existing.currency`.
3. Apply the same rule in the IntegrityError fallback path.
4. Re-run all four preflight tests and the existing 88 R3 tests.
5. Add PostgreSQL tests under `tests/test_money_state_pg.py`
   (per the spec, PG tests are required for Preflight C).

This fix is a **3-line change** in the service, no migration, no
schema change, no architectural shift. It can be applied without
HUMAN gate intervention per F1 §16 R4 (only "no migration 0014"
binds). The BUILDER did NOT apply it because the F1 protocol
explicitly forbids proceeding past a failing preflight.

## C. R3 vs preflight regression

```
$ pytest tests/test_money_state_*.py tests/test_f1_preflight.py -q
88 R3 tests + 2 preflight PASS + 2 preflight FAIL
= 90 passed, 2 failed
```

The 88 R3 tests are unaffected. The two failures are preflight C
only.

## D. GIT state at STOP

```
$ git status --short | wc -l
36     # 23 modified + 13 untracked (R3 dir + new preflight artifact)

$ git log --oneline -3
fdcfca4 feat: add financial money state mutations   ← R4 base
f7aff39 feat: add financial cycle and money read engine
0b2973d fix: make reminder lease timezone-safe

$ git diff --cached --name-only
(empty — nothing staged for R4)
```

- R3 closed at `fdcfca4`.
- R4 not started.
- Pre-existing dirty paths preserved (unrelated).
- `tests/test_f1_preflight.py` is the **only** new file beyond R3,
  sitting untracked as the preflight evidence for this gate.

## E. PROGRESS

```
R1 = 100%
R2 = 100%
R3 = 100% (closure commit fdcfca4)
R4 =   0% (BLOCKED by preflight C failure)

F1 = 90% candidate (R1+R2+R3 = 90%, R4 = 0%, preflight C = blocker)
```

## F. REQUEST TO HUMAN GATE

1. Confirm Preflight C failure analysis (C.1 + C.2 + C.3 + C.4 above).
2. Authorize BUILDER to apply the 3-line fix in
   `app/money/money_state_service.py` (no migration, no schema change,
   no new table — within R4's allowed scope per F1 §PHASE 2).
3. Confirm the rule "currency is immutable per source_key; amount and
   note are mutable" is the binding interpretation for F1 closing.
4. Re-issue the F1 PHASE 1 → PHASE 2 sequence after the fix lands.

Until the HUMAN GATE authorizes the fix and re-arms PHASE 1, the
BUILDER STOPS.

## G. ARTIFACTS

- `tests/test_f1_preflight.py` (untracked, 4 tests):
  - `test_preflight_a_two_occurrences_across_calendar_months` — PASS
  - `test_preflight_b_paid_carry_over_then_template_delete` — PASS
  - `test_preflight_c_extra_currency_immutable_sqlite` — **FAIL**
  - `test_preflight_c_savings_realization_currency_immutable_sqlite` —
    **FAIL**

Hidden dependencies: **NONE** (preflight tests touch only the public
service / engine APIs and the test conftest).