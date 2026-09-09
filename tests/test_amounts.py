from __future__ import annotations

from decimal import Decimal

import pytest

from app.utils.amounts import (
    detect_currency_hint,
    find_dollar_amounts,
    find_thousand_amounts,
    normalize_amount,
    normalize_amounts_multi,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("gasté 10 lucas en café", [Decimal("10000")]),
        ("gaste 10 lucas en un pancho", [Decimal("10000")]),
        ("gasté 10k en Uber", [Decimal("10000")]),
        ("gasté 10 K en Uber", [Decimal("10000")]),
        ("gasté 10 mil en café", [Decimal("10000")]),
        ("gasté 5 palos en comida", [Decimal("5000")]),
        ("gasté 100 lucas en auto", [Decimal("100000")]),
        ("gasté 2 palos en nafta", [Decimal("2000")]),
        ("gasté 10 mangos en algo", [Decimal("10000")]),
        ("gasté 5 gambas en una pinta", [Decimal("5000")]),
    ],
)
def test_thousand_words_multiply(text: str, expected: list[Decimal]) -> None:
    parsed = find_thousand_amounts(text)
    assert [p.value for p in parsed] == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("gasté 20 dólares en Steam", [Decimal("20")]),
        ("gasté 20 dolares en algo", [Decimal("20")]),
        ("gasté 20 USD en algo", [Decimal("20")]),
        ("gasté 20 usd en algo", [Decimal("20")]),
        ("gasté USD 20 en algo", [Decimal("20")]),
    ],
)
def test_dollar_words_no_multiplier(
    text: str, expected: list[Decimal]
) -> None:
    parsed = find_dollar_amounts(text)
    assert [p.value for p in parsed] == expected


def test_multi_expense_thousand_keeps_order() -> None:
    text = "gasté 5k en café y 12k en Uber"
    parsed = find_thousand_amounts(text)
    assert [p.value for p in parsed] == [Decimal("5000"), Decimal("12000")]


def test_multi_expense_lucas_keeps_order() -> None:
    text = "hoy gasté 10 lucas en un café y 20 lucas en un taxi"
    parsed = find_thousand_amounts(text)
    assert [p.value for p in parsed] == [Decimal("10000"), Decimal("20000")]


def test_normalize_amount_uses_parsed_when_present() -> None:
    text = "gasté 10 lucas en café"
    out = normalize_amount(Decimal("10"), text)
    assert out == Decimal("10000")


def test_normalize_amount_falls_back_to_ai_when_text_has_nothing() -> None:
    text = "compré pan por 1500"
    assert normalize_amount(Decimal("1500"), text) == Decimal("1500")


def test_normalize_amounts_multi_overrides_each_when_count_matches() -> None:
    text = "gasté 5k en café y 12k en Uber"
    ai_amounts = [Decimal("5000"), Decimal("12000")]
    result = normalize_amounts_multi(ai_amounts, text)
    assert result == [Decimal("5000"), Decimal("12000")]


def test_normalize_amounts_multi_fixes_wrong_ai_output() -> None:
    """Simulate the AI returning '10' and '12' for '10k' and '12k'."""
    text = "gasté 10k en café y 12k en Uber"
    ai_amounts = [Decimal("10"), Decimal("12")]
    result = normalize_amounts_multi(ai_amounts, text)
    assert result == [Decimal("10000"), Decimal("12000")]


def test_normalize_amounts_multi_more_ai_than_text_uses_text_for_each_match() -> None:
    text = "gasté 10k en café"
    ai_amounts = [Decimal("10"), Decimal("99999")]
    result = normalize_amounts_multi(ai_amounts, text)
    # Only one parsed amount ("10k") in the text → both slots pick it up
    # because there's no reliable way to attribute a different number to a
    # missing expression. The validation layer will reject if both end up
    # identical and the user didn't imply that.
    assert result == [Decimal("10000"), Decimal("10000")]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("gasté 10 lucas en café", "ARS"),
        ("gasté 10 lucas en algo con 20 dólares", "USD"),
        ("$10.000 en café", "ARS"),
        ("gasté 10 lucas y 5 USD en algo", "USD"),
    ],
)
def test_detect_currency_hint(text: str, expected: str) -> None:
    assert detect_currency_hint(text) == expected


def test_normalize_amount_handles_10_lucas_with_ai_returning_decimal_10() -> None:
    """Reproduces the exact failure mode the user reported."""
    text = "gaste 10 lucas en un pancho"
    out = normalize_amount(Decimal("10"), text)
    assert out == Decimal("10000")


def test_normalize_amount_handles_10k_suffix() -> None:
    out = normalize_amount(Decimal("10"), "gasté 10k en Uber")
    assert out == Decimal("10000")


def test_normalize_amount_handles_10_mil() -> None:
    out = normalize_amount(Decimal("10"), "gasté 10 mil en café")
    assert out == Decimal("10000")


def test_no_thousand_no_dollar_in_text() -> None:
    text = "compré entradas por 3000"
    parsed = find_thousand_amounts(text)
    assert parsed == []
    parsed_dollars = find_dollar_amounts(text)
    assert parsed_dollars == []
