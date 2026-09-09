from __future__ import annotations

from datetime import date, datetime
from typing import Sequence

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, joinedload

from app.recurring.models import RecurringExpense
from app.recurring.schemas import RecurringCreate, RecurringOut


class RecurringRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, payload: RecurringCreate) -> RecurringExpense:
        obj = RecurringExpense(
            telegram_user_id=payload.telegram_user_id,
            name=payload.name,
            amount=payload.amount,
            currency=payload.currency,
            category_id=payload.category_id,
            frequency=payload.frequency,
            day_of_month=payload.day_of_month,
            month_of_year=payload.month_of_year,
            next_due_date=payload.next_due_date,
        )
        self.session.add(obj)
        self.session.commit()
        self.session.refresh(obj)
        return obj

    def list_for_user(
        self, user_id: int, include_inactive: bool = False
    ) -> list[RecurringOut]:
        stmt: Select = (
            select(RecurringExpense)
            .options(joinedload(RecurringExpense.category))
            .where(RecurringExpense.telegram_user_id == user_id)
            .order_by(RecurringExpense.next_due_date.asc(), RecurringExpense.id.asc())
        )
        if not include_inactive:
            stmt = stmt.where(RecurringExpense.is_active.is_(True))
        rows = self.session.execute(stmt).scalars().all()
        return [self._to_out(r) for r in rows]

    def get_by_id(
        self, user_id: int, recurring_id: int
    ) -> RecurringOut | None:
        stmt = (
            select(RecurringExpense)
            .options(joinedload(RecurringExpense.category))
            .where(
                RecurringExpense.telegram_user_id == user_id,
                RecurringExpense.id == recurring_id,
            )
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return self._to_out(row)

    def get_by_id_for_update(
        self, user_id: int, recurring_id: int
    ) -> RecurringExpense | None:
        stmt = (
            select(RecurringExpense)
            .where(
                RecurringExpense.telegram_user_id == user_id,
                RecurringExpense.id == recurring_id,
            )
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def due_on(self, user_id: int, day: date) -> Sequence[RecurringExpense]:
        stmt = (
            select(RecurringExpense)
            .where(
                RecurringExpense.telegram_user_id == user_id,
                RecurringExpense.is_active.is_(True),
                RecurringExpense.next_due_date <= day,
            )
            .order_by(RecurringExpense.id.asc())
        )
        return self.session.execute(stmt).scalars().all()

    def update_next_due(
        self, obj: RecurringExpense, next_due: date, *, notified: bool = True
    ) -> RecurringExpense:
        obj.next_due_date = next_due
        if notified:
            obj.last_notified_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(obj)
        return obj

    def set_active(
        self, obj: RecurringExpense, is_active: bool
    ) -> RecurringExpense:
        obj.is_active = is_active
        self.session.commit()
        self.session.refresh(obj)
        return obj

    @staticmethod
    def _to_out(row: RecurringExpense) -> RecurringOut:
        cat = getattr(row, "category", None)
        return RecurringOut(
            id=row.id,
            name=row.name,
            amount=row.amount,
            currency=row.currency,
            category_id=row.category_id,
            category_name=cat.name if cat is not None else "",
            frequency=row.frequency,
            day_of_month=row.day_of_month,
            month_of_year=row.month_of_year,
            next_due_date=row.next_due_date,
            last_notified_at=row.last_notified_at,
            is_active=row.is_active,
        )