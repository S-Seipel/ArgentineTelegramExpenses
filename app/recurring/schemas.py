from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass
class RecurringCreate:
    telegram_user_id: int
    name: str
    amount: Decimal
    currency: str
    category_id: int
    frequency: str
    day_of_month: int
    month_of_year: int | None
    next_due_date: date


@dataclass
class RecurringOut:
    id: int
    name: str
    amount: Decimal
    currency: str
    category_id: int
    category_name: str
    frequency: str
    day_of_month: int
    month_of_year: int | None
    next_due_date: date
    last_notified_at: datetime | None
    is_active: bool