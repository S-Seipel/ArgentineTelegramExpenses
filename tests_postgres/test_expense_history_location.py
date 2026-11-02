"""Real PostgreSQL migration, history serialization and concurrent undo."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.categories.models import Category
from app.expenses.models import Expense, ExpenseChange
from app.expenses.repository import ExpenseRepository
from app.expenses.schemas import ExpenseCreate
from app.expenses.service import ExpenseService
from tests_postgres.conftest import run_alembic


@pytest.fixture
def pg_history(pg_database_url):
    engine = create_engine(pg_database_url, future=True)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_alembic("upgrade", "0018_expense_history_location", database_url=pg_database_url)
    with Session(engine) as session:
        category = session.execute(select(Category.id).limit(1)).scalar_one()
        eid = ExpenseRepository(session).create(ExpenseCreate(
            telegram_user_id=1, name="Original", amount=Decimal("100.01"),
            currency="ARS", category_id=category, expense_date=date(2026, 11, 2),
            original_message="isolated PG fixture", ai_confidence=Decimal("0.900"),
        )).id
    yield engine, eid
    engine.dispose()


def test_migration_roundtrip_preserves_old_expense_columns(pg_database_url):
    engine = create_engine(pg_database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        run_alembic("upgrade", "0017_currency_cycles", database_url=pg_database_url)
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO expenses (telegram_user_id,name,name_key,amount,currency,category_id,expense_date,"
                "original_message,ai_confidence,source_type,source_key,revision) "
                "VALUES (42,'Preserved','preserved',123456.78,'USD',(SELECT min(id) FROM categories),"
                "'2026-10-12','Historical original',0.925,'telegram','historical:42',3)"
            ))
            old = dict(connection.execute(text("SELECT * FROM expenses")).mappings().one())
        run_alembic("upgrade", "0018_expense_history_location", database_url=pg_database_url)
        with engine.connect() as connection:
            now = dict(connection.execute(text("SELECT * FROM expenses")).mappings().one())
            assert {name: now[name] for name in old} == old
            assert now["latitude"] is now["longitude"] is now["location_recorded_at"] is None
            assert connection.execute(text("SELECT count(*) FROM expense_changes")).scalar_one() == 0
        run_alembic("downgrade", "0017_currency_cycles", database_url=pg_database_url)
        assert "expense_changes" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert dict(connection.execute(text("SELECT * FROM expenses")).mappings().one()) == old
    finally:
        engine.dispose()


@pytest.mark.parametrize("lat,lon,recorded", [
    (91, 0, True), (0, 181, True), (None, 1, True),
    (1, None, True), (1, 1, False), (None, None, True),
    (Decimal("NaN"), 0, True),
])
def test_location_constraints_reject_invalid_sql(pg_history, lat, lon, recorded):
    engine, eid = pg_history
    with pytest.raises(IntegrityError) as error:
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE expenses SET latitude=:lat,longitude=:lon,location_recorded_at=:recorded WHERE id=:id"
            ), {"lat": lat, "lon": lon, "recorded": datetime.now(timezone.utc) if recorded else None, "id": eid})
    assert error.value.orig.sqlstate == "23514"
    with engine.connect() as connection:
        assert connection.execute(text("SELECT latitude FROM expenses WHERE id=:id"), {"id": eid}).scalar_one() is None


def test_twelve_concurrent_undo_calls_restore_only_once(pg_history):
    engine, eid = pg_history
    with Session(engine) as session:
        ExpenseRepository(session).update_name(1, eid, "Edited")
    barrier = Barrier(12)

    def undo(_):
        with Session(engine) as session:
            barrier.wait(timeout=20)
            result = ExpenseService(ExpenseRepository(session)).undo_last(1)
            return result.expense.name if result else None

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(undo, range(12)))
    assert results.count("Original") == 1
    assert results.count(None) == 11
    with Session(engine) as session:
        row = session.get(Expense, eid)
        assert row.name == "Original" and row.revision == 3
        assert session.execute(select(ExpenseChange.status)).scalars().all() == ["undone"]


def test_concurrent_edits_make_a_complete_chain_with_ten_safe_undos(pg_history):
    engine, eid = pg_history
    barrier = Barrier(12)

    def edit(amount):
        with Session(engine) as session:
            barrier.wait(timeout=20)
            result = ExpenseRepository(session).update_amount(1, eid, Decimal(amount))
            return result.revision

    with ThreadPoolExecutor(max_workers=12) as executor:
        revisions = list(executor.map(edit, range(2, 14)))
    assert sorted(revisions) == list(range(2, 14))
    with Session(engine) as session:
        changes = session.execute(select(ExpenseChange).order_by(ExpenseChange.id)).scalars().all()
        assert len(changes) == 12
        assert [change.status for change in changes] == ["expired"] * 2 + ["pending"] * 10
        for before, after in zip(changes, changes[1:]):
            assert before.after_snapshot == after.before_snapshot
        expected = Decimal(changes[2].before_snapshot["amount"])
        service = ExpenseService(ExpenseRepository(session))
        for _ in range(10):
            assert service.undo_last(1) is not None
        assert service.undo_last(1) is None
        row = session.get(Expense, eid)
        assert row.amount == expected and row.revision == 23


def test_failed_history_insert_rolls_back_expense_mutation(pg_history, monkeypatch):
    engine, eid = pg_history
    import app.expenses.repository as module
    original = module.record_change

    def broken(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated failure before commit")

    monkeypatch.setattr(module, "record_change", broken)
    with pytest.raises(RuntimeError, match="simulated failure"):
        with Session(engine) as session:
            ExpenseRepository(session).update_amount(1, eid, Decimal("200"))
    with Session(engine) as session:
        assert session.get(Expense, eid).amount == Decimal("100.01")
        assert session.query(ExpenseChange).count() == 0


@pytest.mark.parametrize("kind", ["history", "coordinates"])
def test_downgrade_with_new_data_stops_before_schema_changes(pg_history, pg_database_url, kind):
    engine, eid = pg_history
    if kind == "history":
        with Session(engine) as session:
            ExpenseRepository(session).update_name(1, eid, "New history")
    else:
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE expenses SET latitude=-34.6037221,longitude=-58.3815922,location_recorded_at=now() WHERE id=:id"
            ), {"id": eid})
    import subprocess
    with pytest.raises(subprocess.CalledProcessError):
        run_alembic("downgrade", "0017_currency_cycles", database_url=pg_database_url)
    assert "expense_changes" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0018_expense_history_location"
        if kind == "history":
            assert connection.execute(text("SELECT count(*) FROM expense_changes")).scalar_one() == 1
        else:
            assert connection.execute(text("SELECT latitude FROM expenses WHERE id=:id"), {"id": eid}).scalar_one() == Decimal("-34.6037221")
