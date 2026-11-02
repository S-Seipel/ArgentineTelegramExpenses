"""Monthly first-weekday paydays preserve frozen cycles and exact dates."""
from copy import deepcopy
from datetime import date, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.database.database import get_session_factory, session_scope
from app.money import CycleService, MoneyEngine, UserSettingsService
from app.money.cycle_resolution import cycle_window_for, payday_for, successor_window_for
from app.money.models import (
    CycleIncome, FinancialCycle, SavingsRealization, SavingsReserved, SavingsTarget, UserSettings,
)


USER = 123456
FRIDAY = {"pay_rule": "first_weekday", "payday_weekday": 4}


@pytest.mark.parametrize("year,month,expected", [
    (2027, 1, date(2027, 1, 1)),
    (2027, 5, date(2027, 5, 7)),
    (2026, 10, date(2026, 10, 2)),
    (2026, 11, date(2026, 11, 6)),
    (2028, 2, date(2028, 2, 4)),
    (2028, 3, date(2028, 3, 3)),
])
def test_first_friday_exact_dates(year, month, expected):
    assert payday_for(year, month, 1, **FRIDAY) == expected


@pytest.mark.parametrize("weekday", range(7))
def test_first_weekday_is_always_first_occurrence(weekday):
    due = payday_for(2026, 11, 1, pay_rule="first_weekday", payday_weekday=weekday)
    assert due.weekday() == weekday
    assert 1 <= due.day <= 7


@pytest.mark.parametrize("today,start,end,days", [
    (date(2026, 5, 15), date(2026, 5, 1), date(2026, 6, 4), 35),
    (date(2026, 11, 2), date(2026, 10, 2), date(2026, 11, 5), 35),
    (date(2027, 1, 1), date(2027, 1, 1), date(2027, 2, 4), 35),
    (date(2027, 1, 5), date(2027, 1, 1), date(2027, 2, 4), 35),
    (date(2028, 2, 29), date(2028, 2, 4), date(2028, 3, 2), 28),
    (date(2026, 12, 31), date(2026, 12, 4), date(2026, 12, 31), 28),
])
def test_first_friday_windows_cover_month_boundaries_and_leap_year(today, start, end, days):
    window = cycle_window_for(today, 1, **FRIDAY)
    assert (window.start_date, window.end_date) == (start, end)
    assert (end - start).days + 1 == days
    assert window.pay_rule == "first_weekday"
    assert window.payday_weekday == 4


@pytest.mark.parametrize("rule,weekday", [
    ("first_weekday", None), ("first_weekday", -1), ("first_weekday", 7),
    ("first_weekday", True), ("fixed_day", 4), ("unknown", None),
])
def test_calendar_helpers_reject_invalid_rules(rule, weekday):
    with pytest.raises(ValueError):
        cycle_window_for(date(2026, 11, 2), 1, pay_rule=rule, payday_weekday=weekday)


def test_successor_transition_is_contiguous_and_strictly_after_start():
    transition = successor_window_for(date(2026, 9, 1), 1, **FRIDAY)
    assert (transition.start_date, transition.end_date) == (date(2026, 9, 1), date(2026, 9, 3))
    natural = successor_window_for(date(2026, 9, 4), 1, **FRIDAY)
    assert (natural.start_date, natural.end_date) == (date(2026, 9, 4), date(2026, 10, 1))


def test_configuring_first_friday_does_not_create_cycles_or_money(in_memory_db):
    with session_scope() as session:
        service = UserSettingsService(session)
        default = service.get_settings(USER)
        assert (default.pay_rule, default.payday_weekday) == ("fixed_day", None)
        service.set_payday_day(USER, 15)
        configured = service.set_first_weekday(USER, timezone="America/Argentina/Buenos_Aires")
        assert (configured.pay_rule, configured.payday_weekday, configured.payday_day) == (
            "first_weekday", 4, 15,
        )
        for model in (FinancialCycle, CycleIncome, SavingsTarget, SavingsReserved, SavingsRealization):
            assert session.query(model).count() == 0
        fixed = service.set_payday_day(USER, 20)
        assert (fixed.pay_rule, fixed.payday_weekday, fixed.payday_day) == ("fixed_day", None, 20)


@pytest.mark.parametrize("rule,kwargs", [
    ("fixed_day", {}),
    ("fixed_day", {"payday_day": 5, "weekday": 4}),
    ("first_weekday", {}),
    ("first_weekday", {"weekday": True}),
    ("first_weekday", {"weekday": 7}),
    ("first_weekday", {"weekday": 4, "payday_day": 5}),
    ("other", {}),
])
def test_settings_service_rejects_ambiguous_calendar_inputs(in_memory_db, rule, kwargs):
    with session_scope() as session:
        with pytest.raises(ValueError):
            UserSettingsService(session).set_payday_rule(USER, rule, **kwargs)
        assert session.query(UserSettings).count() == 0


def test_open_cycle_freezes_first_friday_rule_without_inventing_income(in_memory_db):
    with session_scope() as session:
        UserSettingsService(session).set_first_weekday(USER)
        view = CycleService(session).open_initial_cycle(USER, date(2026, 11, 2))
        assert (view.start_date, view.end_date) == (date(2026, 10, 2), date(2026, 11, 5))
        assert (view.pay_rule, view.payday_weekday) == ("first_weekday", 4)
        summary = MoneyEngine(session).get_cycle_summary(USER, view.cycle_id, today=date(2026, 11, 2))
        assert (summary.pay_rule, summary.payday_weekday) == ("first_weekday", 4)
        assert summary.gross_income.is_empty()
        assert summary.savings_reserved.is_empty()
        assert summary.savings_realized.is_empty()


