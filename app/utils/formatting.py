"""Presentation helpers (no business logic, no I/O)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

CATEGORY_ICONS: dict[str, str] = {
    "Café": "☕",
    "Supermercado": "🛒",
    "Restaurante": "🍽️",
    "Comida rápida": "🍔",
    "Delivery": "🛵",
    "Comida": "🍴",
    "Uber": "🚗",
    "Taxi": "🚕",
    "Combustible": "⛽",
    "Transporte público": "🚌",
    "Transporte": "🚦",
    "Juegos": "🎮",
    "Cine": "🎬",
    "Suscripciones": "📺",
    "Salidas": "🎉",
    "Entretenimiento": "🕹️",
    "Ropa": "👕",
    "Tecnología": "💻",
    "Salud": "🩺",
    "Educación": "📚",
    "Hogar": "🏠",
    "Viajes": "✈️",
    "Regalos": "🎁",
    "Servicios": "📑",
    "Otros": "🧾",
}

CURRENCY_SYMBOLS: dict[str, str] = {
    "ARS": "$",
    "USD": "US$",
    "EUR": "€",
    "BRL": "R$",
    "CLP": "CLP$",
    "MXN": "MX$",
    "UYU": "$U",
    "PYG": "₲",
    "GBP": "£",
    "JPY": "¥",
    "CNY": "¥",
}


def format_amount(value: Decimal | float | int) -> str:
    """Argentine-style thousands with dot, no decimals for whole numbers."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    quantized = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    n = abs(int(quantized))
    text = f"{n:,}".replace(",", ".")
    return f"{sign}{text}"


def format_currency_amount(value: Decimal, currency: str) -> str:
    symbol = CURRENCY_SYMBOLS.get(currency.upper(), currency.upper())
    return f"{symbol} {format_amount(value)} {currency.upper()}"


def format_date_short(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def icon_for_category(category: str) -> str:
    return CATEGORY_ICONS.get(category, "🧾")
