"""Real PostgreSQL lock serialization for R4 fixed mirror repair."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier

from sqlalchemy import select

from app.database.database import session_scope
from app.expenses.models import Expense
from app.fixed_expenses.models import FixedExpensePayment
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.service import (
    FixedExpenseDraft, FixedExpenseService, fixed_payment_source_key,
)
from app.money import MoneyEngine
from tests_postgres.test_fixed_payment_concurrency import fresh_db  # noqa: F401


def _assert_single_mirror(user, fixed_id):
    key = fixed_payment_source_key(fixed_id, "2026-10")
    with session_scope() as session:
        payments = session.execute(select(FixedExpensePayment).where(
            FixedExpensePayment.fixed_expense_id == fixed_id,
            FixedExpensePayment.month_year == "2026-10",
        )).scalars().all()
        mirrors = session.execute(select(Expense).where(
            Expense.telegram_user_id == user, Expense.source_key == key,
        )).scalars().all()
        assert len(payments) == len(mirrors) == 1
        assert payments[0].expense_id == mirrors[0].id
        assert mirrors[0].source_type == "fixed_payment"
        assert MoneyEngine(session).detect_invariant_violations(user) == []


def test_mark_paid_vs_repair_mirrors_serializes(fresh_db):
    user = 42
    with session_scope() as session:
        service = FixedExpenseService(FixedExpenseRepository(session))
        fixed_id = service.add(user, FixedExpenseDraft(
            name="Concurrently paid", expected_amount=Decimal("100")
        )).id
        payment = service.mark_paid(user, fixed_id, "2026-10", paid_on=date(2026, 10, 15))[1]
        payment.expense_id = None
    barrier = Barrier(2)

    def mark():
        barrier.wait(timeout=10)
        with session_scope() as session:
            FixedExpenseService(FixedExpenseRepository(session)).mark_paid(
                user, fixed_id, "2026-10", paid_on=date(2026, 10, 15)
            )

    def repair():
        barrier.wait(timeout=10)
        with session_scope() as session:
            FixedExpenseService(FixedExpenseRepository(session)).repair_mirrors(user)

    with ThreadPoolExecutor(max_workers=2) as pool:
        mark_future = pool.submit(mark)
        repair_future = pool.submit(repair)
        mark_future.result(timeout=30)
        repair_future.result(timeout=30)
    _assert_single_mirror(user, fixed_id)


def test_repair_vs_repair_serializes(fresh_db):
    user = 42
    with session_scope() as session:
        service = FixedExpenseService(FixedExpenseRepository(session))
        fixed_id = service.add(user, FixedExpenseDraft(
            name="Repair race", expected_amount=Decimal("100")
        )).id
        payment = service.mark_paid(user, fixed_id, "2026-10", paid_on=date(2026, 10, 15))[1]
        payment.expense_id = None
    barrier = Barrier(2)

    def repair():
        barrier.wait(timeout=10)
        with session_scope() as session:
            return FixedExpenseService(FixedExpenseRepository(session)).repair_mirrors(user)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(repair)
        b = pool.submit(repair)
        results = (a.result(timeout=30), b.result(timeout=30))
    assert sorted(result.repaired for result in results) == [0, 1]
    _assert_single_mirror(user, fixed_id)
