from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as _date
from decimal import Decimal, InvalidOperation
from typing import Optional, Sequence

from sqlalchemy.orm import Session

from app.categories.models import Category
from app.expenses.models import Expense
from app.expenses.repository import ExpenseRepository
from app.expenses.schemas import ExpenseCreate, ExpenseSummary

logger = logging.getLogger(__name__)


class ExpenseValidationError(ValueError):
    """Raised when an extracted expense is unsafe to persist as-is."""


class LinkedFixedPaymentError(RuntimeError):
    """Raised when an operation tries to mutate an Expense that is owned
    by a fixed-expense payment. The reversal must happen through the
    fixed-expense domain, not via the generic Expense CRUD."""


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
        *,
        source_type: str = "telegram",
        source_key: Optional[str] = None,
        telegram_chat_id: Optional[int] = None,
        telegram_message_id: Optional[int] = None,
        commit: bool = True,
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
                    source_type=source_type,
                    source_key=source_key,
                    telegram_chat_id=telegram_chat_id,
                    telegram_message_id=telegram_message_id,
                    revision=1,
                )
            )

        if questions:
            return PersistOutcome(
                needs_clarification=True, questions=questions, saved=[]
            )

        saved: list[Expense] = []
        if valid:
            saved = self.repo.create_many(valid, commit=commit)
        return PersistOutcome(
            needs_clarification=False, questions=[], saved=saved
        )

    @staticmethod
    def _resolve_category_id(
        session: Session,
        name: str,
        cache: dict[str, int],
    ) -> int:
        """Resolve a category NAME to a ``categories.id``.

        Categories are stored as a tree; identity is the pair
        ``(parent_id, name)``, NOT ``name`` alone. The seed has
        ``Café`` under ``Comida`` and ``Otros`` as a top-level
        parent, so a naive ``WHERE name = ?`` can return multiple
        rows after a future migration or a manual ``INSERT`` and
        trip ``MultipleResultsFound`` at registration time.

        Resolution strategy:

        1. The DB is the source of truth for "does this category
           exist". Find all rows matching ``name`` exactly.
        2. Zero rows → fall back to ``DEFAULT_CATEGORY`` (Other).
        3. Exactly one row → use it (covers the common case, plus
           categories added at runtime that the in-memory registry
           hasn't picked up yet).
        4. Multiple rows → use the registry's ``(parent_id, name)``
           to disambiguate. The seed tree is the canonical mapping.
           If the registry doesn't help, raise rather than guess.

        The result is cached by the *input* name so repeated lookups
        within the same ``register_many`` call stay fast.
        """
        if name in cache:
            return cache[name]

        from app.categories.categories import (
            DEFAULT_CATEGORY,
            get_parent_of,
        )

        category_id = ExpenseService._pick_category_id(
            session, name, get_parent_of
        )
        if category_id is None:
            category_id = ExpenseService._pick_category_id(
                session, DEFAULT_CATEGORY, get_parent_of
            )
        if category_id is None:
            raise RuntimeError(
                f"Cannot resolve category {name!r} (default "
                f"{DEFAULT_CATEGORY!r} also missing). Check that the "
                "categories seed (migration 0003) ran."
            )
        cache[name] = category_id
        return category_id

    @staticmethod
    def _pick_category_id(
        session: Session,
        name: str,
        get_parent_of_fn,
    ) -> int | None:
        """Pick a single ``categories.id`` for ``name``, disambiguating
        multiple same-named rows using ``(parent_id, name)``.

        Returns ``None`` when no row matches the (possibly
        parent-qualified) lookup, so the caller can try the default.
        """
        candidates: list[Category] = (
            session.query(Category).filter(Category.name == name).all()
        )
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0].id

        # Multiple rows share this name. The canonical identity is
        # ``(parent_id, name)``; use the registry's parent mapping to
        # pick the right one.
        parent_name = get_parent_of_fn(name)
        if parent_name is None:
            # Registry doesn't know this name (or it's a top-level
            # parent). Don't silently pick — let the caller decide.
            return None
        parent_row = (
            session.query(Category)
            .filter(Category.name == parent_name)
            .first()
        )
        if parent_row is None:
            return None
        for cand in candidates:
            if cand.parent_id == parent_row.id:
                return cand.id
        return None

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
