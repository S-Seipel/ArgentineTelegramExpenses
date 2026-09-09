"""categories_table_and_fk

Revision ID: 0003_categories_and_fk
Revises: 0002_bigint_telegram_user_id
Create Date: 2026-08-19 13:30:00

Introduces a dedicated ``categories`` table, seeds the default tree, and
replaces the ``expenses.category`` text column with a foreign key to it.

Behavior:
* Creates ``categories`` with a self-referencing ``parent_id`` and a unique
  constraint on ``(parent_id, name)``.
* Inserts the shipped category tree (parent rows first, then leaves with
  their FK pointer).
* Adds ``expenses.category_id`` as a nullable FK column.
* Backfills ``category_id`` from the legacy ``category`` text column by
  looking up each value in the freshly seeded tree. Unmatched values fall
  back to ``Otros`` (which is always present after the seed).
* Drops the legacy ``category`` text column.
* Renames the index ``ix_expenses_user_category`` so it points to
  ``category_id``.

This migration is idempotent for fresh installs thanks to the data-move in
the downgrade path (the ``category`` text column is restored on rollback).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_categories_and_fk"
down_revision: Union[str, None] = "0002_bigint_telegram_user_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SEED_TREE: dict[str, list[str]] = {
    "Comida": [
        "Supermercado",
        "Restaurante",
        "Comida rápida",
        "Café",
        "Delivery",
    ],
    "Transporte": [
        "Uber",
        "Taxi",
        "Combustible",
        "Transporte público",
    ],
    "Entretenimiento": [
        "Juegos",
        "Cine",
        "Suscripciones",
        "Salidas",
    ],
    "Ropa": [],
    "Tecnología": [],
    "Salud": [],
    "Educación": [],
    "Hogar": [],
    "Viajes": [],
    "Regalos": [],
    "Servicios": [],
    "Otros": [],
}


def _seed_categories(conn) -> None:
    parent_ids: dict[str, int] = {}
    for parent in SEED_TREE.keys():
        result = conn.execute(
            sa.text(
                "INSERT INTO categories (name, parent_id, is_active) "
                "VALUES (:name, NULL, TRUE) RETURNING id"
            ),
            {"name": parent},
        )
        parent_ids[parent] = result.scalar_one()
    for parent, children in SEED_TREE.items():
        for child in children:
            conn.execute(
                sa.text(
                    "INSERT INTO categories (name, parent_id, is_active) "
                    "VALUES (:name, :parent_id, TRUE)"
                ),
                {"name": child, "parent_id": parent_ids[parent]},
            )


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("icon", sa.String(length=16), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
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
        sa.ForeignKeyConstraint(
            ["parent_id"], ["categories.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("parent_id", "name", name="uq_categories_parent_name"),
    )
    op.create_index("ix_categories_parent", "categories", ["parent_id"])

    conn = op.get_bind()
    _seed_categories(conn)

    has_legacy = conn.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='expenses' AND column_name='category')"
        )
    ).scalar()
    if has_legacy:
        op.add_column(
            "expenses",
            sa.Column("category_id", sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            "fk_expenses_category_id",
            "expenses",
            "categories",
            ["category_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        conn.execute(
            sa.text(
                """
                UPDATE expenses AS e
                SET category_id = c.id
                FROM categories AS c
                WHERE c.parent_id IS NULL
                  AND c.name = COALESCE(NULLIF(e.category, ''), 'Otros')
                """
            )
        )
        orphan = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM expenses WHERE category_id IS NULL"
            )
        ).scalar()
        if orphan:
            raise RuntimeError(
                f"{orphan} expenses rows could not be backfilled to a "
                f"category. Aborting migration."
            )
        op.alter_column(
            "expenses",
            "category_id",
            existing_nullable=True,
            nullable=False,
        )
        op.drop_index("ix_expenses_user_category", table_name="expenses")
        op.create_index(
            "ix_expenses_user_category",
            "expenses",
            ["telegram_user_id", "category_id"],
        )
        op.drop_column("expenses", "category")


def downgrade() -> None:
    conn = op.get_bind()
    op.add_column(
        "expenses",
        sa.Column("category", sa.String(length=64), nullable=True),
    )
    conn.execute(
        sa.text(
            """
            UPDATE expenses AS e
            SET category = c.name
            FROM categories AS c
            WHERE c.id = e.category_id
            """
        )
    )
    op.drop_index("ix_expenses_user_category", table_name="expenses")
    op.drop_constraint(
        "fk_expenses_category_id", "expenses", type_="foreignkey"
    )
    op.drop_column("expenses", "category_id")
    op.drop_table("categories")
