"""Keep calendar-month budget history under an explicit legacy table name.

Revision ID: 0016_legacy_monthly_budget
Revises: 0015_payday_calendar_rule

Renaming preserves rows, IDs, sequence ownership, timestamps, indexes and
constraints. No legacy income is copied into financial cycles.
"""
from alembic import op


revision = "0016_legacy_monthly_budget"
down_revision = "0015_payday_calendar_rule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("monthly_budget", "monthly_budget_legacy")


def downgrade() -> None:
    op.rename_table("monthly_budget_legacy", "monthly_budget")
