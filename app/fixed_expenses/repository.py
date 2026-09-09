"""Repository for fixed expenses + payments + monthly budget."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.fixed_expenses.models import (
    FixedExpense,
    FixedExpensePayment,
    MonthlyBudget,
)


class FixedExpenseRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # --------------------- templates ---------------------

    def create(self, payload) -> FixedExpense:
        obj = FixedExpense(
            telegram_user_id=payload.telegram_user_id,
            name=payload.name.strip()[:255],
            expected_amount=payload.expected_amount,
            currency=payload.currency.upper(),
            category_id=payload.category_id,
            payment_method=(payload.payment_method.upper()[:32]
                           if payload.payment_method else None),
            due_day_of_month=payload.due_day_of_month,
        )
        self.session.add(obj)
        self.session.commit()
        self.session.refresh(obj)
        return obj

    def list_active(
        self, user_id: int, include_inactive: bool = False
    ) -> list[FixedExpense]:
        stmt = (
            select(FixedExpense)
            .where(FixedExpense.telegram_user_id == user_id)
            .order_by(FixedExpense.due_day_of_month.asc(), FixedExpense.id.asc())
        )
        if not include_inactive:
            stmt = stmt.where(FixedExpense.is_active.is_(True))
        return list(self.session.execute(stmt).scalars().all())

    def get_by_id(
        self, user_id: int, fixed_id: int
    ) -> FixedExpense | None:
        stmt = select(FixedExpense).where(
            FixedExpense.telegram_user_id == user_id,
            FixedExpense.id == fixed_id,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def get_by_id_for_update(
        self, user_id: int, fixed_id: int
    ) -> FixedExpense | None:
        stmt = select(FixedExpense).where(
            FixedExpense.telegram_user_id == user_id,
            FixedExpense.id == fixed_id,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def get_by_name(
        self, user_id: int, name: str
    ) -> FixedExpense | None:
        stmt = select(FixedExpense).where(
            FixedExpense.telegram_user_id == user_id,
            FixedExpense.name == name.strip(),
            FixedExpense.is_active.is_(True),
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def set_active(
        self, obj: FixedExpense, is_active: bool
    ) -> FixedExpense:
        obj.is_active = is_active
        self.session.commit()
        self.session.refresh(obj)
        return obj

    # --------------------- payments ---------------------

    def get_payment(
        self, fixed_id: int, month_year: str
    ) -> FixedExpensePayment | None:
        stmt = select(FixedExpensePayment).where(
            FixedExpensePayment.fixed_expense_id == fixed_id,
            FixedExpensePayment.month_year == month_year,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def upsert_payment(
        self,
        fixed_expense_id: int,
        month_year: str,
        *,
        paid_at: datetime | None = None,
        actual_amount: Decimal | None = None,
        note: str | None = None,
        skipped: bool = False,
        expense_id: int | None = None,
    ) -> FixedExpensePayment:
        existing = self.get_payment(fixed_expense_id, month_year)
        if existing is not None:
            if paid_at is not None:
                existing.paid_at = paid_at
            if actual_amount is not None:
                existing.actual_amount = actual_amount
            if note is not None:
                existing.note = note[:500]
            existing.skipped = skipped
            if expense_id is not None:
                existing.expense_id = expense_id
            self.session.commit()
            self.session.refresh(existing)
            return existing
        obj = FixedExpensePayment(
            fixed_expense_id=fixed_expense_id,
            month_year=month_year,
            paid_at=paid_at,
            actual_amount=actual_amount,
            note=note[:500] if note else None,
            skipped=skipped,
            expense_id=expense_id,
        )
        self.session.add(obj)
        self.session.commit()
        self.session.refresh(obj)
        return obj

    def delete_payment(
        self, fixed_expense_id: int, month_year: str
    ) -> bool:
        existing = self.get_payment(fixed_expense_id, month_year)
        if existing is None:
            return False
        self.session.delete(existing)
        self.session.commit()
        return True

    def payments_for_month(
        self, user_id: int, month_year: str
    ) -> list[FixedExpensePayment]:
        stmt = (
            select(FixedExpensePayment)
            .join(FixedExpense, FixedExpense.id == FixedExpensePayment.fixed_expense_id)
            .where(
                FixedExpense.telegram_user_id == user_id,
                FixedExpensePayment.month_year == month_year,
            )
        )
        return list(self.session.execute(stmt).scalars().all())

    def payments_for_fixed(
        self, fixed_expense_id: int
    ) -> list[FixedExpensePayment]:
        stmt = select(FixedExpensePayment).where(
            FixedExpensePayment.fixed_expense_id == fixed_expense_id
        )
        return list(self.session.execute(stmt).scalars().all())

    # --------------------- monthly budget ---------------------

    def get_budget(
        self, user_id: int, month_year: str
    ) -> MonthlyBudget | None:
        stmt = select(MonthlyBudget).where(
            MonthlyBudget.telegram_user_id == user_id,
            MonthlyBudget.month_year == month_year,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def upsert_budget(
        self,
        user_id: int,
        month_year: str,
        *,
        income: Decimal | None = None,
        extra: Decimal | None = None,
        note: str | None = None,
    ) -> MonthlyBudget:
        existing = self.get_budget(user_id, month_year)
        if existing is not None:
            if income is not None:
                existing.income = income
            if extra is not None:
                existing.extra = extra
            if note is not None:
                existing.note = note[:500]
            self.session.commit()
            self.session.refresh(existing)
            return existing
        obj = MonthlyBudget(
            telegram_user_id=user_id,
            month_year=month_year,
            income=income if income is not None else Decimal("0"),
            extra=extra if extra is not None else Decimal("0"),
            note=note[:500] if note else None,
        )
        self.session.add(obj)
        self.session.commit()
        self.session.refresh(obj)
        return obj
