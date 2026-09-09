from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.expenses.service import ExpenseDraft, ExpenseService
from app.queries.intents import QuerySpec
from app.queries.service import QueryService


def _draft(**overrides):
    base = dict(
        name="X",
        amount=Decimal("1000"),
        currency="ARS",
        category="Café",
        expense_date=date(2026, 1, 15),
        confidence=Decimal("0.9"),
    )
    base.update(overrides)
    return ExpenseDraft(**base)


def _seed(rows):
    with session_scope() as s:
        service = ExpenseService(ExpenseRepository(s))
        service.register_many(user_id=1, drafts=rows, original_message="seed")


def test_query_total_today(in_memory_db):
    _seed(
        [
            _draft(amount=Decimal("100"), expense_date=date(2026, 1, 15)),
            _draft(amount=Decimal("200"), expense_date=date(2026, 1, 15)),
            _draft(amount=Decimal("9999"), expense_date=date(2025, 12, 30)),
        ]
    )
    spec = QuerySpec(kind="total", period="today", label="Hoy")
    with session_scope() as s:
        q = QueryService(ExpenseRepository(s))
        q.today = lambda: date(2026, 1, 15)  # type: ignore[assignment]
        result = q.run(1, spec)
    assert result.total_by_currency.get("ARS") == Decimal("300")


def test_query_total_week(in_memory_db):
    _seed(
        [
            _draft(amount=Decimal("100"), expense_date=date(2026, 1, 12)),
            _draft(amount=Decimal("200"), expense_date=date(2026, 1, 15)),
            _draft(amount=Decimal("9999"), expense_date=date(2025, 12, 1)),
        ]
    )
    spec = QuerySpec(kind="total", period="week", label="Esta semana")
    with session_scope() as s:
        q = QueryService(ExpenseRepository(s))
        q.today = lambda: date(2026, 1, 15)  # type: ignore[assignment]
        result = q.run(1, spec)
    assert result.total_by_currency.get("ARS") == Decimal("300")


def test_query_list_recent(in_memory_db):
    _seed(
        [
            _draft(amount=Decimal("100"), name="A"),
            _draft(amount=Decimal("200"), name="B"),
            _draft(amount=Decimal("300"), name="C"),
        ]
    )
    spec = QuerySpec(kind="list", limit=2)
    with session_scope() as s:
        q = QueryService(ExpenseRepository(s))
        result = q.run(1, spec)
    assert result.items is not None
    assert len(result.items) == 2


def test_query_largest(in_memory_db):
    _seed(
        [
            _draft(amount=Decimal("100"), name="A", expense_date=date(2026, 1, 15)),
            _draft(amount=Decimal("500"), name="B", expense_date=date(2026, 1, 16)),
            _draft(amount=Decimal("300"), name="C", expense_date=date(2026, 1, 17)),
        ]
    )
    spec = QuerySpec(
        kind="largest", period="month", label="Este mes", order_by="amount_desc"
    )
    with session_scope() as s:
        q = QueryService(ExpenseRepository(s))
        q.today = lambda: date(2026, 1, 20)  # type: ignore[assignment]
        result = q.run(1, spec)
    assert result.largest is not None
    assert result.largest.name == "B"
