"""create_expenses_table

Revision ID: 0001_create_expenses
Revises:
Create Date: 2026-01-01 00:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_create_expenses"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "expenses",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "telegram_user_id",
            sa.BigInteger(),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="ARS"),
        sa.Column("category", sa.String(length=64), nullable=False, server_default="Otros"),
        sa.Column("expense_date", sa.Date(), nullable=False),
        sa.Column("original_message", sa.String(length=4000), nullable=False, server_default=""),
        sa.Column("ai_confidence", sa.Numeric(4, 3), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_expenses_telegram_user_id", "expenses", ["telegram_user_id"]
    )
    op.create_index(
        "ix_expenses_user_date", "expenses", ["telegram_user_id", "expense_date"]
    )
    op.create_index(
        "ix_expenses_user_category",
        "expenses",
        ["telegram_user_id", "category"],
    )
    op.create_index(
        "ix_expenses_user_currency",
        "expenses",
        ["telegram_user_id", "currency"],
    )
    op.create_index("ix_expenses_date", "expenses", ["expense_date"])


def downgrade() -> None:
    op.drop_index("ix_expenses_date", table_name="expenses")
    op.drop_index("ix_expenses_user_currency", table_name="expenses")
    op.drop_index("ix_expenses_user_category", table_name="expenses")
    op.drop_index("ix_expenses_user_date", table_name="expenses")
    op.drop_index("ix_expenses_telegram_user_id", table_name="expenses")
    op.drop_table("expenses")
