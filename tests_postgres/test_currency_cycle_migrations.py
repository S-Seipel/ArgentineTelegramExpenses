"""F3 preserves historical money and rejects nonrepresentable downgrades."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import json
from subprocess import CalledProcessError
from threading import Barrier

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.money import CycleService, MoneyEngine, MoneyStateService
from app.money.models import CycleIncome, FinancialCycle, SavingsRealization
from app.money.money_state_service import MoneySourceCurrencyConflictError
from tests_postgres.conftest import run_alembic
from tests_postgres.test_fixed_payment_concurrency import fresh_db  # noqa: F401


@pytest.fixture
def pre_f3_engine(pg_database_url):
    engine = create_engine(pg_database_url, future=True)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_alembic("upgrade", "0016_legacy_monthly_budget", database_url=pg_database_url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO user_settings (telegram_user_id,payday_day,timezone) "
            "VALUES (42,1,'America/Argentina/Buenos_Aires')"
        ))
    try:
        yield engine
    finally:
        engine.dispose()


def _old_cycle(connection, *, index=1, status="open", snapshot=None, start="2026-10-01", end="2026-10-31", day=1):
    return connection.execute(text(
        "INSERT INTO financial_cycles "
        "(telegram_user_id,cycle_index,start_date,end_date,payday_day,status,closing_snapshot) "
        "VALUES (42,:index,:start,:end,:day,:status,CAST(:snapshot AS jsonb)) RETURNING id"
    ), {"index": index, "start": start, "end": end, "day": day, "status": status,
        "snapshot": json.dumps(snapshot) if snapshot is not None else None}).scalar_one()


def _old_income(connection, cycle_id, currency="ARS", amount="100"):
    return connection.execute(text(
        "INSERT INTO cycle_incomes (cycle_id,currency,kind,amount) "
        "VALUES (:cycle,:currency,'income',:amount) RETURNING id"
    ), {"cycle": cycle_id, "currency": currency, "amount": Decimal(amount)}).scalar_one()


def _snapshot():
    fields = ("income", "extra", "gross_income", "savings_target", "savings_reserved",
              "savings_realized", "savings_committed", "realized_expenses",
              "pending_fixed_obligations", "pending_recurring_obligations",
              "pending_reserved_obligations", "available_to_spend")
    money = {field: {} for field in fields}
    money.update({"income": {"ARS": "100.00", "USD": "20.00"},
                  "gross_income": {"ARS": "100.00", "USD": "20.00"},
                  "available_to_spend": {"ARS": "100.00", "USD": "18.00"},
                  "savings_reserved": {"USD": "2.00"}, "savings_realized": {"USD": "2.00"},
                  "savings_committed": {"USD": "2.00"}, "daily_safe_to_spend": None, "warnings": []})
    return {"snapshot_version": 1, "money": money, "carried_over_fixed": [],
            "closed_at": "2026-10-31T23:00:00+00:00", "closed_by": "manual"}


def _money_rows(connection):
    return {table: connection.execute(text(
        f"SELECT row_to_json(r)::text FROM {table} r ORDER BY row_to_json(r)::text"
    )).scalars().all() for table in (
        "cycle_incomes", "savings_targets", "savings_reserved", "savings_realizations"
    )}


def test_closed_mixed_history_and_all_money_associations_survive_roundtrip(pre_f3_engine, pg_database_url):
    engine = pre_f3_engine
    frozen = _snapshot()
    with engine.begin() as connection:
        cycle_id = _old_cycle(connection, status="closed", snapshot=frozen)
        ids = [_old_income(connection, cycle_id, "ARS"), _old_income(connection, cycle_id, "USD", "20")]
        connection.execute(text(
            "INSERT INTO savings_targets (cycle_id,currency,target_amount) VALUES (:cycle,'USD',4)"
        ), {"cycle": cycle_id})
        connection.execute(text(
            "INSERT INTO savings_reserved (cycle_id,currency,amount) VALUES (:cycle,'USD',2)"
        ), {"cycle": cycle_id})
        connection.execute(text(
            "INSERT INTO savings_realizations (cycle_id,currency,amount,source_key) VALUES (:cycle,'USD',2,'old-saving')"
        ), {"cycle": cycle_id})
        original = dict(connection.execute(text("SELECT * FROM financial_cycles WHERE id=:id"), {"id": cycle_id}).mappings().one())
        rows = _money_rows(connection)
    run_alembic("upgrade", "head", database_url=pg_database_url)
    with engine.connect() as connection:
        current = dict(connection.execute(text("SELECT * FROM financial_cycles WHERE id=:id"), {"id": cycle_id}).mappings().one())
        assert {key: current[key] for key in original} == original
        assert current["currency"] == "ARS" and current["legacy_mixed_currency"] is True
        assert _money_rows(connection) == rows
        assert connection.execute(text("SELECT id FROM cycle_incomes ORDER BY id")).scalars().all() == ids
    with Session(engine) as session:
        summary = MoneyEngine(session).get_cycle_summary(42, cycle_id)
        assert summary.available_to_spend.get("ARS") == Decimal("100")
        assert summary.available_to_spend.get("USD") == Decimal("18")
    run_alembic("downgrade", "0016_legacy_monthly_budget", database_url=pg_database_url)
    with engine.connect() as connection:
        assert dict(connection.execute(text("SELECT * FROM financial_cycles WHERE id=:id"), {"id": cycle_id}).mappings().one()) == original
        assert _money_rows(connection) == rows
    run_alembic("upgrade", "head", database_url=pg_database_url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT closing_snapshot FROM financial_cycles WHERE id=:id"), {"id": cycle_id}).scalar_one() == frozen


@pytest.mark.parametrize("foreign_source", ["income", "savings", "expense", "fixed", "recurring", "carry_over"])
def test_open_mixed_preflight_preserves_schema_and_data(pre_f3_engine, pg_database_url, foreign_source):
    engine = pre_f3_engine
    with engine.begin() as connection:
        cycle_id = _old_cycle(connection, index=2)
        _old_income(connection, cycle_id)
        category = connection.execute(text("SELECT min(id) FROM categories")).scalar_one()
        if foreign_source == "income":
            _old_income(connection, cycle_id, "USD")
        elif foreign_source == "savings":
            connection.execute(text("INSERT INTO savings_reserved (cycle_id,currency,amount) VALUES (:cycle,'USD',10)"), {"cycle": cycle_id})
        elif foreign_source == "expense":
            connection.execute(text(
                "INSERT INTO expenses (telegram_user_id,name,amount,currency,category_id,expense_date,"
                "original_message,ai_confidence,source_type,revision) "
                "VALUES (42,'Foreign expense',10,'USD',:cat,'2026-10-15','',1,'telegram',1)"
            ), {"cat": category})
        elif foreign_source in {"fixed", "carry_over"}:
            fixed_id = connection.execute(text(
                "INSERT INTO fixed_expenses (telegram_user_id,name,expected_amount,currency,category_id,"
                "due_day_of_month,is_active) VALUES (42,'Foreign fixed',10,'USD',:cat,5,:active) RETURNING id"
            ), {"cat": category, "active": foreign_source == "fixed"}).scalar_one()
            if foreign_source == "carry_over":
                _old_cycle(connection, index=1, status="closed", start="2026-09-01", end="2026-09-30", snapshot={
                    "snapshot_version": 1, "carried_over_fixed": [{"fixed_id": fixed_id, "name": "Foreign fixed",
                    "expected_amount": "10", "currency": "USD", "due_date": "2026-09-05", "original_month_year": "2026-09"}],
                })
        else:
            connection.execute(text(
                "INSERT INTO recurring_expenses (telegram_user_id,name,amount,currency,category_id,frequency,"
                "day_of_month,next_due_date,is_active,is_obligation,obligation_due_date) "
                "VALUES (42,'Foreign recurring',10,'USD',:cat,'monthly',15,'2026-10-15',true,true,'2026-10-15')"
            ), {"cat": category})
        before = _money_rows(connection)
        snapshots = connection.execute(text("SELECT id,closing_snapshot FROM financial_cycles ORDER BY id")).all()
    with pytest.raises(CalledProcessError) as caught:
        run_alembic("upgrade", "head", database_url=pg_database_url)
    assert "Close it with the previous app version" in caught.value.stderr
    assert "currency_payday_settings" not in inspect(engine).get_table_names()
    assert "currency" not in {column["name"] for column in inspect(engine).get_columns("financial_cycles")}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0016_legacy_monthly_budget"
        assert _money_rows(connection) == before
        assert connection.execute(text("SELECT id,closing_snapshot FROM financial_cycles ORDER BY id")).all() == snapshots


def test_existing_usd_cycle_keeps_its_own_frozen_calendar(pre_f3_engine, pg_database_url):
    engine = pre_f3_engine
    with engine.begin() as connection:
        connection.execute(text("UPDATE user_settings SET pay_rule='first_weekday',payday_weekday=4 WHERE telegram_user_id=42"))
        cycle_id = _old_cycle(connection, start="2026-10-02", end="2026-11-01", day=2)
        income_id = _old_income(connection, cycle_id, "USD", "50")
    run_alembic("upgrade", "head", database_url=pg_database_url)
    with engine.begin() as connection:
        assert tuple(connection.execute(text(
            "SELECT currency,payday_day,pay_rule,payday_weekday FROM currency_payday_settings WHERE telegram_user_id=42"
        )).one()) == ("USD", 2, "fixed_day", None)
        assert connection.execute(text("SELECT cycle_id FROM cycle_incomes WHERE id=:id"), {"id": income_id}).scalar_one() == cycle_id
        connection.execute(text("UPDATE user_settings SET pay_rule='fixed_day',payday_weekday=NULL,payday_day=25 WHERE telegram_user_id=42"))
    with Session(engine) as session:
        next_cycle = CycleService(session).ensure_current_cycle(42, date(2026, 11, 2), currency="USD")
        assert (next_cycle.currency, next_cycle.start_date, next_cycle.end_date) == (
            "USD", date(2026, 11, 2), date(2026, 12, 1)
        )


@pytest.mark.parametrize("reason", ["open", "indexes", "settings"])
def test_nonrepresentable_downgrade_fails_before_changing_schema(fresh_db, reason):
    engine = create_engine(fresh_db, future=True)
    try:
        with engine.begin() as connection:
            if reason == "settings":
                connection.execute(text("INSERT INTO currency_payday_settings (telegram_user_id,currency,payday_day) VALUES (42,'USD',20)"))
            else:
                for currency in ("ARS", "USD"):
                    connection.execute(text(
                        "INSERT INTO financial_cycles (telegram_user_id,currency,cycle_index,start_date,end_date,"
                        "payday_day,status) VALUES (42,:currency,1,'2026-10-01','2026-10-31',1,:status)"
                    ), {"currency": currency, "status": "open" if reason == "open" else "closed"})
        with pytest.raises(CalledProcessError) as caught:
            run_alembic("downgrade", "0016_legacy_monthly_budget", database_url=fresh_db)
        assert "Cannot downgrade" in caught.value.stderr
        assert "currency_payday_settings" in inspect(engine).get_table_names()
        assert "currency" in {column["name"] for column in inspect(engine).get_columns("financial_cycles")}
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0017_currency_cycles"
            table = "currency_payday_settings" if reason == "settings" else "financial_cycles"
            assert connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == (1 if reason == "settings" else 2)
    finally:
        engine.dispose()


def test_mixed_legacy_flag_is_rejected_for_an_open_cycle(fresh_db):
    engine = create_engine(fresh_db, future=True)
    try:
        with engine.begin() as connection:
            with pytest.raises(IntegrityError) as caught:
                with connection.begin_nested():
                    connection.execute(text(
                        "INSERT INTO financial_cycles (telegram_user_id,currency,cycle_index,start_date,end_date,"
                        "payday_day,status,legacy_mixed_currency) VALUES (42,'ARS',1,'2026-10-01','2026-10-31',1,'open',true)"
                    ))
            assert caught.value.orig.sqlstate == "23514"
    finally:
        engine.dispose()


def test_concurrent_currency_openings_keep_two_independent_calendars(fresh_db):
    engine = create_engine(fresh_db, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO user_settings (telegram_user_id,payday_day) VALUES (42,5)"))
            connection.execute(text("INSERT INTO currency_payday_settings (telegram_user_id,currency,payday_day) VALUES (42,'USD',20)"))
        def ensure(currency):
            with Session(engine) as session:
                cycle = CycleService(session).ensure_current_cycle(42, date(2026, 11, 2), currency=currency)
                return currency, cycle.id, cycle.start_date, cycle.end_date
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(ensure, currency) for currency in ("ARS", "USD") * 6]
            rows = [future.result(timeout=30) for future in futures]
        assert len({row[1] for row in rows if row[0] == "ARS"}) == 1
        assert len({row[1] for row in rows if row[0] == "USD"}) == 1
        assert all(row[2:] == (date(2026, 10, 5), date(2026, 11, 4)) for row in rows if row[0] == "ARS")
        assert all(row[2:] == (date(2026, 10, 20), date(2026, 11, 19)) for row in rows if row[0] == "USD")
        with Session(engine) as session:
            assert len(session.execute(select(FinancialCycle)).scalars().all()) == 2
    finally:
        engine.dispose()


@pytest.mark.parametrize("method,model", [("record_extra_income", CycleIncome), ("record_savings_realized", SavingsRealization)])
def test_cross_currency_source_key_race_has_one_global_currency(fresh_db, method, model):
    engine = create_engine(fresh_db, future=True)
    try:
        with Session(engine) as session:
            cycles = {currency: CycleService(session).ensure_current_cycle(42, date(2026, 11, 2), currency=currency).id
                      for currency in ("ARS", "USD")}
        barrier = Barrier(12)
        def record(currency):
            with Session(engine) as session:
                barrier.wait(timeout=15)
                try:
                    row = getattr(MoneyStateService(session), method)(
                        user_id=42, cycle_id=cycles[currency], currency=currency,
                        amount=Decimal("10"), source_key="shared-cross-currency-key",
                    )
                    return "ok", row.currency
                except MoneySourceCurrencyConflictError:
                    return "conflict", currency
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(record, currency) for currency in ("ARS", "USD") * 6]
            outcomes = [future.result(timeout=45) for future in futures]
        assert [kind for kind, _ in outcomes].count("ok") == 6
        assert [kind for kind, _ in outcomes].count("conflict") == 6
        with Session(engine) as session:
            rows = session.execute(select(model).where(model.source_key == "shared-cross-currency-key")).scalars().all()
            assert len(rows) == 1
            assert all(currency == rows[0].currency for kind, currency in outcomes if kind == "ok")
    finally:
        engine.dispose()
