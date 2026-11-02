"""R1 repository for the money domain.

Only the primitives the bootstrap needs: settings CRUD and cycle
lookups / inserts. No closing snapshot, no carry-over, no money math.
"""
from __future__ import annotations

from datetime import date, datetime
import re
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.money.models import CurrencyPaydaySettings, FinancialCycle, UserSettings


DEFAULT_PAYDAY_DAY = 1


def normalize_currency(currency: str = "ARS") -> str:
    value = str(currency or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", value):
        raise ValueError("currency must be a three-letter code")
    return value


def cycle_currency_scope(currency: str):
    """Legacy closed snapshots remain shared history, never live scopes."""
    return or_(
        FinancialCycle.currency == normalize_currency(currency),
        FinancialCycle.legacy_mixed_currency.is_(True),
    )


class UserSettingsRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, user_id: int, *, currency: str = "ARS"):
        currency = normalize_currency(currency)
        model = UserSettings if currency == "ARS" else CurrencyPaydaySettings
        stmt = select(model).where(model.telegram_user_id == user_id)
        if currency != "ARS":
            stmt = stmt.where(model.currency == currency)
        stmt = stmt.execution_options(populate_existing=True)
        return self.session.execute(stmt).scalar_one_or_none()

    def get_or_default(self, user_id: int, *, currency: str = "ARS"):
        """Return the row if it exists, else a detached default instance.

        Does NOT persist: callers that want to mutate must ``add()`` it
        explicitly so the absence of a row stays observable.
        """
        currency = normalize_currency(currency)
        existing = self.get(user_id, currency=currency)
        if existing is not None:
            existing.inherited = False
            return existing
        if currency != "ARS":
            base = self.get_or_default(user_id)
            inherited = CurrencyPaydaySettings(
                telegram_user_id=user_id, currency=currency,
                payday_day=base.payday_day, pay_rule=base.pay_rule,
                payday_weekday=base.payday_weekday, timezone=base.timezone,
            )
            inherited.inherited = True
            return inherited
        return UserSettings(
            telegram_user_id=user_id,
            payday_day=DEFAULT_PAYDAY_DAY,
            pay_rule="fixed_day",
            payday_weekday=None,
            timezone=None,
        )

    def materialize_currency(self, user_id: int, currency: str):
        """Freeze inherited settings atomically with the first non-ARS cycle."""
        currency = normalize_currency(currency)
        row = self.get_or_default(user_id, currency=currency)
        if currency == "ARS" or not getattr(row, "inherited", False):
            return row
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            row = self.get(user_id, currency=currency)
            if row is None:
                raise
        row.inherited = False
        return row

    def upsert_payday_day(
        self,
        user_id: int,
        payday_day: int,
        *,
        timezone: Optional[str] = None,
        currency: str = "ARS",
    ) -> UserSettings:
        """Create or update the row, setting ``payday_day`` and ``timezone``.

        Lazily creates the row the first time payday is configured.
        Returns the freshly committed row.
        """
        return self.upsert_payday_rule(
            user_id, "fixed_day", payday_day=payday_day, timezone=timezone, currency=currency
        )

    def upsert_payday_rule(
        self, user_id: int, pay_rule: str, *, weekday: Optional[int] = None,
        payday_day: Optional[int] = None, timezone: Optional[str] = None,
        currency: str = "ARS",
    ) -> UserSettings:
        """Persist a valid monthly calendar without opening a financial cycle."""
        if pay_rule == "fixed_day":
            if type(payday_day) is not int or not 1 <= payday_day <= 31:
                raise ValueError("payday_day must be between 1 and 31")
            if weekday is not None:
                raise ValueError("fixed_day must not specify weekday")
        elif pay_rule == "first_weekday":
            if type(weekday) is not int or not 0 <= weekday <= 6:
                raise ValueError("weekday must be between 0 (Monday) and 6 (Sunday)")
            if payday_day is not None:
                raise ValueError("first_weekday must not specify payday_day")
        else:
            raise ValueError("pay_rule must be fixed_day or first_weekday")
        currency = normalize_currency(currency)
        row = self.get(user_id, currency=currency)
        if row is None:
            inherited = self.get_or_default(user_id, currency=currency)
            model = UserSettings if currency == "ARS" else CurrencyPaydaySettings
            kwargs = {"currency": currency} if currency != "ARS" else {}
            row = model(
                telegram_user_id=user_id,
                payday_day=payday_day if payday_day is not None else inherited.payday_day,
                pay_rule=pay_rule,
                payday_weekday=weekday,
                timezone=timezone if timezone is not None else inherited.timezone,
                **kwargs,
            )
            self.session.add(row)
        else:
            if payday_day is not None:
                row.payday_day = payday_day
            row.pay_rule = pay_rule
            row.payday_weekday = weekday
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

    def find_open_cycle(self, user_id: int, *, currency: str = "ARS") -> Optional[FinancialCycle]:
        stmt = (
            select(FinancialCycle)
            .where(
                FinancialCycle.telegram_user_id == user_id,
                FinancialCycle.currency == normalize_currency(currency),
                FinancialCycle.status == "open",
            )
            .order_by(FinancialCycle.cycle_index.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def find_latest_cycle(self, user_id: int, *, currency: str = "ARS") -> Optional[FinancialCycle]:
        stmt = (
            select(FinancialCycle)
            .where(FinancialCycle.telegram_user_id == user_id, cycle_currency_scope(currency))
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
        self, user_id: int, limit: int = 50, *, currency: str = "ARS"
    ) -> list[FinancialCycle]:
        stmt = (
            select(FinancialCycle)
            .where(FinancialCycle.telegram_user_id == user_id, cycle_currency_scope(currency))
            .order_by(FinancialCycle.cycle_index.desc())
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars().all())

    def has_any_cycle(self, user_id: int, *, currency: str = "ARS") -> bool:
        stmt = select(func.count(FinancialCycle.id)).where(
            FinancialCycle.telegram_user_id == user_id, cycle_currency_scope(currency)
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
        pay_rule: str = "fixed_day",
        payday_weekday: Optional[int] = None,
        currency: str = "ARS",
    ) -> FinancialCycle:
        """Insert one open cycle. Caller owns the transaction.

        Re-raises the underlying IntegrityError for the partial unique
        constraint so the service layer can decide whether to retry
        after re-reading.
        """
        obj = FinancialCycle(
            telegram_user_id=user_id,
            currency=normalize_currency(currency),
            cycle_index=cycle_index,
            start_date=start_date,
            end_date=end_date,
            payday_day=payday_day,
            pay_rule=pay_rule,
            payday_weekday=payday_weekday,
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
