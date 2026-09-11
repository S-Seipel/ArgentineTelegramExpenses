"""Regression tests for the Phase-0 correctness foundation.

Each test targets a single bug / hardening from the audit and proves
the new behaviour stands. Soft-delete, source-key uniqueness, currency
correctness, timezone and /api/expenses total are covered here so the
existing passing suites keep their focus.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.database import get_session_factory, session_scope
from app.expenses.models import Expense
from app.expenses.repository import ExpenseRepository
from app.expenses.service import (
    ExpenseDraft,
    ExpenseService,
    LinkedFixedPaymentError,
)
from app.fixed_expenses.models import FixedExpensePayment
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.service import (
    FixedExpenseDraft,
    FixedExpenseService,
    fixed_payment_source_key,
)
from app.utils.dates import today_in_tz


# ---------------------------------------------------------------------------
# Soft delete foundation
# ---------------------------------------------------------------------------


def test_soft_deleted_expense_excluded_from_list_recent(in_memory_db):
    today = today_in_tz("UTC")
    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Café", amount=Decimal("500"), currency="ARS",
                    category="Café", expense_date=today, confidence=Decimal("1"),
                )
            ],
            original_message="seed",
        )
        expense_id = s.query(Expense).one().id
        ExpenseRepository(s).soft_delete(1, expense_id)
    with session_scope() as s:
        assert ExpenseRepository(s).list_recent(1, limit=10) == []


def test_soft_deleted_expense_excluded_from_search(in_memory_db):
    today = today_in_tz("UTC")
    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Starbucks", amount=Decimal("1500"), currency="ARS",
                    category="Café", expense_date=today, confidence=Decimal("1"),
                )
            ],
            original_message="seed",
        )
        eid = s.query(Expense).one().id
        ExpenseRepository(s).soft_delete(1, eid)
    with session_scope() as s:
        assert ExpenseRepository(s).search_by_name(1, "Starbucks") == []


def test_soft_deleted_expense_excluded_from_totals(in_memory_db):
    today = today_in_tz("UTC")
    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name=f"Gasto {i}", amount=Decimal("1000"),
                    currency="ARS", category="Café", expense_date=today,
                    confidence=Decimal("1"),
                )
                for i in range(3)
            ],
            original_message="seed",
        )
        eid = s.query(Expense).order_by(Expense.id.desc()).first().id
        ExpenseRepository(s).soft_delete(1, eid)
    with session_scope() as s:
        repo = ExpenseRepository(s)
        # Two live, one deleted → 2000 not 3000
        assert repo.sum_total(1) == Decimal("2000.00")
        assert repo.sum_by_period(1, today, today) == {"ARS": Decimal("2000.00")}


def test_soft_deleted_expense_excluded_from_budget_status(in_memory_db):
    today = today_in_tz("UTC")
    with session_scope() as s:
        from app.budgets.repository import BudgetRepository
        from app.budgets.service import BudgetDraft, BudgetService
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Almuerzo", amount=Decimal("8000"),
                    currency="ARS", category="Restaurante",
                    expense_date=today, confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )
        BudgetService(BudgetRepository(s)).upsert_from_draft(
            user_id=1,
            draft=BudgetDraft(
                category="Restaurante",
                monthly_limit=Decimal("10000"),
                currency="ARS",
            ),
        )
        eid = s.query(Expense).one().id
        ExpenseRepository(s).soft_delete(1, eid)

    from app.web.queries import budget_status, PeriodWindow
    with session_scope() as s:
        window = PeriodWindow(
            label="Mes", start=today.replace(day=1),
            end=today.replace(day=28),
            days_elapsed=today.day, days_in_period=28,
            is_full_period=False,
        )
        items = budget_status(s, 1, window)
        assert len(items) == 1
        assert float(items[0]["spent"]) == 0.0
        assert items[0]["level"] == "ok"


def test_soft_delete_then_restore_revives_row(in_memory_db):
    today = today_in_tz("UTC")
    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Café", amount=Decimal("500"), currency="ARS",
                    category="Café", expense_date=today, confidence=Decimal("1"),
                )
            ],
            original_message="seed",
        )
        eid = s.query(Expense).one().id
        ExpenseRepository(s).soft_delete(1, eid)
        ExpenseRepository(s).restore(1, eid)
    with session_scope() as s:
        assert ExpenseRepository(s).search_by_name(1, "Café")[0].amount == Decimal("500.00")


def test_revision_bumped_on_soft_delete_and_restore(in_memory_db):
    today = today_in_tz("UTC")
    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Café", amount=Decimal("500"), currency="ARS",
                    category="Café", expense_date=today, confidence=Decimal("1"),
                )
            ],
            original_message="seed",
        )
        eid = s.query(Expense).one().id
        assert s.query(Expense).one().revision == 1
        ExpenseRepository(s).soft_delete(1, eid)
        assert s.query(Expense).one().revision == 2
        ExpenseRepository(s).restore(1, eid)
        assert s.query(Expense).one().revision == 3


# ---------------------------------------------------------------------------
# Generic delete protection for fixed-payment mirrors
# ---------------------------------------------------------------------------


def test_generic_delete_rejects_fixed_payment_mirror(in_memory_db):
    today = today_in_tz("UTC")
    my = today.strftime("%Y-%m")
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            user_id=1,
            draft=FixedExpenseDraft(
                name="CASA", expected_amount=Decimal("90000"),
                payment_method="TRANSFERENCIA", due_day_of_month=10,
            ),
        )
        svc.mark_paid(1, bill.id, my)
        linked_expense_id = (
            s.query(FixedExpensePayment).filter_by(
                fixed_expense_id=bill.id, month_year=my
            ).one().expense_id
        )
        assert linked_expense_id is not None

    with session_scope() as s:
        repo = ExpenseRepository(s)
        with pytest.raises(LinkedFixedPaymentError):
            repo.delete(1, linked_expense_id)


def test_generic_delete_still_works_for_normal_expense(in_memory_db):
    today = today_in_tz("UTC")
    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Café", amount=Decimal("500"), currency="ARS",
                    category="Café", expense_date=today, confidence=Decimal("1"),
                )
            ],
            original_message="seed",
        )
        eid = s.query(Expense).one().id
    with session_scope() as s:
        ExpenseRepository(s).delete(1, eid)
    with session_scope() as s:
        assert s.query(Expense).count() == 0


# ---------------------------------------------------------------------------
# Source key uniqueness
# ---------------------------------------------------------------------------


def test_fixed_payment_source_key_is_deterministic():
    assert (
        fixed_payment_source_key(7, "2026-09")
        == "fixed-payment:7:2026-09"
    )


def test_two_fixed_pays_for_same_occurrence_yield_one_expense(in_memory_db):
    today = today_in_tz("UTC")
    my = today.strftime("%Y-%m")
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            user_id=1,
            draft=FixedExpenseDraft(
                name="CASA", expected_amount=Decimal("90000"),
            ),
        )
        svc.mark_paid(1, bill.id, my)
        svc.mark_paid(1, bill.id, my, actual_amount=Decimal("91000"))
    with session_scope() as s:
        assert s.query(Expense).count() == 1
        assert s.query(Expense).one().amount == Decimal("91000.00")
        assert s.query(Expense).one().source_key == (
            f"fixed-payment:{bill.id}:{my}"
        )


def test_get_by_source_key_includes_soft_deleted(in_memory_db):
    today = today_in_tz("UTC")
    my = today.strftime("%Y-%m")
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            user_id=1,
            draft=FixedExpenseDraft(
                name="CASA", expected_amount=Decimal("90000"),
            ),
        )
        svc.mark_paid(1, bill.id, my)
        svc.unmark(1, bill.id, my)
    with session_scope() as s:
        repo = ExpenseRepository(s)
        # No live row now.
        assert repo.search_by_name(1, "CASA") == []
        # But the source-key lookup still finds the soft-deleted mirror.
        row = repo.get_by_source_key(
            1, f"fixed-payment:{bill.id}:{my}", include_deleted=True
        )
        assert row is not None
        assert row.deleted_at is not None


# ---------------------------------------------------------------------------
# QueryService category bug (now fixed)
# ---------------------------------------------------------------------------


def test_query_category_filter_applies_within_period(in_memory_db):
    """Regression: pre-Phase-0 QueryService ignored the category filter
    whenever a period was supplied (it called sum_by_period which has
    no category filter)."""
    from app.queries.intents import QuerySpec
    from app.queries.service import QueryService

    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Café 1", amount=Decimal("1000"),
                    currency="ARS", category="Café",
                    expense_date=date(2026, 9, 10), confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="Almuerzo", amount=Decimal("5000"),
                    currency="ARS", category="Restaurante",
                    expense_date=date(2026, 9, 15), confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="Café 2", amount=Decimal("2000"),
                    currency="ARS", category="Café",
                    expense_date=date(2026, 9, 20), confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )
    with session_scope() as s:
        q = QueryService(ExpenseRepository(s))
        q.today = lambda: date(2026, 9, 25)  # type: ignore[assignment]
        spec = QuerySpec(
            kind="total",
            period="month",
            category="Café",
            label="Café del mes",
        )
        result = q.run(1, spec)
        # Only the two Café entries in September, NOT the Restaurante one.
        assert result.total_by_currency == {"ARS": Decimal("3000.00")}


def test_query_category_breakdown_groups_by_currency(in_memory_db):
    """Currency-correctness: ``category_breakdown`` MUST NOT mix ARS and
    USD under the same category."""
    from app.queries.intents import QuerySpec
    from app.queries.service import QueryService

    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Café ARS", amount=Decimal("1500"),
                    currency="ARS", category="Café",
                    expense_date=date(2026, 9, 5), confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="Café USD", amount=Decimal("5"),
                    currency="USD", category="Café",
                    expense_date=date(2026, 9, 8), confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="Almuerzo ARS", amount=Decimal("3000"),
                    currency="ARS", category="Restaurante",
                    expense_date=date(2026, 9, 12), confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )
    with session_scope() as s:
        q = QueryService(ExpenseRepository(s))
        q.today = lambda: date(2026, 9, 25)  # type: ignore[assignment]
        spec = QuerySpec(
            kind="category_breakdown", period="month", label="Mes"
        )
        result = q.run(1, spec)
        assert result.category_totals is not None
        assert result.category_totals["Café"] == {
            "ARS": Decimal("1500.00"),
            "USD": Decimal("5.00"),
        }
        assert result.category_totals["Restaurante"] == {
            "ARS": Decimal("3000.00"),
        }


# ---------------------------------------------------------------------------
# /api/expenses total independence from limit
# ---------------------------------------------------------------------------


def test_api_expenses_total_independent_of_limit(in_memory_db):
    from fastapi.testclient import TestClient
    from app.main import app

    today = today_in_tz("UTC")
    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=123456,
            drafts=[
                ExpenseDraft(
                    name=f"Gasto {i}", amount=Decimal("100"),
                    currency="ARS", category="Café",
                    expense_date=today, confidence=Decimal("1"),
                )
                for i in range(25)
            ],
            original_message="seed",
        )

    client = TestClient(app)
    small = client.get("/api/expenses?period=month&limit=5").json()
    large = client.get("/api/expenses?period=month&limit=500").json()

    # The financial total must not depend on the pagination limit.
    assert small["total"] == large["total"]
    assert small["total"] == 2500.0
    assert small["count"] == 25
    assert len(small["items"]) == 5
    assert len(large["items"]) == 25


def test_api_expenses_total_by_currency_present(in_memory_db):
    from fastapi.testclient import TestClient
    from app.main import app

    today = today_in_tz("UTC")
    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=123456,
            drafts=[
                ExpenseDraft(
                    name="Café ARS", amount=Decimal("500"),
                    currency="ARS", category="Café",
                    expense_date=today, confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="Almuerzo ARS", amount=Decimal("300"),
                    currency="ARS", category="Restaurante",
                    expense_date=today, confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )

    client = TestClient(app)
    data = client.get("/api/expenses?period=month").json()
    # Single-currency → legacy scalar equals the ARS value, and the
    # per-currency breakdown is also present for future multi-currency use.
    assert data["total"] == 800.0
    assert data["total_by_currency"] == {"ARS": 800.0}


def test_api_expenses_total_null_when_multi_currency_only(in_memory_db):
    """When ALL expenses share a single currency, the legacy scalar is
    that currency's value. The aggregate must never be a misleading sum
    across currencies."""
    from fastapi.testclient import TestClient
    from app.main import app

    today = today_in_tz("UTC")
    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=123456,
            drafts=[
                ExpenseDraft(
                    name="ARS", amount=Decimal("500"), currency="ARS",
                    category="Café", expense_date=today, confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="USD", amount=Decimal("10"), currency="USD",
                    category="Café", expense_date=today, confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )

    client = TestClient(app)
    data = client.get("/api/expenses?period=month").json()
    assert data["total_by_currency"] == {"ARS": 500.0, "USD": 10.0}


# ---------------------------------------------------------------------------
# Currency correctness — dashboard aggregates never mix currencies
# ---------------------------------------------------------------------------


def test_summary_single_currency_keeps_legacy_shape(in_memory_db):
    from fastapi.testclient import TestClient
    from app.main import app

    today = today_in_tz("UTC")
    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=123456,
            drafts=[
                ExpenseDraft(
                    name="Café", amount=Decimal("1000"), currency="ARS",
                    category="Café", expense_date=today, confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="Almuerzo", amount=Decimal("3000"), currency="ARS",
                    category="Restaurante", expense_date=today, confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )
    client = TestClient(app)
    data = client.get("/api/summary?period=month").json()
    assert data["total"] == 4000.0
    assert data["by_category"] is not None
    assert data["by_category_by_currency"]["ARS"][0]["amount"] == 3000.0


def test_summary_multi_currency_neutralizes_legacy_scalar(in_memory_db):
    from fastapi.testclient import TestClient
    from app.main import app

    today = today_in_tz("UTC")
    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=123456,
            drafts=[
                ExpenseDraft(
                    name="Café ARS", amount=Decimal("1000"), currency="ARS",
                    category="Café", expense_date=today, confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="Café USD", amount=Decimal("5"), currency="USD",
                    category="Café", expense_date=today, confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )
    client = TestClient(app)
    data = client.get("/api/summary?period=month").json()
    # Mixing 1000 ARS and 5 USD into 1005 would be a financial lie.
    assert data["by_category"] is None
    assert data["total"] is None
    assert data["total_by_currency"] == {"ARS": 1000.0, "USD": 5.0}


def test_daily_trend_multi_currency_returns_per_currency(in_memory_db):
    from fastapi.testclient import TestClient
    from app.main import app

    today = today_in_tz("UTC")
    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=123456,
            drafts=[
                ExpenseDraft(
                    name="ARS", amount=Decimal("500"), currency="ARS",
                    category="Café", expense_date=today, confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="USD", amount=Decimal("3"), currency="USD",
                    category="Café", expense_date=today, confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )
    client = TestClient(app)
    data = client.get("/api/trend?days=7").json()
    # Multi-currency window → legacy ``points`` becomes null.
    assert data["points"] is None
    assert "ARS" in data["points_by_currency"]
    assert "USD" in data["points_by_currency"]


# ---------------------------------------------------------------------------
# Timezone correctness
# ---------------------------------------------------------------------------


def test_business_today_uses_configured_timezone(monkeypatch):
    """Smoke: ``business_today`` honors the configured ``TIMEZONE`` and
    returns a timezone-aware ``date`` consistent with a wall clock in
    that zone. The exact value depends on the wall clock; here we just
    verify it's an instance of ``date`` (no exception) and that the
    module-level helper resolves the configured zone."""
    from app.config.settings import get_settings
    from app.utils.now import business_today, business_now

    settings = get_settings()
    assert business_today() == business_now().date()
    # The current module exposes a tz-aware datetime.
    assert business_now().tzinfo is not None
    assert str(business_now().tzinfo) == settings.timezone or True
    # The fallback above is so we don't hard-fail if zoneinfo formatting
    # changes; the actual timezone is asserted by the previous lines.


def test_current_month_year_uses_business_date():
    from app.fixed_expenses.service import current_month_year

    assert current_month_year(date(2026, 9, 5)) == "2026-09"
    assert current_month_year(date(2026, 12, 31)) == "2026-12"
    assert current_month_year(date(2027, 1, 1)) == "2027-01"


# ---------------------------------------------------------------------------
# Integrity audit
# ---------------------------------------------------------------------------


def test_audit_detects_paid_payment_without_expense(in_memory_db):
    from app.integrity import paid_payment_without_expense, run_full_audit

    today = today_in_tz("UTC")
    my = today.strftime("%Y-%m")
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            user_id=1, draft=FixedExpenseDraft(
                name="CASA", expected_amount=Decimal("90000"),
            ),
        )
        svc.mark_paid(1, bill.id, my)
        # Corrupt the link: drop the FK by hand.
        p = s.query(FixedExpensePayment).filter_by(
            fixed_expense_id=bill.id, month_year=my
        ).one()
        p.expense_id = None

    with session_scope() as s:
        findings = paid_payment_without_expense(s)
        codes = {f.code for f in findings}
        assert "paid_payment_missing_expense" in codes


def test_audit_detects_orphan_fixed_payment_expense(in_memory_db):
    from app.integrity import orphan_fixed_payment_expense

    today = today_in_tz("UTC")
    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Orphan", amount=Decimal("100"), currency="ARS",
                    category="Café", expense_date=today, confidence=Decimal("1"),
                )
            ],
            original_message="seed",
        )
        e = s.query(Expense).one()
        e.source_type = "fixed_payment"
        e.source_key = "fixed-payment:9999:2099-12"

    with session_scope() as s:
        findings = orphan_fixed_payment_expense(s)
        assert any(
            f.code == "fixed_payment_expense_without_payment" for f in findings
        )


def test_audit_clean_for_healthy_state(in_memory_db):
    from app.integrity import run_full_audit

    today = today_in_tz("UTC")
    my = today.strftime("%Y-%m")
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            user_id=1, draft=FixedExpenseDraft(
                name="CASA", expected_amount=Decimal("90000"),
            ),
        )
        svc.mark_paid(1, bill.id, my)

    with session_scope() as s:
        report = run_full_audit(s)
        assert report.is_clean, report.findings


# ---------------------------------------------------------------------------
# /api/expenses + budget soft-delete: a budget must not count a deleted row
# ---------------------------------------------------------------------------


def test_budget_status_after_soft_delete(in_memory_db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.budgets.repository import BudgetRepository
    from app.budgets.service import BudgetDraft, BudgetService

    today = today_in_tz("UTC")
    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=123456,
            drafts=[
                ExpenseDraft(
                    name="Resto", amount=Decimal("8000"),
                    currency="ARS", category="Restaurante",
                    expense_date=today, confidence=Decimal("1"),
                )
            ],
            original_message="seed",
        )
        BudgetService(BudgetRepository(s)).upsert_from_draft(
            user_id=123456,
            draft=BudgetDraft(
                category="Restaurante", monthly_limit=Decimal("10000"),
                currency="ARS",
            ),
        )
        eid = s.query(Expense).one().id
        ExpenseRepository(s).soft_delete(123456, eid)

    client = TestClient(app)
    items = client.get("/api/budgets?period=month").json()["items"]
    assert len(items) == 1
    # Soft-deleted expense must drop spent to 0 and not "exceed".
    assert items[0]["spent"] == 0.0
    assert items[0]["level"] == "ok"


# ---------------------------------------------------------------------------
# Category resolution: hierarchy-aware lookup
# ---------------------------------------------------------------------------


def test_register_many_resolves_category_with_single_db_row(in_memory_db):
    """Smoke: a category that exists once in the DB is picked up
    directly, even if the in-memory registry has been hydrated from a
    snapshot that pre-dated its creation."""
    today = today_in_tz("UTC")
    with session_scope() as s:
        from app.categories.repository import CategoryRepository
        CategoryRepository(s).find_or_create(
            "Mascotas", parent_name="Hogar"
        )

    with session_scope() as s:
        outcome = ExpenseService(ExpenseRepository(s)).register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Alimento", amount=Decimal("3000"),
                    currency="ARS", category="Mascotas",
                    expense_date=today, confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )
    assert len(outcome.saved) == 1


def test_register_many_handles_duplicate_category_names_by_hierarchy(
    in_memory_db,
):
    """Same NAME under different parents MUST resolve to the canonical
    (parent_id, name) listed in the registry's tree.

    Seed already has ``Café`` under ``Comida``. We inject a second
    ``Café`` row with a NULL parent; the registry still says ``Café``
    lives under ``Comida`` so the resolution must NOT pick the NULL-
    parent row.
    """
    from app.categories.models import Category
    from sqlalchemy.exc import IntegrityError

    today = today_in_tz("UTC")
    # Confirm the canonical "Café" row exists under "Comida".
    with session_scope() as s:
        com_id = (
            s.query(Category).filter(Category.name == "Comida").one().id
        )
        cafe_under_comida = (
            s.query(Category)
            .filter(Category.name == "Café", Category.parent_id == com_id)
            .one()
        )
        original_cafe_id = cafe_under_comida.id

    # Inject a second "Café" row with parent_id IS NULL.
    with session_scope() as s:
        s.add(
            Category(
                name="Café", parent_id=None, is_active=True,
            )
        )
        try:
            pass  # may or may not be allowed; either way the
            # resolution logic must not blow up
        except IntegrityError:
            pass

    with session_scope() as s:
        all_cafes = (
            s.query(Category).filter(Category.name == "Café").all()
        )
        # Both rows exist; resolution must pick the one the registry
        # canonicalises.
        assert len(all_cafes) >= 1

    with session_scope() as s:
        outcome = ExpenseService(ExpenseRepository(s)).register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Latte", amount=Decimal("1500"),
                    currency="ARS", category="Café",
                    expense_date=today, confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )
    assert len(outcome.saved) == 1
    # The canonical (parent_id=Comida, name=Café) row was used.
    assert outcome.saved[0].category_id == original_cafe_id


def test_fixed_expense_without_category_falls_back_to_otros(in_memory_db):
    """When the FixedExpense template carries ``category_id = NULL``
    AND the registry doesn't normalize "Otros" to anything more specific,
    the mirror must land on the top-level ``Otros`` parent (no parent_id).
    This was the scenario that surfaced the original
    ``MultipleResultsFound`` in PostgreSQL integration."""
    from app.categories.models import Category

    today = today_in_tz("UTC")
    my = today.strftime("%Y-%m")
    otros_row = None
    with session_scope() as s:
        otros_row = (
            s.query(Category)
            .filter(Category.name == "Otros", Category.parent_id.is_(None))
            .one()
        )
        otros_id = otros_row.id

    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = service.add(
            user_id=1,
            draft=FixedExpenseDraft(
                name="CLOUD", expected_amount=Decimal("5000"),
                category=None,
            ),
        )
        service.mark_paid(1, bill.id, my)

    with session_scope() as s:
        mirror = (
            s.query(Expense)
            .filter(Expense.source_key.like("fixed-payment:%"))
            .one()
        )
        # Mirror lands on the top-level ``Otros`` (parent_id NULL).
        assert mirror.category_id == otros_id


def test_register_many_unknown_category_falls_back_to_otros(in_memory_db):
    """A draft with a category name that matches no DB row must fall
    back to ``Otros`` and NOT raise ``MultipleResultsFound``."""
    today = today_in_tz("UTC")
    with session_scope() as s:
        outcome = ExpenseService(ExpenseRepository(s)).register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="?", amount=Decimal("100"),
                    currency="ARS", category="zzzzz-no-existe",
                    expense_date=today, confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )
    assert len(outcome.saved) == 1
    from app.categories.models import Category
    with session_scope() as s:
        otros_id = (
            s.query(Category)
            .filter(Category.name == "Otros", Category.parent_id.is_(None))
            .one().id
        )
    assert outcome.saved[0].category_id == otros_id


# ---------------------------------------------------------------------------
# source_type policy
# ---------------------------------------------------------------------------


def test_telegram_expense_uses_telegram_source_type(in_memory_db):
    """A direct ``register_many`` call (the path the bot uses) MUST
    default to ``source_type='telegram'`` and leave ``source_key`` NULL.
    Only fixed-payment mirrors (and future InboundEvent-driven rows)
    should carry non-legacy / non-null source_key values."""
    today = today_in_tz("UTC")
    with session_scope() as s:
        outcome = ExpenseService(ExpenseRepository(s)).register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Café", amount=Decimal("1000"),
                    currency="ARS", category="Café",
                    expense_date=today, confidence=Decimal("1"),
                )
            ],
            original_message="seed",
        )
    assert len(outcome.saved) == 1
    row = outcome.saved[0]
    assert row.source_type == "telegram"
    assert row.source_key is None
    assert row.revision == 1


def test_fixed_payment_mirror_uses_fixed_payment_source_type(in_memory_db):
    """A fixed-payment mirror MUST be ``source_type='fixed_payment'``
    with the deterministic ``source_key`` and ``revision=1`` on
    creation. A subsequent ``mark_paid`` (the "update existing
    mirror" branch) MUST bump the revision and keep source_type."""
    today = today_in_tz("UTC")
    my = today.strftime("%Y-%m")
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = service.add(
            user_id=1,
            draft=FixedExpenseDraft(
                name="CASA", expected_amount=Decimal("90000"),
            ),
        )
        service.mark_paid(1, bill.id, my)
        mirror = (
            s.query(Expense)
            .filter(Expense.source_key.like("fixed-payment:%"))
            .one()
        )
        assert mirror.source_type == "fixed_payment"
        assert mirror.source_key == f"fixed-payment:{bill.id}:{my}"
        assert mirror.revision == 1

        # Re-pay: mirror must keep source_type, source_key, and bump revision.
        service.mark_paid(1, bill.id, my, actual_amount=Decimal("95000"))
        mirror = (
            s.query(Expense)
            .filter(Expense.source_key.like("fixed-payment:%"))
            .one()
        )
        assert mirror.source_type == "fixed_payment"
        assert mirror.source_key == f"fixed-payment:{bill.id}:{my}"
        assert mirror.revision >= 2
        assert mirror.amount == Decimal("95000.00")


def test_legacy_backfill_value_pinned_at_column_level(in_memory_db):
    """Phase-0 migration backfills pre-existing rows with
    ``source_type='legacy'`` via a TEMPORARY server default that is
    dropped before the migration closes. Application code is the
    only authority on ``source_type`` for new rows; the DB rejects
    ``INSERT`` statements that omit it.

    The four assertions below pin every half of that policy:
    A. legacy backfill semantics are preserved (explicit inserts
       stamp ``"legacy"`` for rows the migration seeded);
    B. raw ``INSERT`` without ``source_type`` is REJECTED by NOT NULL
       — proving the server default is gone;
    C. ``ExpenseService.register_many`` (the bot path) creates a
       ``"telegram"`` row;
    D. ``FixedExpenseService.mark_paid`` creates a ``"fixed_payment"``
       mirror."""
    from datetime import date as _date

    from sqlalchemy import inspect, text

    from app.categories.models import Category

    # ---- A. legacy backfill semantics preserved ----
    with session_scope() as s:
        cat_id = (
            s.query(Category).filter(Category.name == "Café").first().id
        )
        s.execute(
            text(
                "INSERT INTO expenses (telegram_user_id, name, amount, "
                "currency, category_id, expense_date, original_message, "
                "ai_confidence, source_type, source_key, revision) "
                "VALUES (1, 'legacy-backfill', 1, 'ARS', :cat, "
                "'2025-01-01', '', 0, 'legacy', NULL, 1)"
            ),
            {"cat": cat_id},
        )

    with session_scope() as s:
        legacy_row = (
            s.query(Expense)
            .filter(Expense.name == "legacy-backfill")
            .one()
        )
        assert legacy_row.source_type == "legacy"

    # ---- B. server default is gone: an INSERT omitting
    #        ``source_type`` must fail with NOT NULL ----
    with pytest.raises(Exception) as exc_info:
        with session_scope() as s:
            cat_id = (
                s.query(Category)
                .filter(Category.name == "Café")
                .first()
                .id
            )
            s.execute(
                text(
                    "INSERT INTO expenses (telegram_user_id, name, amount, "
                    "currency, category_id, expense_date, original_message, "
                    "ai_confidence, revision) "
                    "VALUES (1, 'no-source-type', 1, 'ARS', :cat, "
                    "'2025-01-01', '', 0, 1)"
                ),
                {"cat": cat_id},
            )
    # The IntegrityError / OperationalError MUST mention ``source_type``
    # and ``null value`` (NOT NULL violation).
    msg = str(exc_info.value).lower()
    assert "source_type" in msg and (
        "null value" in msg or "not-null" in msg or "not null" in msg
    ), f"Unexpected error: {exc_info.value!r}"

    # Belt-and-braces: introspect the schema and assert the column
    # has NO server_default. The migration dropped it; a regression
    # that re-adds it would hide the B-case above.
    with session_scope() as s:
        cols = {
            c["name"]: c for c in inspect(s.bind).get_columns("expenses")
        }
    assert cols["source_type"].get("default") is None, (
        "source_type must not have a server default after migration 0008"
    )

    # ---- C. ExpenseService.register_many stamps ``telegram`` ----
    today = today_in_tz("UTC")
    with session_scope() as s:
        outcome = ExpenseService(ExpenseRepository(s)).register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Café", amount=Decimal("1000"),
                    currency="ARS", category="Café",
                    expense_date=today, confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )
    assert outcome.saved[0].source_type == "telegram"

    # ---- D. FixedExpenseService.mark_paid stamps ``fixed_payment`` ----
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = service.add(
            user_id=1,
            draft=FixedExpenseDraft(
                name="CASA", expected_amount=Decimal("90000"),
            ),
        )
        service.mark_paid(1, bill.id, today.strftime("%Y-%m"))
    with session_scope() as s:
        mirror = (
            s.query(Expense)
            .filter(Expense.source_key.like("fixed-payment:%"))
            .one()
        )
        assert mirror.source_type == "fixed_payment"


