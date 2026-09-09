from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class BudgetCreate:
    telegram_user_id: int
    category_id: int
    monthly_limit: Decimal
    currency: str


@dataclass
class BudgetOut:
    id: int
    category_id: int
    category_name: str
    monthly_limit: Decimal
    currency: str
    last_alerted_at: datetime | None
    is_active: bool


@dataclass
class BudgetAlert:
    """Result of checking a budget after an expense."""

    budget_id: int
    category_name: str
    spent: Decimal
    limit: Decimal
    currency: str
    percent: Decimal
    level: str  # "warning" | "exceeded"
