"""recurring_expenses

Revision ID: 0004_recurring
Revises: 0003_categories_and_fk
Create Date: 2026-08-21 13:30:00

Adds a ``recurring_expenses`` table to store reminders/templates for
periodic charges (Netflix, Spotify, dominios, etc.). When the user
confirms a reminder the service creates a real row in ``expenses`` and
advances ``next_due_date``.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_recurring"
down_revision: Union[str, None] = "0003_categories_and_fk"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recurring_expenses",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "telegram_user_id",
            sa.BigInteger(),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=8),
            nullable=False,
            server_default="ARS",
        ),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("frequency", sa.String(length=16), nullable=False),
        sa.Column("day_of_month", sa.Integer(), nullable=False),
        sa.Column("month_of_year", sa.Integer(), nullable=True),
        sa.Column("next_due_date", sa.Date(), nullable=False),
        sa.Column(
            "last_notified_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["category_id"], ["categories.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_recurring_user_id",
        "recurring_expenses",
        ["telegram_user_id"],
    )
    op.create_index(
        "ix_recurring_user_next_due",
        "recurring_expenses",
        ["telegram_user_id", "next_due_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_recurring_user_next_due", table_name="recurring_expenses")
    op.drop_index("ix_recurring_user_id", table_name="recurring_expenses")
    op.drop_table("recurring_expenses")