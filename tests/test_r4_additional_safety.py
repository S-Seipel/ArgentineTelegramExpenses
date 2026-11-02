"""History must fail safely and ambiguous repairs must roll back atomically."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database.database import session_scope
from app.expenses.models import Expense
from app.fixed_expenses.models import FixedExpensePayment
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.service import (
    FixedExpenseDraft,
    FixedExpenseService,
    MirrorRepairError,
)
from app.money import CycleService, MoneyEngine, MoneyStateService, SnapshotVersionMismatchError
from app.money.models import FinancialCycle
from app.money.snapshot import ensure_snapshot_version
from tests.test_money_engine_ownership import (
    _setup_paid_payment_without_expense_id,
)


@pytest.mark.parametrize("version", [True, False, 1.5, "1", "1.0", [], {}])
def test_history_version_is_an_exact_integer(version):
    with pytest.raises(SnapshotVersionMismatchError):
        ensure_snapshot_version({"snapshot_version": version})


def test_ambiguous_repair_rolls_back_other_repairs(in_memory_db):
    first_payment = _setup_paid_payment_without_expense_id(1)
    with session_scope() as session:
        service = FixedExpenseService(FixedExpenseRepository(session))
        other_bill = service.add(2, FixedExpenseDraft(
            name="Otro usuario", expected_amount=Decimal("80")
        ))
        _, other_payment = service.mark_paid(
            2, other_bill.id, "2026-10", paid_on=date(2026, 10, 15)
        )
        bill = service.add(1, FixedExpenseDraft(
            name="Ambiguo", expected_amount=Decimal("100")
        ))
        _, payment = service.mark_paid(
            1, bill.id, "2026-10", paid_on=date(2026, 10, 15)
        )
        payment.expense_id = other_payment.expense_id
        session.flush()
        before = len(session.execute(select(Expense)).scalars().all())

    with pytest.raises(MirrorRepairError):
        with session_scope() as session:
            FixedExpenseService(FixedExpenseRepository(session)).repair_mirrors(1)

    with session_scope() as session:
        assert session.get(FixedExpensePayment, first_payment).expense_id is None
        assert len(session.execute(select(Expense)).scalars().all()) == before
        assert len(MoneyEngine(session).detect_invariant_violations(1)) == 2
        assert MoneyEngine(session).detect_invariant_violations(2) == []


def test_repair_preserves_unflushed_changes_in_its_transaction(in_memory_db):
    with session_scope() as session:
        service = FixedExpenseService(FixedExpenseRepository(session))
        bill = service.add(1, FixedExpenseDraft(
            name="Mismo trámite", expected_amount=Decimal("100")
        ))
        _, payment = service.mark_paid(
            1, bill.id, "2026-10", paid_on=date(2026, 10, 15)
        )
        session.flush()
        payment.expense_id = None
        result = service.repair_mirrors(1)
        assert result.repaired == 1
        assert payment.expense_id is not None
        assert MoneyEngine(session).detect_invariant_violations(1) == []


def test_read_open_cycle_rejects_unknown_previous_snapshot(in_memory_db):
    with session_scope() as session:
        service = CycleService(session)
        first = service.open_initial_cycle(1, date(2026, 10, 15)).cycle_id
        service.close_cycle(1, first)
        next_cycle = service.ensure_current_cycle(1, date(2026, 11, 2))
        next_id = next_cycle.id
        row = session.get(FinancialCycle, first)
        row.closing_snapshot = {**row.closing_snapshot, "snapshot_version": 999}

    with session_scope() as session:
        with pytest.raises(SnapshotVersionMismatchError):
            MoneyEngine(session).get_cycle_summary(1, next_id, today=date(2026, 11, 2))


def test_ensure_after_early_close_does_not_create_future_history(in_memory_db):
    today = date(2026, 10, 15)
    with session_scope() as session:
        service = CycleService(session)
        cycle_id = service.open_initial_cycle(1, today).cycle_id
        service.close_cycle(1, cycle_id)
        frozen = session.get(FinancialCycle, cycle_id).closing_snapshot

    with pytest.raises(LookupError, match="cerrado"):
        with session_scope() as session:
            CycleService(session).ensure_current_cycle(1, today)

    with session_scope() as session:
        cycles = session.execute(select(FinancialCycle)).scalars().all()
        assert len(cycles) == 1
        assert cycles[0].status == "closed"
        assert cycles[0].closing_snapshot == frozen
        next_cycle = CycleService(session).ensure_current_cycle(1, date(2026, 11, 1))
        assert next_cycle.start_date == date(2026, 11, 1)
        assert next_cycle.status == "open"


def test_ensure_never_closes_a_future_open_cycle(in_memory_db):
    with session_scope() as session:
        cycle_id = CycleService(session).open_initial_cycle(1, date(2026, 11, 5)).cycle_id

    with pytest.raises(LookupError):
        with session_scope() as session:
            CycleService(session).ensure_current_cycle(1, date(2026, 10, 15))

    with session_scope() as session:
        cycles = session.execute(select(FinancialCycle)).scalars().all()
        assert len(cycles) == 1
        assert cycles[0].id == cycle_id
        assert cycles[0].status == "open"
        assert cycles[0].closing_snapshot is None


def test_stored_closing_snapshot_has_no_daily_allowance(in_memory_db, monkeypatch):
    today = date(2026, 10, 15)
    monkeypatch.setattr("app.money.engine._business_today", lambda: today)
    with session_scope() as session:
        service = CycleService(session)
        cycle_id = service.open_initial_cycle(1, today).cycle_id
        MoneyStateService(session).set_income(1, cycle_id, "ARS", Decimal("1000"))
        active = MoneyEngine(session).get_cycle_summary(1, cycle_id, today=today)
        assert active.daily_safe_to_spend.get("ARS") > 0
        service.close_cycle(1, cycle_id)
        snapshot = session.get(FinancialCycle, cycle_id).closing_snapshot
        assert snapshot["money"]["daily_safe_to_spend"] is None
        assert Decimal(snapshot["money"]["available_to_spend"]["ARS"]) == 1000
