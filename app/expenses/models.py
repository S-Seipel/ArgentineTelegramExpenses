from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
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


class Expense(Base):
    __tablename__ = "expenses"

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
        index=True,
    )
    expense_date: Mapped[date] = mapped_column(Date, nullable=False)
    original_message: Mapped[str] = mapped_column(
        String(4000), nullable=False, default=""
    )
    ai_confidence: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, default=Decimal("0")
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

    category: Mapped["Category"] = relationship(  # noqa: F821
        "Category",
        lazy="joined",
        backref="expenses",
    )

    __table_args__ = (
        Index("ix_expenses_user_date", "telegram_user_id", "expense_date"),
        Index("ix_expenses_user_category", "telegram_user_id", "category_id"),
        Index("ix_expenses_user_currency", "telegram_user_id", "currency"),
        Index("ix_expenses_date", "expense_date"),
    )

    def __repr__(self) -> str:
        return (
            f"<Expense id={self.id} name={self.name!r} "
            f"amount={self.amount} {self.currency} date={self.expense_date}>"
        )
