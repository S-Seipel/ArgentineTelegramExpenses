"""A changed payday applies only to the next contiguous financial cycle."""
from copy import deepcopy
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.database.database import get_session_factory, session_scope
from app.money import CycleService, UserSettingsService
from app.money.cycle_resolution import CycleWindow, successor_window_for
from app.money.models import FinancialCycle, UserSettings


USER = 123456


@pytest.mark.parametrize("start,payday,end", [
    (date(2026, 10, 1), 5, date(2026, 10, 4)),
    (date(2026, 10, 5), 20, date(2026, 10, 19)),
    (date(2026, 10, 20), 5, date(2026, 11, 4)),
    (date(2026, 2, 28), 31, date(2026, 3, 30)),
    (date(2028, 2, 29), 31, date(2028, 3, 30)),
    (date(2026, 2, 20), 31, date(2026, 2, 27)),
    (date(2026, 2, 28), 30, date(2026, 3, 29)),
    (date(2026, 12, 31), 1, date(2026, 12, 31)),
    (date(2026, 10, 5), 5, date(2026, 11, 4)),
])
def test_successor_ends_before_first_strictly_later_payday(start, payday, end):
    assert successor_window_for(start, payday) == CycleWindow(start, end, payday)


@pytest.mark.parametrize("payday", [0, 32, -1])
def test_successor_rejects_invalid_payday(payday):
    with pytest.raises(ValueError):
        successor_window_for(date(2026, 10, 1), payday)


def _initial(old_payday, today):
    with session_scope() as session:
        UserSettingsService(session).set_payday_day(USER, old_payday)
        return CycleService(session).open_initial_cycle(USER, today)


@pytest.mark.parametrize("old_payday,today,new_payday,start,end", [
    (1, date(2026, 9, 15), 5, date(2026, 10, 1), date(2026, 10, 4)),
    (5, date(2026, 9, 15), 20, date(2026, 10, 5), date(2026, 10, 19)),
    (20, date(2026, 9, 25), 5, date(2026, 10, 20), date(2026, 11, 4)),
    (31, date(2026, 1, 31), 30, date(2026, 2, 28), date(2026, 3, 29)),
    (20, date(2026, 1, 25), 31, date(2026, 2, 20), date(2026, 2, 27)),
])
def test_payday_change_preserves_current_then_opens_transition(
    in_memory_db, old_payday, today, new_payday, start, end,
):
    initial = _initial(old_payday, today)
    with session_scope() as session:
        UserSettingsService(session).set_payday_day(USER, new_payday)
        current = CycleService(session).ensure_current_cycle(USER, today)
        assert current.id == initial.cycle_id
        assert current.payday_day == old_payday
        assert (current.start_date, current.end_date) == (initial.start_date, initial.end_date)
    with session_scope() as session:
        successor = CycleService(session).ensure_current_cycle(USER, start)
        assert successor.cycle_index == 2
        assert successor.start_date == initial.end_date + timedelta(days=1) == start
        assert successor.end_date == end
        assert successor.payday_day == new_payday
        old = session.get(FinancialCycle, initial.cycle_id)
        frozen = deepcopy(old.closing_snapshot)
        assert old.payday_day == old_payday
    with session_scope() as session:
        natural = CycleService(session).ensure_current_cycle(USER, end + timedelta(days=1))
        assert natural.start_date == end + timedelta(days=1)
        assert natural.payday_day == new_payday
        assert session.get(FinancialCycle, initial.cycle_id).closing_snapshot == frozen


def test_payday_change_after_manual_close_preserves_frozen_history(in_memory_db):
    initial = _initial(5, date(2026, 9, 15))
    with session_scope() as session:
        CycleService(session).close_cycle(USER, initial.cycle_id)
        frozen = deepcopy(session.get(FinancialCycle, initial.cycle_id).closing_snapshot)
    with session_scope() as session:
        UserSettingsService(session).set_payday_day(USER, 20)
        with pytest.raises(LookupError):
            CycleService(session).ensure_current_cycle(USER, date(2026, 9, 20))
        assert session.query(FinancialCycle).count() == 1
    with session_scope() as session:
        successor = CycleService(session).ensure_current_cycle(USER, date(2026, 10, 5))
        assert (successor.start_date, successor.end_date, successor.payday_day) == (
            date(2026, 10, 5), date(2026, 10, 19), 20,
        )
        old = session.get(FinancialCycle, initial.cycle_id)
        assert old.closing_snapshot == frozen
        assert (old.start_date, old.end_date, old.payday_day) == (
            initial.start_date, initial.end_date, 5,
        )


def test_changed_payday_catch_up_remains_contiguous(in_memory_db):
    _initial(1, date(2026, 9, 15))
    with session_scope() as session:
        UserSettingsService(session).set_payday_day(USER, 5)
        current = CycleService(session).ensure_current_cycle(USER, date(2026, 12, 10))
        assert (current.start_date, current.end_date) == (date(2026, 12, 5), date(2027, 1, 4))
        cycles = session.execute(select(FinancialCycle).where(
            FinancialCycle.telegram_user_id == USER,
        ).order_by(FinancialCycle.cycle_index)).scalars().all()
        assert [row.status for row in cycles] == ["closed"] * 4 + ["open"]
        assert [row.payday_day for row in cycles] == [1, 5, 5, 5, 5]
        for previous, successor in zip(cycles, cycles[1:]):
            assert successor.start_date == previous.end_date + timedelta(days=1)


def test_payday_read_ignores_stale_settings_identity_map(in_memory_db):
    initial = _initial(1, date(2026, 9, 15))
    factory = get_session_factory()
    with factory() as stale_session:
        cached = stale_session.get(UserSettings, USER)
        assert cached.payday_day == 1
        with session_scope() as fresh_session:
            UserSettingsService(fresh_session).set_payday_day(USER, 5)
        successor = CycleService(stale_session).ensure_current_cycle(
            USER, initial.end_date + timedelta(days=1),
        )
        assert successor.payday_day == 5
        assert successor.end_date == date(2026, 10, 4)
