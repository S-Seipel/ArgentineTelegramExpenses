from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.expenses.service import ExpenseDraft, ExpenseService
from app.queries.intents import (
    CategoryDiff,
    ComparisonRow,
    make_comparison_spec,
)
from app.queries.service import QueryService
from app.utils.dates import today_in_tz


def _current_month_dates():
    today = today_in_tz("UTC")
    y, m = today.year, today.month
    # Both dates must be in the past or today so they fall within the
    # query window (which ends at today).
    safe_day = min(5, today.day)
    safe_day_b = min(15, today.day)
    return date(y, m, safe_day), date(y, m, safe_day_b)


def _prev_month_dates():
    today = today_in_tz("UTC")
    if today.month == 1:
        y, m = today.year - 1, 12
    else:
        y, m = today.year, today.month - 1
    return date(y, m, min(5, 28)), date(y, m, min(15, 28))


def _seed_current_month():
    """Seed expenses in the *actual* current month (whatever it is)."""
    from app.utils.dates import today_in_tz
    today = today_in_tz("UTC")
    if today.month == 1:
        cur_year, cur_month = today.year, 1
    else:
        cur_year, cur_month = today.year, today.month
    with session_scope() as s:
        ExpenseService(ExpenseRepository(s)).register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="A",
                    amount=Decimal("10000"),
                    currency="ARS",
                    category="Comida",
                    expense_date=date(cur_year, cur_month, min(5, today.day)),
                    confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="B",
                    amount=Decimal("5000"),
                    currency="ARS",
                    category="Transporte",
                    expense_date=date(cur_year, cur_month, min(15, today.day)),
                    confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )


def _seed_prev_month():
    """Seed expenses in the month *before* the current one."""
    from app.utils.dates import today_in_tz
    today = today_in_tz("UTC")
    if today.month == 1:
        prev_year, prev_month = today.year - 1, 12
    else:
        prev_year, prev_month = today.year, today.month - 1
    with session_scope() as s:
        ExpenseService(ExpenseRepository(s)).register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="C",
                    amount=Decimal("8000"),
                    currency="ARS",
                    category="Comida",
                    expense_date=date(prev_year, prev_month, min(5, 28)),
                    confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="D",
                    amount=Decimal("4000"),
                    currency="ARS",
                    category="Transporte",
                    expense_date=date(prev_year, prev_month, min(15, 28)),
                    confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )


def test_make_comparison_spec_defaults_to_month():
    spec = make_comparison_spec()
    assert spec.kind == "comparison"
    assert spec.period == "month"


def test_comparison_basic_increase(in_memory_db):
    _seed_prev_month()
    _seed_current_month()
    spec = make_comparison_spec()
    with session_scope() as s:
        result = QueryService(ExpenseRepository(s)).run(1, spec)
    comp = result.comparison
    assert comp is not None
    # Current month: 10000 + 5000 = 15000
    # Previous month: 8000 + 4000 = 12000
    # Diff = 25%
    assert comp.current_total == Decimal("15000")
    assert comp.previous_total == Decimal("12000")
    assert comp.total_diff_pct == pytest.approx(25.0)


def test_comparison_basic_decrease(in_memory_db):
    prev_a, _ = _prev_month_dates()
    cur_a, _ = _current_month_dates()
    with session_scope() as s:
        # Previous: 10000
        ExpenseService(ExpenseRepository(s)).register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="X",
                    amount=Decimal("10000"),
                    currency="ARS",
                    category="Comida",
                    expense_date=prev_a,
                    confidence=Decimal("1"),
                )
            ],
            original_message="seed",
        )
        # Current: 5000 (less)
        ExpenseService(ExpenseRepository(s)).register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Y",
                    amount=Decimal("5000"),
                    currency="ARS",
                    category="Comida",
                    expense_date=cur_a,
                    confidence=Decimal("1"),
                )
            ],
            original_message="seed",
        )
    spec = make_comparison_spec()
    with session_scope() as s:
        comp = QueryService(ExpenseRepository(s)).run(1, spec).comparison
    assert comp.current_total == Decimal("5000")
    assert comp.previous_total == Decimal("10000")
    assert comp.total_diff_pct == pytest.approx(-50.0)