@pytest.mark.parametrize("manual_close", [False, True])
def test_switch_fixed_day_to_first_friday_applies_only_to_successor(in_memory_db, manual_close):
    with session_scope() as session:
        settings = UserSettingsService(session)
        settings.set_payday_day(USER, 1)
        initial = CycleService(session).open_initial_cycle(USER, date(2026, 8, 15))
        settings.set_first_weekday(USER)
        current = CycleService(session).ensure_current_cycle(USER, date(2026, 8, 20))
        assert (current.id, current.pay_rule, current.payday_weekday) == (initial.cycle_id, "fixed_day", None)
        assert (current.start_date, current.end_date) == (date(2026, 8, 1), date(2026, 8, 31))
        if manual_close:
            CycleService(session).close_cycle(USER, initial.cycle_id)
    with session_scope() as session:
        successor = CycleService(session).ensure_current_cycle(USER, date(2026, 9, 1))
        assert (successor.start_date, successor.end_date) == (date(2026, 9, 1), date(2026, 9, 3))
        assert (successor.pay_rule, successor.payday_weekday) == ("first_weekday", 4)
        old = session.get(FinancialCycle, initial.cycle_id)
        frozen = deepcopy(old.closing_snapshot)
        old_summary = MoneyEngine(session).get_cycle_summary(USER, initial.cycle_id)
        assert (old_summary.pay_rule, old_summary.payday_weekday) == ("fixed_day", None)
    with session_scope() as session:
        natural = CycleService(session).ensure_current_cycle(USER, date(2026, 9, 4))
        assert (natural.start_date, natural.end_date) == (date(2026, 9, 4), date(2026, 10, 1))
        assert session.get(FinancialCycle, initial.cycle_id).closing_snapshot == frozen


def test_switch_first_friday_to_fixed_preserves_closed_rule(in_memory_db):
    with session_scope() as session:
        settings = UserSettingsService(session)
        settings.set_first_weekday(USER)
        initial = CycleService(session).open_initial_cycle(USER, date(2026, 10, 15))
        settings.set_payday_day(USER, 20)
        successor = CycleService(session).ensure_current_cycle(USER, date(2026, 11, 6))
        assert (successor.start_date, successor.end_date) == (date(2026, 11, 6), date(2026, 11, 19))
        assert (successor.pay_rule, successor.payday_weekday) == ("fixed_day", None)
        old_summary = MoneyEngine(session).get_cycle_summary(USER, initial.cycle_id)
        assert (old_summary.pay_rule, old_summary.payday_weekday) == ("first_weekday", 4)


def test_first_friday_catch_up_preserves_contiguous_28_or_35_day_cycles(in_memory_db):
    with session_scope() as session:
        UserSettingsService(session).set_first_weekday(USER)
        CycleService(session).open_initial_cycle(USER, date(2026, 5, 3))
        current = CycleService(session).ensure_current_cycle(USER, date(2026, 9, 8))
        assert (current.start_date, current.end_date) == (date(2026, 9, 4), date(2026, 10, 1))
        cycles = session.execute(select(FinancialCycle).where(
            FinancialCycle.telegram_user_id == USER,
        ).order_by(FinancialCycle.cycle_index)).scalars().all()
        assert len(cycles) == 5
        for row in cycles:
            assert row.pay_rule == "first_weekday" and row.payday_weekday == 4
            assert row.start_date.weekday() == 4 and 1 <= row.start_date.day <= 7
            assert (row.end_date - row.start_date).days + 1 in {28, 35}
        for previous, following in zip(cycles, cycles[1:]):
            assert following.start_date == previous.end_date + timedelta(days=1)


def test_calendar_rule_uses_fresh_settings_despite_cached_identity_map(in_memory_db):
    with session_scope() as session:
        UserSettingsService(session).set_payday_day(USER, 1)
        CycleService(session).open_initial_cycle(USER, date(2026, 9, 15))
    with get_session_factory()() as stale:
        cached = stale.get(UserSettings, USER)
        assert cached.pay_rule == "fixed_day"
        with session_scope() as fresh:
            UserSettingsService(fresh).set_first_weekday(USER)
        successor = CycleService(stale).ensure_current_cycle(USER, date(2026, 10, 1))
        assert (successor.pay_rule, successor.payday_weekday) == ("first_weekday", 4)
        assert successor.end_date == date(2026, 10, 1)


@pytest.mark.parametrize("model", [UserSettings, FinancialCycle])
@pytest.mark.parametrize("rule,weekday", [
    ("first_weekday", None), ("first_weekday", -1), ("first_weekday", 7),
    ("fixed_day", 4), ("unknown", None),
])
def test_sql_constraints_reject_invalid_calendar_pairs(in_memory_db, model, rule, weekday):
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            kwargs = {"telegram_user_id": USER, "payday_day": 1, "pay_rule": rule, "payday_weekday": weekday}
            if model is FinancialCycle:
                kwargs.update(cycle_index=1, start_date=date(2026, 11, 1), end_date=date(2026, 11, 30))
            session.add(model(**kwargs))


def test_raw_sql_defaults_keep_existing_fixed_day_semantics(in_memory_db):
    with session_scope() as session:
        session.execute(text("INSERT INTO user_settings (telegram_user_id,payday_day) VALUES (:user,15)"), {"user": USER})
        row = session.get(UserSettings, USER)
        assert (row.payday_day, row.pay_rule, row.payday_weekday) == (15, "fixed_day", None)
