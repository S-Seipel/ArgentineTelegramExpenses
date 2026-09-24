"""R1 repository for the money domain.

Only the primitives the bootstrap needs: settings CRUD and cycle
lookups / inserts. No closing snapshot, no carry-over, no money math.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.money.models import FinancialCycle, UserSettings


DEFAULT_PAYDAY_DAY = 1


class UserSettingsRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, user_id: int) -> Optional[UserSettings]:
        stmt = select(UserSettings).where(
            UserSettings.telegram_user_id == user_id
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def get_or_default(self, user_id: int) -> UserSettings:
        """Return the row if it exists, else a detached default instance.

        Does NOT persist: callers that want to mutate must ``add()`` it
        explicitly so the absence of a row stays observable.
        """
        existing = self.get(user_id)
        if existing is not None:
            return existing
        return UserSettings(
            telegram_user_id=user_id,
            payday_day=DEFAULT_PAYDAY_DAY,
            timezone=None,
        )

    def upsert_payday_day(
        self,
        user_id: int,
        payday_day: int,
        *,
        timezone: Optional[str] = None,
    ) -> UserSettings:
        """Create or update the row, setting ``payday_day`` and ``timezone``.

        Lazily creates the row the first time payday is configured.
        Returns the freshly committed row.
        """
        if payday_day < 1 or payday_day > 31:
            raise ValueError("payday_day must be between 1 and 31")
        row = self.get(user_id)
        if row is None:
            row = UserSettings(
                telegram_user_id=user_id,
                payday_day=payday_day,
                timezone=timezone,
            )
            self.session.add(row)
        else:
            row.payday_day = payday_day
            if timezone is not None:
                row.timezone = timezone
        self.session.commit()
        self.session.refresh(row)
        return row


class FinancialCycleRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # lookups
    # ------------------------------------------------------------------

    def find_open_cycle(self, user_id: int) -> Optional[FinancialCycle]:
        stmt = (
            select(FinancialCycle)
            .where(
                FinancialCycle.telegram_user_id == user_id,
                FinancialCycle.status == "open",
            )
            .order_by(FinancialCycle.cycle_index.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def find_latest_cycle(self, user_id: int) -> Optional[FinancialCycle]:
        stmt = (
            select(FinancialCycle)
            .where(FinancialCycle.telegram_user_id == user_id)
            .order_by(FinancialCycle.cycle_index.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def find_cycle_by_id(
        self, user_id: int, cycle_id: int
    ) -> Optional[FinancialCycle]:
        stmt = select(FinancialCycle).where(
            FinancialCycle.telegram_user_id == user_id,
            FinancialCycle.id == cycle_id,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_cycles(
        self, user_id: int, limit: int = 50
    ) -> list[FinancialCycle]:
        stmt = (
            select(FinancialCycle)
            .where(FinancialCycle.telegram_user_id == user_id)
            .order_by(FinancialCycle.cycle_index.desc())
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars().all())

    def has_any_cycle(self, user_id: int) -> bool:
        stmt = select(func.count(FinancialCycle.id)).where(
            FinancialCycle.telegram_user_id == user_id
        )
        return bool(self.session.execute(stmt).scalar_one())

    # ------------------------------------------------------------------
    # inserts (R1 only: initial open)
    # ------------------------------------------------------------------

    def insert_cycle(
        self,
        *,
        user_id: int,
        cycle_index: int,
        start_date: date,
        end_date: date,
        payday_day: int,
        opened_at: datetime,
    ) -> FinancialCycle:
        """Insert one open cycle. Caller owns the transaction.

        Re-raises the underlying IntegrityError for the partial unique
        constraint so the service layer can decide whether to retry
        after re-reading.
        """
        obj = FinancialCycle(
            telegram_user_id=user_id,
            cycle_index=cycle_index,
            start_date=start_date,
            end_date=end_date,
            payday_day=payday_day,
            status="open",
            opened_at=opened_at,
        )
        self.session.add(obj)
        try:
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            raise
        return obj
