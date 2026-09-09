from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.database.database import session_scope
from app.recurring.repository import RecurringRepository
from app.recurring.schemas import RecurringCreate
from app.recurring.service import (
    advance_due_date,
    first_occurrence,
    last_day_of_month,
    postpone_due_date,
)


def _create(**overrides) -> int:
    """Insert a recurring template and return its id."""
    payload = RecurringCreate(
        telegram_user_id=overrides.get("telegram_user_id", 1),
        name=overrides.get("name", "Netflix"),
        amount=Decimal(str(overrides.get("amount", "30000"))),
        currency=overrides.get("currency", "ARS"),
        category_id=overrides["category_id"],
        frequency=overrides.get("frequency", "monthly"),
        day_of_month=overrides.get("day_of_month", 15),
        month_of_year=overrides.get("month_of_year"),
        next_due_date=overrides["next_due_date"],
    )
    with session_scope() as s:
        repo = RecurringRepository(s)
        obj = repo.create(payload)
        return obj.id


def _get_cat_id(name: str) -> int:
    with session_scope() as s:
        from app.categories.models import Category
        return s.query(Category).filter(Category.name == name).one().id


def test_last_day_of_month_known_values():
    assert last_day_of_month(2026, 1) == 31
    assert last_day_of_month(2026, 2) == 28
    assert last_day_of_month(2024, 2) == 29
    assert last_day_of_month(2026, 4) == 30


def test_advance_due_date_monthly_normal():
    today = date(2026, 1, 15)
    next_due = advance_due_date(today, "monthly", day_of_month=15, month_of_year=None)
    assert next_due == date(2026, 2, 15)


def test_advance_due_date_monthly_year_boundary():
    today = date(2026, 12, 10)
    next_due = advance_due_date(today, "monthly", day_of_month=10, month_of_year=None)
    assert next_due == date(2027, 1, 10)


def test_advance_due_date_monthly_clamps_to_month_end():
    today = date(2026, 1, 31)
    next_due = advance_due_date(today, "monthly", day_of_month=31, month_of_year=None)
    assert next_due == date(2026, 2, 28)


def test_advance_due_date_yearly():
    today = date(2026, 3, 15)
    next_due = advance_due_date(
        today, "yearly", day_of_month=15, month_of_year=3
    )
    assert next_due == date(2027, 3, 15)


def test_postpone_due_date():
    today = date(2026, 8, 21)
    assert postpone_due_date(today) == date(2026, 8, 22)


def test_first_occurrence_monthly_future_day():
    today = date(2026, 8, 10)
    assert (
        first_occurrence(today, "monthly", day_of_month=15, month_of_year=None)
        == date(2026, 8, 15)
    )


def test_first_occurrence_monthly_past_day():
    today = date(2026, 8, 20)
    assert (
        first_occurrence(today, "monthly", day_of_month=15, month_of_year=None)
        == date(2026, 9, 15)
    )


def test_first_occurrence_yearly_before():
    today = date(2026, 2, 1)
    assert (
        first_occurrence(today, "yearly", day_of_month=15, month_of_year=3)
        == date(2026, 3, 15)
    )


def test_first_occurrence_yearly_after():
    today = date(2026, 4, 1)
    assert (
        first_occurrence(today, "yearly", day_of_month=15, month_of_year=3)
        == date(2027, 3, 15)
    )


def test_first_occurrence_clamps_day_31_to_feb_end():
    today = date(2026, 1, 1)
    assert (
        first_occurrence(today, "monthly", day_of_month=31, month_of_year=None)
        == date(2026, 1, 31)
    )
    today = date(2026, 2, 1)
    assert (
        first_occurrence(today, "monthly", day_of_month=31, month_of_year=None)
        == date(2026, 2, 28)
    )


def test_create_and_list(in_memory_db):
    cat_id = _get_cat_id("Suscripciones")
    _create(category_id=cat_id, name="Netflix", next_due_date=date(2026, 8, 15))
    _create(category_id=cat_id, name="Spotify", next_due_date=date(2026, 8, 5))

    with session_scope() as s:
        repo = RecurringRepository(s)
        items = repo.list_for_user(1)
    assert len(items) == 2
    assert items[0].name == "Spotify"
    assert items[1].name == "Netflix"


def test_list_excludes_inactive(in_memory_db):
    cat_id = _get_cat_id("Suscripciones")
    rid = _create(category_id=cat_id, next_due_date=date(2026, 8, 15))
    with session_scope() as s:
        repo = RecurringRepository(s)
        obj = repo.get_by_id_for_update(1, rid)
        repo.set_active(obj, is_active=False)
    with session_scope() as s:
        repo = RecurringRepository(s)
        items = repo.list_for_user(1)
    assert items == []
    with session_scope() as s:
        repo = RecurringRepository(s)
        all_items = repo.list_for_user(1, include_inactive=True)
    assert len(all_items) == 1


def test_get_by_id_scoped_to_user(in_memory_db):
    cat_id = _get_cat_id("Suscripciones")
    _create(category_id=cat_id, telegram_user_id=2, next_due_date=date(2026, 8, 15))
    with session_scope() as s:
        repo = RecurringRepository(s)
        assert repo.get_by_id(1, 1) is None
        item = repo.get_by_id(2, 1)
    assert item is not None
    assert item.name == "Netflix"


def test_due_on_finds_overdue(in_memory_db):
    cat_id = _get_cat_id("Suscripciones")
    _create(category_id=cat_id, next_due_date=date(2026, 7, 15))
    _create(category_id=cat_id, name="Spotify", next_due_date=date(2026, 9, 1))
    with session_scope() as s:
        repo = RecurringRepository(s)
        due = repo.due_on(1, date(2026, 8, 21))
    assert len(due) == 1
    assert due[0].name == "Netflix"


def test_due_on_excludes_inactive(in_memory_db):
    cat_id = _get_cat_id("Suscripciones")
    rid = _create(category_id=cat_id, next_due_date=date(2026, 7, 15))
    with session_scope() as s:
        repo = RecurringRepository(s)
        obj = repo.get_by_id_for_update(1, rid)
        repo.set_active(obj, is_active=False)
    with session_scope() as s:
        repo = RecurringRepository(s)
        assert repo.due_on(1, date(2026, 8, 21)) == []


def test_update_next_due(in_memory_db):
    cat_id = _get_cat_id("Suscripciones")
    rid = _create(category_id=cat_id, next_due_date=date(2026, 8, 15))
    with session_scope() as s:
        repo = RecurringRepository(s)
        obj = repo.get_by_id_for_update(1, rid)
        updated = repo.update_next_due(obj, date(2026, 9, 15), notified=True)
    assert updated.next_due_date == date(2026, 9, 15)
    assert updated.last_notified_at is not None