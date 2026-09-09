"""SQLAlchemy model for monthly per-category budgets.

A budget caps how much a user wants to spend in a specific category
within the current calendar month. The bot notifies the user when total
spending in that category crosses 80% and 100% of the configured limit.
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
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.database import Base


class Budget(Base):
    __tablename__ = "budgets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=False,
    )
    monthly_limit: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False
    )
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, default="ARS"
    )
    last_alerted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    category = relationship("Category", lazy="joined")

    __table_args__ = (
        Index(
            "uq_budget_user_category_currency_active",
            "telegram_user_id",
            "category_id",
            "currency",
            unique=True,
            sqlite_where=true(),
            postgresql_where=true(),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Budget id={self.id} cat={self.category_id if not self.category else self.category.name} "
            f"limit={self.monthly_limit} {self.currency}>"
        )