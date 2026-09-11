from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional


@dataclass
class ExpenseCreate:
    telegram_user_id: int
    name: str
    amount: Decimal
    currency: str
    category_id: int
    expense_date: date
    original_message: str
    ai_confidence: Decimal
    source_type: str = "telegram"
    source_key: Optional[str] = None
    telegram_chat_id: Optional[int] = None
    telegram_message_id: Optional[int] = None
    revision: int = 1


@dataclass
class ExpenseSummary:
    id: int
    name: str
    amount: Decimal
    currency: str
    category_id: int
    category_name: str
    expense_date: date
    ai_confidence: Decimal
    created_at: datetime
    original_message: str = ""
    source_type: str = "legacy"
    source_key: Optional[str] = None
    deleted_at: Optional[datetime] = None
    revision: int = 1


class ExpenseOut:
    """Light DTO used at service boundaries."""

    def __init__(
        self,
        id: int,
        name: str,
        amount: Decimal,
        currency: str,
        category_id: int,
        category_name: str,
        expense_date: date,
        ai_confidence: Decimal,
        created_at: datetime,
    ) -> None:
        self.id = id
        self.name = name
        self.amount = amount
        self.currency = currency
        self.category_id = category_id
        self.category_name = category_name
        self.expense_date = expense_date
        self.ai_confidence = ai_confidence
        self.created_at = created_at

    def __repr__(self) -> str:
        return (
            f"<ExpenseOut id={self.id} name={self.name!r} "
            f"amount={self.amount} {self.currency}>"
        )
