from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.database.database import session_scope
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.service import (
    FixedExpenseDraft,
    FixedExpenseService,
    FixedExpenseValidationError,
    current_month_year,
)


def test_current_month_year():
    assert current_month_year(date(2026, 9, 5)) == "2026-09"
    assert current_month_year(date(2026, 12, 31)) == "2026-12"
    assert current_month_year(date(2027, 1, 1)) == "2027-01"


def test_add_fixed_expense(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        obj = service.add(
            user_id=1,
            draft=FixedExpenseDraft(
                name="CASA",
                expected_amount=Decimal("90000"),
                payment_method="TRANSFERENCIA",
                due_day_of_month=10,
            ),
        )
    assert obj.id is not None
    assert obj.name == "CASA"
    assert obj.expected_amount == Decimal("90000")
    assert obj.currency == "ARS"
    assert obj.payment_method == "TRANSFERENCIA"
    assert obj.due_day_of_month == 10
    assert obj.is_active is True


def test_add_invalid_amount(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        try:
            service.add(
                user_id=1,
                draft=FixedExpenseDraft(
                    name="X", expected_amount=Decimal("0"),
                ),
            )
        except FixedExpenseValidationError:
            pass
        else:
            raise AssertionError("expected validation error")


def test_add_invalid_day(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        try:
            service.add(
                user_id=1,
                draft=FixedExpenseDraft(
                    name="X",
                    expected_amount=Decimal("1000"),
                    due_day_of_month=32,
                ),
            )
        except FixedExpenseValidationError:
            pass
        else:
            raise AssertionError("expected validation error")


def test_list_active_orders_by_day(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        service.add(user_id=1, draft=FixedExpenseDraft(
            name="Z", expected_amount=Decimal("100"), due_day_of_month=20))
        service.add(user_id=1, draft=FixedExpenseDraft(
            name="A", expected_amount=Decimal("200"), due_day_of_month=5))
        service.add(user_id=1, draft=FixedExpenseDraft(
            name="M", expected_amount=Decimal("300"), due_day_of_month=15))
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        bills = repo.list_active(1)
    assert [b.name for b in bills] == ["A", "M", "Z"]


def test_set_active(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        obj = service.add(user_id=1, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("1000")))
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service2 = FixedExpenseService(repo)
        paused = service2.set_active(1, obj.id, is_active=False)
    assert paused.is_active is False
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service3 = FixedExpenseService(repo)
        active = service3.list_active(1)
    assert active == []
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        all_bills = repo.list_active(1, include_inactive=True)
    assert len(all_bills) == 1


def test_mark_paid(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        obj = service.add(user_id=1, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000")))
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service2 = FixedExpenseService(repo)
        bill, payment = service2.mark_paid(
            1, obj.id, "2026-09", actual_amount=Decimal("92000"))
    assert payment is not None
    assert payment.paid_at is not None
    assert payment.actual_amount == Decimal("92000")


def test_mark_paid_no_amount_uses_expected(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        obj = service.add(user_id=1, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000")))
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service2 = FixedExpenseService(repo)
        service2.mark_paid(1, obj.id, "2026-09")
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service3 = FixedExpenseService(repo)
        bills = service3.with_status_for_month(1, "2026-09")
    assert bills[0].paid is True
    assert bills[0].actual_amount is None
    assert bills[0].status == "paid_exact"


def test_mark_paid_more_status(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        obj = service.add(user_id=1, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000")))
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service2 = FixedExpenseService(repo)
        service2.mark_paid(1, obj.id, "2026-09",
                           actual_amount=Decimal("95000"))
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service3 = FixedExpenseService(repo)
        bills = service3.with_status_for_month(1, "2026-09")
    assert bills[0].status == "paid_more"
    assert bills[0].diff == Decimal("5000")


def test_mark_paid_less_status(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        obj = service.add(user_id=1, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000")))
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service2 = FixedExpenseService(repo)
        service2.mark_paid(1, obj.id, "2026-09",
                           actual_amount=Decimal("85000"))
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service3 = FixedExpenseService(repo)
        bills = service3.with_status_for_month(1, "2026-09")
    assert bills[0].status == "paid_less"


def test_mark_skipped(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        obj = service.add(user_id=1, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000")))
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service2 = FixedExpenseService(repo)
        service2.mark_skipped(1, obj.id, "2026-09")
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service3 = FixedExpenseService(repo)
        bills = service3.with_status_for_month(1, "2026-09")
    assert bills[0].skipped is True
    assert bills[0].status == "skipped"


def test_unmark_payment(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        obj = service.add(user_id=1, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000")))
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service2 = FixedExpenseService(repo)
        service2.mark_paid(1, obj.id, "2026-09")
        ok = service2.unmark(1, obj.id, "2026-09")
    assert ok is True
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service3 = FixedExpenseService(repo)
        bills = service3.with_status_for_month(1, "2026-09")
    assert bills[0].paid is False


def test_find_by_name(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        service.add(user_id=1, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000")))
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service2 = FixedExpenseService(repo)
        bill = service2.find_by_name(1, "CASA")
        assert bill is not None
        assert bill.name == "CASA"
        # Case-insensitive not implemented yet, but whitespace tolerated
        assert service2.find_by_name(1, "casa") is None


def test_month_summary_with_income(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        casa = service.add(user_id=1, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000")))
        gym = service.add(user_id=1, draft=FixedExpenseDraft(
            name="GYM", expected_amount=Decimal("60000")))
        service.mark_paid(1, casa.id, "2026-09")
        service.mark_paid(1, gym.id, "2026-09", actual_amount=Decimal("70000"))
        service.set_budget(1, "2026-09", income=Decimal("1100000"),
                           extra=Decimal("75000"))
    with session_scope() as s:
        service2 = FixedExpenseService(FixedExpenseRepository(s))
        summary = service2.month_summary(1, "2026-09")
    # 90000 + 70000 = 160000 paid; expected = 150000
    assert summary.total_fixed_paid == Decimal("160000")
    assert summary.total_fixed_expected == Decimal("150000")
    # income (1100000) + extra (75000) - paid (160000) = 1015000
    assert summary.liberado == Decimal("1015000")


def test_month_summary_skipped_excluded(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        casa = service.add(user_id=1, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000")))
        gym = service.add(user_id=1, draft=FixedExpenseDraft(
            name="GYM", expected_amount=Decimal("60000")))
        service.mark_paid(1, casa.id, "2026-09")
        service.mark_skipped(1, gym.id, "2026-09")
        service.set_budget(1, "2026-09", income=Decimal("1000000"))
    with session_scope() as s:
        service2 = FixedExpenseService(FixedExpenseRepository(s))
        summary = service2.month_summary(1, "2026-09")
    # Skipped excluded from paid; only CASA counts (90000)
    assert summary.total_fixed_paid == Decimal("90000")
    # income 1000000 - paid 90000 = 910000
    assert summary.liberado == Decimal("910000")


def test_budget_upsert(in_memory_db):
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        b1 = service.set_budget(1, "2026-09", income=Decimal("1000000"))
    assert b1.income == Decimal("1000000")
    assert b1.extra == Decimal("0")
    with session_scope() as s:
        service2 = FixedExpenseService(FixedExpenseRepository(s))
        b2 = service2.set_budget(1, "2026-09", extra=Decimal("50000"))
    assert b2.income == Decimal("1000000")  # preserved
    assert b2.extra == Decimal("50000")
