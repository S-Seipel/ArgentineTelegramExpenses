from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional


@dataclass
class FixedExpenseCreate:
    telegram_user_id: int
    name: str
    expected_amount: Decimal
    currency: str = "ARS"
    category_id: Optional[int] = None
    payment_method: Optional[str] = None
    due_day_of_month: int = 1


@dataclass
class FixedExpenseOut:
    id: int
    name: str
    expected_amount: Decimal
    currency: str
    category_id: Optional[int]
    category_name: str
    payment_method: Optional[str]
    due_day_of_month: int
    is_active: bool


@dataclass
class FixedExpenseWithStatus:
    """Combined bill + this-month payment status for display."""

    id: int
    name: str
    expected_amount: Decimal
    currency: str
    payment_method: Optional[str]
    due_day_of_month: int
    category_name: str
    is_active: bool
    paid: bool
    skipped: bool
    actual_amount: Optional[Decimal]
    paid_at: Optional[datetime]
    note: Optional[str]

    @property
    def status(self) -> str:
        if self.skipped:
            return "skipped"
        if self.paid:
            if self.actual_amount is None:
                return "paid_exact"
            diff = self.actual_amount - self.expected_amount
            if diff > 0:
                return "paid_more"
            if diff < 0:
                return "paid_less"
            return "paid_exact"
        return "pending"

    @property
    def diff(self) -> Optional[Decimal]:
        if self.actual_amount is None:
            return None
        return self.actual_amount - self.expected_amount


@dataclass
class MonthlyBudgetOut:
    income: Decimal
    extra: Decimal
    total_fixed_expected: Decimal
    total_fixed_paid: Decimal
    liberado: Decimal
