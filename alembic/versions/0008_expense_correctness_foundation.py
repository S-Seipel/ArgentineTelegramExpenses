"""expense correctness foundation: source tracking + soft delete

Revision ID: 0008_expense_foundation
Revises: 0007_fixed_payment_expense_link
Create Date: 2026-09-10 09:30:00

Adds to ``expenses``:
- ``source_type``      identifies how the row was produced. The server
                        default is set ONLY during the migration so
                        pre-existing rows can backfill as ``"legacy"``
                        atomically with the column add. The default is
                        dropped before the migration completes, so
                        application code MUST set ``source_type``
                        explicitly on every new insert. A silent
                        default would let a future regression slip
                        a row past the application without anyone
                        noticing.
- ``source_key``       nullable. Stable identifier within the user's
                        ownership so a logical event can't create two
                        physical rows. NULL for legacy rows.
- ``telegram_chat_id`` nullable BigInt. Not backfilled.
- ``telegram_message_id`` nullable BigInt. Not backfilled.
- ``deleted_at``       nullable timestamp. Foundation for soft delete:
                        reads must filter ``deleted_at IS NULL`` even
                        though no UI ships an Undo button yet.
- ``revision``         NOT NULL, default ``1``. Bumped every time the
                        row is re-touched (re-pay, edit, soft
                        delete/restore). The default stays because
                        ``revision = 1`` is the only sane initial
                        value and there is no semantic risk in
                        letting the DB fill it.

Adds a partial unique index ``uq_expenses_user_source_key`` on
``(telegram_user_id, source_key)`` that applies only when ``source_key``
is NOT NULL. Multiple legacy rows (source_key NULL) coexist freely;
once a key is assigned, the pair is unique per user.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_expense_foundation"
down_revision: Union[str, None] = "0007_fixed_payment_expense_link"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PARTIAL_FILTER = "source_key IS NOT NULL"


def upgrade() -> None:
    # Phase 0A: add the column WITH a temporary server default so that
    # every existing row backfills to ``"legacy"`` in the same DDL
    # statement (NOT NULL + DEFAULT is atomic on Postgres).
    op.add_column(
        "expenses",
        sa.Column(
            "source_type",
            sa.String(length=32),
            nullable=False,
            server_default="legacy",
        ),
    )

    # Phase 0B: defensive explicit UPDATE for any rows that might have
    # raced in between the ADD COLUMN and now, or for any DB engine
    # where the server default didn't fire. Idempotent.
    op.execute(
        "UPDATE expenses SET source_type = 'legacy' "
        "WHERE source_type IS NULL OR source_type = ''"
    )

    op.add_column(
        "expenses",
        sa.Column("source_key", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "expenses",
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "expenses",
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "expenses",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "expenses",
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.create_index(
        "ix_expenses_deleted_at", "expenses", ["deleted_at"]
    )
    op.create_index(
        "uq_expenses_user_source_key",
        "expenses",
        ["telegram_user_id", "source_key"],
        unique=True,
        postgresql_where=sa.text(_PARTIAL_FILTER),
        sqlite_where=sa.text(_PARTIAL_FILTER),
    )

    # Phase 0C: drop the temporary server default. From this point on,
    # every new INSERT MUST provide ``source_type``; the application
    # layer is the single source of truth for that value, and the DB
    # will reject any row that omits it.
    op.alter_column(
        "expenses",
        "source_type",
        server_default=None,
    )


def downgrade() -> None:
    # Downgrade restores the column WITHOUT a server default. If we
    # re-upgrade later, the upgrade path adds the default back
    # temporarily and drops it again at the end.
    op.drop_index(
        "uq_expenses_user_source_key", table_name="expenses"
    )
    op.drop_index("ix_expenses_deleted_at", table_name="expenses")
    op.drop_column("expenses", "revision")
    op.drop_column("expenses", "deleted_at")
    op.drop_column("expenses", "telegram_message_id")
    op.drop_column("expenses", "telegram_chat_id")
    op.drop_column("expenses", "source_key")
    op.drop_column("expenses", "source_type")
