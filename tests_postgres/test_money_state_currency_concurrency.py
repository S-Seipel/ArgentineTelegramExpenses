"""Currency conflicts on a concurrently inserted source key need real PG."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.money.models import CycleIncome, FinancialCycle, SavingsRealization
from app.money.money_state_service import (
    MoneySourceCurrencyConflictError,
    MoneyStateService,
)
from tests_postgres.conftest import run_alembic


@pytest.fixture
def pg_cycle(pg_database_url):
    engine = create_engine(pg_database_url, future=True)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_alembic("upgrade", "head", database_url=pg_database_url)
    with Session(engine) as session:
        cycles = {currency: FinancialCycle(
            telegram_user_id=1, cycle_index=1, currency=currency,
            start_date=date(2026, 10, 1), end_date=date(2026, 10, 31),
            payday_day=1, status="open",
        ) for currency in ("ARS", "USD")}
        session.add_all(cycles.values())
        session.commit()
        cycle_ids = {currency: cycle.id for currency, cycle in cycles.items()}
    yield engine, cycle_ids
    engine.dispose()


@pytest.mark.parametrize(
    "method,model",
    [
        ("record_extra_income", CycleIncome),
        ("record_savings_realized", SavingsRealization),
    ],
)
def test_concurrent_source_key_conflicting_currency(
    pg_cycle, method, model
):
    engine, cycle_ids = pg_cycle
    barrier = Barrier(2)

    def record(currency):
        with Session(engine) as session:
            barrier.wait(timeout=10)
            try:
                row = getattr(MoneyStateService(session), method)(
                    user_id=1, cycle_id=cycle_ids[currency], currency=currency,
                    amount=Decimal("10"), source_key="race",
                )
                return "ok", row.currency
            except MoneySourceCurrencyConflictError as exc:
                assert exc.existing_currency != exc.requested_currency
                return "conflict", currency

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(record, ("ARS", "USD")))

    assert sorted(result[0] for result in results) == ["conflict", "ok"]
    winner_currency = next(currency for outcome, currency in results if outcome == "ok")
    with Session(engine) as session:
        rows = session.execute(
            select(model).where(model.cycle_id.in_(cycle_ids.values()), model.source_key == "race")
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].currency == winner_currency
