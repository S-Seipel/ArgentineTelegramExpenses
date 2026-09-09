from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Sequence

from sqlalchemy.orm import Session

from app.categories.models import Category
from app.recurring.models import RecurringExpense
from app.recurring.repository import RecurringRepository
from app.recurring.schemas import RecurringCreate, RecurringOut

logger = logging.getLogger(__name__)


_ALLOWED_FREQUENCIES = {"monthly", "yearly"}
_ALLOWED_CURRENCIES = {
    "ARS", "USD", "EUR", "BRL", "CLP", "MXN", "UYU", "PYG", "GBP", "JPY", "CNY",
}


class RecurringValidationError(ValueError):
    """Raised when a recurring template is unsafe to persist."""


@dataclass
class RecurringDraft:
    name: str | None
    amount: Decimal | None
    currency: str | None
    category: str | None
    frequency: str | None
    day_of_month: int | None
    month_of_year: int | None
    confidence: float
    needs_clarification: bool = False
    clarification_question: str | None = None


def last_day_of_month(year: int, month: int) -> int:
    """Return the last day of the given month (handles leap years for Feb)."""
    return calendar.monthrange(year, month)[1]


def advance_due_date(
    current: date, frequency: str, *, day_of_month: int, month_of_year: int | None
) -> date:
    """Advance ``current`` to the next occurrence after ``current``.

    ``current`` is also the day the user confirmed/skipped; we want the
    *following* period so we don't re-notify in the same cycle.
    """
    if frequency == "monthly":
        year = current.year + (1 if current.month == 12 else 0)
        month = 1 if current.month == 12 else current.month + 1
        day = min(day_of_month, last_day_of_month(year, month))
        return date(year, month, day)
    if frequency == "yearly":
        if month_of_year is None:
            month_of_year = current.month
        next_year = current.year + 1
        day = min(day_of_month, last_day_of_month(next_year, month_of_year))
        return date(next_year, month_of_year, day)
    raise ValueError(f"unknown frequency: {frequency!r}")


def postpone_due_date(current: date) -> date:
    return current + timedelta(days=1)


def first_occurrence(
    today: date, frequency: str, day_of_month: int, month_of_year: int | None
) -> date:
    """Pick the first valid occurrence on or after ``today``."""
    if frequency == "monthly":
        target_this_month = min(
            day_of_month, last_day_of_month(today.year, today.month)
        )
        if today.day <= target_this_month:
            return date(today.year, today.month, target_this_month)
        next_month = today.month + 1
        next_year = today.year
        if next_month == 13:
            next_month = 1
            next_year += 1
        return date(
            next_year, next_month, min(day_of_month, last_day_of_month(next_year, next_month))
        )
    if frequency == "yearly":
        if month_of_year is None:
            raise ValueError("yearly frequency requires month_of_year")
        day = min(day_of_month, last_day_of_month(today.year, month_of_year))
        this_year = date(today.year, month_of_year, day)
        if today <= this_year:
            return this_year
        next_day = min(
            day_of_month, last_day_of_month(today.year + 1, month_of_year)
        )
        return date(today.year + 1, month_of_year, next_day)
    raise ValueError(f"unknown frequency: {frequency!r}")


