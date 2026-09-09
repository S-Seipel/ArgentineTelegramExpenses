"""fixed_expenses + payments + monthly_budget

Revision ID: 0006_fixed_expenses
Revises: 0005_budgets
Create Date: 2026-09-09 14:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_fixed_expenses"
down_revision: Union[str, None] = "0005_budgets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fixed_expenses",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("expected_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=8),
            nullable=False,
            server_default="ARS",
        ),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("payment_method", sa.String(length=32), nullable=True),
        sa.Column(
            "due_day_of_month",
            sa.Integer(),
            nullable=False,
            server_default="1",
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
            ["category_id"], ["categories.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_fixed_expense_user_id", "fixed_expenses", ["telegram_user_id"]
    )
    op.create_index(
        "ix_fixed_expense_user_active",
        "fixed_expenses",
        ["telegram_user_id", "is_active"],
    )

    op.create_table(
        "fixed_expense_payments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("fixed_expense_id", sa.Integer(), nullable=False),
        sa.Column("month_year", sa.String(length=7), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column(
            "skipped",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
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
            ["fixed_expense_id"],
            ["fixed_expenses.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "fixed_expense_id", "month_year", name="uq_payment_per_month"
        ),
    )
    op.create_index(
        "ix_payment_fixed_expense_id",
        "fixed_expense_payments",
        ["fixed_expense_id"],
    )
    op.create_index(
        "ix_payment_month", "fixed_expense_payments", ["month_year"]
    )

    op.create_table(
        "monthly_budget",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("month_year", sa.String(length=7), nullable=False),
        sa.Column(
            "income",
            sa.Numeric(18, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "extra",
            sa.Numeric(18, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column("note", sa.String(length=500), nullable=True),
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
        sa.UniqueConstraint(
            "telegram_user_id", "month_year", name="uq_budget_per_month_user"
        ),
    )
    op.create_index(
        "ix_budget_user_id", "monthly_budget", ["telegram_user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_budget_user_id", table_name="monthly_budget")
    op.drop_table("monthly_budget")
    op.drop_index("ix_payment_month", table_name="fixed_expense_payments")
    op.drop_index("ix_payment_fixed_expense_id", table_name="fixed_expense_payments")
    op.drop_table("fixed_expense_payments")
    op.drop_index("ix_fixed_expense_user_active", table_name="fixed_expenses")
    op.drop_index("ix_fixed_expense_user_id", table_name="fixed_expenses")
    op.drop_table("fixed_expenses")
