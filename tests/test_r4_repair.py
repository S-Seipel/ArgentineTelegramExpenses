"""R4 fixed-payment repair uses the existing detector and payment identity."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database.database import session_scope
from app.expenses.models import Expense
from app.fixed_expenses.models import FixedExpensePayment
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.service import (
    FixedExpenseDraft,
    FixedExpenseService,
    fixed_payment_source_key,
)
from app.main import app
from app.money import MoneyEngine
from app.money.models import FinancialCycle
from tests.test_money_engine_ownership import (
    _make_normal_expense,
    _setup_paid_payment_with_missing_expense,
    _setup_paid_payment_with_soft_deleted_expense,
    _setup_paid_payment_with_wrong_source_key,
    _setup_paid_payment_with_wrong_source_type,
    _setup_paid_payment_without_expense_id,
)
from tests.test_money_state_telegram import _send, runtime  # noqa: F401


@pytest.mark.parametrize("corrupt", [
    _setup_paid_payment_without_expense_id,
    _setup_paid_payment_with_missing_expense,
    _setup_paid_payment_with_soft_deleted_expense,
    _setup_paid_payment_with_wrong_source_type,
    _setup_paid_payment_with_wrong_source_key,
])
def test_detect_repair_detect_is_idempotent(in_memory_db, corrupt):
    corrupt(1)
    with session_scope() as session:
        assert len(MoneyEngine(session).detect_invariant_violations(1)) == 1
        result = FixedExpenseService(FixedExpenseRepository(session)).repair_mirrors(1)
        assert (result.scanned, result.repaired, result.remaining_violations) == (1, 1, 0)
        assert MoneyEngine(session).detect_invariant_violations(1) == []
        again = FixedExpenseService(FixedExpenseRepository(session)).repair_mirrors(1)
        assert again.repaired == 0
        assert MoneyEngine(session).detect_invariant_violations(1) == []
        payment = session.execute(select(FixedExpensePayment)).scalar_one()
        key = fixed_payment_source_key(payment.fixed_expense_id, payment.month_year)
        mirrors = session.execute(select(Expense).where(
            Expense.telegram_user_id == 1, Expense.source_key == key
        )).scalars().all()
        assert len(mirrors) == 1
        assert payment.expense_id == mirrors[0].id


def test_repair_is_user_scoped(in_memory_db):
    _setup_paid_payment_without_expense_id(2)
    with session_scope() as session:
        result = FixedExpenseService(FixedExpenseRepository(session)).repair_mirrors(1)
        assert (result.scanned, result.repaired) == (0, 0)
        assert len(MoneyEngine(session).detect_invariant_violations(2)) == 1


def test_repair_preserves_frozen_carry_over_values(in_memory_db):
    with session_scope() as session:
        service = FixedExpenseService(FixedExpenseRepository(session))
        bill = service.add(1, FixedExpenseDraft(
            name="Original", expected_amount=Decimal("80"), currency="USD"
        ))
        _, payment = service.mark_paid(1, bill.id, "2026-02", paid_on=date(2026, 3, 5))
        payment.expense_id = None
        session.execute(select(Expense).where(
            Expense.source_key == fixed_payment_source_key(bill.id, "2026-02")
        )).scalar_one().source_key = "orphaned-fixed-mirror"
        session.add(FinancialCycle(
            telegram_user_id=1, cycle_index=1, start_date=date(2026, 2, 1),
            end_date=date(2026, 2, 28), payday_day=1, status="closed",
            closing_snapshot={
                "snapshot_version": 1,
                "carried_over_fixed": [{
                    "fixed_id": bill.id, "name": "Original",
                    "expected_amount": "80", "currency": "USD",
                    "due_date": "2026-02-01", "original_month_year": "2026-02",
                }],
            },
        ))
        bill.name = "Edited"
        bill.expected_amount = Decimal("900")
        bill.currency = "ARS"

    with session_scope() as session:
        result = FixedExpenseService(FixedExpenseRepository(session)).repair_mirrors(1)
        assert result.repaired == 1
        payment = session.execute(select(FixedExpensePayment)).scalar_one()
        mirror = session.get(Expense, payment.expense_id)
        assert (mirror.name, mirror.amount, mirror.currency) == (
            "Original", Decimal("80"), "USD"
        )


def test_repair_does_not_change_normal_expense(in_memory_db):
    with session_scope() as session:
        service = FixedExpenseService(FixedExpenseRepository(session))
        bill = service.add(1, FixedExpenseDraft(
            name="System", expected_amount=Decimal("100")
        ))
        payment_id = service.mark_paid(
            1, bill.id, "2026-10", paid_on=date(2026, 10, 15)
        )[1].id
    normal_id = _make_normal_expense(1)
    with session_scope() as session:
        payment = session.get(FixedExpensePayment, payment_id)
        payment.expense_id = normal_id
    with session_scope() as session:
        result = FixedExpenseService(FixedExpenseRepository(session)).repair_mirrors(1)
        assert result.repaired == 1
        normal = session.get(Expense, normal_id)
        assert normal.source_type == "telegram"
        assert normal.source_key is None
        assert MoneyEngine(session).detect_invariant_violations(1) == []


def test_repair_http_result(in_memory_db):
    _setup_paid_payment_without_expense_id(123456)
    response = TestClient(app).post("/api/fixed-expenses/repair-mirrors")
    assert response.status_code == 200
    assert response.json() == {
        "scanned": 1, "repaired": 1, "remaining_violations": 0
    }


def test_repair_telegram_result(runtime, in_memory_db):
    _setup_paid_payment_without_expense_id(42)
    assert "Reparé 1" in _send(runtime, "/repair_fixed_payments")
    assert "consistentes" in _send(runtime, "/repair_fixed_payments")
