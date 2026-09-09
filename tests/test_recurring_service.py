from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.recurring.repository import RecurringRepository
from app.recurring.service import (
    RecurringDraft,
    RecurringService,
    RecurringValidationError,
)


def _get_cat_id(name: str) -> int:
    with session_scope() as s:
        from app.categories.models import Category
        return s.query(Category).filter(Category.name == name).one().id


def _draft(**overrides) -> RecurringDraft:
    base = dict(
        name="Netflix",
        amount=Decimal("30000"),
        currency="ARS",
        category="Suscripciones",
        frequency="monthly",
        day_of_month=15,
        month_of_year=None,
        confidence=1.0,
    )
    base.update(overrides)
    return RecurringDraft(**base)


def test_create_monthly_advances_to_next_month(in_memory_db):
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        template = service.create_from_draft(
            user_id=1, draft=_draft(), today=date(2026, 1, 10)
        )
    assert template.name == "Netflix"
    assert template.day_of_month == 15
    assert template.next_due_date == date(2026, 1, 15)


def test_create_yearly_requires_month(in_memory_db):
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        with pytest.raises(RecurringValidationError):
            service.create_from_draft(
                user_id=1,
                draft=_draft(frequency="yearly", day_of_month=15, month_of_year=None),
                today=date(2026, 1, 10),
            )


def test_create_yearly_first_occurrence(in_memory_db):
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        template = service.create_from_draft(
            user_id=1,
            draft=_draft(
                frequency="yearly",
                day_of_month=15,
                month_of_year=3,
                name="Dominio",
            ),
            today=date(2026, 1, 10),
        )
    assert template.month_of_year == 3
    assert template.next_due_date == date(2026, 3, 15)


def test_invalid_amount_rejected(in_memory_db):
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        with pytest.raises(RecurringValidationError):
            service.create_from_draft(
                user_id=1, draft=_draft(amount=None), today=date(2026, 1, 10)
            )
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        with pytest.raises(RecurringValidationError):
            service.create_from_draft(
                user_id=1, draft=_draft(amount=Decimal("0")), today=date(2026, 1, 10)
            )


def test_missing_name_rejected(in_memory_db):
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        with pytest.raises(RecurringValidationError):
            service.create_from_draft(
                user_id=1, draft=_draft(name=""), today=date(2026, 1, 10)
            )


def test_unknown_frequency_rejected(in_memory_db):
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        with pytest.raises(RecurringValidationError):
            service.create_from_draft(
                user_id=1,
                draft=_draft(frequency="weekly"),
                today=date(2026, 1, 10),
            )


def test_unknown_currency_rejected(in_memory_db):
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        with pytest.raises(RecurringValidationError):
            service.create_from_draft(
                user_id=1, draft=_draft(currency="XXX"), today=date(2026, 1, 10)
            )


def test_register_occurrence_creates_expense_and_advances(in_memory_db):
    today = date(2026, 8, 15)
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        template = service.create_from_draft(
            user_id=1, draft=_draft(), today=date(2026, 7, 10)
        )
        rid = template.id
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        template_after, saved_expense = service.register_occurrence(
            user_id=1, recurring_id=rid, today=today
        )
    assert saved_expense is not None
    assert saved_expense.amount == Decimal("30000.00")
    assert saved_expense.expense_date == today
    assert template_after.next_due_date == date(2026, 9, 15)

    with session_scope() as s:
        repo = ExpenseRepository(s)
        expenses = repo.list_in_range(1, start=today, end=today)
    assert len(expenses) == 1
    assert "[recurrente" in expenses[0].original_message


def test_skip_occurrence_advances_without_registering(in_memory_db):
    today = date(2026, 8, 15)
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        template = service.create_from_draft(
            user_id=1, draft=_draft(), today=date(2026, 7, 10)
        )
        rid = template.id
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        template_after = service.skip_occurrence(
            user_id=1, recurring_id=rid, today=today
        )
    assert template_after.next_due_date == date(2026, 9, 15)

    with session_scope() as s:
        repo = ExpenseRepository(s)
        expenses = repo.list_in_range(1, start=today, end=today)
    assert expenses == []


def test_postpone_occurrence_pushes_one_day(in_memory_db):
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        template = service.create_from_draft(
            user_id=1, draft=_draft(), today=date(2026, 7, 10)
        )
        rid = template.id
        original = template.next_due_date  # 2026-07-15
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        template_after = service.postpone_occurrence(user_id=1, recurring_id=rid)
    assert template_after.next_due_date == date(2026, 7, 16)
    assert template_after.next_due_date > original


def test_register_occurrence_on_inactive_returns_none(in_memory_db):
    today = date(2026, 8, 15)
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        template = service.create_from_draft(
            user_id=1, draft=_draft(), today=date(2026, 7, 10)
        )
        rid = template.id
        service.set_active(1, rid, is_active=False)
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        template_after, saved_expense = service.register_occurrence(
            user_id=1, recurring_id=rid, today=today
        )
    assert template_after is None
    assert saved_expense is None


def test_set_active_toggles(in_memory_db):
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        template = service.create_from_draft(
            user_id=1, draft=_draft(), today=date(2026, 7, 10)
        )
        rid = template.id
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        paused = service.set_active(1, rid, is_active=False)
    assert paused.is_active is False
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        resumed = service.set_active(1, rid, is_active=True)
    assert resumed.is_active is True