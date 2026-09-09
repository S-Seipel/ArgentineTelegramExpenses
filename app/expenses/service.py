from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as _date
from decimal import Decimal, InvalidOperation
from typing import Sequence

from sqlalchemy.orm import Session

from app.categories.models import Category
from app.expenses.models import Expense
from app.expenses.repository import ExpenseRepository
from app.expenses.schemas import ExpenseCreate, ExpenseSummary

logger = logging.getLogger(__name__)


class ExpenseValidationError(ValueError):
    """Raised when an extracted expense is unsafe to persist as-is."""


@dataclass
class PersistOutcome:
    needs_clarification: bool
    questions: list[str]
    saved: list[Expense]


@dataclass
class ExpenseDraft:
    name: str | None
    amount: Decimal | None
    currency: str
    category: str
    expense_date: _date
    confidence: Decimal
    needs_clarification: bool = False
    clarification_question: str | None = None


class ExpenseService:
    """Business logic for the expenses domain.

    The AI layer never talks to the DB. It returns ``ExpenseDraft`` objects
    which this service is responsible for validating and persisting.
    """

    def __init__(self, repo: ExpenseRepository) -> None:
        self.repo = repo

    def register_many(
        self,
        user_id: int,
        drafts: Sequence[ExpenseDraft],
        original_message: str,
    ) -> PersistOutcome:
        questions: list[str] = []
        valid: list[ExpenseCreate] = []
        cached_ids: dict[str, int] = {}
        for draft in drafts:
            error = self._validate_draft(draft)
            if error is not None:
                questions.append(error)
                continue
            category_id = self._resolve_category_id(
                self.repo.session, draft.category, cache=cached_ids
            )
            valid.append(
                ExpenseCreate(
                    telegram_user_id=user_id,
                    name=draft.name or "Gasto",
                    amount=draft.amount,
                    currency=draft.currency,
                    category_id=category_id,
                    expense_date=draft.expense_date,
                    original_message=original_message,
                    ai_confidence=draft.confidence,
                )
            )

        if questions:
            return PersistOutcome(
                needs_clarification=True, questions=questions, saved=[]
            )

        saved: list[Expense] = []
        if valid:
            saved = self.repo.create_many(valid)
        return PersistOutcome(
            needs_clarification=False, questions=[], saved=saved
        )

    @staticmethod
    def _resolve_category_id(
        session: Session,
        name: str,
        cache: dict[str, int],
    ) -> int:
        if name in cache:
            return cache[name]
        row = (
            session.query(Category)
            .filter(Category.name == name)
            .one_or_none()
        )
        if row is None:
            row = (
                session.query(Category)
                .filter(Category.name == "Otros")
                .one()
            )
        cache[name] = row.id
        return row.id

    @staticmethod
    def _validate_draft(draft: ExpenseDraft) -> str | None:
        if draft.needs_clarification and draft.clarification_question:
            return draft.clarification_question
        try:
            amount = (
                Decimal(str(draft.amount)) if draft.amount is not None else None
            )
        except (InvalidOperation, TypeError):
            return (
                "Necesito el monto del gasto. ¿Cuánto fue? (ej. *gasté "
                "10.000 en café*)."
            )
        if amount is None or amount <= 0:
            return (
                "Necesito el monto del gasto. ¿Cuánto fue? (ej. *gasté "
                "10.000 en café*)."
            )
        if amount > Decimal("100000000"):
            return "El monto parece demasiado grande, ¿podés confirmarlo?"
        currency = (draft.currency or "ARS").upper()
        if currency not in _ALLOWED_CURRENCIES:
            return f"No reconozco la moneda {currency!r}."
        name = (draft.name or "").strip()
        if not name:
            return "¿Qué compraste? Decime el nombre del gasto."
        if len(name) > 200:
            return "El nombre del gasto es demasiado largo."
        return None


_ALLOWED_CURRENCIES = {
    "ARS",
    "USD",
    "EUR",
    "BRL",
    "CLP",
    "MXN",
    "UYU",
    "PYG",
    "GBP",
    "JPY",
    "CNY",
}
