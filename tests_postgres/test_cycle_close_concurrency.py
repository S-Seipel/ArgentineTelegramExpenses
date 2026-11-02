"""Closed financial state survives stale sessions and concurrent writes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from decimal import Decimal
from queue import Queue
import time

import pytest
from sqlalchemy import create_engine, select, text

from app.database.database import get_session_factory, session_scope
from app.money import CycleService, MoneyStateService
from app.money.models import (
    CycleIncome, FinancialCycle, SavingsRealization, SavingsReserved,
)
from app.money.money_state_service import (
    CycleClosedError, ExtraIncomeNotFoundError, SavingsRealizationNotFoundError,
)
from tests_postgres.test_fixed_payment_concurrency import fresh_db  # noqa: F401


def _open_cycle():
    with session_scope() as session:
        return CycleService(session).open_initial_cycle(
            user_id=42, today=date(2026, 10, 15), currency="USD"
        ).cycle_id


def test_stale_session_cannot_mutate_a_closed_cycle(fresh_db):
    cycle_id = _open_cycle()
    factory = get_session_factory()
    with factory() as stale, factory() as closer:
        cached = stale.get(FinancialCycle, cycle_id)
        assert cached.status == "open"
        CycleService(closer).close_cycle(42, cycle_id)
        with pytest.raises(CycleClosedError):
            MoneyStateService(stale).set_income(
                user_id=42, cycle_id=cycle_id,
                currency="USD", amount=Decimal("125.25"),
            )
        stale.rollback()

    with session_scope() as session:
        assert session.execute(select(CycleIncome).where(
            CycleIncome.cycle_id == cycle_id
        )).scalars().all() == []
        assert session.get(FinancialCycle, cycle_id).status == "closed"


def test_stale_close_preserves_the_original_snapshot(fresh_db):
    cycle_id = _open_cycle()
    factory = get_session_factory()
    with factory() as stale, factory() as closer:
        cached = stale.get(FinancialCycle, cycle_id)
        assert cached.status == "open"
        CycleService(closer).close_cycle(42, cycle_id)
        closed = closer.get(FinancialCycle, cycle_id)
        frozen = deepcopy(closed.closing_snapshot)
        closed_at = closed.closed_at
        CycleService(stale).close_cycle(42, cycle_id)

    with session_scope() as session:
        closed = session.get(FinancialCycle, cycle_id)
        assert closed.closing_snapshot == frozen
        assert closed.closed_at == closed_at


def test_stale_reservation_release_preserves_both_deltas(fresh_db):
    cycle_id = _open_cycle()
    with session_scope() as session:
        MoneyStateService(session).set_savings_reserved(
            user_id=42, cycle_id=cycle_id,
            currency="USD", amount=Decimal("100"),
        )
    factory = get_session_factory()
    with factory() as stale, factory() as other:
        cached = stale.get(SavingsReserved, (cycle_id, "USD"))
        assert cached.amount == Decimal("100")
        assert MoneyStateService(other).release_savings(
            user_id=42, cycle_id=cycle_id,
            currency="USD", amount=Decimal("20"),
        ).amount == Decimal("80")
        released = MoneyStateService(stale).release_savings(
            user_id=42, cycle_id=cycle_id,
            currency="USD", amount=Decimal("30"),
        )
        assert released.amount == Decimal("50")
    with session_scope() as session:
        assert session.get(SavingsReserved, (cycle_id, "USD")).amount == Decimal("50")


@pytest.mark.parametrize("model,record,update", [
    (CycleIncome, "record_extra_income", "update_extra_income"),
    (SavingsRealization, "record_savings_realized", "update_savings_realized"),
])
def test_stale_money_edit_preserves_the_current_amount_and_revision(
    fresh_db, model, record, update,
):
    cycle_id = _open_cycle()
    with session_scope() as session:
        row_id = getattr(MoneyStateService(session), record)(
            user_id=42, cycle_id=cycle_id, currency="USD",
            amount=Decimal("10"), source_key="stale-edit",
        ).id
    factory = get_session_factory()
    with factory() as stale, factory() as other:
        cached = stale.get(model, row_id)
        assert cached.revision == 1
        key = "extra_id" if model is CycleIncome else "realization_id"
        assert getattr(MoneyStateService(other), update)(
            user_id=42, **{key: row_id}, amount=Decimal("20"),
        ).revision == 2
        edited = getattr(MoneyStateService(stale), update)(
            user_id=42, **{key: row_id}, note="Newest note",
        )
        assert edited.amount == Decimal("20")
        assert edited.revision == 3
    with session_scope() as session:
        row = session.get(model, row_id)
        assert (row.amount, row.revision, row.note) == (
            Decimal("20"), 3, "Newest note"
        )


@pytest.mark.parametrize("model,record,update,delete,error", [
    (CycleIncome, "record_extra_income", "update_extra_income",
     "soft_delete_extra_income", ExtraIncomeNotFoundError),
    (SavingsRealization, "record_savings_realized", "update_savings_realized",
     "soft_delete_savings_realized", SavingsRealizationNotFoundError),
])
def test_stale_money_edit_cannot_change_a_deleted_row(
    fresh_db, model, record, update, delete, error,
):
    cycle_id = _open_cycle()
    with session_scope() as session:
        row_id = getattr(MoneyStateService(session), record)(
            user_id=42, cycle_id=cycle_id, currency="USD",
            amount=Decimal("10"), source_key="stale-delete",
        ).id
    factory = get_session_factory()
    key = "extra_id" if model is CycleIncome else "realization_id"
    with factory() as stale, factory() as other:
        cached = stale.get(model, row_id)
        assert cached.deleted_at is None
        deleted = getattr(MoneyStateService(other), delete)(
            user_id=42, **{key: row_id},
        )
        deleted_at = deleted.deleted_at
        with pytest.raises(error):
            getattr(MoneyStateService(stale), update)(
                user_id=42, **{key: row_id}, amount=Decimal("20"),
            )
        stale.rollback()
    with session_scope() as session:
        row = session.get(model, row_id)
        assert row.deleted_at == deleted_at
        assert row.amount == Decimal("10")


def test_cycle_close_waits_for_the_money_mutation_lock(fresh_db):
    cycle_id = _open_cycle()
    factory = get_session_factory()
    closer_pid = Queue()

    def close():
        with factory() as session:
            session.execute(text("SET LOCAL lock_timeout = '10s'"))
            closer_pid.put(session.execute(text("SELECT pg_backend_pid()")).scalar_one())
            return CycleService(session).close_cycle(42, cycle_id)

    observer = create_engine(fresh_db, future=True)
    try:
        with factory() as writer, ThreadPoolExecutor(max_workers=1) as pool:
            # Hold the exact gate that every money-state mutation uses.
            # Only the main thread releases it; the blocked closer never
            # participates in a barrier while holding another row lock.
            service = MoneyStateService(writer)
            service._require_open_cycle(42, cycle_id)
            future = pool.submit(close)
            try:
                pid = closer_pid.get(timeout=10)
                deadline = time.monotonic() + 5
                while True:
                    with observer.connect() as connection:
                        wait_type = connection.execute(text(
                            "SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"
                        ), {"pid": pid}).scalar_one_or_none()
                    if wait_type == "Lock":
                        break
                    if future.done():
                        future.result()
                        pytest.fail("Closing did not wait for the financial mutation gate")
                    if time.monotonic() >= deadline:
                        pytest.fail("The concurrent close never waited on the cycle lock")
                    time.sleep(0.01)
                service.set_income(
                    user_id=42, cycle_id=cycle_id,
                    currency="USD", amount=Decimal("125.25"),
                )
            finally:
                writer.rollback()
            summary = future.result(timeout=15)
            assert summary.base_income.get("USD") == Decimal("125.25")
        with session_scope() as session:
            closed = session.get(FinancialCycle, cycle_id)
            assert closed.status == "closed"
            assert closed.closing_snapshot["money"]["income"] == {"USD": "125.25"}
    finally:
        observer.dispose()
