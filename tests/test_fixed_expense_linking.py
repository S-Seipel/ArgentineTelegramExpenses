from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.service import FixedExpenseDraft, FixedExpenseService


def _add_one(service, user_id=123):
    return service.add(
        user_id, FixedExpenseDraft(
            name="CASA",
            expected_amount=Decimal("90000"),
            payment_method="TRANSFERENCIA",
            due_day_of_month=10,
        )
    )


def test_mark_paid_creates_linked_expense(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = _add_one(service)
        bid = bill.id
        today = date.today()
        my = today.strftime("%Y-%m")
        service.mark_paid(123, bid, my, actual_amount=Decimal("92000"))

    with session_scope() as s:
        expense_repo = ExpenseRepository(s)
        expenses = expense_repo.search_by_name(123, "CASA")
        assert len(expenses) == 1
        e = expenses[0]
        assert e.amount == Decimal("92000.00")
        assert e.currency == "ARS"
        assert e.expense_date == today

        payment = FixedExpenseRepository(s).get_payment(bid, my)
        assert payment.expense_id == e.id


def test_mark_paid_uses_expected_amount_when_no_actual(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = _add_one(service)
        today = date.today()
        my = today.strftime("%Y-%m")
        service.mark_paid(123, bill.id, my)

    with session_scope() as s:
        expenses = ExpenseRepository(s).search_by_name(123, "CASA")
        assert len(expenses) == 1
        assert expenses[0].amount == Decimal("90000.00")


def test_mark_paid_twice_updates_same_expense(in_memory_db):
    """Calling /pague twice must update the linked expense, not duplicate it."""
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = _add_one(service)
        today = date.today()
        my = today.strftime("%Y-%m")
        service.mark_paid(123, bill.id, my, actual_amount=Decimal("90000"))
        service.mark_paid(123, bill.id, my, actual_amount=Decimal("95000"))

    with session_scope() as s:
        expenses = ExpenseRepository(s).search_by_name(123, "CASA")
        # Only ONE expense, with the LATEST amount.
        assert len(expenses) == 1
        assert expenses[0].amount == Decimal("95000.00")


def test_unmark_removes_linked_expense(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = _add_one(service)
        today = date.today()
        my = today.strftime("%Y-%m")
        service.mark_paid(123, bill.id, my)
        service.unmark(123, bill.id, my)

    with session_scope() as s:
        # Payment gone
        assert FixedExpenseRepository(s).get_payment(bill.id, my) is None
        # Expense gone
        assert ExpenseRepository(s).search_by_name(123, "CASA") == []


def test_skip_does_not_create_expense(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = _add_one(service)
        today = date.today()
        my = today.strftime("%Y-%m")
        service.mark_skipped(123, bill.id, my)

    with session_scope() as s:
        assert ExpenseRepository(s).search_by_name(123, "CASA") == []


def test_skip_after_pay_removes_expense(in_memory_db):
    """Mark paid → expense exists. Then mark skipped → expense removed."""
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = _add_one(service)
        today = date.today()
        my = today.strftime("%Y-%m")
        service.mark_paid(123, bill.id, my)
        service.mark_skipped(123, bill.id, my)

    with session_scope() as s:
        assert ExpenseRepository(s).search_by_name(123, "CASA") == []


def test_paid_then_skipped_then_paid_recreates_expense(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = _add_one(service)
        today = date.today()
        my = today.strftime("%Y-%m")
        service.mark_paid(123, bill.id, my)
        service.mark_skipped(123, bill.id, my)
        service.mark_paid(123, bill.id, my, actual_amount=Decimal("88000"))

    with session_scope() as s:
        expenses = ExpenseRepository(s).search_by_name(123, "CASA")
        assert len(expenses) == 1
        assert expenses[0].amount == Decimal("88000.00")


def test_dashboard_total_includes_paid_fixed_expenses(in_memory_db):
    """The whole point of this feature: dashboard 'Total' counts paid bills."""
    from app.utils.dates import today_in_tz
    today = today_in_tz("UTC")
    my = today.strftime("%Y-%m")
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        for name, amount, day in [
            ("CASA", Decimal("90000"), 5),
            ("UTN", Decimal("165000"), 10),
            ("GYM", Decimal("60000"), 15),
        ]:
            service.add(123, FixedExpenseDraft(
                name=name, expected_amount=amount,
                payment_method="TRANSFERENCIA", due_day_of_month=day))
        # Pay CASA and UTN
        for bill in service.list_active(123):
            service.mark_paid(123, bill.id, my)
    with session_scope() as s:
        expense_repo = ExpenseRepository(s)
        all_expenses = expense_repo.list_in_range(
            123, start=today.replace(day=1), end=today
        )
        total = sum(e.amount for e in all_expenses)
        # 90000 + 165000 + 60000 = 315000
        assert total == Decimal("315000.00")
