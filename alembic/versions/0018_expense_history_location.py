"""Durable expense undo history and opt-in coordinates.

Revision ID: 0018_expense_history_location
Revises: 0017_currency_cycles
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0018_expense_history_location"
down_revision = "0017_currency_cycles"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("expenses", sa.Column("latitude", sa.Numeric(10, 7), nullable=True))
    op.add_column("expenses", sa.Column("longitude", sa.Numeric(10, 7), nullable=True))
    op.add_column("expenses", sa.Column("location_recorded_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("ck_expenses_location", "expenses",
        "(latitude IS NULL AND longitude IS NULL AND location_recorded_at IS NULL) OR "
        "(latitude IS NOT NULL AND longitude IS NOT NULL AND location_recorded_at IS NOT NULL "
        "AND latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180)")
    op.create_table("expense_changes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("expense_id", sa.Integer(), sa.ForeignKey("expenses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("before_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("after_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("before_revision", sa.Integer(), nullable=False),
        sa.Column("after_revision", sa.Integer(), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("action IN ('amount','name','delete','restore','location')", name="ck_expense_changes_action"),
        sa.CheckConstraint("status IN ('pending','undone','expired')", name="ck_expense_changes_status"),
        sa.CheckConstraint("before_revision >= 1 AND after_revision > before_revision AND expected_revision >= after_revision", name="ck_expense_changes_revision"),
    )
    op.create_index("ix_expense_changes_user_pending", "expense_changes", ["telegram_user_id", "status", "id"])
    op.create_index("ix_expense_changes_expense", "expense_changes", ["expense_id"])


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM expense_changes LIMIT 1")).first() or bind.execute(sa.text(
        "SELECT 1 FROM expenses WHERE latitude IS NOT NULL OR longitude IS NOT NULL OR location_recorded_at IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError("Cannot downgrade while expense history or locations exist; restoring the pre-upgrade backup preserves that data.")
    op.drop_index("ix_expense_changes_expense", table_name="expense_changes")
    op.drop_index("ix_expense_changes_user_pending", table_name="expense_changes")
    op.drop_table("expense_changes")
    op.drop_constraint("ck_expenses_location", "expenses", type_="check")
    op.drop_column("expenses", "location_recorded_at")
    op.drop_column("expenses", "longitude")
    op.drop_column("expenses", "latitude")
