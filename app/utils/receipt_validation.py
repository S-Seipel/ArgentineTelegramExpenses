"""Sanity checks for receipt-extracted fields.

Vision models can hallucinate plausible-looking numbers (e.g. picking
the wrong line item, fabricating a date). These helpers flag suspicious
extractions so the caller can lower the confidence or refuse the result.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

MAX_DAYS_AGO = 365

_CURRENCY_RANGES: dict[str, tuple[Decimal, Decimal]] = {
    "ARS": (Decimal("100"), Decimal("10000000")),
    "USD": (Decimal("0.50"), Decimal("50000")),
    "EUR": (Decimal("0.50"), Decimal("50000")),
    "BRL": (Decimal("1"), Decimal("100000")),
    "CLP": (Decimal("500"), Decimal("10000000")),
    "MXN": (Decimal("10"), Decimal("500000")),
    "UYU": (Decimal("20"), Decimal("500000")),
    "PYG": (Decimal("1000"), Decimal("10000000")),
    "GBP": (Decimal("0.50"), Decimal("50000")),
    "JPY": (Decimal("100"), Decimal("10000000")),
    "CNY": (Decimal("1"), Decimal("100000")),
}


def is_amount_plausible(
    amount: Optional[Decimal],
    currency: str = "ARS",
) -> bool:
    """Return True if the amount is in a believable range for the currency."""
    if amount is None:
        return False
    try:
        amount = Decimal(str(amount))
    except Exception:
        return False
    if amount <= 0:
        return False
    rng = _CURRENCY_RANGES.get(currency.upper(), (Decimal("0.01"), Decimal("999999999")))
    return rng[0] <= amount <= rng[1]


def is_date_recent(
    date_str: Optional[str],
    today: date,
    max_days_ago: int = MAX_DAYS_AGO,
) -> bool:
    """Return True if the date parses and is within the last ``max_days_ago``."""
    if not date_str or not isinstance(date_str, str):
        return False
    try:
        parsed = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        return False
    earliest = today - timedelta(days=max_days_ago)
    return earliest <= parsed <= today


def parse_iso_date(date_str: Optional[str]) -> Optional[date]:
    if not date_str or not isinstance(date_str, str):
        return None
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def score_receipt_confidence(parsed: dict, today: date) -> float:
    """Heuristic confidence based on how many fields we successfully extracted.

    Overrides the model's self-reported confidence, which we don't trust
    because vision LLMs tend to overestimate.
    """
    base = 0.30
    if parsed.get("merchant"):
        base += 0.15
    if parsed.get("total") and is_amount_plausible(parsed.get("total")):
        base += 0.30
    elif parsed.get("total"):
        base += 0.05
    if is_date_recent(parsed.get("date"), today):
        base += 0.15
    elif parsed.get("date"):
        base += 0.05
    category = parsed.get("category")
    if category and category != "Otros":
        base += 0.10
    return max(0.0, min(base, 1.0))
