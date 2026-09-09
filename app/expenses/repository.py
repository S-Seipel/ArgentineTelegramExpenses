from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session, joinedload

from app.expenses.models import Expense
from app.expenses.schemas import ExpenseCreate, ExpenseSummary
from app.categories.models import Category


class ExpenseRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, payload: ExpenseCreate) -> Expense:
        obj = Expense(
            telegram_user_id=payload.telegram_user_id,
            name=payload.name,
            amount=payload.amount,
            currency=payload.currency,
            category_id=payload.category_id,
            expense_date=payload.expense_date,
            original_message=payload.original_message,
            ai_confidence=payload.ai_confidence,
        )
        self.session.add(obj)
        self.session.commit()
        self.session.refresh(obj)
        return obj

    def create_many(self, payloads: Sequence[ExpenseCreate]) -> list[Expense]:
        objs = [
            Expense(
                telegram_user_id=p.telegram_user_id,
                name=p.name,
                amount=p.amount,
                currency=p.currency,
                category_id=p.category_id,
                expense_date=p.expense_date,
                original_message=p.original_message,
                ai_confidence=p.ai_confidence,
            )
            for p in payloads
        ]
        self.session.add_all(objs)
        self.session.commit()
        for obj in objs:
            self.session.refresh(obj)
        return objs

    def list_recent(
        self, user_id: int, limit: int = 10
    ) -> list[ExpenseSummary]:
        stmt: Select = (
            select(Expense)
            .options(joinedload(Expense.category))
            .where(Expense.telegram_user_id == user_id)
            .order_by(Expense.expense_date.desc(), Expense.id.desc())
            .limit(limit)
        )
        rows = self.session.execute(stmt).scalars().all()
        return [self._to_summary(r) for r in rows]

    def list_in_range(
        self,
        user_id: int,
        start: date | None = None,
        end: date | None = None,
    ) -> list[ExpenseSummary]:
        stmt: Select = (
            select(Expense)
            .options(joinedload(Expense.category))
            .where(Expense.telegram_user_id == user_id)
            .order_by(Expense.expense_date.desc(), Expense.id.desc())
        )
        if start is not None:
            stmt = stmt.where(Expense.expense_date >= start)
        if end is not None:
            stmt = stmt.where(Expense.expense_date <= end)
        rows = self.session.execute(stmt).scalars().all()
        return [self._to_summary(r) for r in rows]

    def sum_total(
        self,
        user_id: int,
        start: date | None = None,
        end: date | None = None,
        category_id: int | None = None,
        category_name: str | None = None,
    ) -> Decimal:
        stmt = select(func.coalesce(func.sum(Expense.amount), 0)).where(
            Expense.telegram_user_id == user_id
        )
        if start is not None:
            stmt = stmt.where(Expense.expense_date >= start)
        if end is not None:
            stmt = stmt.where(Expense.expense_date <= end)
        if category_id is not None:
            stmt = stmt.where(Expense.category_id == category_id)
        elif category_name is not None:
            from app.categories.models import Category

            stmt = stmt.join(Category).where(Category.name == category_name)
        result = self.session.execute(stmt).scalar_one()
        return Decimal(result or 0)

    def sum_by_period(
        self,
        user_id: int,
        start: date,
        end: date,
    ) -> dict[str, Decimal]:
        stmt = (
            select(Expense.currency, func.coalesce(func.sum(Expense.amount), 0))
            .where(
                and_(
                    Expense.telegram_user_id == user_id,
                    Expense.expense_date >= start,
                    Expense.expense_date <= end,
                )
            )
            .group_by(Expense.currency)
        )
        rows = self.session.execute(stmt).all()
        return {currency: Decimal(total or 0) for currency, total in rows}

    def sum_by_category(
        self,
        user_id: int,
        start: date | None = None,
        end: date | None = None,
        currency: str | None = None,
    ) -> dict[str, Decimal]:
        stmt = (
            select(Category.name, func.coalesce(func.sum(Expense.amount), 0))
            .join(Category, Category.id == Expense.category_id)
            .where(Expense.telegram_user_id == user_id)
            .group_by(Category.name)
            .order_by(func.sum(Expense.amount).desc())
        )
        if start is not None:
            stmt = stmt.where(Expense.expense_date >= start)
        if end is not None:
            stmt = stmt.where(Expense.expense_date <= end)
        if currency is not None:
            stmt = stmt.where(Expense.currency == currency.upper())
        rows = self.session.execute(stmt).all()
        return {name: Decimal(total or 0) for name, total in rows}

    def sum_for_category(
        self,
        user_id: int,
        category_name: str,
        start: date | None = None,
        end: date | None = None,
        currency: str | None = None,
    ) -> Decimal:
        """Return the total spent in a single category within the window."""
        stmt = (
            select(func.coalesce(func.sum(Expense.amount), 0))
            .join(Category, Category.id == Expense.category_id)
            .where(
                Expense.telegram_user_id == user_id,
                Category.name == category_name,
            )
        )
        if start is not None:
            stmt = stmt.where(Expense.expense_date >= start)
        if end is not None:
            stmt = stmt.where(Expense.expense_date <= end)
        if currency is not None:
            stmt = stmt.where(Expense.currency == currency.upper())
        total = self.session.execute(stmt).scalar_one()
        return Decimal(total or 0)

    def largest(
        self,
        user_id: int,
        start: date | None = None,
        end: date | None = None,
    ) -> ExpenseSummary | None:
        stmt = (
            select(Expense)
            .options(joinedload(Expense.category))
            .where(Expense.telegram_user_id == user_id)
        )
        if start is not None:
            stmt = stmt.where(Expense.expense_date >= start)
        if end is not None:
            stmt = stmt.where(Expense.expense_date <= end)
        stmt = stmt.order_by(Expense.amount.desc(), Expense.id.desc()).limit(1)
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return self._to_summary(row)

    def get_by_id(
        self, user_id: int, expense_id: int
    ) -> ExpenseSummary | None:
        stmt = (
            select(Expense)
            .options(joinedload(Expense.category))
            .where(
                Expense.telegram_user_id == user_id, Expense.id == expense_id
            )
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return self._to_summary(row)

    def get_latest(self, user_id: int) -> ExpenseSummary | None:
        stmt = (
            select(Expense)
            .options(joinedload(Expense.category))
            .where(Expense.telegram_user_id == user_id)
            .order_by(Expense.id.desc())
            .limit(1)
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return self._to_summary(row)

    def delete(self, user_id: int, expense_id: int) -> ExpenseSummary | None:
        row = (
            self.session.query(Expense)
            .filter(
                Expense.telegram_user_id == user_id, Expense.id == expense_id
            )
            .one_or_none()
        )
        if row is None:
            return None
        summary = self._to_summary(row)
        self.session.delete(row)
        self.session.commit()
        return summary

    def update_amount(
        self, user_id: int, expense_id: int, new_amount: Decimal
    ) -> ExpenseSummary | None:
        row = (
            self.session.query(Expense)
            .filter(
                Expense.telegram_user_id == user_id, Expense.id == expense_id
            )
            .one_or_none()
        )
        if row is None:
            return None
        if new_amount <= 0:
            raise ValueError("amount must be positive")
        row.amount = new_amount
        self.session.commit()
        self.session.refresh(row)
        return self._to_summary(row)

    def update_name(
        self, user_id: int, expense_id: int, new_name: str
    ) -> ExpenseSummary | None:
        row = (
            self.session.query(Expense)
            .filter(
                Expense.telegram_user_id == user_id, Expense.id == expense_id
            )
            .one_or_none()
        )
        if row is None:
            return None
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("name cannot be empty")
        if len(new_name) > 200:
            raise ValueError("name too long")
        row.name = new_name
        self.session.commit()
        self.session.refresh(row)
        return self._to_summary(row)

    @staticmethod
    def _to_summary(row: Expense) -> ExpenseSummary:
        cat = getattr(row, "category", None)
        cat_name = cat.name if cat is not None else ""
        return ExpenseSummary(
            id=row.id,
            name=row.name,
            amount=row.amount,
            currency=row.currency,
            category_id=row.category_id,
            category_name=cat_name,
            expense_date=row.expense_date,
            ai_confidence=row.ai_confidence,
            created_at=row.created_at,
            original_message=row.original_message or "",
        )
