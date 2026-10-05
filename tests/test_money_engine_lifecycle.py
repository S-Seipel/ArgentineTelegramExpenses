"""R2 cycle lifecycle tests: close / rollover / catch-up / snapshot.

Covers the spec §12-§17 behaviours in their SQLite-backed form.
Postgres-specific concurrency lives in :mod:`tests.test_money_engine_pg`.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from app.database.database import session_scope
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.service import (
    FixedExpenseDraft,
    FixedExpenseHasHistoricalCarryOverError,
    FixedExpenseService,
)
from app.fixed_expenses.models import FixedExpensePayment
from app.money import (
    CycleCatchUpLimitExceeded,
    CycleHasPendingError,
    CycleService,
    MoneyEngine,
    SnapshotVersionMismatchError,
    UserSettingsService,
)
from app.money.fixed_obligations import (
    fixed_payment_source_key,
    occurrence_month_year,
)
from app.money.models import (
    CycleIncome,
    FinancialCycle,
)
from app.money.snapshot import (
    CURRENT_SNAPSHOT_VERSION,
    ensure_snapshot_version,
)


# ---------------------------------------------------------------------------
# helpers (mirror those in test_money_engine.py)
# ---------------------------------------------------------------------------


def _open_cycle(user_id: int, *, payday_day: int, today: date):
    with session_scope() as s:
        UserSettingsService(s).set_payday_day(
            user_id=user_id, payday_day=payday_day
        )
        view = CycleService(s).open_initial_cycle(user_id=user_id, today=today)
    return view


def _add_income(cycle_id: int, *, currency: str, amount: Decimal):
    with session_scope() as s:
        s.add(
            CycleIncome(
                cycle_id=cycle_id,
                currency=currency,
                kind="income",
                amount=amount,
            )
        )


def _add_fixed(
    user_id: int, *, name: str, amount: Decimal, due_day: int = 5
):
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            user_id,
            FixedExpenseDraft(
                name=name,
                expected_amount=amount,
                due_day_of_month=due_day,
            ),
        )
    return bill


# ---------------------------------------------------------------------------
# close_cycle
# ---------------------------------------------------------------------------


def test_manual_close_clean(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1000000"))

    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        summary = service.close_cycle(1, view.cycle_id)
    assert summary.status == "closed"
    assert summary.snapshot_version == CURRENT_SNAPSHOT_VERSION
    with session_scope() as s:
        cycle = s.get(FinancialCycle, view.cycle_id)
        assert cycle.status == "closed"
        assert cycle.closed_at is not None
        assert cycle.closing_snapshot is not None


def test_force_false_rejects_pending(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1000000"))
    _add_fixed(1, name="X", amount=Decimal("50000"), due_day=15)

    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        with pytest.raises(CycleHasPendingError):
            service.close_cycle(1, view.cycle_id, force=False)


def test_force_true_allows_pending(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1000000"))
    _add_fixed(1, name="X", amount=Decimal("50000"), due_day=15)
    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        summary = service.close_cycle(1, view.cycle_id, force=True)
    assert summary.status == "closed"
    # The unresolved obligation survives as a carried-over entry.
    assert any(occ.name == "X" for occ in summary.carried_over_fixed)


# ---------------------------------------------------------------------------
# rollover / catch-up
# ---------------------------------------------------------------------------


def test_rollover_pending_allowed(in_memory_db):
    """Natural rollover closes an expired cycle even with pending obligations."""
    _open_cycle(1, payday_day=2, today=date(2026, 10, 15))
    _add_fixed(1, name="LUZ", amount=Decimal("80"), due_day=15)
    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        cycle = service.rollover_cycle(1, today=date(2026, 11, 2))
    # Rollover created a new cycle (cycle_index=2) that covers Nov.
    assert cycle.cycle_index == 2
    assert cycle.start_date == date(2026, 11, 2)
    with session_scope() as s:
        closed = (
            s.query(FinancialCycle)
            .filter(FinancialCycle.cycle_index == 1)
            .one()
        )
        assert closed.status == "closed"
        snapshot = closed.closing_snapshot
        assert snapshot is not None
        assert snapshot["closed_by"] == "rollover"
        carried = snapshot["carried_over_fixed"]
        assert len(carried) == 1
        assert carried[0]["name"] == "LUZ"
        assert carried[0]["expected_amount"] == "80.00"


def test_catch_up_walks_multiple_cycles(in_memory_db):
    """Spec §14: last open = Sep, today = Dec → close Sep, Oct, Nov, open Dec."""
    _open_cycle(1, payday_day=2, today=date(2026, 9, 15))
    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        cycle = service.ensure_current_cycle(1, today=date(2026, 12, 5))
    # The new cycle must cover Dec.
    assert cycle.start_date <= date(2026, 12, 5) <= cycle.end_date
    with session_scope() as s:
        all_cycles = (
            s.query(FinancialCycle)
            .filter(FinancialCycle.telegram_user_id == 1)
            .order_by(FinancialCycle.cycle_index.asc())
            .all()
        )
    statuses = [c.status for c in all_cycles]
    # 4 cycles: Sep closed (rollover), Oct closed, Nov closed, Dec open.
    assert statuses == ["closed", "closed", "closed", "open"]
    indexes = [c.cycle_index for c in all_cycles]
    assert indexes == [1, 2, 3, 4]


def test_catch_up_limit_exceeded(in_memory_db):
    """Defensive guard: too many cycles raises explicitly."""
    _open_cycle(1, payday_day=2, today=date(2026, 9, 15))
    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        with pytest.raises(CycleCatchUpLimitExceeded):
            # Far in the future → 12 steps not enough.
            service.ensure_current_cycle(
                1, today=date(2030, 12, 1)
            )


def test_ensure_current_cycle_is_noop_when_today_inside_open(in_memory_db):
    view = _open_cycle(1, payday_day=2, today=date(2026, 10, 15))
    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        cycle = service.ensure_current_cycle(1, today=date(2026, 10, 20))
    assert cycle.id == view.cycle_id
    assert cycle.status == "open"


# ---------------------------------------------------------------------------
# snapshot version
# ---------------------------------------------------------------------------


def test_snapshot_version_constant_is_one():
    assert CURRENT_SNAPSHOT_VERSION == 1


def test_snapshot_missing_version_raises():
    with pytest.raises(SnapshotVersionMismatchError):
        ensure_snapshot_version({"money": {}})


def test_snapshot_wrong_version_raises():
    with pytest.raises(SnapshotVersionMismatchError):
        ensure_snapshot_version({"snapshot_version": 2})


def test_snapshot_round_trip_via_summary(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1000000"))
    _add_fixed(1, name="X", amount=Decimal("50000"), due_day=15)
    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        service.close_cycle(1, view.cycle_id, force=True)

    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        summary = engine.get_cycle_summary(1, view.cycle_id)
    # Closed cycles serve the snapshot, not a live recompute.
    assert summary.status == "closed"
    assert summary.snapshot_version == 1
    assert any(occ.name == "X" for occ in summary.carried_over_fixed)


def test_daily_safe_to_spend_null_for_closed_cycle(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1000000"))
    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        service.close_cycle(1, view.cycle_id, force=True)
    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        daily = engine.get_daily_safe_to_spend(1, view.cycle_id)
    assert daily is None


def test_daily_safe_to_spend_negative_inherits_sign(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("100"))
    from app.expenses.repository import ExpenseRepository
    from app.expenses.schemas import ExpenseCreate

    with session_scope() as s:
        ExpenseRepository(s).create(
            ExpenseCreate(
                telegram_user_id=1,
                name="x",
                amount=Decimal("1000"),
                currency="ARS",
                category_id=1,
                expense_date=date(2026, 10, 5),
                original_message="",
                ai_confidence=Decimal("0"),
                source_type="telegram",
                revision=1,
            )
        )
    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        daily = engine.get_daily_safe_to_spend(
            1, view.cycle_id, today=today
        )
    assert daily is not None
    assert daily.get("ARS") < 0


# ---------------------------------------------------------------------------
# Carry-over transitive
# ---------------------------------------------------------------------------


def test_carry_over_chain_keeps_single_obligation(in_memory_db):
    """Sep LUZ 80 unpaid → Oct pending 80 → Nov pending 80 → Dec pending 80.

    Always one obligation of 80, never 80+80+80.
    """
    today = date(2026, 9, 5)
    _open_cycle(1, payday_day=2, today=today)
    _add_fixed(1, name="LUZ", amount=Decimal("80"), due_day=15)

    # Roll forward one month at a time, never paying LUZ.
    for jump in (date(2026, 10, 3), date(2026, 11, 3), date(2026, 12, 3)):
        with session_scope() as s:
            engine = MoneyEngine(s)
            service = CycleService(s)
            service.ensure_current_cycle(1, today=jump)
        with session_scope() as s:
            engine = MoneyEngine(s)
            service = CycleService(s)
            current = (
                s.query(FinancialCycle)
                .filter(FinancialCycle.status == "open")
                .one()
            )
            summary = engine.get_cycle_summary(1, current.id, today=jump)
        assert summary.pending_fixed_obligations.get("ARS") == Decimal(
            "80.00"
        )
        # The historical entry stays frozen at the September amount.
        carried = summary.carried_over_fixed
        assert any(
            occ.name == "LUZ"
            and occ.expected_amount == Decimal("80.00")
            and occ.original_month_year == "2026-09"
            for occ in carried
        )


def test_carry_over_archived_in_snapshot(in_memory_db):
    """Closing snapshot must carry the unresolved obligation verbatim."""
    today = date(2026, 9, 5)
    _open_cycle(1, payday_day=2, today=today)
    _add_fixed(1, name="LUZ", amount=Decimal("80"), due_day=15)
    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        # Close the Sep cycle with the unpaid obligation.
        # We close the only open cycle (cycle_index=1).
        service.close_cycle(1, 1, force=True)

    with session_scope() as s:
        cycle = s.get(FinancialCycle, 1)
        snapshot = cycle.closing_snapshot
    assert snapshot is not None
    carried = snapshot["carried_over_fixed"]
    assert len(carried) == 1
    assert carried[0]["name"] == "LUZ"
    assert carried[0]["expected_amount"] == "80.00"
    assert carried[0]["original_month_year"] == "2026-09"


def test_template_inactive_skips_new_occurrences_but_carries_old(in_memory_db):
    today = date(2026, 9, 5)
    _open_cycle(1, payday_day=2, today=today)
    bill = _add_fixed(1, name="GAS", amount=Decimal("100"), due_day=10)

    # Rollover Sep → Oct, leave GAS unpaid.
    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        service.rollover_cycle(1, today=date(2026, 10, 3))

    # Now deactivate the template.
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        svc = FixedExpenseService(repo)
        svc.set_active(1, bill.id, False)

    # Move again to Nov. The historical Sep obligation should still be
    # carried, even though no new occurrences are generated.
    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        service.rollover_cycle(1, today=date(2026, 11, 3))

    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        current = (
            s.query(FinancialCycle).filter_by(status="open").one()
        )
        summary = engine.get_cycle_summary(
            1, current.id, today=date(2026, 11, 4)
        )
    assert any(
        occ.name == "GAS" and occ.original_month_year == "2026-09"
        for occ in summary.carried_over_fixed
    )


def test_template_amount_change_does_not_affect_historical_occurrence(in_memory_db):
    """Editing ``expected_amount`` after the cycle is closed does not
    rewrite the carried-over entry — the historical obligation stays frozen.
    """
    today = date(2026, 9, 5)
    _open_cycle(1, payday_day=2, today=today)
    bill = _add_fixed(1, name="X", amount=Decimal("100"), due_day=10)

    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        service.rollover_cycle(1, today=date(2026, 10, 3))

    # Edit the template's expected_amount now.
    with session_scope() as s:
        from app.expenses.service import LinkedFixedPaymentError

        repo = FixedExpenseRepository(s)
        bill_row = repo.get_by_id(1, bill.id)
        assert bill_row is not None
        bill_row.expected_amount = Decimal("9999")
        s.commit()

    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        current = (
            s.query(FinancialCycle).filter_by(status="open").one()
        )
        summary = engine.get_cycle_summary(
            1, current.id, today=date(2026, 10, 4)
        )
    carried = [
        occ for occ in summary.carried_over_fixed if occ.name == "X"
    ]
    assert carried
    assert carried[0].expected_amount == Decimal("100.00")


def test_delete_template_with_outstanding_debt_rejected(in_memory_db):
    today = date(2026, 9, 5)
    _open_cycle(1, payday_day=2, today=today)
    bill = _add_fixed(1, name="Y", amount=Decimal("100"), due_day=10)

    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        service.rollover_cycle(1, today=date(2026, 10, 3))

    with session_scope() as s:
        from app.money.engine import (
            FixedExpenseHasOutstandingDebtError,
        )

        svc = FixedExpenseService(FixedExpenseRepository(s))
        with pytest.raises(FixedExpenseHasOutstandingDebtError):
            svc.delete(1, bill.id)


@pytest.mark.parametrize("resolution", ["paid", "skipped"])
def test_resolved_carry_over_template_cannot_be_deleted(
    in_memory_db, resolution
):
    first = _open_cycle(1, payday_day=2, today=date(2026, 9, 5))
    bill = _add_fixed(1, name="LUZ", amount=Decimal("80"), due_day=15)
    with session_scope() as s:
        current = CycleService(s).rollover_cycle(1, date(2026, 10, 3))
        current_id = current.id
        snapshot_before = json.dumps(
            s.get(FinancialCycle, first.cycle_id).closing_snapshot,
            sort_keys=True,
        )
        assert "2026-09" in snapshot_before

    # Keep the open-cycle pending bucket focused on the September debt.
    # An active template would also produce a new October obligation.
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        svc.set_active(1, bill.id, False)
        if resolution == "paid":
            svc.mark_paid(1, bill.id, "2026-09", paid_on=date(2026, 10, 3))
        else:
            svc.mark_skipped(1, bill.id, "2026-09")

    with session_scope() as s:
        summary = MoneyEngine(s).get_cycle_summary(
            1, current_id, today=date(2026, 10, 3)
        )
        assert summary.pending_fixed_obligations.get("ARS") == 0
        svc = FixedExpenseService(FixedExpenseRepository(s))
        with pytest.raises(FixedExpenseHasHistoricalCarryOverError):
            svc.delete(1, bill.id)

    with session_scope() as s:
        payment = s.query(FixedExpensePayment).filter_by(
            fixed_expense_id=bill.id, month_year="2026-09"
        ).one()
        assert (payment.paid_at is not None) if resolution == "paid" else payment.skipped
        assert s.get(FinancialCycle, first.cycle_id).closing_snapshot is not None
        assert json.dumps(
            s.get(FinancialCycle, first.cycle_id).closing_snapshot,
            sort_keys=True,
        ) == snapshot_before
        summary = MoneyEngine(s).get_cycle_summary(
            1, current_id, today=date(2026, 10, 3)
        )
        assert summary.pending_fixed_obligations.get("ARS") == 0


def test_template_without_carry_over_retains_hard_delete_semantics(in_memory_db):
    bill = _add_fixed(1, name="NO_HISTORY", amount=Decimal("50"), due_day=10)
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        svc.mark_paid(1, bill.id, "2026-10", paid_on=date(2026, 10, 10))
    with session_scope() as s:
        assert FixedExpenseService(FixedExpenseRepository(s)).delete(1, bill.id)
    with session_scope() as s:
        assert FixedExpenseRepository(s).get_by_id(1, bill.id) is None
        assert s.query(FixedExpensePayment).filter_by(fixed_expense_id=bill.id).count() == 0


def test_historical_delete_guard_scans_beyond_recent_48_cycles(in_memory_db):
    bill = _add_fixed(1, name="OLD_LUZ", amount=Decimal("80"), due_day=10)
    with session_scope() as s:
        for index in range(1, 51):
            s.add(FinancialCycle(
                telegram_user_id=1, cycle_index=index,
                start_date=date(2022, 1, 1), end_date=date(2022, 1, 31),
                payday_day=1, status="closed",
                closing_snapshot={"carried_over_fixed": [{
                    "fixed_id": bill.id, "name": "OLD_LUZ",
                    "expected_amount": "80", "currency": "ARS",
                    "due_date": "2022-01-10", "original_month_year": "2022-01",
                }]} if index == 1 else {},
            ))
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        with pytest.raises(FixedExpenseHasHistoricalCarryOverError):
            svc.delete(1, bill.id)


# ---------------------------------------------------------------------------
# Misc invariants
# ---------------------------------------------------------------------------


def test_cycle_summary_snapshot_includes_carried_over_key_format(in_memory_db):
    """``source_key`` for carry-over entries follows the F0 convention."""
    today = date(2026, 9, 5)
    _open_cycle(1, payday_day=2, today=today)
    _add_fixed(1, name="INTERNET", amount=Decimal("50"), due_day=10)

    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        service.rollover_cycle(1, today=date(2026, 10, 3))
        current = (
            s.query(FinancialCycle).filter_by(status="open").one()
        )
        summary = engine.get_cycle_summary(
            1, current.id, today=date(2026, 10, 4)
        )
    assert summary.carried_over_fixed
    occ = summary.carried_over_fixed[0]
    assert occ.source_key() == fixed_payment_source_key(
        occ.fixed_id, occ.original_month_year
    )
    assert occ.original_month_year == "2026-09"
    assert occ.source_key().startswith("fixed-payment:")


def test_status_string_for_open_and_closed(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        summary_open = engine.get_cycle_summary(1, view.cycle_id, today=today)
    assert summary_open.status == "open"
    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        service.close_cycle(1, view.cycle_id, force=True)
    with session_scope() as s:
        engine = MoneyEngine(s)
        service = CycleService(s)
        summary_closed = engine.get_cycle_summary(1, view.cycle_id)
    assert summary_closed.status == "closed"
