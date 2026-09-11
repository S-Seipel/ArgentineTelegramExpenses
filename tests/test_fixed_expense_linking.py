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


def test_unmark_soft_deletes_linked_expense(in_memory_db):
    """Unpay keeps the payment row (cleared) and soft-deletes the mirror.

    The historical link (``expense_id``) is preserved so a re-``mark_paid``
    can restore the same row.
    """
    from app.expenses.models import Expense
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = _add_one(service)
        today = date.today()
        my = today.strftime("%Y-%m")
        service.mark_paid(123, bill.id, my)
        service.unmark(123, bill.id, my)

    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        # Payment row is preserved with paid_at cleared.
        payment = repo.get_payment(bill.id, my)
        assert payment is not None
        assert payment.paid_at is None
        assert payment.actual_amount is None
        # Expense is soft-deleted, not removed.
        assert ExpenseRepository(s).search_by_name(123, "CASA") == []
        # The underlying row is still there with deleted_at populated.
        row = s.query(Expense).filter(Expense.source_key.like("fixed-payment:%")).one()
        assert row.deleted_at is not None
        assert row.revision >= 2


def test_skip_does_not_create_expense(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = _add_one(service)
        today = date.today()
        my = today.strftime("%Y-%m")
        service.mark_skipped(123, bill.id, my)

    with session_scope() as s:
        assert ExpenseRepository(s).search_by_name(123, "CASA") == []


def test_skip_after_pay_soft_deletes_expense(in_memory_db):
    """Mark paid → expense exists. Then mark skipped → expense soft-deleted."""
    from app.expenses.models import Expense
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = _add_one(service)
        today = date.today()
        my = today.strftime("%Y-%m")
        service.mark_paid(123, bill.id, my)
        service.mark_skipped(123, bill.id, my)

    with session_scope() as s:
        assert ExpenseRepository(s).search_by_name(123, "CASA") == []
        # The row is still around for audit purposes.
        assert s.query(Expense).count() == 1
        row = s.query(Expense).one()
        assert row.deleted_at is not None


def test_paid_then_skipped_then_paid_restores_same_expense(in_memory_db):
    """Pay → skip → pay must restore the same mirror, not duplicate it."""
    from app.expenses.models import Expense
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = _add_one(service)
        today = date.today()
        my = today.strftime("%Y-%m")
        service.mark_paid(123, bill.id, my)
        first_id = (
            s.query(Expense).filter(Expense.source_key.like("fixed-payment:%")).one().id
        )
        service.mark_skipped(123, bill.id, my)
        service.mark_paid(123, bill.id, my, actual_amount=Decimal("88000"))

    with session_scope() as s:
        expenses = s.query(Expense).all()
        # Same physical row, restored (deleted_at cleared) and updated amount.
        assert len(expenses) == 1
        assert expenses[0].id == first_id
        assert expenses[0].amount == Decimal("88000.00")
        assert expenses[0].deleted_at is None
        assert expenses[0].revision >= 3
        # Visible reads see it again.
        live = ExpenseRepository(s).search_by_name(123, "CASA")
        assert len(live) == 1


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
