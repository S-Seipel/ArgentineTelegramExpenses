"""SQLAlchemy models for fixed monthly expenses + per-month payment status.

Two tables:
- ``fixed_expenses``: the recurring bill template (name, expected amount,
  payment method, due day).
- ``fixed_expense_payments``: per-month payment record (paid_at, actual
  amount, note). Unique on (fixed_expense_id, month_year).

A third table ``monthly_budget`` holds the user's monthly income and
extra to compute the month's "liberado" (income - extra - spent fixed).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.database import Base


class FixedExpense(Base):
    __tablename__ = "fixed_expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    expected_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False
    )
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, default="ARS"
    )
    category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    payment_method: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    due_day_of_month: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
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

    payments = relationship(
        "FixedExpensePayment",
        back_populates="fixed_expense",
        cascade="all, delete-orphan",
    )
    category = relationship("Category", lazy="joined")

    __table_args__ = (
        Index(
            "ix_fixed_expense_user_active",
            "telegram_user_id",
            "is_active",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<FixedExpense id={self.id} name={self.name!r} "
            f"amount={self.expected_amount} {self.currency}>"
        )


class FixedExpensePayment(Base):
    """One row per (fixed_expense, month). None if not paid."""

    __tablename__ = "fixed_expense_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fixed_expense_id: Mapped[int] = mapped_column(
        ForeignKey("fixed_expenses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    month_year: Mapped[str] = mapped_column(
        String(7), nullable=False
    )  # 'YYYY-MM'
    paid_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    actual_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 2), nullable=True
    )
    note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    skipped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    expense_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("expenses.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
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

    fixed_expense = relationship("FixedExpense", back_populates="payments")

    __table_args__ = (
        UniqueConstraint(
            "fixed_expense_id", "month_year",
            name="uq_payment_per_month",
        ),
        Index(
            "ix_payment_month",
            "month_year",
        ),
    )


class MonthlyBudget(Base):
    """User-defined income / extra for a given month."""

    __tablename__ = "monthly_budget"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )
    month_year: Mapped[str] = mapped_column(String(7), nullable=False)
    income: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    extra: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
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

    __table_args__ = (
        UniqueConstraint(
            "telegram_user_id", "month_year",
            name="uq_budget_per_month_user",
        ),
    )
