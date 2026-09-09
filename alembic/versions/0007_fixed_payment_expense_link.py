"""linked_expense on fixed_expense_payments

Revision ID: 0007_fixed_payment_expense_link
Revises: 0006_fixed_expenses
Create Date: 2026-09-09 14:30:00

Adds ``expense_id`` column to ``fixed_expense_payments`` so that marking
a fixed expense as paid also persists the corresponding real expense
row. This keeps the dashboard's totals (which read from the ``expenses``
table) in sync with the user's checkmarks.

ON DELETE SET NULL: deleting the linked expense (e.g. via the regular
expenses editor) just clears the link, the payment record stays as a
historical receipt.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_fixed_payment_expense_link"
down_revision: Union[str, None] = "0006_fixed_expenses"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "fixed_expense_payments",
        sa.Column("expense_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_fixed_payment_expense",
        "fixed_expense_payments",
        "expenses",
        ["expense_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_fixed_payment_expense",
        "fixed_expense_payments",
        ["expense_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_fixed_payment_expense", table_name="fixed_expense_payments")
    op.drop_constraint(
        "fk_fixed_payment_expense", "fixed_expense_payments", type_="foreignkey"
    )
    op.drop_column("fixed_expense_payments", "expense_id")
