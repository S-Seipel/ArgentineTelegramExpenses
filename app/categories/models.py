"""Category model — categories live in their own table and ``expenses`` points
to them via a foreign key.

The tree structure is preserved with a self-referencing ``parent_id``. The
``name`` is the human-readable label (e.g. ``"Café"``) and is unique within
its parent.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.database import Base


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    icon: Mapped[str] = mapped_column(String(16), nullable=True)
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

    parent: Mapped["Category"] = relationship(
        "Category",
        remote_side="Category.id",
        backref="children",
        uselist=False,
    )

    __table_args__ = (
        UniqueConstraint("parent_id", "name", name="uq_categories_parent_name"),
        Index("ix_categories_parent", "parent_id"),
    )

    def __repr__(self) -> str:
        return f"<Category id={self.id} name={self.name!r}>"

    @property
    def display(self) -> str:
        if self.parent is not None:
            return f"{self.parent.name} > {self.name}"
        return self.name