class RecurringService:
    def __init__(self, repo: RecurringRepository) -> None:
        self.repo = repo

    def create_from_draft(
        self, user_id: int, draft: RecurringDraft, *, today: date
    ) -> RecurringExpense:
        if draft.needs_clarification and draft.clarification_question:
            raise RecurringValidationError(draft.clarification_question)

        if not draft.name or not draft.name.strip():
            raise RecurringValidationError(
                "¿Qué gasto recurrente querés registrar? Decime el nombre."
            )
        if draft.amount is None or draft.amount <= 0:
            raise RecurringValidationError(
                "Necesito el monto del gasto recurrente."
            )
        if draft.amount > Decimal("100000000"):
            raise RecurringValidationError(
                "El monto parece demasiado grande, ¿podés confirmarlo?"
            )

        frequency = (draft.frequency or "").strip().lower()
        if frequency not in _ALLOWED_FREQUENCIES:
            raise RecurringValidationError(
                "No reconozco la frecuencia. Usá 'monthly' o 'yearly'."
            )
        currency = (draft.currency or "ARS").upper()
        if currency not in _ALLOWED_CURRENCIES:
            raise RecurringValidationError(f"No reconozco la moneda {currency!r}.")

        if draft.day_of_month is None or not (1 <= draft.day_of_month <= 31):
            raise RecurringValidationError(
                "Necesito el día del mes (1-31). Ej: día 15."
            )
        month_of_year: int | None = None
        if frequency == "yearly":
            if draft.month_of_year is None or not (1 <= draft.month_of_year <= 12):
                raise RecurringValidationError(
                    "Para frecuencia anual necesito el mes (1-12)."
                )
            month_of_year = draft.month_of_year

        category_name = draft.category or "Otros"
        category_id = self._resolve_category_id(
            self.repo.session, category_name
        )

        next_due = first_occurrence(
            today,
            frequency,
            draft.day_of_month,
            month_of_year,
        )

        payload = RecurringCreate(
            telegram_user_id=user_id,
            name=draft.name.strip()[:200],
            amount=draft.amount,
            currency=currency,
            category_id=category_id,
            frequency=frequency,
            day_of_month=draft.day_of_month,
            month_of_year=month_of_year,
            next_due_date=next_due,
        )
        return self.repo.create(payload)

    def list(self, user_id: int) -> list[RecurringOut]:
        return self.repo.list_for_user(user_id)

    def get(self, user_id: int, recurring_id: int) -> RecurringOut | None:
        return self.repo.get_by_id(user_id, recurring_id)

    def set_active(
        self, user_id: int, recurring_id: int, is_active: bool
    ) -> RecurringExpense | None:
        obj = self.repo.get_by_id_for_update(user_id, recurring_id)
        if obj is None:
            return None
        return self.repo.set_active(obj, is_active)

    def register_occurrence(
        self,
        user_id: int,
        recurring_id: int,
        today: date,
    ) -> tuple[RecurringExpense | None, RecurringExpense | None]:
        """Register today's expense and advance the template.

        Returns ``(template_after_advance, saved_expense)``.
        """
        from app.expenses.service import ExpenseService
        from app.expenses.schemas import ExpenseCreate as ExpenseCreateDTO

        template_obj = self.repo.get_by_id_for_update(user_id, recurring_id)
        if template_obj is None or not template_obj.is_active:
            return None, None

        original_message = (
            f"[recurrente #{template_obj.id}] {template_obj.name}"
        )
        expense_payload = ExpenseCreateDTO(
            telegram_user_id=user_id,
            name=template_obj.name,
            amount=template_obj.amount,
            currency=template_obj.currency,
            category_id=template_obj.category_id,
            expense_date=today,
            original_message=original_message,
            ai_confidence=Decimal("1"),
        )
        expense_repo_module = __import__(
            "app.expenses.repository", fromlist=["ExpenseRepository"]
        )
        expense_service = ExpenseService(expense_repo_module.ExpenseRepository(self.repo.session))
        saved = [expense_service.repo.create(expense_payload)]

        next_due = advance_due_date(
            today,
            template_obj.frequency,
            day_of_month=template_obj.day_of_month,
            month_of_year=template_obj.month_of_year,
        )
        updated = self.repo.update_next_due(
            template_obj, next_due, notified=True
        )
        return updated, saved[0]

    def skip_occurrence(
        self, user_id: int, recurring_id: int, today: date
    ) -> RecurringExpense | None:
        """Advance to next period without registering."""
        template_obj = self.repo.get_by_id_for_update(user_id, recurring_id)
        if template_obj is None or not template_obj.is_active:
            return None
        next_due = advance_due_date(
            today,
            template_obj.frequency,
            day_of_month=template_obj.day_of_month,
            month_of_year=template_obj.month_of_year,
        )
        return self.repo.update_next_due(
            template_obj, next_due, notified=True
        )

    def postpone_occurrence(
        self, user_id: int, recurring_id: int
    ) -> RecurringExpense | None:
        template_obj = self.repo.get_by_id_for_update(user_id, recurring_id)
        if template_obj is None or not template_obj.is_active:
            return None
        new_due = postpone_due_date(template_obj.next_due_date)
        template_obj.next_due_date = new_due
        self.session_commit()
        return template_obj

    def session_commit(self) -> None:
        self.repo.session.commit()

    @staticmethod
    def _resolve_category_id(session: Session, name: str) -> int:
        row = (
            session.query(Category).filter(Category.name == name).one_or_none()
        )
        if row is not None:
            return row.id
        fallback = (
            session.query(Category).filter(Category.name == "Otros").one()
        )
        return fallback.id