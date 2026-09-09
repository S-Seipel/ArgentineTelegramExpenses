from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, joinedload

from app.budgets.models import Budget
from app.budgets.schemas import BudgetCreate, BudgetOut


class BudgetRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(
        self, payload: BudgetCreate
    ) -> tuple[Budget, bool]:
        """Create or update the active budget for (user, category, currency).

        Returns ``(row, created)`` where ``created`` is True if a new row
        was inserted, False if an existing active row was updated.
        """
        stmt: Select = (
            select(Budget)
            .options(joinedload(Budget.category))
            .where(
                Budget.telegram_user_id == payload.telegram_user_id,
                Budget.category_id == payload.category_id,
                Budget.currency == payload.currency,
                Budget.is_active.is_(True),
            )
        )
        existing = self.session.execute(stmt).scalar_one_or_none()
        if existing is not None:
            existing.monthly_limit = payload.monthly_limit
            self.session.commit()
            self.session.refresh(existing)
            return existing, False

        obj = Budget(
            telegram_user_id=payload.telegram_user_id,
            category_id=payload.category_id,
            monthly_limit=payload.monthly_limit,
            currency=payload.currency,
        )
        self.session.add(obj)
        self.session.commit()
        self.session.refresh(obj)
        return obj, True

    def list_for_user(
        self, user_id: int, include_inactive: bool = False
    ) -> list[BudgetOut]:
        stmt: Select = (
            select(Budget)
            .options(joinedload(Budget.category))
            .where(Budget.telegram_user_id == user_id)
        )
        if not include_inactive:
            stmt = stmt.where(Budget.is_active.is_(True))
        stmt = stmt.order_by(Budget.id.asc())
        rows = self.session.execute(stmt).scalars().all()
        return [self._to_out(r) for r in rows]

    def get_active(
        self,
        user_id: int,
        category_id: int,
        currency: str,
    ) -> BudgetOut | None:
        stmt = (
            select(Budget)
            .options(joinedload(Budget.category))
            .where(
                Budget.telegram_user_id == user_id,
                Budget.category_id == category_id,
                Budget.currency == currency,
                Budget.is_active.is_(True),
            )
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return self._to_out(row)

    def get_by_id_for_update(
        self, user_id: int, budget_id: int
    ) -> Budget | None:
        stmt = select(Budget).where(
            Budget.telegram_user_id == user_id,
            Budget.id == budget_id,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def mark_alerted(self, obj: Budget) -> Budget:
        obj.last_alerted_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(obj)
        return obj

    def set_active(self, obj: Budget, is_active: bool) -> Budget:
        obj.is_active = is_active
        self.session.commit()
        self.session.refresh(obj)
        return obj

    @staticmethod
    def _to_out(row: Budget) -> BudgetOut:
        cat = getattr(row, "category", None)
        return BudgetOut(
            id=row.id,
            category_id=row.category_id,
            category_name=cat.name if cat is not None else "",
            monthly_limit=row.monthly_limit,
            currency=row.currency,
            last_alerted_at=row.last_alerted_at,
            is_active=row.is_active,
        )
