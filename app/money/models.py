"""SQLAlchemy models for the F1 money domain (R1 foundation + R2 read model).

R1 introduced ``UserSettings`` and ``FinancialCycle``. R2 layers the
read model the ``MoneyEngine`` needs:

- ``CycleIncome`` — live income rows per cycle (base + extras).
- ``SavingsTarget`` — per (cycle, currency) target (pct XOR amount).
- ``SavingsReserved`` — per (cycle, currency) explicit reservation.
- ``SavingsRealization`` — append-only history of savings actually
  transferred, identified by a stable ``source_key``.

No mutation API lives here: this module is purely the schema
mirror. ``MoneyEngine`` (in :mod:`app.money.engine`) reads these
tables but never writes them.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class UserSettings(Base):
    """One row per Telegram user. Holds payday configuration."""

    __tablename__ = "user_settings"

    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True
    )
    payday_day: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    pay_rule: Mapped[str] = mapped_column(
        String(24), nullable=False, default="fixed_day", server_default="fixed_day"
    )
    payday_weekday: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    timezone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "payday_day BETWEEN 1 AND 31",
            name="ck_user_settings_payday_day_range",
        ),
        CheckConstraint(
            "(pay_rule = 'fixed_day' AND payday_weekday IS NULL) OR "
            "(pay_rule = 'first_weekday' AND payday_weekday IS NOT NULL "
            "AND payday_weekday BETWEEN 0 AND 6)",
            name="ck_user_settings_payday_rule",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<UserSettings telegram_user_id={self.telegram_user_id} "
            f"payday_day={self.payday_day}>"
        )


class FinancialCycle(Base):
    """One row per financial cycle the user opens.

    ``closing_snapshot`` is always NULL in R1; the field exists in the
    schema because it belongs to the final approved model and R2 will
    populate it once the MoneyEngine lands.
    """

    __tablename__ = "financial_cycles"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    cycle_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    payday_day: Mapped[int] = mapped_column(Integer, nullable=False)
    # Frozen calendar metadata: settings changes affect future cycles only.
    pay_rule: Mapped[str] = mapped_column(
        String(24), nullable=False, default="fixed_day", server_default="fixed_day"
    )
    payday_weekday: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default="open"
    )
    closing_snapshot: Mapped[Optional[dict]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "telegram_user_id",
            "cycle_index",
            name="uq_financial_cycles_user_index",
        ),
        CheckConstraint(
            "status IN ('open', 'closed')",
            name="ck_financial_cycles_status",
        ),
        CheckConstraint(
            "end_date >= start_date",
            name="ck_financial_cycles_date_range",
        ),
        CheckConstraint(
            "(pay_rule = 'fixed_day' AND payday_weekday IS NULL) OR "
            "(pay_rule = 'first_weekday' AND payday_weekday IS NOT NULL "
            "AND payday_weekday BETWEEN 0 AND 6)",
            name="ck_financial_cycles_payday_rule",
        ),
        Index(
            "ix_financial_cycles_user_status_end",
            "telegram_user_id",
            "status",
            text("end_date DESC"),
        ),
        Index(
            "uq_financial_cycles_user_open",
            "telegram_user_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<FinancialCycle id={self.id} "
            f"telegram_user_id={self.telegram_user_id} "
            f"cycle_index={self.cycle_index} "
            f"{self.start_date}..{self.end_date} "
            f"status={self.status}>"
        )


# ===========================================================================
# R2 — Money engine state. Read by MoneyEngine; mutations land in R3.
# ===========================================================================


_BASE_INCOME_LIVE_PREDICATE = "kind = 'income' AND deleted_at IS NULL"


class CycleIncome(Base):
    """One income row per cycle.

    Two flavours:

    - ``kind='income'`` — the base salary for ``(cycle_id, currency)``.
      At most one live row per ``(cycle_id, currency)`` is allowed by
      the partial unique index. Soft-delete unblocks a fresh insert.
    - ``kind='extra'`` — an additional inflow (gift, freelance, refund).
      ``source_key`` is mandatory and unique per cycle: a re-issued
      ``/extra`` with the same key restores the previous row instead
      of creating a duplicate. The unique identity survives soft-delete
      so historical traceability is preserved.

    ``amount`` is always non-negative. A "negative income" is an
    expense and must be recorded via :class:`app.expenses.models.Expense`.
    """

    __tablename__ = "cycle_incomes"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    cycle_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    source_key: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["cycle_id"], ["financial_cycles.id"], ondelete="CASCADE"
        ),
        CheckConstraint("amount >= 0", name="ck_cycle_incomes_amount_nonneg"),
        CheckConstraint(
            "kind IN ('income', 'extra')", name="ck_cycle_incomes_kind_valid"
        ),
        CheckConstraint(
            "(kind <> 'extra') OR (source_key IS NOT NULL AND amount > 0)",
            name="ck_cycle_incomes_extra_requires_source_key",
        ),
        UniqueConstraint(
            "cycle_id",
            "source_key",
            name="uq_cycle_incomes_extra_source_key",
        ),
        Index(
            "uq_cycle_incomes_live_base_income",
            "cycle_id",
            "currency",
            unique=True,
            postgresql_where=text(_BASE_INCOME_LIVE_PREDICATE),
            sqlite_where=text(_BASE_INCOME_LIVE_PREDICATE),
        ),
        Index(
            "ix_cycle_incomes_cycle_currency",
            "cycle_id",
            "currency",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<CycleIncome id={self.id} cycle_id={self.cycle_id} "
            f"currency={self.currency} kind={self.kind} "
            f"amount={self.amount}>"
        )


class SavingsTarget(Base):
    """Per (cycle, currency) savings target.

    Exactly one of ``target_pct`` (0..1) and ``target_amount`` (>=0)
    is non-null; the other must be null. The DB enforces the XOR
    via a CHECK constraint, mirroring migration 0013.
    """

    __tablename__ = "savings_targets"

    cycle_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    currency: Mapped[str] = mapped_column(String(8), primary_key=True)
    target_pct: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(5, 4), nullable=True
    )
    target_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 2), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["cycle_id"], ["financial_cycles.id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            "target_pct IS NULL OR (target_pct >= 0 AND target_pct <= 1)",
            name="ck_savings_targets_pct_range",
        ),
        CheckConstraint(
            "target_amount IS NULL OR target_amount >= 0",
            name="ck_savings_targets_amount_nonneg",
        ),
        CheckConstraint(
            "(target_pct IS NULL) <> (target_amount IS NULL)",
            name="ck_savings_targets_xor",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<SavingsTarget cycle_id={self.cycle_id} "
            f"currency={self.currency} pct={self.target_pct} "
            f"amount={self.target_amount}>"
        )


class SavingsReserved(Base):
    """Per (cycle, currency) explicit savings reservation amount.

    Distinct from the historical ``SavingsRealization`` rows: this row
    captures the user's planned reservation (``/reservar`` style intent)
    while :class:`SavingsRealization` captures what was actually
    transferred to the savings account.

    Defaults to "no row ⇒ 0" in the read model so R3 can introduce the
    write API without breaking R2 reads.
    """

    __tablename__ = "savings_reserved"

    cycle_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    currency: Mapped[str] = mapped_column(String(8), primary_key=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["cycle_id"], ["financial_cycles.id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            "amount >= 0", name="ck_savings_reserved_amount_nonneg"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<SavingsReserved cycle_id={self.cycle_id} "
            f"currency={self.currency} amount={self.amount}>"
        )


class SavingsRealization(Base):
    """Append-only history of savings actually transferred.

    Identity is ``(cycle_id, source_key)`` and survives soft-delete
    (a re-issued command with the same key restores the previous row
    instead of creating a second physical row). ``amount`` is always
    strictly positive; "releasing" savings is modelled by reducing
    the ``SavingsTarget``, not by inserting a negative realization.
    """

    __tablename__ = "savings_realizations"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    cycle_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["cycle_id"], ["financial_cycles.id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            "amount > 0", name="ck_savings_realizations_amount_positive"
        ),
        UniqueConstraint(
            "cycle_id",
            "source_key",
            name="uq_savings_realizations_source_key",
        ),
        Index(
            "ix_savings_realizations_cycle_currency",
            "cycle_id",
            "currency",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<SavingsRealization id={self.id} cycle_id={self.cycle_id} "
            f"currency={self.currency} amount={self.amount}>"
        )
