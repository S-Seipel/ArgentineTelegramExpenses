"""persist normalized expense names for safe grouping

Revision ID: 0009_expense_name_key
Revises: 0008_expense_foundation
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.expenses.names import normalize_expense_name

revision: str = "0009_expense_name_key"
down_revision: Union[str, None] = "0008_expense_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "expenses",
        sa.Column(
            "name_key",
            sa.String(length=255),
            nullable=True,
            server_default="",
        ),
    )

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, name FROM expenses")).mappings().all()
    for row in rows:
        bind.execute(
            sa.text("UPDATE expenses SET name_key = :name_key WHERE id = :id"),
            {
                "id": row["id"],
                "name_key": normalize_expense_name(row["name"]),
            },
        )

    with op.batch_alter_table("expenses") as batch:
        batch.alter_column("name_key", nullable=False)

    op.create_index("ix_expenses_name_key", "expenses", ["name_key"])


def downgrade() -> None:
    op.drop_index("ix_expenses_name_key", table_name="expenses")
    op.drop_column("expenses", "name_key")