def test_comparison_by_category_breakdown(in_memory_db):
    _seed_prev_month()
    _seed_current_month()
    spec = make_comparison_spec()
    with session_scope() as s:
        comp = QueryService(ExpenseRepository(s)).run(1, spec).comparison
    cats = {r.category: r for r in comp.by_category}
    assert "Comida" in cats
    # Comida: current 10000, previous 8000 -> +25%
    assert cats["Comida"].current == Decimal("10000")
    assert cats["Comida"].previous == Decimal("8000")
    assert cats["Comida"].diff_pct == pytest.approx(25.0)
    # Transporte: current 5000, previous 4000 -> +25%
    assert cats["Transporte"].diff_pct == pytest.approx(25.0)


def test_comparison_handles_new_category(in_memory_db):
    prev_a, _ = _prev_month_dates()
    cur_a, cur_b = _current_month_dates()
    # Previous: only Comida
    with session_scope() as s:
        ExpenseService(ExpenseRepository(s)).register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="X",
                    amount=Decimal("5000"),
                    currency="ARS",
                    category="Comida",
                    expense_date=prev_a,
                    confidence=Decimal("1"),
                )
            ],
            original_message="seed",
        )
    # Current: Comida + new Transporte
    with session_scope() as s:
        ExpenseService(ExpenseRepository(s)).register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="A",
                    amount=Decimal("5000"),
                    currency="ARS",
                    category="Comida",
                    expense_date=cur_a,
                    confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="B",
                    amount=Decimal("3000"),
                    currency="ARS",
                    category="Transporte",
                    expense_date=cur_b,
                    confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )
    spec = make_comparison_spec()
    with session_scope() as s:
        comp = QueryService(ExpenseRepository(s)).run(1, spec).comparison
    cats = {r.category: r for r in comp.by_category}
    # Transporte is new (previous = 0), so diff_pct should be None
    assert cats["Transporte"].current == Decimal("3000")
    assert cats["Transporte"].previous == Decimal("0")
    assert cats["Transporte"].diff_pct is None


def test_comparison_no_previous_data(in_memory_db):
    # Only current month data
    _seed_current_month()
    spec = make_comparison_spec()
    with session_scope() as s:
        comp = QueryService(ExpenseRepository(s)).run(1, spec).comparison
    assert comp.current_total == Decimal("15000")
    assert comp.previous_total == Decimal("0")
    assert comp.total_diff_pct is None


def test_comparison_no_current_data(in_memory_db):
    _seed_prev_month()
    spec = make_comparison_spec()
    with session_scope() as s:
        comp = QueryService(ExpenseRepository(s)).run(1, spec).comparison
    assert comp.current_total == Decimal("0")
    assert comp.previous_total == Decimal("12000")
    # current=0, previous=12000 -> -100%
    assert comp.total_diff_pct == pytest.approx(-100.0)


def test_comparison_scoped_per_user(in_memory_db):
    _seed_prev_month()
    _seed_current_month()
    cur_a, _ = _current_month_dates()
    # Other user has different data
    with session_scope() as s:
        ExpenseService(ExpenseRepository(s)).register_many(
            user_id=2,
            drafts=[
                ExpenseDraft(
                    name="Other",
                    amount=Decimal("99999"),
                    currency="ARS",
                    category="Comida",
                    expense_date=cur_a,
                    confidence=Decimal("1"),
                )
            ],
            original_message="seed",
        )
    spec = make_comparison_spec()
    with session_scope() as s:
        comp = QueryService(ExpenseRepository(s)).run(1, spec).comparison
    assert comp.current_total == Decimal("15000")


def test_comparison_orders_categories_by_current_desc(in_memory_db):
    _seed_prev_month()
    _seed_current_month()
    spec = make_comparison_spec()
    with session_scope() as s:
        comp = QueryService(ExpenseRepository(s)).run(1, spec).comparison
    cats = [r.category for r in comp.by_category]
    assert cats[0] == "Comida"  # 10000 > 5000
    assert cats[1] == "Transporte"
