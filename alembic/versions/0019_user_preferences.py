"""Explicit user allowlist and language/voice/timezone preferences.

Revision ID: 0019_user_preferences
Revises: 0018_expense_history_location

No user is enrolled by a schema migration. Application startup seeds only
the configured legacy owner. Financial records and their user IDs remain
unchanged. A nonempty allowlist cannot be discarded by downgrade.
"""
from alembic import op
import sqlalchemy as sa

revision = "0019_user_preferences"
down_revision = "0018_expense_history_location"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_users",
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(12), nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("language", sa.String(2), nullable=False, server_default="es"),
        sa.Column("voice_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("timezone", sa.Text(), nullable=False, server_default="America/Argentina/Buenos_Aires"),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("telegram_user_id", name="pk_app_users"),
        sa.CheckConstraint("telegram_user_id > 0", name="ck_app_users_positive_id"),
        sa.CheckConstraint("role IN ('owner','user')", name="ck_app_users_role"),
        sa.CheckConstraint("language IN ('es','en','pt')", name="ck_app_users_language"),
        sa.CheckConstraint("length(timezone) BETWEEN 1 AND 128", name="ck_app_users_timezone"),
        sa.CheckConstraint("role <> 'owner' OR is_active", name="ck_app_users_owner_active"),
    )
    op.create_index("uq_app_users_single_owner", "app_users", ["role"], unique=True,
                    postgresql_where=sa.text("role = 'owner'"), sqlite_where=sa.text("role = 'owner'"))


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM app_users LIMIT 1")).first():
        raise RuntimeError("Cannot discard user access or preferences during downgrade; restore the matching backup instead.")
    op.drop_index("uq_app_users_single_owner", table_name="app_users")
    op.drop_table("app_users")
