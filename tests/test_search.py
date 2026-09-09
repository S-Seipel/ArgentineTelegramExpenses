from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.expenses.service import ExpenseDraft, ExpenseService


def _seed(user_id: int = 1):
    today = date(2026, 8, 21)
    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=user_id,
            drafts=[
                ExpenseDraft(
                    name="Starbucks Palermo",
                    amount=Decimal("4500"),
                    currency="ARS",
                    category="Café",
                    expense_date=today,
                    confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="Starbucks Centro",
                    amount=Decimal("3200"),
                    currency="ARS",
                    category="Café",
                    expense_date=today,
                    confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="McDonald's",
                    amount=Decimal("5500"),
                    currency="ARS",
                    category="Comida",
                    expense_date=today,
                    confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="Uber al aeropuerto",
                    amount=Decimal("8500"),
                    currency="ARS",
                    category="Transporte",
                    expense_date=today,
                    confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )


def test_search_finds_partial_match(in_memory_db):
    _seed()
    with session_scope() as s:
        repo = ExpenseRepository(s)
        results = repo.search_by_name(1, "starbucks")
    assert len(results) == 2
    names = {r.name for r in results}
    assert "Starbucks Palermo" in names
    assert "Starbucks Centro" in names


def test_search_case_insensitive(in_memory_db):
    _seed()
    with session_scope() as s:
        repo = ExpenseRepository(s)
        results = repo.search_by_name(1, "STARBUCKS")
    assert len(results) == 2


def test_search_returns_empty_for_no_match(in_memory_db):
    _seed()
    with session_scope() as s:
        repo = ExpenseRepository(s)
        results = repo.search_by_name(1, "carrefour")
    assert results == []


def test_search_empty_query_returns_empty(in_memory_db):
    _seed()
    with session_scope() as s:
        repo = ExpenseRepository(s)
        assert repo.search_by_name(1, "") == []
        assert repo.search_by_name(1, "   ") == []


def test_search_scoped_per_user(in_memory_db):
    _seed(user_id=1)
    _seed(user_id=2)
    with session_scope() as s:
        repo = ExpenseRepository(s)
        mine = repo.search_by_name(1, "starbucks")
        theirs = repo.search_by_name(2, "starbucks")
    assert len(mine) == 2
    assert len(theirs) == 2


def test_search_respects_limit(in_memory_db):
    _seed()
    with session_scope() as s:
        repo = ExpenseRepository(s)
        results = repo.search_by_name(1, "starbucks", limit=1)
    assert len(results) == 1


def test_search_orders_by_date_desc(in_memory_db):
    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Coca-Cola",
                    amount=Decimal("1000"),
                    currency="ARS",
                    category="Comida",
                    expense_date=date(2026, 7, 1),
                    confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="Coca-Cola",
                    amount=Decimal("1000"),
                    currency="ARS",
                    category="Comida",
                    expense_date=date(2026, 8, 21),
                    confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="Coca-Cola",
                    amount=Decimal("1000"),
                    currency="ARS",
                    category="Comida",
                    expense_date=date(2026, 7, 15),
                    confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )
    with session_scope() as s:
        repo = ExpenseRepository(s)
        results = repo.search_by_name(1, "coca")
    assert [r.expense_date for r in results] == [
        date(2026, 8, 21), date(2026, 7, 15), date(2026, 7, 1)
    ]
