from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.expenses.service import ExpenseDraft, ExpenseService


def _draft(**overrides) -> ExpenseDraft:
    base = dict(
        name="Café",
        amount=Decimal("10000"),
        currency="ARS",
        category="Café",
        expense_date=date(2026, 1, 19),
        confidence=Decimal("0.9"),
    )
    base.update(overrides)
    return ExpenseDraft(**base)


def _seed(user_id: int = 1) -> dict:
    """Seed three expenses and return a map of name -> id."""
    with session_scope() as s:
        repo = ExpenseRepository(s)
        service = ExpenseService(repo)
        outcome = service.register_many(
            user_id=user_id,
            drafts=[
                _draft(name="Café", amount=Decimal("5000")),
                _draft(name="Uber", amount=Decimal("12000")),
                _draft(name="Cine", amount=Decimal("8000")),
            ],
            original_message="seed",
        )
    return {e.name: e.id for e in outcome.saved}


def test_get_by_id_returns_correct_expense(in_memory_db):
    ids = _seed()
    with session_scope() as s:
        repo = ExpenseRepository(s)
        row = repo.get_by_id(1, ids["Uber"])
    assert row is not None
    assert row.name == "Uber"
    assert row.amount == Decimal("12000.00")


def test_get_by_id_returns_none_for_missing(in_memory_db):
    with session_scope() as s:
        repo = ExpenseRepository(s)
        assert repo.get_by_id(1, 99999) is None


def test_get_by_id_is_scoped_per_user(in_memory_db):
    _seed(user_id=1)
    other_ids = _seed(user_id=2)
    with session_scope() as s:
        repo = ExpenseRepository(s)
        assert repo.get_by_id(1, other_ids["Café"]) is None


def test_get_latest_returns_newest(in_memory_db):
    _seed()
    with session_scope() as s:
        repo = ExpenseRepository(s)
        latest = repo.get_latest(1)
    assert latest is not None
    assert latest.name == "Cine"


def test_get_latest_empty(in_memory_db):
    with session_scope() as s:
        repo = ExpenseRepository(s)
        assert repo.get_latest(1) is None


def test_delete_removes_expense(in_memory_db):
    ids = _seed()
    with session_scope() as s:
        repo = ExpenseRepository(s)
        deleted = repo.delete(1, ids["Uber"])
    assert deleted is not None
    assert deleted.name == "Uber"
    with session_scope() as s:
        repo = ExpenseRepository(s)
        assert repo.get_by_id(1, ids["Uber"]) is None


def test_delete_returns_none_for_missing(in_memory_db):
    with session_scope() as s:
        repo = ExpenseRepository(s)
        assert repo.delete(1, 99999) is None


def test_delete_is_scoped_per_user(in_memory_db):
    ids = _seed(user_id=1)
    with session_scope() as s:
        repo = ExpenseRepository(s)
        assert repo.delete(2, ids["Café"]) is None
    with session_scope() as s:
        repo = ExpenseRepository(s)
        assert repo.get_by_id(1, ids["Café"]) is not None


def test_update_amount(in_memory_db):
    ids = _seed()
    with session_scope() as s:
        repo = ExpenseRepository(s)
        updated = repo.update_amount(1, ids["Café"], Decimal("7500"))
    assert updated is not None
    assert updated.amount == Decimal("7500.00")
    with session_scope() as s:
        repo = ExpenseRepository(s)
        assert repo.get_by_id(1, ids["Café"]).amount == Decimal("7500.00")


def test_update_amount_rejects_zero(in_memory_db):
    ids = _seed()
    with session_scope() as s:
        repo = ExpenseRepository(s)
        try:
            repo.update_amount(1, ids["Café"], Decimal("0"))
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for zero amount")


def test_update_name(in_memory_db):
    ids = _seed()
    with session_scope() as s:
        repo = ExpenseRepository(s)
        updated = repo.update_name(1, ids["Café"], "Café con leche")
    assert updated is not None
    assert updated.name == "Café con leche"


def test_update_name_rejects_empty(in_memory_db):
    ids = _seed()
    with session_scope() as s:
        repo = ExpenseRepository(s)
        try:
            repo.update_name(1, ids["Café"], "   ")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for empty name")


def test_list_in_range_filters_by_date(in_memory_db):
    with session_scope() as s:
        repo = ExpenseRepository(s)
        service = ExpenseService(repo)
        service.register_many(
            user_id=1,
            drafts=[
                _draft(name="A", amount=Decimal("100"), expense_date=date(2026, 1, 5)),
                _draft(name="B", amount=Decimal("200"), expense_date=date(2026, 2, 5)),
                _draft(name="C", amount=Decimal("300"), expense_date=date(2026, 3, 5)),
            ],
            original_message="seed",
        )
    with session_scope() as s:
        repo = ExpenseRepository(s)
        rows = repo.list_in_range(1, start=date(2026, 2, 1), end=date(2026, 2, 28))
    assert {r.name for r in rows} == {"B"}


def test_list_in_range_inclusive_bounds(in_memory_db):
    with session_scope() as s:
        repo = ExpenseRepository(s)
        service = ExpenseService(repo)
        service.register_many(
            user_id=1,
            drafts=[
                _draft(name="Edge", amount=Decimal("100"), expense_date=date(2026, 2, 1)),
                _draft(name="Mid", amount=Decimal("100"), expense_date=date(2026, 2, 15)),
                _draft(name="Outside", amount=Decimal("100"), expense_date=date(2026, 3, 1)),
            ],
            original_message="seed",
        )
    with session_scope() as s:
        repo = ExpenseRepository(s)
        rows = repo.list_in_range(1, start=date(2026, 2, 1), end=date(2026, 2, 28))
    assert {r.name for r in rows} == {"Edge", "Mid"}


def test_sum_by_category(in_memory_db):
    with session_scope() as s:
        repo = ExpenseRepository(s)
        service = ExpenseService(repo)
        service.register_many(
            user_id=1,
            drafts=[
                _draft(name="Café A", category="Café", amount=Decimal("5000")),
                _draft(name="Café B", category="Café", amount=Decimal("3000")),
                _draft(name="Uber", category="Uber", amount=Decimal("12000")),
            ],
            original_message="seed",
        )
    with session_scope() as s:
        repo = ExpenseRepository(s)
        totals = repo.sum_by_category(1)
    assert totals["Café"] == Decimal("8000.00")
    assert totals["Uber"] == Decimal("12000.00")
    # Ordered by amount desc
    assert list(totals.keys())[0] == "Uber"


def test_sum_by_category_with_period(in_memory_db):
    with session_scope() as s:
        repo = ExpenseRepository(s)
        service = ExpenseService(repo)
        service.register_many(
            user_id=1,
            drafts=[
                _draft(name="A", category="Café", amount=Decimal("5000"), expense_date=date(2026, 1, 10)),
                _draft(name="B", category="Café", amount=Decimal("3000"), expense_date=date(2026, 2, 10)),
            ],
            original_message="seed",
        )
    with session_scope() as s:
        repo = ExpenseRepository(s)
        totals = repo.sum_by_category(
            1, start=date(2026, 1, 1), end=date(2026, 1, 31)
        )
    assert totals.get("Café") == Decimal("5000.00")
