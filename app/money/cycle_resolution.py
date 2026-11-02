"""Pure date-arithmetic helpers for financial cycle windows.

No DB. No money.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from app.utils.dates import last_day_of_month


def payday_for(
    year: int, month: int, payday_day: int, *,
    pay_rule: str = "fixed_day", payday_weekday: int | None = None,
) -> date:
    """Return the real payday date for a given year/month.

    ``payday_day`` is clamped to the last calendar day of the month
    (so payday_day=31 in February yields Feb 28 in non-leap years and
    Feb 29 in leap years; payday_day=31 in April yields Apr 30).
    ``first_weekday`` instead returns the first requested weekday in the
    month, where Monday is 0 and Friday is 4. ``payday_day`` is retained
    only as compatible metadata for that rule.
    """
    _validate_rule(payday_day, pay_rule, payday_weekday)
    if pay_rule == "first_weekday":
        first = date(year, month, 1)
        return first + timedelta(days=(payday_weekday - first.weekday()) % 7)
    last = last_day_of_month(year, month)
    day = min(payday_day, last)
    return date(year, month, day)


def _validate_rule(payday_day: int, pay_rule: str, payday_weekday: int | None) -> None:
    if type(payday_day) is not int or not 1 <= payday_day <= 31:
        raise ValueError("payday_day must be between 1 and 31")
    if pay_rule == "fixed_day":
        if payday_weekday is not None:
            raise ValueError("fixed_day must not specify payday_weekday")
    elif pay_rule == "first_weekday":
        if type(payday_weekday) is not int or not 0 <= payday_weekday <= 6:
            raise ValueError("first_weekday requires weekday between 0 and 6")
    else:
        raise ValueError("pay_rule must be fixed_day or first_weekday")


@dataclass(frozen=True)
class CycleWindow:
    """Business date window for one financial cycle.

    Both bounds are inclusive dates. A natural cycle starts on payday.
    A transition after a payday change instead starts immediately after
    the previous cycle; both end the day before the next payday.
    """

    start_date: date
    end_date: date
    payday_day: int
    pay_rule: str = "fixed_day"
    payday_weekday: int | None = None

    def contains(self, day: date) -> bool:
        return self.start_date <= day <= self.end_date


def cycle_window_for(
    today: date, payday_day: int, *,
    pay_rule: str = "fixed_day", payday_weekday: int | None = None,
) -> CycleWindow:
    """Resolve the cycle window that ``today`` falls in.

    Walks back to the most recent payday on/before ``today`` and forward
    to the next payday to compute the inclusive end date.
    """
    rule = {"pay_rule": pay_rule, "payday_weekday": payday_weekday}
    candidate = payday_for(today.year, today.month, payday_day, **rule)
    if candidate > today:
        candidate = payday_for(*_previous_month(today.year, today.month), payday_day, **rule)

    next_start = payday_for(*_next_month(candidate.year, candidate.month), payday_day, **rule)
    return CycleWindow(
        start_date=candidate,
        end_date=next_start - timedelta(days=1),
        payday_day=payday_day,
        **rule,
    )


def successor_window_for(
    start_date: date, payday_day: int, *,
    pay_rule: str = "fixed_day", payday_weekday: int | None = None,
) -> CycleWindow:
    """Start contiguously and end before the next configured payday.

    The next payday is strictly after ``start_date``. This preserves the
    existing cycle's boundaries when payday changes, creates one transition
    window, and then resumes natural windows without gaps or overlaps.
    """
    rule = {"pay_rule": pay_rule, "payday_weekday": payday_weekday}
    next_payday = payday_for(start_date.year, start_date.month, payday_day, **rule)
    if next_payday <= start_date:
        next_payday = payday_for(
            *_next_month(start_date.year, start_date.month), payday_day, **rule
        )
    return CycleWindow(
        start_date=start_date,
        end_date=next_payday - timedelta(days=1),
        payday_day=payday_day,
        **rule,
    )


def _previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)
