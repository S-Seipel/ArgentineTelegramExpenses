"""money engine state: cycle_incomes, savings_targets, savings_reserved, savings_realizations (F1 R2)

Revision ID: 0013_money_engine_state
Revises: 0012_cycle_foundation

R2 introduces the read model the MoneyEngine needs:

- ``cycle_incomes``: live income rows per cycle, partitioned by currency.
  One row per ``(cycle_id, currency)`` for ``kind='income'`` (UNIQUE
  live, with the open currency pool naturally preventing two ``ARS``
  base salaries in the same cycle). Extras use a per-source_key
  identity that survives soft-delete so re-issuing an idempotent
  ``/extra`` with the same key restores the previous row.
- ``savings_targets``: one row per ``(cycle_id, currency)`` storing the
  user-configured target either as a percentage or an absolute amount,
  never both.
- ``savings_reserved``: one row per ``(cycle_id, currency)`` storing the
  amount explicitly set aside. R3 will own the write API; for now the
  read engine tolerates ``None`` (= 0).
- ``savings_realizations``: append-only history of actually transferred
  savings per cycle. Identified by ``source_key`` so a re-issued
  command restores the previous row instead of duplicating.

All money columns are ``NUMERIC(18,2)`` and never signed for amounts
the user can only spend (savings_target amount, savings_reserved
amount, savings_realizations amount). CycleIncome amount is
constrained ``>= 0`` because a negative "income" is an expense, not
income. Invariant violations between the read model and the legacy
``expenses`` mirror are detected by ``MoneyEngine.detect_invariant_violations``
and surfaced, never auto-repaired.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0013_money_engine_state"
down_revision: Union[str, None] = "0012_cycle_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Cycles whose status is anything other than ``closed`` are the "live"
# universe; the unique index on ``(cycle_id, currency) WHERE kind='income'``
# uses the same predicate so two ``ARS`` base salaries on a single
# cycle cannot coexist.
_CYCLE_LIVE_KIND_INCOME = (
    "kind = 'income' "
    "AND deleted_at IS NULL"
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # cycle_incomes
    # ------------------------------------------------------------------
    op.create_table(
        "cycle_incomes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("cycle_id", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
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
            ["cycle_id"], ["financial_cycles.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cycle_incomes"),
        sa.CheckConstraint(
            "amount >= 0",
            name="ck_cycle_incomes_amount_nonneg",
        ),
        sa.CheckConstraint(
            "kind IN ('income', 'extra')",
            name="ck_cycle_incomes_kind_valid",
        ),
        # ``extra`` rows are uniquely identified by ``(cycle_id, source_key)``
        # even after a soft-delete, so a re-issued idempotent command
        # restores the previous row instead of creating a duplicate. The
        # base ``income`` rows are uniquely identified per
        # ``(cycle_id, currency)`` over the live set only (so a soft
        # delete followed by a fresh insert is still allowed).
        sa.UniqueConstraint(
            "cycle_id",
            "source_key",
            name="uq_cycle_incomes_extra_source_key",
        ),
    )
    op.create_index(
        "uq_cycle_incomes_live_base_income",
        "cycle_incomes",
        ["cycle_id", "currency"],
        unique=True,
        postgresql_where=sa.text(_CYCLE_LIVE_KIND_INCOME),
        sqlite_where=sa.text(_CYCLE_LIVE_KIND_INCOME),
    )
    op.create_index(
        "ix_cycle_incomes_cycle_currency",
        "cycle_incomes",
        ["cycle_id", "currency"],
    )
    # Enforce: kind='extra' rows have a source_key and a positive amount.
    # Done via a CHECK so it stays in the schema even when the row was
    # inserted directly with SQL.
    op.create_check_constraint(
        "ck_cycle_incomes_extra_requires_source_key",
        "cycle_incomes",
        "(kind <> 'extra') OR (source_key IS NOT NULL AND amount > 0)",
    )

    # ------------------------------------------------------------------
    # savings_targets
    # ------------------------------------------------------------------
    op.create_table(
        "savings_targets",
        sa.Column("cycle_id", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("target_pct", sa.Numeric(5, 4), nullable=True),
        sa.Column("target_amount", sa.Numeric(18, 2), nullable=True),
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
            ["cycle_id"], ["financial_cycles.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "cycle_id", "currency", name="pk_savings_targets"
        ),
        sa.CheckConstraint(
            "target_pct IS NULL OR (target_pct >= 0 AND target_pct <= 1)",
            name="ck_savings_targets_pct_range",
        ),
        sa.CheckConstraint(
            "target_amount IS NULL OR target_amount >= 0",
            name="ck_savings_targets_amount_nonneg",
        ),
        # XOR: exactly one of the two is set.
        sa.CheckConstraint(
            "(target_pct IS NULL) <> (target_amount IS NULL)",
            name="ck_savings_targets_xor",
        ),
    )

    # ------------------------------------------------------------------
    # savings_reserved
    # ------------------------------------------------------------------
    op.create_table(
        "savings_reserved",
        sa.Column("cycle_id", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
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
            ["cycle_id"], ["financial_cycles.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "cycle_id", "currency", name="pk_savings_reserved"
        ),
        sa.CheckConstraint(
            "amount >= 0",
            name="ck_savings_reserved_amount_nonneg",
        ),
    )

    # ------------------------------------------------------------------
    # savings_realizations
    # ------------------------------------------------------------------
    op.create_table(
        "savings_realizations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("cycle_id", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
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
            ["cycle_id"], ["financial_cycles.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_savings_realizations"),
        sa.CheckConstraint(
            "amount > 0",
            name="ck_savings_realizations_amount_positive",
        ),
        sa.UniqueConstraint(
            "cycle_id",
            "source_key",
            name="uq_savings_realizations_source_key",
        ),
    )
    op.create_index(
        "ix_savings_realizations_cycle_currency",
        "savings_realizations",
        ["cycle_id", "currency"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_savings_realizations_cycle_currency",
        table_name="savings_realizations",
    )
    op.drop_table("savings_realizations")
    op.drop_table("savings_reserved")
    op.drop_table("savings_targets")
    op.drop_index(
        "ix_cycle_incomes_cycle_currency", table_name="cycle_incomes"
    )
    op.drop_index(
        "uq_cycle_incomes_live_base_income", table_name="cycle_incomes"
    )
    op.drop_table("cycle_incomes")
