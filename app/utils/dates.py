from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timedelta

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


def parse_relative_date(
    text: str | None,
    *,
    today: date | None = None,
    tz_name: str = "America/Argentina/Buenos_Aires",
) -> date | None:
    """Lightweight parser for the date strings that come from the AI.

    The AI already produces ISO-format dates by spec; this helper is for the
    fallback path where we may need to handle simple Spanish expressions.
    """
    if not text:
        return None
    raw = text.strip().lower()
    if not raw:
        return None

    if today is None:
        today = today_in_tz(tz_name)

    if raw in ("hoy", "today"):
        return today
    if raw in ("ayer", "yesterday"):
        return today - timedelta(days=1)
    if raw in ("anteayer", "anteayer.", "day before yesterday"):
        return today - timedelta(days=2)

    if raw in ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"):
        target = _WEEKDAYS_ES[raw]
        diff = (today.weekday() - target) % 7
        if diff == 0:
            return today - timedelta(days=7)
        return today - timedelta(days=diff)

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def last_day_of_month(year: int, month: int) -> int:
    """Return the last calendar day for the given month (handles Feb leap years)."""
    return calendar.monthrange(year, month)[1]


_WEEKDAYS_ES = {
    "lunes": 0,
    "martes": 1,
    "miércoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sábado": 5,
    "domingo": 6,
}


def today_in_tz(tz_name: str) -> date:
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning("Unknown timezone %s, falling back to UTC", tz_name)
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()
