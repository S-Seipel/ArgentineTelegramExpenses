from __future__ import annotations

from decimal import Decimal

from app.utils.formatting import (
    format_amount,
    format_currency_amount,
    format_date_short,
    icon_for_category,
)


def test_format_amount_basic():
    assert format_amount(Decimal("10000")) == "10.000"
    assert format_amount(Decimal("0")) == "0"
    assert format_amount(Decimal("1234567")) == "1.234.567"


def test_format_amount_decimal_rounding():
    assert format_amount(Decimal("10.4")) == "10"
    assert format_amount(Decimal("10.5")) == "11"


def test_format_currency_amount():
    text = format_currency_amount(Decimal("10000"), "ARS")
    assert "10.000" in text and "ARS" in text
    text_usd = format_currency_amount(Decimal("20"), "USD")
    assert "US$" in text_usd and "20" in text_usd and "USD" in text_usd


def test_format_date_short():
    text = format_date_short(_d(2026, 1, 19))
    assert text == "19/01/2026"


def test_icon_for_category():
    assert icon_for_category("Café") == "☕"
    assert icon_for_category("Uber") == "🚗"
    assert icon_for_category("FooBar") == "🧾"


from datetime import date as _d
