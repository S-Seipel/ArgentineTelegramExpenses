"""Centralized business-date / now helpers.

Single source of truth for "today" and "now" used by Telegram-facing
logic. Honors the configured ``TIMEZONE`` setting so ``/hoy``, ``/semana``,
``/mes`` and the dashboard stay consistent regardless of the server clock.

Anything that would otherwise call ``date.today()``, ``datetime.utcnow()``
or naive ``datetime.now()`` in business logic should go through here.
SQLAlchemy ``DateTime(timezone=True)`` columns still rely on the DB-side
``now()`` and stay untouched — they are technical timestamps, not
business dates.
"""
from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import logging

logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _tz_for(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("Unknown timezone %s, falling back to UTC", name)
        return ZoneInfo("UTC")


def _configured_tz() -> ZoneInfo:
    # Imported lazily so this module is safe to import at app boot
    # before settings have been initialized.
    from app.config.settings import get_settings

    return _tz_for(get_settings().timezone)


def business_now() -> datetime:
    """Aware datetime in the configured business timezone."""
    return datetime.now(_configured_tz())


def business_today() -> date:
    return business_now().date()
