"""Unit tests for cycle date arithmetic (no DB)."""
from __future__ import annotations

from datetime import date

import pytest

from app.money.cycle_resolution import (
    CycleWindow,
    cycle_window_for,
    payday_for,
)


def test_payday_normal_clamp_not_needed():
    assert payday_for(2026, 1, 15) == date(2026, 1, 15)


def test_payday_31_january():
    assert payday_for(2026, 1, 31) == date(2026, 1, 31)


def test_payday_31_february_non_leap_year():
    assert payday_for(2026, 2, 31) == date(2026, 2, 28)


def test_payday_31_february_leap_year():
    assert payday_for(2028, 2, 31) == date(2028, 2, 29)


def test_payday_31_april():
    assert payday_for(2026, 4, 31) == date(2026, 4, 30)


def test_payday_rejects_zero():
    with pytest.raises(ValueError):
        payday_for(2026, 1, 0)


def test_payday_rejects_above_31():
    with pytest.raises(ValueError):
        payday_for(2026, 1, 32)


def test_window_payday_2_mid_month():
    w = cycle_window_for(date(2026, 10, 15), 2)
    assert w == CycleWindow(
        start_date=date(2026, 10, 2),
        end_date=date(2026, 11, 1),
        payday_day=2,
    )


def test_window_crossing_year_december_to_january():
    w = cycle_window_for(date(2026, 12, 15), 10)
    assert w.start_date == date(2026, 12, 10)
    assert w.end_date == date(2027, 1, 9)


def test_window_crossing_year_january_payday_in_current_year():
    """When today is BEFORE the January payday, the cycle started in December."""
    w = cycle_window_for(date(2027, 1, 5), 10)
    assert w.start_date == date(2026, 12, 10)
    assert w.end_date == date(2027, 1, 9)


def test_window_default_payday_1():
    w = cycle_window_for(date(2026, 6, 17), 1)
    assert w.start_date == date(2026, 6, 1)
    assert w.end_date == date(2026, 6, 30)


def test_window_payday_31_on_february_28_today_is_payday():
    w = cycle_window_for(date(2026, 2, 28), 31)
    assert w.start_date == date(2026, 2, 28)
    assert w.end_date == date(2026, 3, 30)


def test_window_payday_31_on_march_30_still_previous_cycle():
    w = cycle_window_for(date(2026, 3, 30), 31)
    assert w.start_date == date(2026, 2, 28)
    assert w.end_date == date(2026, 3, 30)


def test_window_payday_31_on_march_31_today_is_payday():
    w = cycle_window_for(date(2026, 3, 31), 31)
    assert w.start_date == date(2026, 3, 31)
    assert w.end_date == date(2026, 4, 29)


def test_cycle_window_contains():
    w = cycle_window_for(date(2026, 10, 15), 2)
    assert w.contains(date(2026, 10, 2))
    assert w.contains(date(2026, 10, 15))
    assert w.contains(date(2026, 11, 1))
    assert not w.contains(date(2026, 10, 1))
    assert not w.contains(date(2026, 11, 2))


def test_invalid_payday_zero_rejected():
    with pytest.raises(ValueError):
        cycle_window_for(date(2026, 10, 15), 0)


def test_invalid_payday_32_rejected():
    with pytest.raises(ValueError):
        cycle_window_for(date(2026, 10, 15), 32)


def test_invalid_payday_negative_rejected():
    with pytest.raises(ValueError):
        cycle_window_for(date(2026, 10, 15), -1)
