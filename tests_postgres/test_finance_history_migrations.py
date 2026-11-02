"""Legacy finance migrations preserve data instead of replaying it as income."""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, inspect, text

from tests_postgres.conftest import run_alembic


def test_legacy_monthly_budget_rename_roundtrip_preserves_every_value(pg_database_url):
    engine = create_engine(pg_database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        run_alembic("upgrade", "0015_payday_calendar_rule", database_url=pg_database_url)
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO monthly_budget "
                "(id,telegram_user_id,month_year,income,extra,note,created_at,updated_at) "
                "VALUES (77,42,'2026-09',1100000.25,75000.50,'History only',:created,:updated)"
            ), {
                "created": datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
                "updated": datetime(2026, 9, 2, 13, tzinfo=timezone.utc),
            })
            before = connection.execute(text("SELECT * FROM monthly_budget WHERE id=77")).mappings().one()
        run_alembic("upgrade", "0016_legacy_monthly_budget", database_url=pg_database_url)
        assert "monthly_budget" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            migrated = connection.execute(text("SELECT * FROM monthly_budget_legacy WHERE id=77")).mappings().one()
            assert dict(migrated) == dict(before)
            assert migrated["income"] == Decimal("1100000.25")
            assert connection.execute(text("SELECT count(*) FROM cycle_incomes")).scalar_one() == 0
        run_alembic("downgrade", "0015_payday_calendar_rule", database_url=pg_database_url)
        assert "monthly_budget_legacy" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            restored = connection.execute(text("SELECT * FROM monthly_budget WHERE id=77")).mappings().one()
            assert dict(restored) == dict(before)
        run_alembic("upgrade", "0016_legacy_monthly_budget", database_url=pg_database_url)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM monthly_budget_legacy")).scalar_one() == 1
            assert connection.execute(text("SELECT count(*) FROM cycle_incomes")).scalar_one() == 0
    finally:
        engine.dispose()
