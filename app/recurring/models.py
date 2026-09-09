"""SQLAlchemy model for recurring expense templates.

A row represents a *template* the bot will remind the user about, not an
actual expense. When the user confirms the reminder, the service creates a
real ``Expense`` row and advances ``next_due_date`` to the next period.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.database import Base


class RecurringExpense(Base):
    __tablename__ = "recurring_expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="ARS")
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=False,
    )
    frequency: Mapped[str] = mapped_column(String(16), nullable=False)
    day_of_month: Mapped[int] = mapped_column(Integer, nullable=False)
    month_of_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    next_due_date: Mapped[date] = mapped_column(Date, nullable=False)
    last_notified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    category = relationship("Category", lazy="joined")

    __table_args__ = (
        Index(
            "ix_recurring_user_next_due",
            "telegram_user_id",
            "next_due_date",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<RecurringExpense id={self.id} name={self.name!r} "
            f"amount={self.amount} {self.currency} freq={self.frequency} "
            f"next={self.next_due_date}>"
        )