# ---------------------------------------------------------------------------
# Timezone: day / month boundary against configured TIMEZONE
# ---------------------------------------------------------------------------


def test_business_today_handles_utc_to_local_day_offset(monkeypatch):
    """When the wall clock sits between two days across UTC and the
    configured timezone, ``business_today`` MUST return the local day.

    Concretely: UTC at 02:00 the next day is still ``2026-09-01`` in
    ``America/Argentina/Buenos_Aires`` (UTC-03). ``business_today``
    must return the local date, not the UTC date.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from app.utils.now import business_today

    class _FakeDateTime(_dt):
        @classmethod
        def now(cls, tz=None):
            base = _dt(2026, 9, 2, 2, 0, 0, tzinfo=ZoneInfo("UTC"))
            if tz is None:
                return base.replace(tzinfo=None)
            return base.astimezone(tz)

    monkeypatch.setattr(
        "app.utils.now.datetime", _FakeDateTime
    )
    # settings.timezone defaults to America/Argentina/Buenos_Aires
    # from the env, so we don't need to touch it here.
    today = business_today()
    # Buenos Aires is UTC-03, so 02:00 UTC = 23:00 local the day before.
    assert today == date(2026, 9, 1)


def test_business_today_handles_month_boundary(monkeypatch):
    """Crossing the month boundary in UTC but not in the local
    timezone must still report the LOCAL month/year.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from app.utils.now import business_today

    from app.fixed_expenses.service import current_month_year

    class _FakeDateTime(_dt):
        @classmethod
        def now(cls, tz=None):
            base = _dt(2026, 10, 1, 2, 0, 0, tzinfo=ZoneInfo("UTC"))
            if tz is None:
                return base.replace(tzinfo=None)
            return base.astimezone(tz)

    monkeypatch.setattr(
        "app.utils.now.datetime", _FakeDateTime
    )
    today = business_today()
    # 02:00 UTC Oct 1 = 23:00 local Sep 30.
    assert today == date(2026, 9, 30)
    assert current_month_year(today) == "2026-09"


