"""DB queries used by the dashboard endpoints.

Kept separate from ``routes.py`` so they can be unit-tested independently
of HTTP plumbing.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budgets.models import Budget
from app.categories.models import Category
from app.expenses.models import Expense
from app.recurring.models import RecurringExpense


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
        )
    ).all()

    by_category: dict[str, Decimal] = {}
    total_by_currency: dict[str, Decimal] = {}
    for cat_name, currency, amount in by_cat_rows:
        by_category[cat_name] = by_category.get(cat_name, Decimal("0")) + (
            Decimal(amount or 0)
        )
        total_by_currency[currency or "ARS"] = total_by_currency.get(
            currency or "ARS", Decimal("0")
        ) + Decimal(amount or 0)

    # Sort categories by amount descending; keep top 10 for the pie chart.
    sorted_cats = sorted(
        by_category.items(), key=lambda kv: kv[1], reverse=True
    )
    return {
        "period_label": window.label,
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "days_elapsed": window.days_elapsed,
        "days_in_period": window.days_in_period,
        "is_full_period": window.is_full_period,
        "total_by_currency": {
            k: float(v) for k, v in total_by_currency.items()
        },
        "by_category": [
            {"name": name, "amount": float(amt)}
            for name, amt in sorted_cats[:10]
        ],
    }


def daily_trend(
    session: Session,
    user_id: int,
    end: date,
    days: int,
) -> dict:
    """Return ``days`` trailing daily totals ending at ``end`` (inclusive)."""
    start = end - timedelta(days=days - 1)
    rows = session.execute(
        select(Expense.expense_date, Expense.amount, Expense.currency)
        .where(
            Expense.telegram_user_id == user_id,
            Expense.expense_date >= start,
            Expense.expense_date <= end,
        )
    ).all()
    by_date: dict[date, Decimal] = {}
    for d, amount, _currency in rows:
        by_date[d] = by_date.get(d, Decimal("0")) + Decimal(amount or 0)
    points = []
    cursor = start
    while cursor <= end:
        points.append(
            {
                "date": cursor.isoformat(),
                "amount": float(by_date.get(cursor, Decimal("0"))),
            }
        )
        cursor += timedelta(days=1)
    return {"start": start.isoformat(), "end": end.isoformat(), "points": points}


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
        .where(Expense.telegram_user_id == user_id)
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
            "amount": float(amount),
            "currency": currency,
            "category": cat_name,
            "date": d.isoformat(),
        }
        for rid, name, amount, currency, d, cat_name in rows
    ]


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
            )
        ).all()
        spent = sum((Decimal(a or 0) for (a,) in total_row), Decimal("0"))
        limit_d = Decimal(b.monthly_limit)
        # For month-ish periods, use the budget limit as-is. For shorter
        # or longer periods, scale linearly so the bar makes sense.
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
                "limit": float(limit_d),
                "effective_limit": float(effective_limit),
                "spent": float(spent),
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
    """Side-by-side totals between two windows."""
    def _by_cat(window: PeriodWindow) -> dict[str, Decimal]:
        rows = session.execute(
            select(Category.name, Expense.amount)
            .join(Category, Category.id == Expense.category_id)
            .where(
                Expense.telegram_user_id == user_id,
                Expense.expense_date >= window.start,
                Expense.expense_date <= window.end,
            )
        ).all()
        out: dict[str, Decimal] = {}
        for name, amount in rows:
            out[name] = out.get(name, Decimal("0")) + Decimal(amount or 0)
        return out

    cur = _by_cat(current)
    prev = _by_cat(previous)
    cur_total = sum(cur.values(), Decimal("0"))
    prev_total = sum(prev.values(), Decimal("0"))
    delta_pct: float | None
    if prev_total > 0:
        delta_pct = float((cur_total - prev_total) / prev_total * 100)
    else:
        delta_pct = None

    rows = []
    all_cats = set(cur) | set(prev)
    for cat in sorted(all_cats, key=lambda c: -(cur.get(c, Decimal("0")))):
        c_amt = cur.get(cat, Decimal("0"))
        p_amt = prev.get(cat, Decimal("0"))
        if p_amt > 0:
            d_pct = float((c_amt - p_amt) / p_amt * 100)
        else:
            d_pct = None
        rows.append(
            {
                "category": cat,
                "current": float(c_amt),
                "previous": float(p_amt),
                "diff_pct": round(d_pct, 1) if d_pct is not None else None,
            }
        )
    return {
        "current": {
            "label": current.label,
            "total": float(cur_total),
        },
        "previous": {
            "label": previous.label,
            "total": float(prev_total),
        },
        "delta_pct": round(delta_pct, 1) if delta_pct is not None else None,
        "by_category": rows,
    }


def projection(
    session: Session,
    user_id: int,
    window: PeriodWindow,
    previous: PeriodWindow,
) -> dict:
    """Linear projection of month-end spending vs prior period total."""
    rows = session.execute(
        select(Expense.amount)
        .where(
            Expense.telegram_user_id == user_id,
            Expense.expense_date >= window.start,
            Expense.expense_date <= window.end,
        )
    ).all()
    spent = sum((Decimal(a or 0) for (a,) in rows), Decimal("0"))
    if window.days_elapsed <= 0 or window.days_in_period <= 0:
        projected = spent
    else:
        projected = spent * Decimal(window.days_in_period) / Decimal(
            window.days_elapsed
        )

    prev_rows = session.execute(
        select(Expense.amount)
        .where(
            Expense.telegram_user_id == user_id,
            Expense.expense_date >= previous.start,
            Expense.expense_date <= previous.end,
        )
    ).all()
    prev_total = sum(
        (Decimal(a or 0) for (a,) in prev_rows), Decimal("0")
    )
    if prev_total > 0:
        delta_vs_prev = float((projected - prev_total) / prev_total * 100)
    else:
        delta_vs_prev = None
    return {
        "spent": float(spent),
        "projected_total": float(projected),
        "days_elapsed": window.days_elapsed,
        "days_in_period": window.days_in_period,
        "previous_total": float(prev_total),
        "delta_vs_previous_pct": (
            round(delta_vs_prev, 1) if delta_vs_prev is not None else None
        ),
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
