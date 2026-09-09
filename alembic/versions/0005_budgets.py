"""budgets

Revision ID: 0005_budgets
Revises: 0004_recurring
Create Date: 2026-09-09 12:00:00

Adds a ``budgets`` table to store per-category monthly spending limits.
The bot notifies the user when their monthly spending in a category
crosses 80% and 100% of the configured limit.

Only one active budget per (user, category, currency) at a time; we
enforce that with a partial unique index.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_budgets"
down_revision: Union[str, None] = "0004_recurring"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "budgets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "telegram_user_id",
            sa.BigInteger(),
            nullable=False,
        ),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column(
            "monthly_limit",
            sa.Numeric(18, 2),
            nullable=False,
        ),
        sa.Column(
            "currency",
            sa.String(length=8),
            nullable=False,
            server_default="ARS",
        ),
        sa.Column(
            "last_alerted_at",
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
        "ix_budgets_user_id",
        "budgets",
        ["telegram_user_id"],
    )
    # Partial unique index: only one active budget per (user, category, currency).
    op.create_index(
        "uq_budget_user_category_currency_active",
        "budgets",
        ["telegram_user_id", "category_id", "currency"],
        unique=True,
        postgresql_where=sa.text("is_active = TRUE"),
        sqlite_where=sa.text("is_active = 1"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_budget_user_category_currency_active", table_name="budgets"
    )
    op.drop_index("ix_budgets_user_id", table_name="budgets")
    op.drop_table("budgets")
