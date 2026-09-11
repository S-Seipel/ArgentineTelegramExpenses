from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

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
    text,
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

    # Foundation columns added in migration 0008. ``source_type``
    # identifies how the row was produced. The migration sets a
    # TEMPORARY ``DEFAULT 'legacy'`` while adding the column so
    # existing rows backfill, then drops the default before the
    # migration closes — application code is the only authority on
    # the value for new rows. The model default below matches what
    # the ORM issues when callers omit ``source_type``; combined with
    # NOT NULL this keeps the type declarative. In production the
    # service layer always sets it explicitly.
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="legacy"
    )
    source_key: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    telegram_chat_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    telegram_message_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    # When set, the row is soft-deleted: business reads MUST filter it out,
    # but it stays around so the historical link with a fixed payment or a
    # future undo flow can be reconstructed.
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Bumped every time the row is re-touched (re-pay after unpay, edit,
    # soft-delete / restore). >= 1 by backfill.
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
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
        Index("ix_expenses_deleted_at", "deleted_at"),
        # Partial unique index: when a non-null source_key is assigned, the
        # pair (user, source_key) must be unique. NULL source_keys (legacy
        # rows) are excluded so they don't conflict with each other.
        Index(
            "uq_expenses_user_source_key",
            "telegram_user_id",
            "source_key",
            unique=True,
            sqlite_where=text("source_key IS NOT NULL"),
            postgresql_where=text("source_key IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Expense id={self.id} name={self.name!r} "
            f"amount={self.amount} {self.currency} date={self.expense_date}>"
        )
