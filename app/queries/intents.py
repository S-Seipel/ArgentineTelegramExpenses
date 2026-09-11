from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

Period = Literal["today", "yesterday", "week", "month", "all", "last_n_days"]
SortOrder = Literal["amount_desc", "amount_asc", "date_desc", "date_asc"]


@dataclass
class QuerySpec:
    kind: Literal[
        "total", "category_total", "category_breakdown",
        "list", "largest", "comparison",
    ]
    period: Period | None = None
    days: int | None = None
    category: str | None = None
    currency: str | None = None
    limit: int | None = None
    order_by: SortOrder | None = None
    label: str = ""


@dataclass
class QueryResult:
    total_by_currency: dict[str, Decimal]
    items: list["ExpenseRow"] | None = None
    largest: "ExpenseRow | None" = None
    category_totals: dict[str, "dict[str, Decimal]"] | None = None
    comparison: "ComparisonRow | None" = None
    period_label: str = ""
    filters: str = ""


@dataclass
class CategoryDiff:
    category: str
    current: Decimal
    previous: Decimal
    diff_pct: float | None  # None when previous was 0


@dataclass
class ComparisonRow:
    current_label: str
    previous_label: str
    current_total: Decimal
    previous_total: Decimal
    total_diff_pct: float | None
    by_category: list[CategoryDiff]


@dataclass
class ExpenseRow:
    id: int
    name: str
    amount: Decimal
    currency: str
    category: str
    expense_date: date


def resolve_query_spec(
    ai_intent, *, default_currency: str = "ARS"
) -> QuerySpec:
    """Translate an AI ``QueryIntent`` into a deterministic ``QuerySpec``."""
    from app.ai.schemas import IntentType  # noqa: F401

    q = ai_intent.query
    if q is None:
        return QuerySpec(kind="list", limit=10, label="Últimos gastos")

    period = q.period
    days = q.days
    category = q.category
    currency = (q.currency or default_currency).upper()
    limit = q.limit or 10
    order_by = q.order_by or "date_desc"
    explicit_limit = q.limit is not None

    label_for: dict[str, str] = {
        "today": "Hoy",
        "yesterday": "Ayer",
        "week": "Esta semana",
        "month": "Este mes",
        "all": "Histórico",
        "last_n_days": f"Últimos {days or 7} días",
    }

    label = label_for.get(period or "", "Últimos gastos")

    kind = "total"
    if period is None:
        kind = "list" if explicit_limit or order_by == "date_desc" else "total"
    elif period == "last_n_days":
        kind = "total"
    else:
        kind = "total"

    if (
        category is None
        and order_by in ("amount_desc",)
        and explicit_limit
        and period is None
    ):
        kind = "list"

    if (
        category is None
        and order_by in ("amount_desc",)
        and limit == 1
    ):
        kind = "largest"
        label = f"Gasto más grande ({label.lower()})"

    return QuerySpec(
        kind=kind,
        period=period if period != "all" else None,
        days=days,
        category=category,
        currency=currency,
        limit=limit,
        order_by=order_by,
        label=label,
    )


def make_category_breakdown_spec(
    *, period: Period | None = "month", label: str | None = None
) -> QuerySpec:
    """Build a QuerySpec that groups expenses by category within a period."""
    label_for: dict[str, str] = {
        "today": "Hoy",
        "yesterday": "Ayer",
        "week": "Esta semana",
        "month": "Este mes",
        "all": "Histórico",
    }
    if label is None:
        label = (
            f"Desglose {label_for.get(period or '', 'Histórico').lower()}"
        )
    return QuerySpec(
        kind="category_breakdown",
        period=period if period != "all" else None,
        label=label,
    )


def make_comparison_spec(
    *, period: Period | None = "month", label: str | None = None
) -> QuerySpec:
    """Build a QuerySpec that compares the given period against the previous one."""
    label_for: dict[str, str] = {
        "today": "Hoy",
        "yesterday": "Ayer",
        "week": "Esta semana",
        "month": "Este mes",
        "all": "Histórico",
    }
    if label is None:
        label = (
            f"Comparativa {label_for.get(period or '', 'Histórico').lower()}"
        )
    return QuerySpec(
        kind="comparison",
        period=period if period != "all" else None,
        label=label,
    )
