"""Monthly fixed-day or first-weekday payday rules.

Revision ID: 0015_payday_calendar_rule
Revises: 0014_recurring_obligations

Existing settings and cycles retain fixed_day semantics. No dates,
closing snapshots, incomes or savings are changed.
"""
from alembic import op
import sqlalchemy as sa


revision = "0015_payday_calendar_rule"
down_revision = "0014_recurring_obligations"
branch_labels = None
depends_on = None


_RULE_CHECK = (
    "(pay_rule = 'fixed_day' AND payday_weekday IS NULL) OR "
    "(pay_rule = 'first_weekday' AND payday_weekday IS NOT NULL "
    "AND payday_weekday BETWEEN 0 AND 6)"
)


def upgrade() -> None:
    for table in ("user_settings", "financial_cycles"):
        op.add_column(table, sa.Column(
            "pay_rule", sa.String(24), nullable=False, server_default="fixed_day",
        ))
        op.add_column(table, sa.Column("payday_weekday", sa.Integer(), nullable=True))
        op.create_check_constraint(f"ck_{table}_payday_rule", table, _RULE_CHECK)


def downgrade() -> None:
    for table in ("financial_cycles", "user_settings"):
        op.drop_constraint(f"ck_{table}_payday_rule", table, type_="check")
        op.drop_column(table, "payday_weekday")
        op.drop_column(table, "pay_rule")
