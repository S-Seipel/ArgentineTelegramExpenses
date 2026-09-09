"""Initial seed of categories.

This is the canonical list shipped with the app. The Alembic migration
``0003_categories_table`` loads these rows into the ``categories`` table.

To add or modify categories after install, prefer INSERT/UPDATE statements
on the table directly rather than editing this file. The application
re-reads the active category tree at every startup via
``app.categories.registry.refresh_from_db``.
"""
from __future__ import annotations

from typing import Optional


CATEGORY_ICON_OVERRIDES: dict[str, str] = {
    "Comida": "🍴",
    "Transporte": "🚦",
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
    "Café": "☕",
    "Supermercado": "🛒",
    "Restaurante": "🍽️",
    "Comida rápida": "🍔",
    "Delivery": "🛵",
    "Uber": "🚗",
    "Taxi": "🚕",
    "Combustible": "⛽",
    "Transporte público": "🚌",
    "Juegos": "🎮",
    "Cine": "🎬",
    "Suscripciones": "📺",
    "Salidas": "🎉",
}


DEFAULT_CATEGORY_TREE: dict[str, list[str]] = {
    "Comida": [
        "Supermercado",
        "Restaurante",
        "Comida rápida",
        "Café",
        "Delivery",
    ],
    "Transporte": [
        "Uber",
        "Taxi",
        "Combustible",
        "Transporte público",
    ],
    "Entretenimiento": [
        "Juegos",
        "Cine",
        "Suscripciones",
        "Salidas",
    ],
    "Ropa": [],
    "Tecnología": [],
    "Salud": [],
    "Educación": [],
    "Hogar": [],
    "Viajes": [],
    "Regalos": [],
    "Servicios": [],
    "Otros": [],
}


def iter_seed_rows() -> list[tuple[Optional[str], str, Optional[str]]]:
    """Yield ``(parent_name_or_none, name, icon)`` rows in parent-first order."""
    rows: list[tuple[Optional[str], str, Optional[str]]] = []
    for parent, children in DEFAULT_CATEGORY_TREE.items():
        rows.append((None, parent, CATEGORY_ICON_OVERRIDES.get(parent)))
        for child in children:
            rows.append((parent, child, CATEGORY_ICON_OVERRIDES.get(child)))
    return rows
