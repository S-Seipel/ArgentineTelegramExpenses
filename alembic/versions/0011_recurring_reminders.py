"""persist one pending reminder per recurring occurrence

Revision ID: 0011_recurring_reminders
Revises: 0010_fixed_expense_category
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0011_recurring_reminders"
down_revision: Union[str, None] = "0010_fixed_expense_category"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recurring_reminders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("recurring_id", sa.Integer(), nullable=False),
        sa.Column("occurrence_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "delivery_status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["recurring_id"], ["recurring_expenses.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "telegram_user_id",
            "recurring_id",
            "occurrence_date",
            name="uq_recurring_reminder_occurrence",
        ),
    )
    op.create_index(
        "ix_recurring_reminders_telegram_user_id",
        "recurring_reminders",
        ["telegram_user_id"],
    )
    op.create_index(
        "ix_recurring_reminders_recurring_id",
        "recurring_reminders",
        ["recurring_id"],
    )
    op.create_index(
        "ix_recurring_reminder_pending_user",
        "recurring_reminders",
        ["telegram_user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recurring_reminder_pending_user",
        table_name="recurring_reminders",
    )
    op.drop_index(
        "ix_recurring_reminders_recurring_id",
        table_name="recurring_reminders",
    )
    op.drop_index(
        "ix_recurring_reminders_telegram_user_id",
        table_name="recurring_reminders",
    )
    op.drop_table("recurring_reminders")
