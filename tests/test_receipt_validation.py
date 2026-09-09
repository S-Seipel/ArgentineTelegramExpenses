from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.utils.receipt_validation import (
    is_amount_plausible,
    is_date_recent,
    parse_iso_date,
    score_receipt_confidence,
)


def test_is_amount_plausible_basic():
    assert is_amount_plausible(Decimal("100"))
    assert is_amount_plausible(Decimal("19280.50"))
    assert is_amount_plausible(Decimal("10000000"))


def test_is_amount_plausible_too_small():
    assert not is_amount_plausible(Decimal("50"))
    assert not is_amount_plausible(Decimal("0"))
    assert not is_amount_plausible(Decimal("0.01"))


def test_is_amount_plausible_negative():
    assert not is_amount_plausible(Decimal("-100"))


def test_is_amount_plausible_too_large():
    assert not is_amount_plausible(Decimal("20000000"))


def test_is_amount_plausible_usd():
    assert is_amount_plausible(Decimal("10"), currency="USD")
    assert not is_amount_plausible(Decimal("100000"), currency="USD")


def test_is_amount_plausible_none_or_invalid():
    assert not is_amount_plausible(None)
    assert not is_amount_plausible("not a number")


def test_is_date_recent_today():
    assert is_date_recent("2026-09-09", today=date(2026, 9, 9))


def test_is_date_recent_yesterday():
    assert is_date_recent("2026-09-08", today=date(2026, 9, 9))


def test_is_date_recent_old():
    assert not is_date_recent("2024-01-01", today=date(2026, 9, 9))


def test_is_date_recent_future():
    assert not is_date_recent("2027-01-01", today=date(2026, 9, 9))


def test_is_date_recent_invalid_format():
    assert not is_date_recent("not a date", today=date(2026, 9, 9))
    assert not is_date_recent(None, today=date(2026, 9, 9))
    assert not is_date_recent("01/01/2026", today=date(2026, 9, 9))


def test_parse_iso_date():
    assert parse_iso_date("2026-09-09") == date(2026, 9, 9)
    assert parse_iso_date("not a date") is None
    assert parse_iso_date(None) is None


def test_score_confidence_minimal():
    today = date(2026, 9, 9)
    parsed = {}
    assert score_receipt_confidence(parsed, today) == 0.30


def test_score_confidence_full():
    today = date(2026, 9, 9)
    parsed = {
        "merchant": "TIGRE",
        "total": Decimal("19280.50"),
        "currency": "ARS",
        "date": "2026-09-09",
        "category": "Supermercado",
    }
    assert score_receipt_confidence(parsed, today) == 1.0


def test_score_confidence_amount_invalid_caps_score():
    today = date(2026, 9, 9)
    parsed = {
        "merchant": "TIGRE",
        "total": Decimal("20"),  # too small
        "date": "2026-09-09",
        "category": "Supermercado",
    }
    # base 0.30 + 0.15 (merchant) + 0.05 (amount present but implausible) + 0.15 (date) + 0.10 (cat) = 0.75
    score = score_receipt_confidence(parsed, today)
    assert score < 1.0
    assert score >= 0.30


def test_score_confidence_other_category_no_bonus():
    today = date(2026, 9, 9)
    parsed = {
        "merchant": "X",
        "total": Decimal("1000"),
        "date": "2026-09-09",
        "category": "Otros",
    }
    # base 0.30 + 0.15 + 0.30 + 0.15 + 0 (no bonus for "Otros") = 0.90
    score = score_receipt_confidence(parsed, today)
    assert score == 0.90
