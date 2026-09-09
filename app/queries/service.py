from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config.settings import get_settings
from app.expenses.repository import ExpenseRepository
from app.queries.intents import ExpenseRow, QueryResult, QuerySpec

logger = logging.getLogger(__name__)


@dataclass
class _Range:
    start: date | None
    end: date | None
    label: str


class QueryService:
    """Safe, SQL-builder based query layer.

    The LLM never sees SQL. ``QueryService`` builds parameterized statements
    out of validated ``QuerySpec`` objects.
    """

    def __init__(self, repo: ExpenseRepository) -> None:
        self.repo = repo

    def today(self) -> date:
        return self._today_in_tz()

    def run(self, user_id: int, spec: QuerySpec) -> QueryResult:
        period = self._resolve_period(spec)
        filters = self._filters_text(spec)
        period_label = period.label or spec.label

        if spec.kind == "largest":
            row = self.repo.largest(user_id, period.start, period.end)
            if row is None:
                return QueryResult(
                    total_by_currency={},
                    items=[],
                    largest=None,
                    period_label=period_label,
                    filters=filters,
                )
            er = ExpenseRow(
                id=row.id,
                name=row.name,
                amount=row.amount,
                currency=row.currency,
                category=row.category_name,
                expense_date=row.expense_date,
            )
            return QueryResult(
                total_by_currency={row.currency: row.amount},
                items=None,
                largest=er,
                period_label=period_label,
                filters=filters,
            )

        if spec.kind == "list":
            limit = spec.limit or 10
            rows = self.repo.list_recent(user_id, limit=limit)
            items = [
                ExpenseRow(
                    id=r.id,
                    name=r.name,
                    amount=r.amount,
                    currency=r.currency,
                    category=r.category_name,
                    expense_date=r.expense_date,
                )
                for r in rows
            ]
            return QueryResult(
                total_by_currency={},
                items=items,
                largest=None,
                period_label=period_label,
                filters=filters,
            )

        if spec.kind == "category_breakdown":
            category_totals = self.repo.sum_by_category(
                user_id,
                start=period.start,
                end=period.end,
                currency=spec.currency,
            )
            return QueryResult(
                total_by_currency={},
                items=None,
                largest=None,
                category_totals=category_totals,
                period_label=period_label,
                filters=filters,
            )

        total_by_currency = self._compute_total(user_id, spec, period)
        return QueryResult(
            total_by_currency=total_by_currency,
            items=None,
            largest=None,
            period_label=period_label,
            filters=filters,
        )

    def _compute_total(
        self, user_id: int, spec: QuerySpec, period: _Range
    ) -> dict[str, Decimal]:
        category_name = spec.category
        category_id = None
        if category_name is not None:
            from app.categories.models import Category

            row = (
                self.repo.session.query(Category)
                .filter(Category.name == category_name)
                .one_or_none()
            )
            if row is not None:
                category_id = row.id

        if (category_name is not None or category_id is not None) and (
            period.start is None and period.end is None
        ):
            amount = self.repo.sum_total(
                user_id,
                start=None,
                end=None,
                category_id=category_id,
                category_name=None if category_id is not None else category_name,
            )
            return {spec.currency or "ARS": Decimal(amount)}

        if period.start is None and period.end is None:
            amount = self.repo.sum_total(
                user_id,
                category_id=category_id,
                category_name=None if category_id is not None else category_name,
            )
            return {spec.currency or "ARS": Decimal(amount)}

        return {
            currency: Decimal(total)
            for currency, total in self.repo.sum_by_period(
                user_id, period.start, period.end
            ).items()
        }

    def _resolve_period(self, spec: QuerySpec) -> _Range:
        today = self.today()
        if spec.kind == "list":
            return _Range(start=None, end=None, label="Últimos gastos")
        if spec.period is None:
            return _Range(start=None, end=today, label="Histórico")
        if spec.period == "today":
            return _Range(start=today, end=today, label="Hoy")
        if spec.period == "yesterday":
            y = today - timedelta(days=1)
            return _Range(start=y, end=y, label="Ayer")
        if spec.period == "week":
            start = today - timedelta(days=today.weekday())
            return _Range(start=start, end=today, label="Esta semana")
        if spec.period == "month":
            start = today.replace(day=1)
            return _Range(start=start, end=today, label="Este mes")
        if spec.period == "last_n_days":
            days = spec.days or 7
            start = today - timedelta(days=days - 1)
            return _Range(start=start, end=today, label=f"Últimos {days} días")
        return _Range(start=None, end=today, label="Histórico")

    def _filters_text(self, spec: QuerySpec) -> str:
        bits: list[str] = []
        if spec.category:
            bits.append(f"categoría: {spec.category}")
        if spec.currency and spec.currency.upper() != "ARS":
            bits.append(f"moneda: {spec.currency}")
        return ", ".join(bits)

    def _today_in_tz(self) -> date:
        tz_name = get_settings().timezone
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            logger.warning("Unknown timezone %s, falling back to UTC", tz_name)
            tz = ZoneInfo("UTC")
        return datetime.now(tz).date()
