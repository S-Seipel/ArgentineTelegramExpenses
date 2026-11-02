"""F1 FINAL INTEGRATION PREFLIGHT (PHASE 1).

Three adversarial tests gating R4 implementation:

A. Monthly occurrence across calendar months (no month-end clamping bug)
B. Paid carry-over + template delete (no debt resurrection)
C. source_key currency immutability (no silent ARS->USD flip)

If any of A/B/C fails, R4 must NOT proceed and the HUMAN GATE
must be notified with the F1 FINAL INTEGRATION PREFLIGHT FAILURE
report. The tests are the artifact; the failures are the evidence.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.database.database import session_scope
from app.fixed_expenses.models import FixedExpensePayment
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.service import (
    FixedExpenseDraft,
    FixedExpenseHasHistoricalCarryOverError,
    FixedExpenseService,
    fixed_payment_source_key,
)
from app.money import (
    CycleService,
    MoneyEngine,
    MoneyStateService,
    UserSettingsService,
)
from app.money.fixed_obligations import resolve_current_obligations
from app.money.models import CycleIncome, FinancialCycle, SavingsRealization


# ---------------------------------------------------------------------------
# helpers (mirror the convention used by other money tests)
# ---------------------------------------------------------------------------


def _open_cycle(user_id: int, *, payday_day: int, today: date):
    with session_scope() as s:
        UserSettingsService(s).set_payday_day(
            user_id=user_id, payday_day=payday_day
        )
        view = CycleService(s).open_initial_cycle(
            user_id=user_id, today=today
        )
    return view


def _ensure_cycle(user_id: int, *, today: date):
    with session_scope() as s:
        cycle = CycleService(s).ensure_current_cycle(
            user_id=user_id, today=today
        )
    return cycle


def _add_fixed(
    user_id: int, *, name: str, amount: Decimal, due_day: int,
    currency: str = "ARS",
):
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            user_id,
            FixedExpenseDraft(
                name=name,
                expected_amount=amount,
                due_day_of_month=due_day,
                currency=currency,
            ),
        )
    return bill


def _add_income(
    cycle_id: int, *, currency: str, amount: Decimal, kind: str = "income",
):
    with session_scope() as s:
        s.add(
            CycleIncome(
                cycle_id=cycle_id,
                currency=currency,
                kind=kind,
                amount=amount,
            )
        )


# ===========================================================================
# PREFLIGHT A — monthly occurrence across calendar months
# ===========================================================================
#
# FixedExpense is MONTHLY.
# The occurrence identity is ``(fixed_expense_id, month_year)``.
# Therefore the generation must NOT assume:
#     1 template = always exactly 1 occurrence per financial cycle
# when the cycle window contains two calendar months and the template's
# due_day falls in BOTH after clamping.
#
# Setup:
#   payday_day = 31
#   cycle      : 2026-02-28 → 2026-03-30   (Feb 28 → Mar 30)
#   template   : due_day = 30, amount = 80000, currency = ARS
#
# Expected occurrence dates inside this cycle:
#   February: day 30 clamps to 2026-02-28  (last day of Feb 2026)
#   March:    2026-03-30
#
# Both occurrences must exist as DISTINCT obligations:
#   (fixed_id, "2026-02")   pending 80000
#   (fixed_id, "2026-03")   pending 80000
#
# Total pending for the cycle = 160000 (NOT 80000).
#
# After paying the February obligation (NOT the March one):
#   pending_fixed for cycle = 80000 (only March remains).
#   The March occurrence is INDEPENDENT — it does not inherit the
#   Feb resolution.
# ---------------------------------------------------------------------------


def test_preflight_a_two_occurrences_across_calendar_months(in_memory_db):
    user_id = 7100

    view = _open_cycle(
        user_id, payday_day=31, today=date(2026, 2, 28),
    )

    assert view.start_date == date(2026, 2, 28)
    assert view.end_date == date(2026, 3, 30)

    bill = _add_fixed(
        user_id, name="EDESUR", amount=Decimal("80000"), due_day=30,
    )

    with session_scope() as s:
        cycle = s.get(FinancialCycle, view.cycle_id)
        obligations = resolve_current_obligations(s, user_id, cycle)

    occ_month_years = sorted(o.month_year for o in obligations.occurrences)
    assert occ_month_years == ["2026-02", "2026-03"], (
        f"expected both Feb+Mar occurrences, got {occ_month_years}"
    )

    assert obligations.pending_by_currency.get(
        "ARS"
    ) == Decimal("160000"), (
        f"expected 160000 ARS pending (80k Feb + 80k Mar), got "
        f"{obligations.pending_by_currency}"
    )

    # Pay ONLY February. March must remain pending independently.
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        svc.mark_paid(user_id, bill.id, "2026-02", Decimal("80000"))

    with session_scope() as s:
        cycle = s.get(FinancialCycle, view.cycle_id)
        engine = MoneyEngine(s)
        summary = engine.get_cycle_summary(user_id, view.cycle_id)

    assert summary.pending_fixed_obligations.get(
        "ARS"
    ) == Decimal("80000"), (
        "paying February must NOT silently resolve March; expected "
        f"80000 ARS pending, got {summary.pending_fixed_obligations}"
    )

    # Verify the exact occurrence identities created.
    with session_scope() as s:
        payments = (
            s.query(FixedExpensePayment)
            .filter(FixedExpensePayment.fixed_expense_id == bill.id)
            .all()
        )
        payment_keys = {(p.fixed_expense_id, p.month_year) for p in payments}
        assert payment_keys == {(bill.id, "2026-02")}, (
            f"expected only Feb payment created, got {payment_keys}"
        )


# ===========================================================================
# PREFLIGHT B — paid carry-over + template delete must not resurrect debt
# ===========================================================================
#
# Cycle A: LUZ Sep 80 unpaid → close force=True → snapshot freezes it as
#   carried_over_fixed=[(fixed_id, 80, "ARS", "2026-09", "2026-09")].
# Cycle B: roll over, mark_paid(LUZ, "2026-09", actual=80).
#   payment row exists for (LUZ, "2026-09") with paid_at IS NOT NULL.
#   pending_fixed in cycle B = 0.
#
# Now attempt to delete the LUZ template.
#
# RULE: deleting the template must NOT cause the historical obligation
# to reappear as pending in cycle B. The closing_snapshot of cycle A is
# immutable — its carried_over_fixed block freezes the original AR$ 80.
# If the delete implementation soft-deletes the expense mirror and
# deletes the FixedExpensePayment row that resolved the carry-over,
# the next engine read of cycle B's pending_fixed will resolve the
# snapshot's carried-over entry to "no payment exists" and re-flag it
# as pending, breaking the audit trail.
#
# Current approved contract: reject hard deletion of every template
# referenced by a historical carry-over snapshot, including paid ones.
# Payment history, pending totals and the frozen snapshot remain intact.
# ---------------------------------------------------------------------------


def test_preflight_b_paid_carry_over_then_template_delete(in_memory_db):
    user_id = 7200

    # Cycle A starts on Sep 15, 2026 (payday=15), ends Oct 14.
    # LUZ due_day = 30 → September occurrence: 2026-09-30 in cycle A.
    view_a = _open_cycle(user_id, payday_day=15, today=date(2026, 9, 15))

    bill = _add_fixed(
        user_id, name="LUZ", amount=Decimal("80000"), due_day=30,
    )

    # Use rollover_cycle from inside cycle A's open session. The
    # rollover closes A with force=True (carries over the unpaid LUZ
    # obligation into the snapshot) AND opens cycle B in the same
    # transaction.
    with session_scope() as s:
        service = CycleService(s)
        cycle_b = service.rollover_cycle(
            user_id, today=date(2026, 10, 15)
        )

    assert cycle_b.id != view_a.cycle_id

    # Snapshot must carry over the unpaid LUZ Sep obligation.
    with session_scope() as s:
        cycle_a = s.get(FinancialCycle, view_a.cycle_id)
        assert cycle_a.status == "closed"
        carried_in_a = cycle_a.closing_snapshot.get(
            "carried_over_fixed", []
        )

    assert any(
        entry.get("original_month_year") == "2026-09"
        for entry in carried_in_a
    ), (
        f"expected cycle A snapshot to carry over LUZ Sep, got "
        f"{carried_in_a}"
    )

    # Pay the carried-over Sep obligation.
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        svc.mark_paid(
            user_id, bill.id, "2026-09", Decimal("80000"),
            paid_on=date(2026, 10, 15),
        )

    # Confirm the payment row exists with paid_at set.
    with session_scope() as s:
        pmt = (
            s.query(FixedExpensePayment)
            .filter(
                FixedExpensePayment.fixed_expense_id == bill.id,
                FixedExpensePayment.month_year == "2026-09",
            )
            .one_or_none()
        )
        assert pmt is not None, "FixedExpensePayment for LUZ Sep was not created"
        assert pmt.paid_at is not None, f"payment.paid_at is None: {pmt}"

    # Snapshot the cycle B summary BEFORE delete and record:
    # 1. The total pending_fixed (which may include a fresh Oct
    #    occurrence — that's fine; not what this test gates).
    # 2. The carried_over_fixed occurrences reported by the engine
    #    (the engine returns them from the snapshot block, verbatim).
    # 3. Whether the LUZ Sep carry-over is still marked pending
    #    (paid_at IS NOT NULL → must be reported as paid, not
    #    counted into pending_fixed_obligations).
    with session_scope() as s:
        engine = MoneyEngine(s)
        summary_b_before = engine.get_cycle_summary(
            user_id, cycle_b.id
        )

    # Find the LUZ Sep carry-over in summary and confirm it's paid.
    sep_carryover_before = [
        occ for occ in summary_b_before.carried_over_fixed
        if occ.original_month_year == "2026-09"
    ]
    assert len(sep_carryover_before) == 1, (
        f"expected exactly 1 LUZ Sep carry-over, got "
        f"{sep_carryover_before}"
    )

    pending_before = summary_b_before.pending_fixed_obligations.get(
        "ARS"
    ) or Decimal("0")
    pending_before_from_sep = pending_before  # tracked for diff

    # Snapshot the current cycle A snapshot bytes (immutable
    # invariant — must NOT change after delete).
    with session_scope() as s:
        snapshot_bytes_before_delete = (
            s.get(FinancialCycle, view_a.cycle_id).closing_snapshot
        )

    # A paid historical carry-over still needs its template and payment
    # history. Reject hard deletion before removing either record.
    with pytest.raises(FixedExpenseHasHistoricalCarryOverError):
        with session_scope() as s:
            svc = FixedExpenseService(FixedExpenseRepository(s))
            svc.delete(user_id, bill.id)

    with session_scope() as s:
        snapshot_bytes_after_delete = (
            s.get(FinancialCycle, view_a.cycle_id).closing_snapshot
        )
        engine = MoneyEngine(s)
        summary_b_after = engine.get_cycle_summary(
            user_id, cycle_b.id
        )
        remaining_payment = s.query(FixedExpensePayment).filter_by(
            fixed_expense_id=bill.id, month_year="2026-09"
        ).one()
        assert remaining_payment.paid_at is not None
        assert remaining_payment.expense_id == pmt.expense_id
        assert FixedExpenseRepository(s).get_by_id(user_id, bill.id) is not None
        cycle = s.get(FinancialCycle, cycle_b.id)
        historical_occurrence = [
            occurrence
            for occurrence in resolve_current_obligations(s, user_id, cycle).occurrences
            if occurrence.fixed_expense_id == bill.id
            and occurrence.original_month_year == "2026-09"
        ]
        assert len(historical_occurrence) == 1
        assert historical_occurrence[0].paid

    # Invariant 1: cycle A closing_snapshot must be byte-identical
    # after delete.
    assert snapshot_bytes_after_delete == snapshot_bytes_before_delete, (
        "cycle A closing_snapshot must be byte-identical after delete"
    )

    # Invariant 2: the historical LUZ Sep carry-over must NOT be
    # re-classified as pending after delete. Its expected_amount is
    # 80000 — if the engine reports the Sep carry-over with
    # paid_at NULL or re-flags it as unpaid, that is debt
    # resurrection.
    sep_carryover_after = [
        occ for occ in summary_b_after.carried_over_fixed
        if occ.original_month_year == "2026-09"
    ]
    assert len(sep_carryover_after) == 1, (
        f"LUZ Sep carry-over must survive the delete; got "
        f"{sep_carryover_after}"
    )
    assert sep_carryover_after[0].expected_amount == Decimal("80000"), (
        f"carry-over amount mutated by delete: "
        f"{sep_carryover_after[0].expected_amount}"
    )

    # Invariant 3: pending_fixed for the historical Sep identity
    # must remain unchanged, including any fresh October occurrence.
    pending_after = summary_b_after.pending_fixed_obligations.get(
        "ARS"
    ) or Decimal("0")
    assert pending_after == pending_before_from_sep, (
        f"CRITICAL: template delete resurrected historical Sep debt. "
        f"pending ARS before delete={pending_before_from_sep}, "
        f"after delete={pending_after}. "
        f"Rejected deletion must preserve the pending total."
    )


# ===========================================================================
# PREFLIGHT C — source_key currency immutability
# ===========================================================================
#
# source_key is the idempotent identity of an operation.
#
# Extra income (record_extra_income) and savings realization
# (record_savings_realized) must both REFUSE to silently change the
# currency of an existing source_key. Amount/note may be corrected;
# currency may NOT — that would corrupt the audit trail and the
# currency-partitioned read model.
#
# Expected behavior: an explicit identity/currency conflict error
# (e.g. ``ExtraIncomeCurrencyMismatchError`` /
# ``SavingsRealizationCurrencyMismatchError``) or equivalent domain
# error. The existing physical row's currency MUST NOT be overwritten.
#
# Both paths must be checked on SQLite AND PostgreSQL (the user
# requires PG tests for this case).
# ---------------------------------------------------------------------------


def test_preflight_c_extra_currency_immutable_sqlite(in_memory_db):
    user_id = 7300

    view = _open_cycle(user_id, payday_day=1, today=date(2026, 9, 1))

    with session_scope() as s:
        svc = MoneyStateService(s)
        svc.record_extra_income(
            user_id=user_id,
            cycle_id=view.cycle_id,
            currency="ARS",
            amount=Decimal("1000"),
            source_key="extra-x",
        )

    # Retry/correction must NOT silently switch currency.
    from app.money.money_state_service import MoneyStateError

    raised = False
    with session_scope() as s:
        svc = MoneyStateService(s)
        try:
            svc.record_extra_income(
                user_id=user_id,
                cycle_id=view.cycle_id,
                currency="USD",
                amount=Decimal("1000"),
                source_key="extra-x",
            )
        except MoneyStateError:
            raised = True

    assert raised, (
        "expected MoneyStateError when re-recording the same "
        "source_key with a different currency (ARS -> USD)"
    )

    # Verify the original row's currency was NOT overwritten.
    with session_scope() as s:
        from sqlalchemy import select

        row = s.execute(
            select(CycleIncome).where(
                CycleIncome.cycle_id == view.cycle_id,
                CycleIncome.source_key == "extra-x",
            )
        ).scalar_one()
        assert row.currency == "ARS", (
            f"currency was silently mutated: now {row.currency!r}"
        )


def test_preflight_c_savings_realization_currency_immutable_sqlite(
    in_memory_db,
):
    user_id = 7301

    view = _open_cycle(user_id, payday_day=1, today=date(2026, 9, 1))

    with session_scope() as s:
        svc = MoneyStateService(s)
        svc.record_savings_realized(
            user_id=user_id,
            cycle_id=view.cycle_id,
            currency="ARS",
            amount=Decimal("1000"),
            source_key="savings-x",
        )

    from app.money.money_state_service import MoneyStateError

    raised = False
    with session_scope() as s:
        svc = MoneyStateService(s)
        try:
            svc.record_savings_realized(
                user_id=user_id,
                cycle_id=view.cycle_id,
                currency="USD",
                amount=Decimal("1000"),
                source_key="savings-x",
            )
        except MoneyStateError:
            raised = True

    assert raised, (
        "expected MoneyStateError when re-recording the same "
        "source_key with a different currency (ARS -> USD) for "
        "savings_realized"
    )

    with session_scope() as s:
        from sqlalchemy import select

        row = s.execute(
            select(SavingsRealization).where(
                SavingsRealization.cycle_id == view.cycle_id,
                SavingsRealization.source_key == "savings-x",
            )
        ).scalar_one()
        assert row.currency == "ARS", (
            f"currency was silently mutated: now {row.currency!r}"
        )
