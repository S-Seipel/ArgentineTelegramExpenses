from __future__ import annotations

from datetime import date

import pytest

from app.utils.corrections import parse_correction


@pytest.fixture
def base_current():
    return {
        "name": "Starbucks",
        "amount": 19280,
        "currency": "ARS",
        "category": "Café",
        "date": "2026-09-08",
        "confidence": 0.5,
    }


def test_parse_bare_number_is_amount(base_current):
    out = parse_correction("4500", base_current, today=date(2026, 9, 9))
    assert out["amount"] == 4500
    assert out["confidence"] == 1.0


def test_parse_bare_decimal(base_current):
    out = parse_correction("19280.50", base_current, today=date(2026, 9, 9))
    assert out["amount"] == 19280.50


def test_parse_bare_decimal_with_comma(base_current):
    out = parse_correction("19280,50", base_current, today=date(2026, 9, 9))
    assert out["amount"] == 19280.50


def test_parse_monto_prefix(base_current):
    out = parse_correction("monto: 4500", base_current, today=date(2026, 9, 9))
    assert out["amount"] == 4500
    assert out["confidence"] == 1.0


def test_parse_total_prefix(base_current):
    out = parse_correction("total: 9999", base_current, today=date(2026, 9, 9))
    assert out["amount"] == 9999


def test_parse_precio_prefix(base_current):
    out = parse_correction("precio: 150", base_current, today=date(2026, 9, 9))
    assert out["amount"] == 150


def test_parse_cuanto_prefix(base_current):
    out = parse_correction("cuánto: 100", base_current, today=date(2026, 9, 9))
    assert out["amount"] == 100


def test_parse_nombre_prefix(base_current):
    out = parse_correction("nombre: Carrefour", base_current, today=date(2026, 9, 9))
    assert out["name"] == "Carrefour"


def test_parse_comercio_prefix(base_current):
    out = parse_correction("comercio: Carrefour", base_current, today=date(2026, 9, 9))
    assert out["name"] == "Carrefour"


def test_parse_fecha_iso(base_current):
    out = parse_correction("fecha: 2026-09-08", base_current, today=date(2026, 9, 9))
    assert out["date"] == "2026-09-08"


def test_parse_other_text_falls_back_to_name(base_current):
    out = parse_correction("Carrefour", base_current, today=date(2026, 9, 9))
    assert out["name"] == "Carrefour"


def test_parse_negative_amount_rejected(base_current):
    out = parse_correction("-100", base_current, today=date(2026, 9, 9))
    # Negative should NOT match the bare-number branch, falls through to name
    assert out["amount"] == 19280  # unchanged
    assert out["name"] == "-100"


def test_parse_zero_amount_rejected(base_current):
    out = parse_correction("0", base_current, today=date(2026, 9, 9))
    assert out["amount"] == 19280  # unchanged
    assert out["name"] == "0"


def test_parse_preserves_other_fields(base_current):
    out = parse_correction("4500", base_current, today=date(2026, 9, 9))
    # Other fields preserved
    assert out["currency"] == "ARS"
    assert out["category"] == "Café"


def test_parse_empty_text(base_current):
    out = parse_correction("", base_current, today=date(2026, 9, 9))
    assert out == base_current  # no change