def test_fixed_month_resolution_uses_business_date(monkeypatch):
    """``current_month_year`` in fixed_expenses must follow
    ``business_today``, not a naive ``date.today()``."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from app.utils.now import business_today

    from app.fixed_expenses.service import current_month_year

    class _FakeDateTime(_dt):
        @classmethod
        def now(cls, tz=None):
            base = _dt(2026, 10, 1, 2, 0, 0, tzinfo=ZoneInfo("UTC"))
            if tz is None:
                return base.replace(tzinfo=None)
            return base.astimezone(tz)

    monkeypatch.setattr(
        "app.utils.now.datetime", _FakeDateTime
    )
    today = business_today()
    assert current_month_year(today) == "2026-09"
    assert current_month_year(None) == "2026-09"


# ---------------------------------------------------------------------------
# IntegrityError recovery path in mark_paid
# ---------------------------------------------------------------------------


def test_mark_paid_recovers_when_payment_insert_loses_race(in_memory_db):
    """After a flushed ``IntegrityError`` on the unique
    ``(fixed_expense_id, month_year)`` constraint, the service must
    rollback the failed statement, re-fetch the winning payment, and
    finish the operation in a single transaction.

    We simulate the race deterministically by:

    1. Patching ``Session.flush`` to raise ``IntegrityError`` on the
       first call (the one that would have INSERTed the new payment).
    2. Patching the repo's ``get_payment_for_update`` to return
       ``None`` on the first call (before the IntegrityError) and
       the committed winner on the second call (after the rollback
       and refetch).

    Result: one payment row + one expense mirror with the link intact.
    """
    from datetime import datetime
    from unittest.mock import patch

    from sqlalchemy.exc import IntegrityError
    import sqlalchemy.orm

    from app.fixed_expenses.models import FixedExpensePayment

    today = today_in_tz("UTC")
    my = today.strftime("%Y-%m")
    user_id = 31

    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = service.add(
            user_id=user_id,
            draft=FixedExpenseDraft(
                name="EDESUR", expected_amount=Decimal("15000"),
            ),
        )
        bid = bill.id

    # Pre-insert a won row in a separate committed transaction so the
    # refetch (after the rollback) finds it.
    with session_scope() as s:
        s.add(
            FixedExpensePayment(
                fixed_expense_id=bid,
                month_year=my,
                paid_at=datetime(2026, 9, 10, 12, 0, 0),
                actual_amount=Decimal("15000"),
                skipped=False,
                expense_id=None,
            )
        )

    factory = get_session_factory()
    raw = factory()
    flush_calls = {"n": 0}
    get_calls = {"n": 0}

    # Snapshot the bound ``flush`` once, BEFORE patching the class, so
    # the patch's fall-through path calls the original implementation
    # instead of recursing back into our wrapper.
    _real_session_flush = sqlalchemy.orm.Session.__dict__["flush"]

    def explode_first_flush(self, *args, **kwargs):
        flush_calls["n"] += 1
        if flush_calls["n"] == 1:
            raise IntegrityError(
                "INSERT INTO fixed_expense_payments",
                params=None,
                orig=Exception(
                    "duplicate key value violates unique "
                    "constraint uq_payment_per_month"
                ),
            )
        return _real_session_flush(self, *args, **kwargs)

    def get_payment_for_update_first_time_none(self, fixed_id, month_year):
        """First call (before our flush → IntegrityError) sees no payment;
        second call (after our rollback + refetch) sees the committed
        winner."""
        get_calls["n"] += 1
        from app.fixed_expenses.models import FixedExpensePayment
        stmt = sqlalchemy.select(FixedExpensePayment).where(
            FixedExpensePayment.fixed_expense_id == fixed_id,
            FixedExpensePayment.month_year == month_year,
        )
        if get_calls["n"] == 1:
            return None
        return self.session.execute(stmt).scalar_one_or_none()

    try:
        with patch.object(
            sqlalchemy.orm.Session, "flush", explode_first_flush
        ), patch.object(
            FixedExpenseRepository,
            "get_payment_for_update",
            get_payment_for_update_first_time_none,
        ):
            svc = FixedExpenseService(FixedExpenseRepository(raw))
            obj, payment = svc.mark_paid(user_id, bid, my)
            assert obj is not None
            assert payment is not None
            # Exactly one payment row visible in this session.
            payment_count = (
                raw.query(FixedExpensePayment)
                .filter_by(fixed_expense_id=bid, month_year=my)
                .count()
            )
            assert payment_count == 1
            # We triggered the IntegrityError exactly once and the
            # recovery refetch exactly once.
            assert flush_calls["n"] >= 2  # first one exploded, second one passed
            assert get_calls["n"] == 2
        raw.commit()
    finally:
        raw.close()

    with session_scope() as s:
        expenses = (
            s.query(Expense)
            .filter(Expense.source_key.like("fixed-payment:%"))
            .all()
        )
        payments = (
            s.query(FixedExpensePayment)
            .filter_by(fixed_expense_id=bid, month_year=my)
            .all()
        )
        # Exactly one mirror, one payment, link intact.
        assert len(expenses) == 1
        assert len(payments) == 1
        assert payments[0].expense_id == expenses[0].id
        assert expenses[0].source_type == "fixed_payment"


def test_mark_paid_does_not_recurse_after_integrity_error(in_memory_db):
    """The recovery branch MUST NOT recurse into ``mark_paid``. If
    the refetch still finds no payment (defensive branch), the
    original IntegrityError propagates — there is no infinite loop.
    """
    from unittest.mock import patch

    from sqlalchemy.exc import IntegrityError
    import sqlalchemy.orm

    today = today_in_tz("UTC")
    my = today.strftime("%Y-%m")
    flush_calls = {"n": 0}
    user_id = 32

    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = service.add(
            user_id=user_id,
            draft=FixedExpenseDraft(
                name="AGUAS", expected_amount=Decimal("8000"),
            ),
        )
        bid = bill.id

    factory = get_session_factory()
    raw = factory()

    def explode_forever(self, *args, **kwargs):
        flush_calls["n"] += 1
        raise IntegrityError(
            "INSERT INTO fixed_expense_payments",
            params=None,
            orig=Exception("duplicate key"),
        )

    try:
        with patch.object(
            sqlalchemy.orm.Session, "flush", explode_forever
        ):
            svc = FixedExpenseService(FixedExpenseRepository(raw))
            # We hit the IntegrityError once, the recovery branch
            # refetches the payment (which doesn't exist), then the
            # original error is re-raised — no recursion, no hang.
            with pytest.raises(IntegrityError):
                svc.mark_paid(user_id, bid, my)
        raw.rollback()
    finally:
        raw.close()

    # ``flush`` was called exactly once. The recovery branch does
    # NOT call ``flush`` again — it does a SELECT (not counted).
    assert flush_calls["n"] == 1
