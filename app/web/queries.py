"""DB queries used by the dashboard endpoints.

Kept separate from ``routes.py`` so they can be unit-tested independently
of HTTP plumbing.

Currency correctness (Phase 0):

- Every aggregate is grouped by currency in the SQL itself.
- The legacy scalar fields (``by_category``, ``points``, ``spent`` …)
  are still emitted when only ONE currency is present, so the existing
  dashboard keeps rendering without changes.
- When MULTIPLE currencies are present, the legacy scalars that would
  otherwise mix currencies are replaced with ``null`` (omitting them
  would risk breaking frontend assumptions) and per-currency
  counterparts (``by_category_by_currency``, ``points_by_currency`` …)
  are added.

Soft delete:

- Every read filters ``expenses.deleted_at IS NULL`` so a soft-deleted
  row (e.g. one produced by a fixed payment that was then un-paid)
  never reaches the dashboard or any aggregate.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.budgets.models import Budget
from app.categories.models import Category
from app.expenses.models import Expense
from app.recurring.models import RecurringExpense


_NOT_DELETED = Expense.deleted_at.is_(None)


def _money(value: Decimal) -> float:
    """Render Decimal money as a fixed-precision float for the JSON API.

    ``quantize`` keeps it at 2 decimals before the cast so the float
    doesn't introduce spurious digits. Aggregations are done in Decimal
    end-to-end — this conversion only happens at the serialization
    boundary, where the dashboard's chart libs expect numbers.
    """
    return float(value.quantize(Decimal("0.01")))


def _scalar_for_single_currency(
    per_currency: dict[str, Decimal],
) -> Decimal | None:
    """Return the single currency's value, or ``None`` if there are
    multiple currencies (mixing them would be a financial lie)."""
    if len(per_currency) == 1:
        return next(iter(per_currency.values()))
    return None


@dataclass
class PeriodWindow:
    label: str
    start: date
    end: date
    days_elapsed: int
    days_in_period: int
    is_full_period: bool


def resolve_period(
    period: str,
    today: date,
    *,
    custom_start: date | None = None,
    custom_end: date | None = None,
) -> PeriodWindow:
    """Map a string period identifier to a ``(start, end)`` window.

    Supports: today, week, month, last_month, year, all, custom.
    """
    if period == "today":
        return PeriodWindow(
            label="Hoy",
            start=today,
            end=today,
            days_elapsed=1,
            days_in_period=1,
            is_full_period=True,
        )
    if period == "week":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
        return PeriodWindow(
            label="Esta semana",
            start=start,
            end=end,
            days_elapsed=(today - start).days + 1,
            days_in_period=7,
            is_full_period=False,
        )
    if period == "month":
        start = today.replace(day=1)
        _, last = calendar.monthrange(today.year, today.month)
        end = today.replace(day=last)
        return PeriodWindow(
            label="Este mes",
            start=start,
            end=end,
            days_elapsed=today.day,
            days_in_period=last,
            is_full_period=False,
        )
    if period == "last_month":
        if today.month == 1:
            start = today.replace(year=today.year - 1, month=12, day=1)
        else:
            start = today.replace(month=today.month - 1, day=1)
        _, last = calendar.monthrange(start.year, start.month)
        end = start.replace(day=last)
        return PeriodWindow(
            label="Mes pasado",
            start=start,
            end=end,
            days_elapsed=last,
            days_in_period=last,
            is_full_period=True,
        )
    if period == "year":
        start = today.replace(month=1, day=1)
        end = today.replace(month=12, day=31)
        return PeriodWindow(
            label="Este año",
            start=start,
            end=end,
            days_elapsed=(today - start).days + 1,
            days_in_period=(end - start).days + 1,
            is_full_period=False,
        )
    if period == "yesterday":
        y = today - timedelta(days=1)
        return PeriodWindow(
            label="Ayer",
            start=y,
            end=y,
            days_elapsed=1,
            days_in_period=1,
            is_full_period=True,
        )
    if period == "last_week":
        this_week_start = today - timedelta(days=today.weekday())
        prev_week_start = this_week_start - timedelta(days=7)
        prev_week_end = this_week_start - timedelta(days=1)
        return PeriodWindow(
            label="Semana pasada",
            start=prev_week_start,
            end=prev_week_end,
            days_elapsed=7,
            days_in_period=7,
            is_full_period=True,
        )
    if period == "last_year":
        return PeriodWindow(
            label="Año pasado",
            start=today.replace(year=today.year - 1, month=1, day=1),
            end=today.replace(year=today.year - 1, month=12, day=31),
            days_elapsed=365,
            days_in_period=365,
            is_full_period=True,
        )
    if period == "custom" and custom_start and custom_end:
        days = (custom_end - custom_start).days + 1
        elapsed = max(1, min(days, (today - custom_start).days + 1))
        return PeriodWindow(
            label=f"{custom_start} → {custom_end}",
            start=custom_start,
            end=custom_end,
            days_elapsed=elapsed,
            days_in_period=days,
            is_full_period=(today >= custom_end),
        )
    # Default to month.
    return resolve_period("month", today)


def summary_for_period(
    session: Session,
    user_id: int,
    window: PeriodWindow,
) -> dict:
    """Return totals grouped by category and by currency for the window."""
    by_cat_rows = session.execute(
        select(
            Category.name,
            Expense.currency,
            Expense.amount,
        )
        .join(Category, Category.id == Expense.category_id)
        .where(
            Expense.telegram_user_id == user_id,
            Expense.expense_date >= window.start,
            Expense.expense_date <= window.end,
            _NOT_DELETED,
        )
    ).all()

    # Per-category-per-currency: keep currencies separated.
    cat_per_currency: dict[str, dict[str, Decimal]] = {}
    # Flat per-currency totals (USD and ARS stay separate).
    total_by_currency: dict[str, Decimal] = {}
    for cat_name, currency, amount in by_cat_rows:
        cur = currency or "ARS"
        cat_per_currency.setdefault(cat_name, {})
        cat_per_currency[cat_name][cur] = (
            cat_per_currency[cat_name].get(cur, Decimal("0"))
            + Decimal(amount or 0)
        )
        total_by_currency[cur] = total_by_currency.get(
            cur, Decimal("0")
        ) + Decimal(amount or 0)

    # Top categories by amount (per-currency). Keeps the dashboard's
    # "top 10" cap while being currency-safe.
    by_category_by_currency: dict[str, list[dict]] = {}
    for cur in total_by_currency:
        rows = [
            (name, per[cur])
            for name, per in cat_per_currency.items()
            if per.get(cur)
        ]
        rows.sort(key=lambda kv: kv[1], reverse=True)
        by_category_by_currency[cur] = [
            {"name": name, "amount": _money(amt)} for name, amt in rows[:10]
        ]

    single_total = _scalar_for_single_currency(total_by_currency)

    # Legacy field. When there's one currency, keep the existing shape
    # so the dashboard JS doesn't change. When there are multiple
    # currencies, the scalar would mix them, which is a financial lie,
    # so we return ``None`` (rendered as JSON null).
    if single_total is None:
        by_category_legacy = None
    else:
        cur = next(iter(total_by_currency))
        rows = [
            (name, per[cur])
            for name, per in cat_per_currency.items()
            if per.get(cur)
        ]
        rows.sort(key=lambda kv: kv[1], reverse=True)
        by_category_legacy = [
            {"name": name, "amount": float(amt)}
            for name, amt in rows[:10]
        ]

    return {
        "period_label": window.label,
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "days_elapsed": window.days_elapsed,
        "days_in_period": window.days_in_period,
        "is_full_period": window.is_full_period,
        # Always present, currency-aware.
        "total_by_currency": {
            k: _money(v) for k, v in total_by_currency.items()
        },
        "by_category_by_currency": by_category_by_currency,
        # Legacy: single currency → list, multi-currency → null.
        "by_category": by_category_legacy,
        # New: legacy "Total" (kept for the main number) is a Decimal
        # for the single-currency case, null otherwise. (The dashboard
        # reads the per-currency version above.)
        "total": _money(single_total) if single_total is not None else None,
    }


def daily_trend(
    session: Session,
    user_id: int,
    end: date,
    days: int,
) -> dict:
    """Return ``days`` trailing daily totals ending at ``end`` (inclusive).

    Single currency → ``points`` array (legacy). Multi-currency →
    ``points_by_currency`` map; ``points`` becomes ``null`` to avoid the
    silent cross-currency sum.
    """
    start = end - timedelta(days=days - 1)
    rows = session.execute(
        select(Expense.expense_date, Expense.amount, Expense.currency)
        .where(
            Expense.telegram_user_id == user_id,
            Expense.expense_date >= start,
            Expense.expense_date <= end,
            _NOT_DELETED,
        )
    ).all()
    by_date_per_currency: dict[date, dict[str, Decimal]] = {}
    totals_per_currency: dict[str, Decimal] = {}
    for d, amount, currency in rows:
        cur = currency or "ARS"
        by_date_per_currency.setdefault(d, {})
        by_date_per_currency[d][cur] = by_date_per_currency[d].get(
            cur, Decimal("0")
        ) + Decimal(amount or 0)
        totals_per_currency[cur] = totals_per_currency.get(
            cur, Decimal("0")
        ) + Decimal(amount or 0)

    # Per-currency series, aligned to the same date axis.
    points_by_currency: dict[str, list[dict]] = {
        cur: [] for cur in totals_per_currency
    }
    cursor = start
    while cursor <= end:
        per = by_date_per_currency.get(cursor, {})
        for cur in points_by_currency:
            points_by_currency[cur].append(
                {
                    "date": cursor.isoformat(),
                    "amount": _money(per.get(cur, Decimal("0"))),
                }
            )
        cursor += timedelta(days=1)

    single_currency = _scalar_for_single_currency(totals_per_currency)
    if single_currency is None and totals_per_currency:
        # Multiple currencies — mixing them would be a financial lie.
        points: list[dict] | None = None
    elif totals_per_currency:
        cur = next(iter(totals_per_currency))
        points = points_by_currency[cur]
    else:
        # No expenses at all in the window: still emit the date skeleton
        # so the chart can render the timeline.
        points = [
            {
                "date": (start + timedelta(days=i)).isoformat(),
                "amount": 0.0,
            }
            for i in range((end - start).days + 1)
        ]

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "points": points,
        "points_by_currency": {
            k: v for k, v in points_by_currency.items()
        },
    }


def recent_expenses(
    session: Session,
    user_id: int,
    limit: int,
    *,
    start: date | None = None,
    end: date | None = None,
) -> list[dict]:
    stmt = (
        select(
            Expense.id,
            Expense.name,
            Expense.amount,
            Expense.currency,
            Expense.expense_date,
            Category.name,
        )
        .join(Category, Category.id == Expense.category_id)
        .where(
            Expense.telegram_user_id == user_id,
            _NOT_DELETED,
        )
        .order_by(Expense.expense_date.desc(), Expense.id.desc())
        .limit(limit)
    )
    if start is not None:
        stmt = stmt.where(Expense.expense_date >= start)
    if end is not None:
        stmt = stmt.where(Expense.expense_date <= end)
    rows = session.execute(stmt).all()
    return [
        {
            "id": rid,
            "name": name,
            "amount": _money(amount),
            "currency": currency,
            "category": cat_name,
            "date": d.isoformat(),
        }
        for rid, name, amount, currency, d, cat_name in rows
    ]


def period_total_by_currency(
    session: Session,
    user_id: int,
    *,
    start: date,
    end: date,
) -> dict[str, Decimal]:
    """Financial total for the period grouped by currency.

    Independent of the ``limit`` used by ``recent_expenses`` — this is
    the fix for the audit-found bug where the dashboard's ``total`` was
    just ``sum(items.amount)`` and changed as the user scrolled.
    """
    rows = session.execute(
        select(
            Expense.currency, func.coalesce(func.sum(Expense.amount), 0)
        )
        .where(
            Expense.telegram_user_id == user_id,
            Expense.expense_date >= start,
            Expense.expense_date <= end,
            _NOT_DELETED,
        )
        .group_by(Expense.currency)
    ).all()
    return {cur or "ARS": Decimal(total or 0) for cur, total in rows}


def period_count(
    session: Session,
    user_id: int,
    *,
    start: date,
    end: date,
) -> int:
    """Number of expenses (any currency) in the period.

    Used by ``/api/expenses`` so the ``count`` field reflects the full
    set, not the paginated page.
    """
    stmt = select(func.count(Expense.id)).where(
        Expense.telegram_user_id == user_id,
        Expense.expense_date >= start,
        Expense.expense_date <= end,
        _NOT_DELETED,
    )
    return int(session.execute(stmt).scalar_one() or 0)


def budget_status(
    session: Session,
    user_id: int,
    window: PeriodWindow,
) -> list[dict]:
    """Per active budget: limit, spent within the window, percent, alert level.

    Budgets are inherently monthly targets, so for periods other than
    month we scale the limit proportionally to the period length.
    """
    rows = session.execute(
        select(Budget)
        .where(Budget.telegram_user_id == user_id, Budget.is_active.is_(True))
    ).scalars().all()
    out: list[dict] = []
    is_month_period = (window.days_in_period >= 28 and window.days_in_period <= 31)
    for b in rows:
        cat_name = b.category.name if getattr(b, "category", None) else ""
        total_row = session.execute(
            select(Expense.amount)
            .join(Category, Category.id == Expense.category_id)
            .where(
                Expense.telegram_user_id == user_id,
                Category.name == cat_name,
                Expense.expense_date >= window.start,
                Expense.expense_date <= window.end,
                Expense.currency == b.currency,
                _NOT_DELETED,
            )
        ).all()
        spent = sum((Decimal(a or 0) for (a,) in total_row), Decimal("0"))
        limit_d = Decimal(b.monthly_limit)
        if is_month_period:
            effective_limit = limit_d
        else:
            ratio = Decimal(window.days_in_period) / Decimal("30")
            effective_limit = (limit_d * ratio).quantize(Decimal("0.01"))
        percent = float(
            (spent / effective_limit * 100) if effective_limit > 0 else 0
        )
        if percent >= 100:
            level = "exceeded"
        elif percent >= 80:
            level = "warning"
        else:
            level = "ok"
        out.append(
            {
                "id": b.id,
                "category": cat_name,
                "limit": _money(limit_d),
                "effective_limit": _money(effective_limit),
                "spent": _money(spent),
                "currency": b.currency,
                "percent": round(percent, 1),
                "level": level,
            }
        )
    out.sort(key=lambda x: -x["percent"])
    return out


def comparison(
    session: Session,
    user_id: int,
    current: PeriodWindow,
    previous: PeriodWindow,
) -> dict:
    """Side-by-side totals between two windows.

    Currency correctness: ``by_category`` and the legacy
    ``current.total`` / ``previous.total`` scalars are only emitted when
    a single currency is present. With multiple currencies the legacy
    fields are ``null`` and the per-currency counterparts carry the
    data.
    """
    def _by_cat_per_currency(
        window: PeriodWindow,
    ) -> dict[str, dict[str, Decimal]]:
        rows = session.execute(
            select(Category.name, Expense.currency, Expense.amount)
            .join(Category, Category.id == Expense.category_id)
            .where(
                Expense.telegram_user_id == user_id,
                Expense.expense_date >= window.start,
                Expense.expense_date <= window.end,
                _NOT_DELETED,
            )
        ).all()
        cat_per_cur: dict[str, dict[str, Decimal]] = {}
        totals: dict[str, Decimal] = {}
        for name, currency, amount in rows:
            cur = currency or "ARS"
            cat_per_cur.setdefault(name, {})
            cat_per_cur[name][cur] = cat_per_cur[name].get(
                cur, Decimal("0")
            ) + Decimal(amount or 0)
            totals[cur] = totals.get(cur, Decimal("0")) + Decimal(
                amount or 0
            )
        return cat_per_cur, totals

    cur_cat, cur_totals = _by_cat_per_currency(current)
    prev_cat, prev_totals = _by_cat_per_currency(previous)

    all_currencies = set(cur_totals) | set(prev_totals)

    # Per-currency diff for each category present in either window.
    by_category_by_currency: dict[str, list[dict]] = {}
    for cur in all_currencies:
        rows: list[dict] = []
        all_cats = {
            name
            for name, per in cur_cat.items()
            if per.get(cur)
        } | {
            name
            for name, per in prev_cat.items()
            if per.get(cur)
        }
        for cat in sorted(
            all_cats,
            key=lambda c: -cur_cat.get(c, {}).get(cur, Decimal("0")),
        ):
            c_amt = cur_cat.get(cat, {}).get(cur, Decimal("0"))
            p_amt = prev_cat.get(cat, {}).get(cur, Decimal("0"))
            d_pct: float | None
            if p_amt > 0:
                d_pct = float((c_amt - p_amt) / p_amt * 100)
            else:
                d_pct = None
            rows.append(
                {
                    "category": cat,
                    "current": _money(c_amt),
                    "previous": _money(p_amt),
                    "diff_pct": round(d_pct, 1) if d_pct is not None else None,
                }
            )
        by_category_by_currency[cur] = rows

    # Aggregate per-currency totals.
    cur_total = sum(cur_totals.values(), Decimal("0"))
    prev_total = sum(prev_totals.values(), Decimal("0"))
    delta_pct: float | None
    if prev_total > 0:
        delta_pct = float((cur_total - prev_total) / prev_total * 100)
    else:
        delta_pct = None

    single_cur_total_cur = _scalar_for_single_currency(cur_totals)
    single_cur_total_prev = _scalar_for_single_currency(prev_totals)

    return {
        "current": {
            "label": current.label,
            "total": (
                _money(single_cur_total_cur)
                if single_cur_total_cur is not None
                else None
            ),
        },
        "previous": {
            "label": previous.label,
            "total": (
                _money(single_cur_total_prev)
                if single_cur_total_prev is not None
                else None
            ),
        },
        "delta_pct": round(delta_pct, 1) if delta_pct is not None else None,
        "current_total_by_currency": {
            k: _money(v) for k, v in cur_totals.items()
        },
        "previous_total_by_currency": {
            k: _money(v) for k, v in prev_totals.items()
        },
        "by_category": (
            by_category_by_currency[next(iter(cur_totals))]
            if single_cur_total_cur is not None
            else None
        ),
        "by_category_by_currency": by_category_by_currency,
    }


def projection(
    session: Session,
    user_id: int,
    window: PeriodWindow,
    previous: PeriodWindow,
) -> dict:
    """Linear projection of month-end spending vs prior period total.

    Multi-currency safe: spent / projected / previous totals are
    per-currency maps; the legacy scalars are only emitted when one
    currency is present.
    """
    rows = session.execute(
        select(Expense.currency, Expense.amount)
        .where(
            Expense.telegram_user_id == user_id,
            Expense.expense_date >= window.start,
            Expense.expense_date <= window.end,
            _NOT_DELETED,
        )
    ).all()
    spent_by_currency: dict[str, Decimal] = {}
    for currency, amount in rows:
        cur = currency or "ARS"
        spent_by_currency[cur] = spent_by_currency.get(
            cur, Decimal("0")
        ) + Decimal(amount or 0)

    if window.days_elapsed <= 0 or window.days_in_period <= 0:
        projected_by_currency = dict(spent_by_currency)
    else:
        factor = Decimal(window.days_in_period) / Decimal(window.days_elapsed)
        projected_by_currency = {
            cur: (amt * factor) for cur, amt in spent_by_currency.items()
        }

    prev_rows = session.execute(
        select(Expense.currency, Expense.amount)
        .where(
            Expense.telegram_user_id == user_id,
            Expense.expense_date >= previous.start,
            Expense.expense_date <= previous.end,
            _NOT_DELETED,
        )
    ).all()
    previous_total_by_currency: dict[str, Decimal] = {}
    for currency, amount in prev_rows:
        cur = currency or "ARS"
        previous_total_by_currency[cur] = previous_total_by_currency.get(
            cur, Decimal("0")
        ) + Decimal(amount or 0)

    # delta vs prev per currency (and a flat fallback when single cur).
    delta_by_currency: dict[str, float | None] = {}
    for cur in set(projected_by_currency) | set(previous_total_by_currency):
        proj = projected_by_currency.get(cur, Decimal("0"))
        prev = previous_total_by_currency.get(cur, Decimal("0"))
        if prev > 0:
            delta_by_currency[cur] = float((proj - prev) / prev * 100)
        else:
            delta_by_currency[cur] = None

    single_spent = _scalar_for_single_currency(spent_by_currency)
    single_projected = _scalar_for_single_currency(projected_by_currency)
    single_prev = _scalar_for_single_currency(previous_total_by_currency)

    return {
        "spent": _money(single_spent) if single_spent is not None else None,
        "projected_total": (
            _money(single_projected) if single_projected is not None else None
        ),
        "days_elapsed": window.days_elapsed,
        "days_in_period": window.days_in_period,
        "previous_total": (
            _money(single_prev) if single_prev is not None else None
        ),
        "delta_vs_previous_pct": (
            delta_by_currency[next(iter(spent_by_currency))]
            if single_spent is not None and spent_by_currency
            else None
        ),
        "spent_by_currency": {
            k: _money(v) for k, v in spent_by_currency.items()
        },
        "projected_total_by_currency": {
            k: _money(v) for k, v in projected_by_currency.items()
        },
        "previous_total_by_currency": {
            k: _money(v) for k, v in previous_total_by_currency.items()
        },
        "delta_vs_previous_pct_by_currency": {
            k: (round(v, 1) if v is not None else None)
            for k, v in delta_by_currency.items()
        },
    }


def fixed_expenses_current_month(
    session: Session,
    user_id: int,
    today: date,
) -> list[dict]:
    """Return fixed expenses with payment status for the current month."""
    from app.fixed_expenses.repository import FixedExpenseRepository
    from app.fixed_expenses.service import (
        FixedExpenseService,
        current_month_year,
    )
    repo = FixedExpenseRepository(session)
    service = FixedExpenseService(repo)
    my = current_month_year(today)
    bills = service.with_status_for_month(user_id, my)
    return [
        {
            "id": b.id,
            "name": b.name,
            "expected_amount": float(b.expected_amount),
            "currency": b.currency,
            "payment_method": b.payment_method,
            "due_day_of_month": b.due_day_of_month,
            "category_name": b.category_name,
            "status": b.status,
            "actual_amount": (
                float(b.actual_amount) if b.actual_amount is not None else None
            ),
            "diff": float(b.diff) if b.diff is not None else None,
        }
        for b in bills
    ]


def month_summary_dashboard(
    session: Session,
    user_id: int,
    today: date,
) -> dict:
    """Income + extra + fixed totals + liberado for the current month."""
    from app.fixed_expenses.repository import FixedExpenseRepository
    from app.fixed_expenses.service import (
        FixedExpenseService,
        current_month_year,
    )
    repo = FixedExpenseRepository(session)
    service = FixedExpenseService(repo)
    my = current_month_year(today)
    summary = service.month_summary(user_id, my)
    return {
        "month_year": my,
        "income": float(summary.income),
        "extra": float(summary.extra),
        "total_fixed_expected": float(summary.total_fixed_expected),
        "total_fixed_paid": float(summary.total_fixed_paid),
        "liberado": float(summary.liberado),
    }


def recurring_upcoming(
    session: Session,
    user_id: int,
    today: date,
    horizon_days: int = 7,
) -> list[dict]:
    """Recurring templates due within the next horizon days."""
    horizon = today + timedelta(days=horizon_days)
    rows = session.execute(
        select(RecurringExpense)
        .where(
            RecurringExpense.telegram_user_id == user_id,
            RecurringExpense.is_active.is_(True),
            RecurringExpense.next_due_date <= horizon,
            RecurringExpense.next_due_date >= today,
        )
        .order_by(RecurringExpense.next_due_date.asc())
    ).scalars().all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "amount": float(r.amount),
            "currency": r.currency,
            "next_due_date": r.next_due_date.isoformat(),
        }
        for r in rows
    ]
