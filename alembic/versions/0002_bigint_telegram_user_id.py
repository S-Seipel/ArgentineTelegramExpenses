"""bigint_telegram_user_id

Revision ID: 0002_bigint_telegram_user_id
Revises: 0001_create_expenses
Create Date: 2026-08-19 12:00:00

Telegram user IDs are 64-bit (they comfortably exceed PostgreSQL's 32-bit
INTEGER range of ~2.1 billion). This migration widens the column to BIGINT,
which is the correct type for fresh installs going forward.

The original 0001 migration already declares BIGINT. Running this migration
on a fresh database is effectively a no-op (BIGINT → BIGINT). On installs
that applied the old INTEGER version of 0001, this performs the safe
``ALTER COLUMN ... TYPE BIGINT`` to bring the schema in line.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_bigint_telegram_user_id"
down_revision: Union[str, None] = "0001_create_expenses"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "expenses",
        "telegram_user_id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
        postgresql_using="telegram_user_id::bigint",
    )


def downgrade() -> None:
    op.alter_column(
        "expenses",
        "telegram_user_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using="telegram_user_id::integer",
    )
