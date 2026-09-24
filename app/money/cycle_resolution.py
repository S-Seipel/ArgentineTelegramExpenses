"""Pure date-arithmetic helpers for financial cycle windows.

R1 only. No DB. No money.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from app.utils.dates import last_day_of_month


def payday_for(year: int, month: int, payday_day: int) -> date:
    """Return the real payday date for a given year/month.

    ``payday_day`` is clamped to the last calendar day of the month
    (so payday_day=31 in February yields Feb 28 in non-leap years and
    Feb 29 in leap years; payday_day=31 in April yields Apr 30).
    """
    if payday_day < 1 or payday_day > 31:
        raise ValueError("payday_day must be between 1 and 31")
    last = last_day_of_month(year, month)
    day = min(payday_day, last)
    return date(year, month, day)


@dataclass(frozen=True)
class CycleWindow:
    """Business date window for one financial cycle.

    Both bounds are inclusive dates. ``start_date`` equals the payday
    that started the cycle; ``end_date`` equals the day before the next
    payday — that is, the last day of the cycle.
    """

    start_date: date
    end_date: date
    payday_day: int

    def contains(self, day: date) -> bool:
        return self.start_date <= day <= self.end_date


def cycle_window_for(today: date, payday_day: int) -> CycleWindow:
    """Resolve the cycle window that ``today`` falls in.

    Walks back to the most recent payday on/before ``today`` and forward
    to the next payday to compute the inclusive end date.
    """
    if payday_day < 1 or payday_day > 31:
        raise ValueError("payday_day must be between 1 and 31")

    candidate = payday_for(today.year, today.month, payday_day)
    if candidate > today:
        candidate = payday_for(*_previous_month(today.year, today.month), payday_day)

    next_start = payday_for(*_next_month(candidate.year, candidate.month), payday_day)
    return CycleWindow(
        start_date=candidate,
        end_date=next_start - timedelta(days=1),
        payday_day=payday_day,
    )


def _previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)
