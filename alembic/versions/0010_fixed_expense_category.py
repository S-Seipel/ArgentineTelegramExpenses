"""add a dedicated category for fixed-expense payment mirrors

Revision ID: 0010_fixed_expense_category
Revises: 0009_expense_name_key
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_fixed_expense_category"
down_revision: Union[str, None] = "0009_expense_name_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            INSERT INTO categories (name, parent_id, is_active)
            SELECT 'Gastos fijos', NULL, TRUE
            WHERE NOT EXISTS (
                SELECT 1 FROM categories
                WHERE name = 'Gastos fijos' AND parent_id IS NULL
            )
            """
        )
    )
    fixed_id = conn.execute(
        sa.text(
            "SELECT id FROM categories "
            "WHERE name = 'Gastos fijos' AND parent_id IS NULL"
        )
    ).scalar_one()

    # Existing fixed payments are linked through the payment table, which is
    # more reliable than source_type for rows created before migration 0008.
    conn.execute(
        sa.text(
            """
            UPDATE expenses
            SET category_id = :category_id
            WHERE EXISTS (
                SELECT 1 FROM fixed_expense_payments p
                WHERE p.expense_id = expenses.id
            )
            """
        ),
        {"category_id": fixed_id},
    )


def downgrade() -> None:
    conn = op.get_bind()
    otros_id = conn.execute(
        sa.text(
            "SELECT id FROM categories "
            "WHERE name = 'Otros' AND parent_id IS NULL"
        )
    ).scalar_one()
    fixed_id = conn.execute(
        sa.text(
            "SELECT id FROM categories "
            "WHERE name = 'Gastos fijos' AND parent_id IS NULL"
        )
    ).scalar_one_or_none()
    if fixed_id is None:
        return
    conn.execute(
        sa.text(
            "UPDATE expenses SET category_id = :otros_id "
            "WHERE category_id = :fixed_id"
        ),
        {"otros_id": otros_id, "fixed_id": fixed_id},
    )
    conn.execute(
        sa.text(
            "DELETE FROM categories WHERE id = :fixed_id"
        ),
        {"fixed_id": fixed_id},
    )
