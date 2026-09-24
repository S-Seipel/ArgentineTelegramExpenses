"""SQLite-backed integration tests for the R1 cycle foundation.

These exercise the model + repository + service on the in-memory
SQLite fixture, which mirrors the F0 test harness. Concurrency at the
real PostgreSQL level is covered separately in
``test_money_cycle_pg.py``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.database.database import session_scope
from app.money import (
    CycleRolloverRequired,
    CycleService,
    UserSettingsService,
)
from app.money.models import FinancialCycle, UserSettings
from app.money.repository import (
    DEFAULT_PAYDAY_DAY,
    FinancialCycleRepository,
    UserSettingsRepository,
)


# ----------------------------------------------------------------------
# UserSettings
# ----------------------------------------------------------------------


def test_get_settings_returns_default_when_no_row(in_memory_db):
    with session_scope() as s:
        service = UserSettingsService(s)
        row = service.get_settings(user_id=42)
    assert row.payday_day == DEFAULT_PAYDAY_DAY
    assert row.telegram_user_id == 42


def test_set_payday_day_creates_row_lazily(in_memory_db):
    with session_scope() as s:
        service = UserSettingsService(s)
        row = service.set_payday_day(user_id=1, payday_day=15)
    assert row.payday_day == 15
    with session_scope() as s:
        repo = UserSettingsRepository(s)
        loaded = repo.get(1)
    assert loaded is not None
    assert loaded.payday_day == 15


def test_set_payday_day_updates_existing(in_memory_db):
    with session_scope() as s:
        UserSettingsService(s).set_payday_day(user_id=1, payday_day=5)
    with session_scope() as s:
        UserSettingsService(s).set_payday_day(user_id=1, payday_day=20)
    with session_scope() as s:
        repo = UserSettingsRepository(s)
        assert repo.get(1).payday_day == 20


def test_set_payday_day_rejects_zero(in_memory_db):
    with session_scope() as s:
        with pytest.raises(ValueError):
            UserSettingsService(s).set_payday_day(user_id=1, payday_day=0)


def test_set_payday_day_rejects_32(in_memory_db):
    with session_scope() as s:
        with pytest.raises(ValueError):
            UserSettingsService(s).set_payday_day(user_id=1, payday_day=32)


# ----------------------------------------------------------------------
# CycleService.open_initial_cycle
# ----------------------------------------------------------------------


def test_open_initial_cycle_creates_with_index_1(in_memory_db):
    with session_scope() as s:
        UserSettingsService(s).set_payday_day(user_id=1, payday_day=2)
    with session_scope() as s:
        service = CycleService(s)
        view = service.open_initial_cycle(user_id=1, today=date(2026, 10, 15))
    assert view.cycle_index == 1
    assert view.start_date == date(2026, 10, 2)
    assert view.end_date == date(2026, 11, 1)
    assert view.payday_day == 2
    assert view.status == "open"


def test_open_initial_cycle_uses_default_payday_when_no_settings(in_memory_db):
    with session_scope() as s:
        service = CycleService(s)
        view = service.open_initial_cycle(user_id=1, today=date(2026, 6, 17))
    assert view.payday_day == DEFAULT_PAYDAY_DAY
    assert view.start_date == date(2026, 6, 1)
    assert view.end_date == date(2026, 6, 30)


def test_open_initial_cycle_is_idempotent_on_same_today(in_memory_db):
    with session_scope() as s:
        UserSettingsService(s).set_payday_day(user_id=1, payday_day=10)
    with session_scope() as s:
        first = CycleService(s).open_initial_cycle(user_id=1, today=date(2026, 8, 15))
    with session_scope() as s:
        second = CycleService(s).open_initial_cycle(user_id=1, today=date(2026, 8, 15))
    assert first.cycle_id == second.cycle_id
    assert first.cycle_index == second.cycle_index == 1
    with session_scope() as s:
        rows = s.query(FinancialCycle).filter(
            FinancialCycle.telegram_user_id == 1
        ).all()
    assert len(rows) == 1


def test_open_initial_cycle_raises_rollover_when_history_exists(in_memory_db):
    with session_scope() as s:
        UserSettingsService(s).set_payday_day(user_id=1, payday_day=5)
        CycleService(s).open_initial_cycle(user_id=1, today=date(2026, 7, 1))
    # Move "today" to a date outside the previous window.
    with session_scope() as s:
        service = CycleService(s)
        with pytest.raises(CycleRolloverRequired):
            service.open_initial_cycle(user_id=1, today=date(2026, 10, 1))


def test_closing_snapshot_is_null_after_open(in_memory_db):
    with session_scope() as s:
        UserSettingsService(s).set_payday_day(user_id=1, payday_day=15)
    with session_scope() as s:
        CycleService(s).open_initial_cycle(user_id=1, today=date(2026, 9, 20))
    with session_scope() as s:
        row = s.query(FinancialCycle).filter(
            FinancialCycle.telegram_user_id == 1
        ).one()
    assert row.closing_snapshot is None
    assert row.status == "open"


# ----------------------------------------------------------------------
# CycleService.get_current_cycle (read-only)
# ----------------------------------------------------------------------


def test_get_current_cycle_returns_open_in_window(in_memory_db):
    with session_scope() as s:
        UserSettingsService(s).set_payday_day(user_id=1, payday_day=10)
        CycleService(s).open_initial_cycle(user_id=1, today=date(2026, 8, 15))
    with session_scope() as s:
        view = CycleService(s).get_current_cycle(
            user_id=1, today=date(2026, 8, 25)
        )
    assert view is not None
    assert view.start_date == date(2026, 8, 10)
    assert view.end_date == date(2026, 9, 9)


def test_get_current_cycle_returns_none_when_no_cycles(in_memory_db):
    with session_scope() as s:
        view = CycleService(s).get_current_cycle(
            user_id=999, today=date(2026, 8, 15)
        )
    assert view is None


def test_get_current_cycle_returns_none_when_open_but_expired(in_memory_db):
    """Direct-DB scenario: insert a cycle whose window doesn't contain today.

    Mirrors what a future rollover failure (R2 territory) would leave
    behind. R1 must refuse to surface it.
    """
    with session_scope() as s:
        UserSettingsService(s).set_payday_day(user_id=1, payday_day=10)
        repo = FinancialCycleRepository(s)
        repo.insert_cycle(
            user_id=1,
            cycle_index=1,
            start_date=date(2026, 1, 10),
            end_date=date(2026, 2, 9),
            payday_day=10,
            opened_at=datetime.now(timezone.utc),
        )
    with session_scope() as s:
        view = CycleService(s).get_current_cycle(
            user_id=1, today=date(2026, 8, 15)
        )
    assert view is None


def test_get_current_cycle_does_not_mutate(in_memory_db):
    """Read-only contract: get_current_cycle must not write anything.

    Counts rows before/after and confirms opened_at is untouched.
    """
    with session_scope() as s:
        UserSettingsService(s).set_payday_day(user_id=1, payday_day=10)
        cycle = CycleService(s).open_initial_cycle(
            user_id=1, today=date(2026, 8, 15)
        )
    before_count = 0
    before_opened = None
    with session_scope() as s:
        before_count = s.query(FinancialCycle).filter(
            FinancialCycle.telegram_user_id == 1
        ).count()
        before_opened = s.query(FinancialCycle).filter(
            FinancialCycle.id == cycle.cycle_id
        ).one().opened_at
    with session_scope() as s:
        CycleService(s).get_current_cycle(user_id=1, today=date(2026, 8, 20))
        CycleService(s).get_current_cycle(user_id=1, today=date(2026, 8, 25))
    after_count = 0
    after_opened = None
    with session_scope() as s:
        after_count = s.query(FinancialCycle).filter(
            FinancialCycle.telegram_user_id == 1
        ).count()
        after_opened = s.query(FinancialCycle).filter(
            FinancialCycle.id == cycle.cycle_id
        ).one().opened_at
    assert before_count == after_count == 1
    assert before_opened == after_opened


# ----------------------------------------------------------------------
# Repository primitives
# ----------------------------------------------------------------------


def test_repository_find_latest_cycle_returns_highest_index(in_memory_db):
    with session_scope() as s:
        repo = FinancialCycleRepository(s)
        first = repo.insert_cycle(
            user_id=1, cycle_index=1, start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31), payday_day=1,
            opened_at=datetime.now(timezone.utc),
        )
        # Only one OPEN cycle is allowed per user at a time — close the
        # first row before opening the next.
        first.status = "closed"
        first.closed_at = datetime.now(timezone.utc)
        s.commit()
        repo.insert_cycle(
            user_id=1, cycle_index=2, start_date=date(2026, 2, 1),
            end_date=date(2026, 2, 28), payday_day=1,
            opened_at=datetime.now(timezone.utc),
        )
    with session_scope() as s:
        latest = FinancialCycleRepository(s).find_latest_cycle(user_id=1)
    assert latest is not None
    assert latest.cycle_index == 2


def test_repository_list_cycles_orders_desc(in_memory_db):
    with session_scope() as s:
        repo = FinancialCycleRepository(s)
        for idx in (1, 2, 3):
            row = repo.insert_cycle(
                user_id=1, cycle_index=idx, start_date=date(2026, idx, 1),
                end_date=date(2026, idx, 28), payday_day=1,
                opened_at=datetime.now(timezone.utc),
            )
            row.status = "closed"
            row.closed_at = datetime.now(timezone.utc)
        s.commit()
    with session_scope() as s:
        items = FinancialCycleRepository(s).list_cycles(user_id=1)
    assert [c.cycle_index for c in items] == [3, 2, 1]


def test_repository_find_by_id_scoped_to_user(in_memory_db):
    with session_scope() as s:
        repo = FinancialCycleRepository(s)
        repo.insert_cycle(
            user_id=1, cycle_index=1, start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31), payday_day=1,
            opened_at=datetime.now(timezone.utc),
        )
    with session_scope() as s:
        repo = FinancialCycleRepository(s)
        assert repo.find_cycle_by_id(user_id=2, cycle_id=1) is None
        assert repo.find_cycle_by_id(user_id=1, cycle_id=999) is None
        assert repo.find_cycle_by_id(user_id=1, cycle_id=1) is not None
