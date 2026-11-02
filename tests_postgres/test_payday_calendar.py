"""Calendar payday rules preserve old data and serialize real PG rollover."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.database.database import get_session_factory, session_scope
from app.money import CycleService, MoneyEngine, MoneyStateService, UserSettingsService
from app.money.models import FinancialCycle
from tests_postgres.conftest import run_alembic
from tests_postgres.test_fixed_payment_concurrency import fresh_db  # noqa: F401


def test_calendar_upgrade_and_downgrade_preserve_existing_cycle_dates_and_snapshot(
    pg_database_url,
):
    engine = create_engine(pg_database_url, future=True)
    frozen = {"snapshot_version": 1, "money": {"available_to_spend": {"ARS": "123.45"}},
              "carried_over_fixed": []}
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        run_alembic("upgrade", "0014_recurring_obligations", database_url=pg_database_url)
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO user_settings (telegram_user_id,payday_day,timezone) "
                "VALUES (42,31,'America/Argentina/Buenos_Aires')"
            ))
            cycle_id = connection.execute(text(
                "INSERT INTO financial_cycles "
                "(telegram_user_id,cycle_index,start_date,end_date,payday_day,status,"
                "closing_snapshot,closed_at) "
                "VALUES (42,1,'2026-10-31','2026-11-29',31,'closed',"
                "CAST(:snapshot AS jsonb),'2026-11-29T23:00:00Z') RETURNING id"
            ), {"snapshot": json.dumps(frozen)}).scalar_one()

        def original_values():
            with engine.connect() as connection:
                return tuple(connection.execute(text(
                    "SELECT start_date,end_date,payday_day,closing_snapshot,closed_at "
                    "FROM financial_cycles WHERE id=:id"
                ), {"id": cycle_id}).one())

        before = original_values()
        run_alembic("upgrade", "head", database_url=pg_database_url)
        assert original_values() == before
        with engine.connect() as connection:
            for table in ("user_settings", "financial_cycles"):
                assert tuple(connection.execute(text(
                    f"SELECT pay_rule,payday_weekday FROM {table} "
                    "WHERE telegram_user_id=42"
                )).one()) == ("fixed_day", None)
        run_alembic("downgrade", "0014_recurring_obligations", database_url=pg_database_url)
        assert original_values() == before
        for table in ("user_settings", "financial_cycles"):
            columns = {column["name"] for column in inspect(engine).get_columns(table)}
            assert "pay_rule" not in columns and "payday_weekday" not in columns
        run_alembic("upgrade", "head", database_url=pg_database_url)
        assert original_values() == before
        assert before[3] == frozen
    finally:
        engine.dispose()


def test_calendar_constraints_reject_invalid_rule_weekday_combinations(fresh_db):
    engine = create_engine(fresh_db, future=True)
    invalid = [("unknown_rule", None), ("fixed_day", 4), ("first_weekday", None),
               ("first_weekday", -1), ("first_weekday", 7)]
    try:
        with engine.begin() as connection:
            for table in ("user_settings", "financial_cycles"):
                for number, (rule, weekday) in enumerate(invalid, start=1000):
                    if table == "user_settings":
                        statement = (
                            "INSERT INTO user_settings "
                            "(telegram_user_id,payday_day,pay_rule,payday_weekday) "
                            "VALUES (:user,1,:rule,:weekday)"
                        )
                    else:
                        statement = (
                            "INSERT INTO financial_cycles "
                            "(telegram_user_id,cycle_index,start_date,end_date,payday_day,"
                            "status,pay_rule,payday_weekday) VALUES "
                            "(:user,1,'2026-10-02','2026-11-05',1,'open',:rule,:weekday)"
                        )
                    with pytest.raises(IntegrityError) as caught:
                        with connection.begin_nested():
                            connection.execute(text(statement), {
                                "user": number, "rule": rule, "weekday": weekday,
                            })
                    assert caught.value.orig.sqlstate == "23514"
            connection.execute(text(
                "INSERT INTO user_settings (telegram_user_id,payday_day,pay_rule,payday_weekday) "
                "VALUES (5000,1,'first_weekday',4)"
            ))
            connection.execute(text(
                "INSERT INTO financial_cycles "
                "(telegram_user_id,cycle_index,start_date,end_date,payday_day,status,pay_rule,payday_weekday) "
                "VALUES (5000,1,'2026-10-02','2026-11-05',1,'open','first_weekday',4)"
            ))
    finally:
        engine.dispose()


def test_first_friday_35_day_cycle_rolls_over_once_under_concurrent_callers(fresh_db):
    with session_scope() as session:
        UserSettingsService(session).set_first_weekday(
            42, weekday=4, timezone="America/Argentina/Buenos_Aires"
        )
        cycle = CycleService(session).open_initial_cycle(42, date(2026, 10, 2))
        cycle_id = cycle.cycle_id
        assert (cycle.start_date, cycle.end_date) == (date(2026, 10, 2), date(2026, 11, 5))
        assert (cycle.end_date - cycle.start_date).days + 1 == 35
        MoneyStateService(session).set_income(42, cycle_id, "USD", Decimal("350"))
        summary = MoneyEngine(session).get_cycle_summary(42, cycle_id, today=date(2026, 10, 2))
        assert summary.daily_safe_to_spend.get("USD") == Decimal("10.00")

    factory = get_session_factory()

    def ensure():
        with factory() as session:
            return CycleService(session).ensure_current_cycle(42, date(2026, 11, 6)).id

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(ensure) for _ in range(12)]
        ids = [future.result(timeout=30) for future in futures]
    assert len(set(ids)) == 1
    with session_scope() as session:
        cycles = session.execute(select(FinancialCycle).where(
            FinancialCycle.telegram_user_id == 42
        ).order_by(FinancialCycle.cycle_index)).scalars().all()
        assert len(cycles) == 2
        closed, current = cycles
        assert closed.status == "closed" and current.status == "open"
        assert (current.start_date, current.end_date) == (date(2026, 11, 6), date(2026, 12, 3))
        assert (current.pay_rule, current.payday_weekday) == ("first_weekday", 4)
        frozen = deepcopy(closed.closing_snapshot)
        assert frozen["money"]["income"] == {"USD": "350.00"}
        assert frozen["money"]["daily_safe_to_spend"] is None
        CycleService(session).ensure_current_cycle(42, date(2026, 11, 7))
        session.refresh(closed)
        assert closed.closing_snapshot == frozen
