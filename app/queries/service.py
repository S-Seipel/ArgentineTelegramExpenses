from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config.settings import get_settings
from app.expenses.repository import ExpenseRepository
from app.queries.intents import (
    CategoryDiff,
    ComparisonRow,
    ExpenseRow,
    QueryResult,
    QuerySpec,
)

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

        if spec.kind == "comparison":
            comparison = self._build_comparison(user_id, spec)
            return QueryResult(
                total_by_currency={},
                items=None,
                largest=None,
                comparison=comparison,
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

    def _build_comparison(
        self, user_id: int, spec: QuerySpec
    ) -> ComparisonRow:
        current = self._resolve_period(spec)
        prev_label_for = {
            "month": "Mes pasado",
            "week": "Semana pasada",
            "today": "Ayer",
            "yesterday": "Anteayer",
            "all": "Histórico previo",
        }
        previous = self._previous_period(spec, current)

        def _by_cat(p: _Range) -> dict[str, Decimal]:
            return self.repo.sum_by_category(
                user_id,
                start=p.start,
                end=p.end,
                currency=spec.currency,
            )

        cur_cats = _by_cat(current)
        prev_cats = _by_cat(previous)
        cur_total = sum(cur_cats.values(), Decimal("0"))
        prev_total = sum(prev_cats.values(), Decimal("0"))

        total_diff_pct: float | None
        if prev_total > 0:
            total_diff_pct = float(
                (cur_total - prev_total) / prev_total * 100
            )
        else:
            total_diff_pct = None

        rows: list[CategoryDiff] = []
        all_cats = set(cur_cats) | set(prev_cats)
        for cat in sorted(
            all_cats, key=lambda c: -cur_cats.get(c, Decimal("0"))
        ):
            c_amt = cur_cats.get(cat, Decimal("0"))
            p_amt = prev_cats.get(cat, Decimal("0"))
            d_pct: float | None
            if p_amt > 0:
                d_pct = float((c_amt - p_amt) / p_amt * 100)
            else:
                d_pct = None
            rows.append(
                CategoryDiff(
                    category=cat,
                    current=c_amt,
                    previous=p_amt,
                    diff_pct=d_pct,
                )
            )
        return ComparisonRow(
            current_label=current.label or spec.label or "Período actual",
            previous_label=prev_label_for.get(
                spec.period or "month", "Período anterior"
            ),
            current_total=cur_total,
            previous_total=prev_total,
            total_diff_pct=total_diff_pct,
            by_category=rows,
        )

    def _previous_period(
        self, spec: QuerySpec, current: _Range
    ) -> _Range:
        today = self.today()
        if spec.period == "month" and current.start is not None:
            if current.start.month == 1:
                prev_year = current.start.year - 1
                prev_month = 12
            else:
                prev_year = current.start.year
                prev_month = current.start.month - 1
            from calendar import monthrange

            _, last = monthrange(prev_year, prev_month)
            return _Range(
                start=date(prev_year, prev_month, 1),
                end=date(prev_year, prev_month, last),
                label="Mes pasado",
            )
        if spec.period == "today" and current.start is not None:
            y = current.start - timedelta(days=1)
            return _Range(start=y, end=y, label="Ayer")
        if spec.period == "week" and current.start is not None:
            return _Range(
                start=current.start - timedelta(days=7),
                end=current.end - timedelta(days=7),
                label="Semana pasada",
            )
        # Default: same length immediately preceding
        if current.start is not None and current.end is not None:
            length = (current.end - current.start).days
            prev_end = current.start - timedelta(days=1)
            prev_start = prev_end - timedelta(days=length)
            return _Range(
                start=prev_start, end=prev_end, label="Período anterior"
            )
        return _Range(
            start=date(today.year, 1, 1) if today.month > 1 else None,
            end=today.replace(year=today.year - 1, month=12, day=31)
            if today.month == 1
            else today.replace(month=today.month - 1, day=1),
            label="Período anterior",
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
