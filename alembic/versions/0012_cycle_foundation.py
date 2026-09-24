"""cycle foundation: user_settings + financial_cycles (F1 R1)

Revision ID: 0012_cycle_foundation
Revises: 0011_recurring_reminders

Creates the two tables required to represent and open financial cycles
according to a configurable payday. R1 introduces NO financial math yet:
``financial_cycles.closing_snapshot`` exists in the schema because it
belongs to the final approved model, but it is always NULL until R2
implements the snapshot when the MoneyEngine lands.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0012_cycle_foundation"
down_revision: Union[str, None] = "0011_recurring_reminders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OPEN_CYCLE_FILTER = "status = 'open'"


def upgrade() -> None:
    op.create_table(
        "user_settings",
        sa.Column(
            "telegram_user_id",
            sa.BigInteger(),
            nullable=False,
        ),
        sa.Column(
            "payday_day",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("timezone", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint(
            "telegram_user_id", name="pk_user_settings"
        ),
        sa.CheckConstraint(
            "payday_day BETWEEN 1 AND 31",
            name="ck_user_settings_payday_day_range",
        ),
    )

    op.create_table(
        "financial_cycles",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("cycle_index", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("payday_day", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="open",
        ),
        sa.Column("closing_snapshot", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "closed_at",
            sa.DateTime(timezone=True),
            nullable=True,
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
        sa.PrimaryKeyConstraint("id", name="pk_financial_cycles"),
        sa.UniqueConstraint(
            "telegram_user_id",
            "cycle_index",
            name="uq_financial_cycles_user_index",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'closed')",
            name="ck_financial_cycles_status",
        ),
        sa.CheckConstraint(
            "end_date >= start_date",
            name="ck_financial_cycles_date_range",
        ),
    )

    op.create_index(
        "ix_financial_cycles_user_status_end",
        "financial_cycles",
        ["telegram_user_id", "status", sa.text("end_date DESC")],
    )

    op.create_index(
        "uq_financial_cycles_user_open",
        "financial_cycles",
        ["telegram_user_id"],
        unique=True,
        postgresql_where=sa.text(_OPEN_CYCLE_FILTER),
        sqlite_where=sa.text(_OPEN_CYCLE_FILTER),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_financial_cycles_user_open", table_name="financial_cycles"
    )
    op.drop_index(
        "ix_financial_cycles_user_status_end", table_name="financial_cycles"
    )
    op.drop_table("financial_cycles")
    op.drop_table("user_settings")
