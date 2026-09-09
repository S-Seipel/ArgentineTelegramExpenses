from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.expenses.service import (
    ExpenseDraft,
    ExpenseService,
)
from app.expenses.schemas import ExpenseCreate


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


def test_valid_draft_is_persisted(in_memory_db):
    draft = _draft()
    with session_scope() as s:
        repo = ExpenseRepository(s)
        service = ExpenseService(repo)
        outcome = service.register_many(
            user_id=42, drafts=[draft], original_message="gasté 10k en café"
        )
    assert outcome.needs_clarification is False
    assert len(outcome.saved) == 1
    assert outcome.saved[0].amount == Decimal("10000.00")


def test_missing_amount_returns_clarification(in_memory_db):
    draft = _draft(amount=None)
    with session_scope() as s:
        repo = ExpenseRepository(s)
        service = ExpenseService(repo)
        outcome = service.register_many(
            user_id=42, drafts=[draft], original_message="algo"
        )
    assert outcome.needs_clarification is True
    assert any("monto" in q.lower() for q in outcome.questions)


def test_negative_amount_returns_clarification(in_memory_db):
    draft = _draft(amount=Decimal("-1"))
    with session_scope() as s:
        repo = ExpenseRepository(s)
        service = ExpenseService(repo)
        outcome = service.register_many(
            user_id=42, drafts=[draft], original_message="algo"
        )
    assert outcome.needs_clarification is True


def test_unknown_currency_returns_clarification(in_memory_db):
    draft = _draft(currency="XXX")
    with session_scope() as s:
        repo = ExpenseRepository(s)
        service = ExpenseService(repo)
        outcome = service.register_many(
            user_id=42, drafts=[draft], original_message="algo"
        )
    assert outcome.needs_clarification is True


def test_excessive_amount_returns_clarification(in_memory_db):
    draft = _draft(amount=Decimal("1000000000"))
    with session_scope() as s:
        repo = ExpenseRepository(s)
        service = ExpenseService(repo)
        outcome = service.register_many(
            user_id=42, drafts=[draft], original_message="algo"
        )
    assert outcome.needs_clarification is True


def test_multiple_with_one_invalid_skips_only_invalid(in_memory_db):
    good = _draft(amount=Decimal("100"))
    bad = _draft(amount=None)
    with session_scope() as s:
        repo = ExpenseRepository(s)
        service = ExpenseService(repo)
        outcome = service.register_many(
            user_id=42, drafts=[good, bad], original_message="x"
        )
    assert outcome.needs_clarification is True
    assert any("monto" in q.lower() for q in outcome.questions)


def test_repository_sums_by_period(in_memory_db):
    with session_scope() as s:
        repo = ExpenseRepository(s)
        service = ExpenseService(repo)
        service.register_many(
            user_id=1,
            drafts=[
                _draft(amount=Decimal("1000"), expense_date=date(2026, 1, 10)),
                _draft(amount=Decimal("2000"), expense_date=date(2026, 1, 12)),
            ],
            original_message="x",
        )
    with session_scope() as s:
        repo = ExpenseRepository(s)
        total = repo.sum_by_period(1, date(2026, 1, 1), date(2026, 1, 31))
    assert total.get("ARS") == Decimal("3000")


def test_repository_largest(in_memory_db):
    drafts = [
        _draft(amount=Decimal("1000"), name="A"),
        _draft(amount=Decimal("5000"), name="B"),
        _draft(amount=Decimal("2000"), name="C"),
    ]
    with session_scope() as s:
        repo = ExpenseRepository(s)
        service = ExpenseService(repo)
        service.register_many(user_id=1, drafts=drafts, original_message="x")
    with session_scope() as s:
        repo = ExpenseRepository(s)
        largest = repo.largest(1, date(2026, 1, 1), date(2026, 1, 31))
    assert largest is not None
    assert largest.name == "B"
