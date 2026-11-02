"""Opt-in recurring commitments and a stable financial occurrence date.

Revision ID: 0014_recurring_obligations
Revises: 0013_money_engine_state

Existing reminders remain ordinary reminders. No historical expense or
closed financial snapshot is changed by this migration.
"""
from alembic import op
import sqlalchemy as sa


revision = "0014_recurring_obligations"
down_revision = "0013_money_engine_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recurring_expenses",
        sa.Column("is_obligation", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "recurring_expenses",
        sa.Column("obligation_due_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recurring_expenses", "obligation_due_date")
    op.drop_column("recurring_expenses", "is_obligation")
