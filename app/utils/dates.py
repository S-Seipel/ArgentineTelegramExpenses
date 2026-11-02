from __future__ import annotations

import calendar
import logging
import re
import unicodedata
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


def parse_date_range(text: str, *, today: date) -> tuple[date, date] | None:
    """Explicit inclusive ranges; without a year, use the current year."""
    raw = ''.join(char for char in unicodedata.normalize('NFKD', text.lower()) if not unicodedata.combining(char))
    iso = re.search(r'\b(?:del?|desde|from)\s+(\d{4}-\d{2}-\d{2})\s+(?:al?|hasta|to)\s+(\d{4}-\d{2}-\d{2})\b', raw)
    if iso:
        start, end = date.fromisoformat(iso[1]), date.fromisoformat(iso[2])
    else:
        months = {name: index for index, name in enumerate(
            ('', 'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre')) if name}
        common = re.search(r'\b(?:del?|desde)\s+(\d{1,2})\s+(?:al?|hasta)\s+(\d{1,2})\s+de\s+([a-z]+)(?:\s+(?:de\s+)?(\d{4}))?\b', raw)
        cross = re.search(r'\b(?:del?|desde)\s+(\d{1,2})\s+de\s+([a-z]+)\s+(?:al?|hasta)\s+(\d{1,2})\s+de\s+([a-z]+)(?:\s+(?:de\s+)?(\d{4}))?\b', raw)
        if common and common[3] in months:
            year = int(common[4] or today.year)
            start, end = date(year, months[common[3]], int(common[1])), date(year, months[common[3]], int(common[2]))
        elif cross and cross[2] in months and cross[4] in months:
            year = int(cross[5] or today.year)
            start, end = date(year, months[cross[2]], int(cross[1])), date(year, months[cross[4]], int(cross[3]))
        else:
            return None
    if start > end:
        raise ValueError('La fecha inicial debe ser anterior a la final.')
    return start, end


def today_in_tz(tz_name: str) -> date:
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning("Unknown timezone %s, falling back to UTC", tz_name)
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()
