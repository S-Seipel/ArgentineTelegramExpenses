"""Service layer for fixed monthly expenses.

Combines the template (``FixedExpense``) with the monthly payment record
(``FixedExpensePayment``) to produce a single view object per bill per
month, suitable for rendering in chat or the dashboard.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from app.categories.models import Category
from app.fixed_expenses.models import FixedExpense, FixedExpensePayment, MonthlyBudget
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.schemas import (
    FixedExpenseCreate,
    FixedExpenseWithStatus,
    MonthlyBudgetOut,
)


class FixedExpenseValidationError(ValueError):
    pass


_ALLOWED_METHODS = {
    "TRANSFERENCIA", "EFECTIVO", "DEBITO", "DEBITO AUTOMATICO",
    "TARJETA", "CREDITO", "MERCADO PAGO", "APP", "OTRO",
}
_ALLOWED_CURRENCIES = {
    "ARS", "USD", "EUR", "BRL", "CLP", "MXN", "UYU", "PYG", "GBP", "JPY",
}


@dataclass
class FixedExpenseDraft:
    name: str
    expected_amount: Decimal
    currency: str | None = None
    payment_method: str | None = None
    due_day_of_month: int | None = None
    category: str | None = None
    confidence: float = 1.0


def current_month_year(today=None) -> str:
    """Return 'YYYY-MM' for the given (or current) date."""
    from datetime import date as _date
    if today is None:
        today = _date.today()
    return f"{today.year:04d}-{today.month:02d}"


def _resolve_category_id(
    session: Session, name: str | None
) -> int | None:
    if not name:
        return None
    row = (
        session.query(Category)
        .filter(Category.name == name.strip())
        .one_or_none()
    )
    if row is not None:
        return row.id
    return None


class FixedExpenseService:
    def __init__(self, repo: FixedExpenseRepository) -> None:
        self.repo = repo

    def add(self, user_id: int, draft: FixedExpenseDraft) -> FixedExpense:
        if not draft.name or not draft.name.strip():
            raise FixedExpenseValidationError(
                "Decime el nombre del gasto fijo."
            )
        if draft.expected_amount is None or draft.expected_amount <= 0:
            raise FixedExpenseValidationError(
                "El monto esperado tiene que ser mayor a cero."
            )
        if draft.expected_amount > Decimal("100000000"):
            raise FixedExpenseValidationError(
                "El monto parece demasiado grande, ¿podés confirmarlo?"
            )
        currency = (draft.currency or "ARS").upper()
        if currency not in _ALLOWED_CURRENCIES:
            raise FixedExpenseValidationError(
                f"No reconozco la moneda {currency!r}."
            )
        method = (draft.payment_method or "").upper().strip() or None
        if method and method not in _ALLOWED_METHODS:
            method = "OTRO"
        due_day = draft.due_day_of_month or 1
        if not (1 <= due_day <= 31):
            raise FixedExpenseValidationError(
                "El día de vencimiento tiene que estar entre 1 y 31."
            )

        category_id = _resolve_category_id(self.repo.session, draft.category)
        payload = FixedExpenseCreate(
            telegram_user_id=user_id,
            name=draft.name.strip()[:255],
            expected_amount=draft.expected_amount,
            currency=currency,
            category_id=category_id,
            payment_method=method,
            due_day_of_month=due_day,
        )
        return self.repo.create(payload)

    def list_active(self, user_id: int) -> list[FixedExpense]:
        return self.repo.list_active(user_id)

    def set_active(
        self, user_id: int, fixed_id: int, is_active: bool
    ) -> FixedExpense | None:
        obj = self.repo.get_by_id(user_id, fixed_id)
        if obj is None:
            return None
        return self.repo.set_active(obj, is_active)

    def mark_paid(
        self,
        user_id: int,
        fixed_id: int,
        month_year: str,
        actual_amount: Decimal | None = None,
        note: str | None = None,
    ) -> tuple[FixedExpense | None, FixedExpensePayment | None]:
        obj = self.repo.get_by_id(user_id, fixed_id)
        if obj is None:
            return None, None
        payment = self.repo.upsert_payment(
            fixed_id,
            month_year,
            paid_at=datetime_now(),
            actual_amount=actual_amount,
            note=note,
            skipped=False,
        )
        return obj, payment

    def mark_skipped(
        self,
        user_id: int,
        fixed_id: int,
        month_year: str,
    ) -> tuple[FixedExpense | None, FixedExpensePayment | None]:
        obj = self.repo.get_by_id(user_id, fixed_id)
        if obj is None:
            return None, None
        payment = self.repo.upsert_payment(
            fixed_id,
            month_year,
            paid_at=None,
            actual_amount=None,
            note=None,
            skipped=True,
        )
        return obj, payment

    def unmark(
        self,
        user_id: int,
        fixed_id: int,
        month_year: str,
    ) -> bool:
        obj = self.repo.get_by_id(user_id, fixed_id)
        if obj is None:
            return False
        return self.repo.delete_payment(fixed_id, month_year)

    def find_by_name(
        self, user_id: int, name: str
    ) -> FixedExpense | None:
        return self.repo.get_by_name(user_id, name)

    def with_status_for_month(
        self,
        user_id: int,
        month_year: str,
    ) -> list[FixedExpenseWithStatus]:
        """Combine each active fixed expense with this month's payment."""
        bills = self.repo.list_active(user_id)
        payments_by_id = {
            p.fixed_expense_id: p
            for p in self.repo.payments_for_month(user_id, month_year)
        }
        result: list[FixedExpenseWithStatus] = []
        for b in bills:
            p = payments_by_id.get(b.id)
            cat_name = ""
            if getattr(b, "category", None) is not None:
                cat_name = b.category.name or ""
            result.append(
                FixedExpenseWithStatus(
                    id=b.id,
                    name=b.name,
                    expected_amount=b.expected_amount,
                    currency=b.currency,
                    payment_method=b.payment_method,
                    due_day_of_month=b.due_day_of_month,
                    category_name=cat_name,
                    is_active=b.is_active,
                    paid=(p is not None and p.paid_at is not None),
                    skipped=(p.skipped if p else False),
                    actual_amount=(p.actual_amount if p else None),
                    paid_at=(p.paid_at if p else None),
                    note=(p.note if p else None),
                )
            )
        return result

    def month_summary(
        self,
        user_id: int,
        month_year: str,
    ) -> MonthlyBudgetOut:
        budget = self.repo.get_budget(user_id, month_year)
        income = budget.income if budget else Decimal("0")
        extra = budget.extra if budget else Decimal("0")
        bills = self.with_status_for_month(user_id, month_year)
        total_expected = sum(
            (b.expected_amount for b in bills if b.is_active),
            Decimal("0"),
        )
        total_paid = Decimal("0")
        for b in bills:
            if not b.is_active or b.skipped or not b.paid:
                continue
            total_paid += (
                b.actual_amount
                if b.actual_amount is not None
                else b.expected_amount
            )
        liberado = income + extra - total_paid
        return MonthlyBudgetOut(
            income=income,
            extra=extra,
            total_fixed_expected=total_expected,
            total_fixed_paid=total_paid,
            liberado=liberado,
        )

    def set_budget(
        self,
        user_id: int,
        month_year: str,
        *,
        income: Decimal | None = None,
        extra: Decimal | None = None,
        note: str | None = None,
    ):
        return self.repo.upsert_budget(
            user_id, month_year, income=income, extra=extra, note=note
        )


def datetime_now():
    from datetime import datetime
    return datetime.utcnow()
