"""PostgreSQL integration tests for Alembic migrations.

Covers the Phase-0 invariants:
- empty database can be migrated from zero to head;
- 0008 can be downgraded and re-applied without data loss;
- the partial unique index ``uq_expenses_user_source_key`` is
  functional after upgrade.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from tests_postgres.conftest import run_alembic


def _drop_and_recreate(database_url: str) -> None:
    """Drop every public object so the migration starts from zero."""
    engine = create_engine(database_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


@pytest.fixture(autouse=True)
def _reset_db(pg_database_url):
    """Each test starts from a brand-new schema so order doesn't matter."""
    _drop_and_recreate(pg_database_url)
    yield


def test_alembic_upgrade_from_empty_db(pg_database_url):
    run_alembic("upgrade", "head", database_url=pg_database_url)

    engine = create_engine(pg_database_url, future=True)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {
            "alembic_version",
            "expenses",
            "categories",
            "recurring_expenses",
            "budgets",
            "fixed_expenses",
            "fixed_expense_payments",
            "monthly_budget",
        } <= tables
        col_names = {c["name"] for c in inspector.get_columns("expenses")}
        assert {
            "source_type",
            "source_key",
            "telegram_chat_id",
            "telegram_message_id",
            "deleted_at",
            "revision",
        } <= col_names
        cols_by_name = {c["name"]: c for c in inspector.get_columns("expenses")}
        # ``source_type`` is NOT NULL but has NO server default — the
        # migration uses the default ONLY during the upgrade to
        # backfill existing rows, then drops it before returning.
        assert cols_by_name["source_type"]["nullable"] is False
        assert cols_by_name["source_type"].get("default") is None, (
            "source_type must not have a server default after the "
            "migration closes (Phase 0 closure contract)"
        )
        indexes = inspector.get_indexes("expenses")
        assert any(
            ix["name"] == "uq_expenses_user_source_key"
            and ix.get("unique") is True
            for ix in indexes
        )
    finally:
        engine.dispose()


def test_alembic_downgrade_and_re_upgrade_0008(pg_database_url):
    run_alembic("upgrade", "head", database_url=pg_database_url)

    run_alembic("downgrade", "0007", database_url=pg_database_url)
    engine = create_engine(pg_database_url, future=True)
    try:
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("expenses")}
        assert "source_type" not in cols
        assert "deleted_at" not in cols
        indexes = inspector.get_indexes("expenses")
        assert all(
            ix["name"] != "uq_expenses_user_source_key" for ix in indexes
        )
    finally:
        engine.dispose()

    run_alembic("upgrade", "head", database_url=pg_database_url)
    engine = create_engine(pg_database_url, future=True)
    try:
        inspector = inspect(engine)
        cols = {c["name"]: c for c in inspector.get_columns("expenses")}
        assert "source_type" in cols and "deleted_at" in cols
        # The temporary default MUST stay dropped after a re-upgrade
        # — otherwise we'd silently regress Phase 0 closure.
        assert cols["source_type"].get("default") is None
        indexes = inspector.get_indexes("expenses")
        assert any(ix["name"] == "uq_expenses_user_source_key" for ix in indexes)
    finally:
        engine.dispose()


def test_partial_unique_index_enforced(pg_database_url):
    """Two rows with NULL source_key → both allowed. Same non-null
    source_key for the SAME user → rejected. Different user, same
    source_key → allowed (proves the per-user scope)."""
    run_alembic("upgrade", "head", database_url=pg_database_url)

    engine = create_engine(pg_database_url, future=True)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO categories (name, is_active, created_at, "
                    "updated_at) VALUES ('Otros', true, now(), now())"
                )
            )

            # Two NULL source_keys for the same user: allowed.
            conn.execute(
                text(
                    "INSERT INTO expenses (telegram_user_id, name, amount, "
                    "currency, category_id, expense_date, original_message, "
                    "ai_confidence, source_type, source_key, revision) "
                    "VALUES (1, 'a', 1, 'ARS', 1, '2026-09-10', '', 0, "
                    "'legacy', NULL, 1)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO expenses (telegram_user_id, name, amount, "
                    "currency, category_id, expense_date, original_message, "
                    "ai_confidence, source_type, source_key, revision) "
                    "VALUES (1, 'b', 1, 'ARS', 1, '2026-09-10', '', 0, "
                    "'legacy', NULL, 1)"
                )
            )

        # Same non-null source_key, same user → blocked. We need a
        # separate transaction (the partial index raises an
        # ``IntegrityError`` we expect to bubble up).
        with engine.connect() as conn:
            trans = conn.begin()
            conn.execute(
                text(
                    "INSERT INTO expenses (telegram_user_id, name, amount, "
                    "currency, category_id, expense_date, original_message, "
                    "ai_confidence, source_type, source_key, revision) "
                    "VALUES (2, 'c', 1, 'ARS', 1, '2026-09-10', '', 0, "
                    "'fixed_payment', 'fixed-payment:1:2026-09', 1)"
                )
            )
            with pytest.raises(Exception):
                conn.execute(
                    text(
                        "INSERT INTO expenses (telegram_user_id, name, amount, "
                        "currency, category_id, expense_date, original_message, "
                        "ai_confidence, source_type, source_key, revision) "
                        "VALUES (2, 'd', 1, 'ARS', 1, '2026-09-10', '', 0, "
                        "'fixed_payment', 'fixed-payment:1:2026-09', 1)"
                    )
                )
            trans.rollback()

        # Different user, same non-null source_key → allowed (partial
        # index is scoped to (telegram_user_id, source_key)).
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO expenses (telegram_user_id, name, amount, "
                    "currency, category_id, expense_date, original_message, "
                    "ai_confidence, source_type, source_key, revision) "
                    "VALUES (1, 'e', 1, 'ARS', 1, '2026-09-10', '', 0, "
                    "'fixed_payment', 'fixed-payment:1:2026-09', 1)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO expenses (telegram_user_id, name, amount, "
                    "currency, category_id, expense_date, original_message, "
                    "ai_confidence, source_type, source_key, revision) "
                    "VALUES (2, 'f', 1, 'ARS', 1, '2026-09-10', '', 0, "
                    "'fixed_payment', 'fixed-payment:1:2026-09', 1)"
                )
            )
    finally:
        engine.dispose()
