from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from app.budgets.models import Budget
from app.budgets.repository import BudgetRepository
from app.budgets.schemas import (
    BudgetAlert,
    BudgetCreate,
    BudgetOut,
)
from app.categories.models import Category

_ALLOWED_CURRENCIES = {
    "ARS", "USD", "EUR", "BRL", "CLP", "MXN", "UYU", "PYG", "GBP", "JPY",
}

WARNING_THRESHOLD = Decimal("0.8")
EXCEEDED_THRESHOLD = Decimal("1.0")


class BudgetValidationError(ValueError):
    """Raised when a budget draft is unsafe to persist."""


@dataclass
class BudgetDraft:
    category: str
    monthly_limit: Decimal
    currency: str | None
    confidence: float = 1.0


class BudgetService:
    def __init__(self, repo: BudgetRepository) -> None:
        self.repo = repo

    def upsert_from_draft(
        self, user_id: int, draft: BudgetDraft
    ) -> tuple[Budget, bool]:
        if draft.monthly_limit is None or draft.monthly_limit <= 0:
            raise BudgetValidationError(
                "El límite mensual tiene que ser mayor a cero."
            )
        if draft.monthly_limit > Decimal("100000000"):
            raise BudgetValidationError(
                "El límite parece demasiado grande, ¿podés confirmarlo?"
            )
        currency = (draft.currency or "ARS").upper()
        if currency not in _ALLOWED_CURRENCIES:
            raise BudgetValidationError(
                f"No reconozco la moneda {currency!r}."
            )
        category_name = (draft.category or "").strip()
        if not category_name:
            raise BudgetValidationError(
                "Decime la categoría. Ej: 'comida', 'transporte', 'salidas'."
            )

        category_id = self._resolve_category_id(self.repo.session, category_name)

        payload = BudgetCreate(
            telegram_user_id=user_id,
            category_id=category_id,
            monthly_limit=draft.monthly_limit,
            currency=currency,
        )
        return self.repo.upsert(payload)

    def list(self, user_id: int) -> list[BudgetOut]:
        return self.repo.list_for_user(user_id)

    def get(
        self, user_id: int, budget_id: int
    ) -> BudgetOut | None:
        return self.repo.get_by_id_for_update(user_id, budget_id)

    def delete(self, user_id: int, budget_id: int) -> bool:
        obj = self.repo.get_by_id_for_update(user_id, budget_id)
        if obj is None:
            return False
        self.repo.set_active(obj, is_active=False)
        return True

    def deactivate(self, user_id: int, budget_id: int) -> Budget | None:
        obj = self.repo.get_by_id_for_update(user_id, budget_id)
        if obj is None:
            return None
        return self.repo.set_active(obj, is_active=False)

    def reactivate(self, user_id: int, budget_id: int) -> Budget | None:
        obj = self.repo.get_by_id_for_update(user_id, budget_id)
        if obj is None:
            return None
        return self.repo.set_active(obj, is_active=True)

    def evaluate_after_expense(
        self,
        user_id: int,
        category_id: int,
        currency: str,
        spent_in_month: Decimal,
        *,
        today: date,
    ) -> BudgetAlert | None:
        """Decide whether to alert the user after a new expense.

        Returns ``None`` if no budget applies, no threshold was crossed, or
        we already alerted this calendar month.
        """
        budget = self.repo.get_active(user_id, category_id, currency)
        if budget is None:
            return None

        percent = spent_in_month / budget.monthly_limit
        level: str | None = None
        if percent >= EXCEEDED_THRESHOLD:
            level = "exceeded"
        elif percent >= WARNING_THRESHOLD:
            level = "warning"
        if level is None:
            return None

        # Don't re-alert within the same calendar month.
        if budget.last_alerted_at is not None:
            alerted_month = budget.last_alerted_at.date().replace(day=1)
            current_month = today.replace(day=1)
            if alerted_month >= current_month:
                return None

        alert = BudgetAlert(
            budget_id=budget.id,
            category_name=budget.category_name,
            spent=spent_in_month,
            limit=budget.monthly_limit,
            currency=budget.currency,
            percent=percent,
            level=level,
        )
        obj = self.repo.get_by_id_for_update(user_id, budget.id)
        if obj is not None:
            self.repo.mark_alerted(obj)
        return alert

    @staticmethod
    def _resolve_category_id(session: Session, name: str) -> int:
        # Try exact match first, then case-insensitive.
        row = (
            session.query(Category)
            .filter(Category.name == name)
            .one_or_none()
        )
        if row is not None:
            return row.id
        normalized = name.strip().lower()
        for cat in session.query(Category).all():
            parent_name = (
                cat.parent.name if getattr(cat, "parent", None) is not None
                else None
            )
            haystacks = {cat.name.lower(), parent_name.lower() if parent_name else ""}
            if normalized in haystacks:
                return cat.id
        # Final fallback to "Otros"
        fallback = (
            session.query(Category).filter(Category.name == "Otros").one()
        )
        return fallback.id
