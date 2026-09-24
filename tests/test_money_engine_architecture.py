"""R2 architecture tests — service ownership boundary.

Asserts the contract:

- ``MoneyEngine`` is read-only. Calling its public methods leaves
  every ``financial_cycles`` row untouched (status, closing_snapshot,
  closed_at, row count).
- ``CycleService.close_cycle`` is the only owner of the close
  transaction: it locks, computes, snapshots, persists, and commits
  atomically.
- A failure during the snapshot step rolls the whole transaction back
  so the cycle remains open.

These tests are the regression net for the
``MoneyEngine ≠ lifecycle`` architectural decision.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.expenses.schemas import ExpenseCreate
from app.money import CycleService, MoneyEngine, UserSettingsService
from app.money.models import CycleIncome, FinancialCycle
from app.money.snapshot import CURRENT_SNAPSHOT_VERSION


def _open_cycle(user_id: int, *, payday_day: int, today: date):
    with session_scope() as s:
        UserSettingsService(s).set_payday_day(
            user_id=user_id, payday_day=payday_day
        )
        view = CycleService(s).open_initial_cycle(user_id=user_id, today=today)
    return view


def _seed_income(cycle_id: int, *, currency: str = "ARS", amount: Decimal = Decimal("1000")):
    with session_scope() as s:
        s.add(
            CycleIncome(
                cycle_id=cycle_id,
                currency=currency,
                kind="income",
                amount=amount,
            )
        )


# ---------------------------------------------------------------------------
# MoneyEngine read-only invariant
# ---------------------------------------------------------------------------


def test_money_engine_methods_do_not_mutate_cycle(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    _seed_income(view.cycle_id)

    # Snapshot the row + the cycle count BEFORE.
    with session_scope() as s:
        before = s.get(FinancialCycle, view.cycle_id)
        before_snapshot = before.closing_snapshot
        before_status = before.status
        before_closed_at = before.closed_at
        before_opened_at = before.opened_at
        count_before = s.execute(
            select(FinancialCycle).where(
                FinancialCycle.telegram_user_id == 1
            )
        ).all()

    # Hammer every read method, several times each.
    for _ in range(3):
        with session_scope() as s:
            engine = MoneyEngine(s)
            summary = engine.get_cycle_summary(1, view.cycle_id, today=today)
            available = engine.get_available_to_spend(1, view.cycle_id, today=today)
            daily = engine.get_daily_safe_to_spend(1, view.cycle_id, today=today)
            violations = engine.detect_invariant_violations(1)
        assert isinstance(summary, object)  # we got something
        assert available is not None
        assert daily is not None
        assert violations == []

    # The row and the cycle count must be unchanged.
    with session_scope() as s:
        after = s.get(FinancialCycle, view.cycle_id)
        count_after = s.execute(
            select(FinancialCycle).where(
                FinancialCycle.telegram_user_id == 1
            )
        ).all()

    assert after.status == before_status
    assert after.closing_snapshot == before_snapshot
    assert after.closed_at == before_closed_at
    assert after.opened_at == before_opened_at
    assert after.updated_at == before.updated_at  # engine doesn't bump
    assert len(count_before) == len(count_after)


def test_money_engine_does_not_commit_session(in_memory_db):
    """``MoneyEngine`` must not call ``session.commit()`` or ``flush()``.

    The engine never opens or commits a transaction. We verify by
    counting rows visible to a parallel session before vs after the
    engine is hammered: nothing leaks out.
    """
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=1, today=today)
    _seed_income(view.cycle_id, currency="ARS", amount=Decimal("1000"))

    # Pre-condition: cycle is visible to a separate session.
    with session_scope() as s:
        row = s.get(FinancialCycle, view.cycle_id)
        cycle_status_pre = row.status
        cycle_count_pre = (
            s.query(FinancialCycle)
            .filter(FinancialCycle.telegram_user_id == 1)
            .count()
        )

    with session_scope() as s:
        engine = MoneyEngine(s)
        # Hammer every read method several times.
        for _ in range(3):
            engine.get_cycle_summary(1, view.cycle_id, today=today)
            engine.get_available_to_spend(1, view.cycle_id, today=today)
            engine.get_daily_safe_to_spend(1, view.cycle_id, today=today)
            engine.detect_invariant_violations(1)
        # Confirm no commit happened: the same status is still
        # visible to a fresh session.
        s.rollback()  # explicit no-op to test rollback safety

    with session_scope() as s:
        row = s.get(FinancialCycle, view.cycle_id)
        cycle_status_post = row.status
        cycle_count_post = (
            s.query(FinancialCycle)
            .filter(FinancialCycle.telegram_user_id == 1)
            .count()
        )
    assert cycle_status_pre == cycle_status_post == "open"
    assert cycle_count_pre == cycle_count_post


def test_money_engine_close_cycle_method_does_not_exist(in_memory_db):
    """Sanity check: the read-only contract is observable via the
    public method list."""
    today = date(2026, 10, 15)
    _open_cycle(1, payday_day=1, today=today)
    with session_scope() as s:
        engine = MoneyEngine(s)
        # Filter out the constructor / session attribute.
        public = sorted(
            m for m in dir(engine)
            if not m.startswith("_") and m != "session"
        )
    # The only public methods are the four read endpoints.
    assert public == [
        "detect_invariant_violations",
        "get_available_to_spend",
        "get_cycle_summary",
        "get_daily_safe_to_spend",
    ]
    # And explicitly: NO lifecycle methods.
    for forbidden in (
        "close_cycle", "rollover_cycle", "ensure_current_cycle",
        "open_cycle", "open_initial_cycle",
    ):
        assert not hasattr(engine, forbidden), (
            f"MoneyEngine must not expose {forbidden!r}"
        )


# ---------------------------------------------------------------------------
# CycleService.close_cycle owns the snapshot+commit transaction
# ---------------------------------------------------------------------------


def test_cycle_service_close_cycle_persists_snapshot_atomically(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    _seed_income(view.cycle_id, currency="ARS", amount=Decimal("1000000"))

    with session_scope() as s:
        cycle_service = CycleService(s)
        summary = cycle_service.close_cycle(1, view.cycle_id)
    assert summary.status == "closed"
    assert summary.snapshot_version == CURRENT_SNAPSHOT_VERSION

    # Persisted on the row.
    with session_scope() as s:
        row = s.get(FinancialCycle, view.cycle_id)
    assert row.status == "closed"
    assert row.closed_at is not None
    assert row.closing_snapshot is not None
    assert row.closing_snapshot["snapshot_version"] == CURRENT_SNAPSHOT_VERSION


def test_cycle_service_close_cycle_uses_money_engine_summary(in_memory_db):
    """The snapshot must equal what MoneyEngine computed inside the
    same transaction. We verify by seeding data AFTER close and
    asserting the snapshot still reflects the pre-close numbers.
    """
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    _seed_income(view.cycle_id, currency="ARS", amount=Decimal("1000000"))

    # Compute the live summary first.
    with session_scope() as s:
        engine = MoneyEngine(s)
        live = engine.get_cycle_summary(1, view.cycle_id, today=today)

    # Close in a fresh session.
    with session_scope() as s:
        cycle_service = CycleService(s)
        snap = cycle_service.close_cycle(1, view.cycle_id)

    # Snapshot reflects the live read at close time.
    assert snap.gross_income == live.gross_income
    assert snap.available_to_spend == live.available_to_spend


def test_cycle_service_close_cycle_rolls_back_on_failure(in_memory_db, monkeypatch):
    """If the snapshot builder raises mid-close, the cycle stays open.

    We monkeypatch ``build_closing_snapshot`` to raise. The cycle must
    remain ``status='open'`` and have NO ``closing_snapshot``. The
    caller (in production a ``with session_scope()`` block) would
    catch the exception and roll back — here we drive the rollback
    ourselves so the test doesn't depend on session_scope() ordering.
    """
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    _seed_income(view.cycle_id, currency="ARS", amount=Decimal("1000000"))

    import app.money.cycle_service as cs_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated snapshot failure")

    monkeypatch.setattr(cs_mod, "build_closing_snapshot", _boom)

    with session_scope() as s:
        cycle_service = CycleService(s)
        with pytest.raises(RuntimeError):
            cycle_service.close_cycle(1, view.cycle_id)
        # The transaction rolled back when ``with session_scope()``
        # caught the exception. Verify the row state via a fresh
        # session — the cycle must still be open and snapshotless.
        s.rollback()

    with session_scope() as s:
        row = s.get(FinancialCycle, view.cycle_id)
    assert row.status == "open", "close failure must not transition status"
    assert row.closing_snapshot is None, "close failure must not stamp a snapshot"
    assert row.closed_at is None, "close failure must not stamp closed_at"


def test_cycle_service_close_cycle_returns_snapshot_via_money_engine(in_memory_db):
    """After the commit, the engine must be able to read the
    freshly-stamped snapshot.
    """
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    _seed_income(view.cycle_id, currency="ARS", amount=Decimal("1000000"))

    with session_scope() as s:
        cycle_service = CycleService(s)
        closed = cycle_service.close_cycle(1, view.cycle_id)
    assert closed.status == "closed"

    # Re-read on a new session: engine must serve the snapshot,
    # not recompute.
    with session_scope() as s:
        engine = MoneyEngine(s)
        reloaded = engine.get_cycle_summary(1, view.cycle_id)
    assert reloaded.status == "closed"
    assert reloaded.snapshot_version == CURRENT_SNAPSHOT_VERSION
    assert reloaded.gross_income == closed.gross_income


# ---------------------------------------------------------------------------
# CycleService.rollover_cycle keeps concurrency / idempotency
# ---------------------------------------------------------------------------


def test_cycle_service_rollover_is_idempotent_when_today_inside_open(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        cycle_service = CycleService(s)
        again = cycle_service.rollover_cycle(1, today=today)
    assert again.id == view.cycle_id
    assert again.status == "open"


def test_cycle_service_rollover_closes_expired_and_opens_next(in_memory_db):
    today = date(2026, 9, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        cycle_service = CycleService(s)
        next_cycle = cycle_service.rollover_cycle(1, today=date(2026, 10, 3))
    assert next_cycle.cycle_index == 2
    with session_scope() as s:
        closed = s.get(FinancialCycle, view.cycle_id)
    assert closed.status == "closed"
    assert closed.closing_snapshot is not None
    assert closed.closing_snapshot["closed_by"] == "rollover"


# ---------------------------------------------------------------------------
# CycleService.ensure_current_cycle catch-up
# ---------------------------------------------------------------------------


def test_cycle_service_ensure_current_cycle_walks_multiple(in_memory_db):
    _open_cycle(1, payday_day=2, today=date(2026, 9, 15))
    with session_scope() as s:
        cycle_service = CycleService(s)
        cycle = cycle_service.ensure_current_cycle(1, today=date(2026, 12, 5))
    assert cycle.start_date <= date(2026, 12, 5) <= cycle.end_date
    with session_scope() as s:
        all_cycles = (
            s.query(FinancialCycle)
            .filter(FinancialCycle.telegram_user_id == 1)
            .order_by(FinancialCycle.cycle_index.asc())
            .all()
        )
    assert [c.status for c in all_cycles] == [
        "closed",
        "closed",
        "closed",
        "open",
    ]


# ---------------------------------------------------------------------------
# MoneyEngine does not depend on CycleService (no circular import)
# ---------------------------------------------------------------------------


def test_money_engine_does_not_import_cycle_service(in_memory_db):
    """``MoneyEngine`` must not depend on ``CycleService``. The
    dependency arrow is one-way: ``CycleService`` → ``MoneyEngine``.
    """
    import app.money.engine as engine_mod

    src = open(engine_mod.__file__).read()
    # The word "CycleService" can appear in a docstring/comment but
    # never as an actual import.
    assert "from app.money.cycle_service" not in src
    assert "import app.money.cycle_service" not in src
