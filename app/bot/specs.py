"""Mapping from slash-commands to ``QuerySpec`` instances."""
from __future__ import annotations

from typing import Optional

from app.queries.intents import QuerySpec, make_category_breakdown_spec


def command_to_spec(command: str) -> Optional[QuerySpec]:
    table = {
        "/hoy": QuerySpec(kind="total", period="today", label="Hoy"),
        "/semana": QuerySpec(kind="total", period="week", label="Esta semana"),
        "/mes": QuerySpec(kind="total", period="month", label="Este mes"),
        "/gastos": QuerySpec(
            kind="list", limit=10, label="Últimos gastos"
        ),
        "/desglose": make_category_breakdown_spec(period="month"),
        "/desglose_hoy": make_category_breakdown_spec(period="today"),
        "/desglose_semana": make_category_breakdown_spec(period="week"),
        "/desglose_mes": make_category_breakdown_spec(period="month"),
    }
    return table.get(command.lower())
