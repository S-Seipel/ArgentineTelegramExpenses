"""Explicit Telegram access and presentation preferences.

Financial records retain their existing Telegram user IDs. A profile is
created only for the configured owner or when that owner authorizes a user.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Index, String, Text, false, func, text, true
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class AppUser(Base):
    __tablename__ = "app_users"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(12), nullable=False, default="user", server_default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    language: Mapped[str] = mapped_column(String(2), nullable=False, default="es", server_default="es")
    voice_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="America/Argentina/Buenos_Aires", server_default="America/Argentina/Buenos_Aires")
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("telegram_user_id > 0", name="ck_app_users_positive_id"),
        CheckConstraint("role IN ('owner','user')", name="ck_app_users_role"),
        CheckConstraint("language IN ('es','en','pt')", name="ck_app_users_language"),
        CheckConstraint("length(timezone) BETWEEN 1 AND 128", name="ck_app_users_timezone"),
        CheckConstraint("role <> 'owner' OR is_active", name="ck_app_users_owner_active"),
        Index("uq_app_users_single_owner", "role", unique=True,
              postgresql_where=text("role = 'owner'"), sqlite_where=text("role = 'owner'")),
    )
