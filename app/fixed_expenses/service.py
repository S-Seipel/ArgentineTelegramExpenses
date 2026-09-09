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
        paid_on: "date | None" = None,
    ) -> tuple[FixedExpense | None, FixedExpensePayment | None]:
        """Mark a fixed expense as paid for the given month.

        Also creates or updates a real ``expenses`` row so the
        dashboard's totals (which read from ``expenses``) stay in sync
        with the user's checkmarks.
        """
        from datetime import date as _date
        obj = self.repo.get_by_id(user_id, fixed_id)
        if obj is None:
            return None, None

        amount = actual_amount if actual_amount is not None else obj.expected_amount
        paid_on_date = paid_on or _date.today()

        # Look up existing payment for this (fixed, month) pair.
        existing_payment = self.repo.get_payment(fixed_id, month_year)
        existing_expense_id = (
            existing_payment.expense_id if existing_payment else None
        )

        # Create or update the real expense row.
        expense_id = _upsert_expense_for_payment(
            session=self.repo.session,
            user_id=user_id,
            amount=amount,
            currency=obj.currency,
            name=obj.name,
            category_id=obj.category_id,
            paid_on=paid_on_date,
            existing_expense_id=existing_expense_id,
            original_message=(
                f"[gastofijo #{obj.id}] {obj.name} "
                f"({obj.payment_method or 's/método'})"
            ),
        )

        payment = self.repo.upsert_payment(
            fixed_id,
            month_year,
            paid_at=datetime_now(),
            actual_amount=actual_amount,
            note=note,
            skipped=False,
            expense_id=expense_id,
        )
        return obj, payment

    def mark_skipped(
        self,
        user_id: int,
        fixed_id: int,
        month_year: str,
    ) -> tuple[FixedExpense | None, FixedExpensePayment | None]:
        """Mark as skipped: no payment, no expense. Pure status flag."""
        obj = self.repo.get_by_id(user_id, fixed_id)
        if obj is None:
            return None, None
        # Remove a previously-linked expense if any (so the dashboard
        # doesn't count a skipped bill).
        existing = self.repo.get_payment(fixed_id, month_year)
        if existing and existing.expense_id:
            _delete_expense(self.repo.session, existing.expense_id)
        payment = self.repo.upsert_payment(
            fixed_id,
            month_year,
            paid_at=None,
            actual_amount=None,
            note=None,
            skipped=True,
            expense_id=None,
        )
        return obj, payment

    def unmark(
        self,
        user_id: int,
        fixed_id: int,
        month_year: str,
    ) -> bool:
        """Undo a previous mark. Also removes the linked expense."""
        obj = self.repo.get_by_id(user_id, fixed_id)
        if obj is None:
            return False
        existing = self.repo.get_payment(fixed_id, month_year)
        if existing and existing.expense_id:
            _delete_expense(self.repo.session, existing.expense_id)
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


# ---------------------------------------------------------------------------
# Helpers: keep the real ``expenses`` table in sync with fixed payments.
# ---------------------------------------------------------------------------

def _upsert_expense_for_payment(
    *,
    session,
    user_id: int,
    amount: Decimal,
    currency: str,
    name: str,
    category_id: int | None,
    paid_on: "date",
    existing_expense_id: int | None,
    original_message: str,
) -> int:
    """Create or update the real ``expenses`` row for a fixed payment.

    Returns the ``expenses.id`` of the row.
    """
    from datetime import datetime as _dt
    from app.expenses.models import Expense
    from app.expenses.service import ExpenseService
    from app.expenses.repository import ExpenseRepository

    if existing_expense_id is not None:
        existing = (
            session.query(Expense)
            .filter(Expense.id == existing_expense_id)
            .one_or_none()
        )
        if existing is not None:
            existing.amount = amount
            existing.currency = currency
            existing.expense_date = paid_on
            if category_id is not None:
                existing.category_id = category_id
            session.commit()
            session.refresh(existing)
            return existing.id

    # Create new.
    expense_repo = ExpenseRepository(session)
    expense_service = ExpenseService(expense_repo)
    from app.expenses.service import ExpenseDraft
    draft = ExpenseDraft(
        name=name[:255],
        amount=amount,
        currency=currency,
        category=_resolve_category_name(session, category_id),
        expense_date=paid_on,
        confidence=Decimal("1"),
    )
    outcome = expense_service.register_many(
        user_id=user_id,
        drafts=[draft],
        original_message=original_message,
    )
    if outcome.saved:
        return outcome.saved[0].id
    raise RuntimeError("Failed to create linked expense for fixed payment")


def _delete_expense(session, expense_id: int) -> None:
    from app.expenses.models import Expense

    row = (
        session.query(Expense).filter(Expense.id == expense_id).one_or_none()
    )
    if row is None:
        return
    session.delete(row)
    session.commit()


def _resolve_category_name(session, category_id: int | None) -> str:
    """Reverse-resolve a category_id back to a name for ExpenseDraft."""
    if category_id is None:
        return "Otros"
    from app.categories.models import Category
    row = (
        session.query(Category).filter(Category.id == category_id).one_or_none()
    )
    if row is None:
        return "Otros"
    return row.name or "Otros"
