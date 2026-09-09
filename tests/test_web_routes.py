from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.expenses.service import ExpenseDraft, ExpenseService
from app.budgets.repository import BudgetRepository
from app.budgets.service import BudgetDraft, BudgetService
from app.main import app
from app.utils.dates import today_in_tz


@pytest.fixture
def client(in_memory_db):
    return TestClient(app)


def _draft(**kw) -> ExpenseDraft:
    base = dict(
        name="Café",
        amount=Decimal("5000"),
        currency="ARS",
        category="Café",
        expense_date=today_in_tz("UTC"),
        confidence=Decimal("1"),
    )
    base.update(kw)
    return ExpenseDraft(**base)


# Tests use the user_id configured in conftest (123456).
USER_ID = 123456


def test_root_redirects_to_dashboard(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/dashboard"


def test_dashboard_html_returns(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Chart.js" in r.text or "chart.js" in r.text.lower()


def test_api_summary_empty(client):
    r = client.get("/api/summary?period=month")
    assert r.status_code == 200
    data = r.json()
    assert "total_by_currency" in data
    assert "by_category" in data
    assert data["total_by_currency"] == {}


def test_api_summary_with_expenses(in_memory_db):
    today = today_in_tz("UTC")
    with session_scope() as s:
        service = ExpenseService(ExpenseRepository(s))
        service.register_many(
            user_id=USER_ID,
            drafts=[
                _draft(name="Café", amount=Decimal("5000"), category="Café",
                       expense_date=today),
                _draft(name="Almuerzo", amount=Decimal("15000"), category="Restaurante",
                       expense_date=today),
                _draft(name="Bondi", amount=Decimal("2000"), category="Transporte público",
                       expense_date=today),
            ],
            original_message="seed",
        )

    client = TestClient(app)
    r = client.get("/api/summary?period=month")
    assert r.status_code == 200
    data = r.json()
    assert data["total_by_currency"].get("ARS", 0) == 22000.0
    cats = {c["name"]: c["amount"] for c in data["by_category"]}
    assert cats["Café"] == 5000.0
    assert cats["Restaurante"] == 15000.0


def test_api_trend_returns_daily_series(client):
    r = client.get("/api/trend?days=30")
    assert r.status_code == 200
    data = r.json()
    assert len(data["points"]) == 30
    assert "date" in data["points"][0]
    assert "amount" in data["points"][0]


def test_api_recent_returns_limit(client):
    today = today_in_tz("UTC")
    with session_scope() as s:
        service = ExpenseService(ExpenseRepository(s))
        drafts = [
            _draft(name=f"Gasto {i}", amount=Decimal("1000"), expense_date=today)
            for i in range(15)
        ]
        service.register_many(
            user_id=USER_ID, drafts=drafts, original_message="seed"
        )
    client = TestClient(app)
    r = client.get("/api/recent?limit=5")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 5


def test_api_recent_limit_validation(client):
    r = client.get("/api/recent?limit=0")
    assert r.status_code == 422
    r = client.get("/api/recent?limit=999")
    assert r.status_code == 422


def test_api_budgets(in_memory_db):
    today = today_in_tz("UTC")
    with session_scope() as s:
        ExpenseService(ExpenseRepository(s)).register_many(
            user_id=USER_ID,
            drafts=[
                _draft(name="Café", amount=Decimal("45000"), category="Café",
                       expense_date=today),
            ],
            original_message="seed",
        )
        BudgetService(BudgetRepository(s)).upsert_from_draft(
            user_id=USER_ID,
            draft=BudgetDraft(category="Café", monthly_limit=Decimal("50000"),
                              currency=None),
        )

    client = TestClient(app)
    r = client.get("/api/budgets")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    b = items[0]
    assert b["category"] == "Café"
    assert b["spent"] == 45000.0
    assert b["percent"] == 90.0
    assert b["level"] == "warning"


def test_api_comparison(in_memory_db):
    today = today_in_tz("UTC")
    last_month = today.replace(day=1)
    if last_month.month == 1:
        prev = last_month.replace(year=last_month.year - 1, month=12)
    else:
        prev = last_month.replace(month=last_month.month - 1)

    with session_scope() as s:
        ExpenseService(ExpenseRepository(s)).register_many(
            user_id=USER_ID,
            drafts=[
                _draft(name="A", amount=Decimal("10000"), category="Café",
                       expense_date=today),
                _draft(name="B", amount=Decimal("5000"), category="Café",
                       expense_date=prev),
            ],
            original_message="seed",
        )
    client = TestClient(app)
    r = client.get("/api/comparison")
    assert r.status_code == 200
    data = r.json()
    assert data["current"]["total"] == 10000.0
    assert data["previous"]["total"] == 5000.0
    assert data["delta_pct"] == 100.0


def test_api_projection_basic(in_memory_db):
    today = today_in_tz("UTC")
    with session_scope() as s:
        # Spend 10k on day 10 of a 30-day month → projected 30k
        day10 = today.replace(day=min(10, today.day))
        ExpenseService(ExpenseRepository(s)).register_many(
            user_id=USER_ID,
            drafts=[_draft(name="X", amount=Decimal("10000"), category="Café",
                           expense_date=day10)],
            original_message="seed",
        )
    client = TestClient(app)
    r = client.get("/api/projection")
    assert r.status_code == 200
    data = r.json()
    assert data["spent"] == 10000.0
    # Projection should be > spent if not at end of month
    if today.day < 28:
        assert data["projected_total"] > data["spent"]


def test_api_recurring_upcoming_empty(client):
    r = client.get("/api/recurring/upcoming")
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_api_summary_periods(client):
    for period in ("today", "week", "month", "year", "last_month"):
        r = client.get(f"/api/summary?period={period}")
        assert r.status_code == 200, period
        assert "period_label" in r.json()


def test_api_summary_unknown_period_defaults_to_month(client):
    r = client.get("/api/summary?period=nonsense")
    assert r.status_code == 200
    assert r.json()["period_label"] == "Este mes"


def test_dashboard_data_uses_only_allowed_user(in_memory_db):
    today = today_in_tz("UTC")
    with session_scope() as s:
        ExpenseService(ExpenseRepository(s)).register_many(
            user_id=USER_ID,
            drafts=[_draft(name="Mine", amount=Decimal("1000"), category="Café",
                           expense_date=today)],
            original_message="seed",
        )
        # Another user's expense that should NOT show up.
        ExpenseService(ExpenseRepository(s)).register_many(
            user_id=999,
            drafts=[_draft(name="Other", amount=Decimal("9999"), category="Café",
                           expense_date=today)],
            original_message="seed",
        )
    client = TestClient(app)
    r = client.get("/api/summary?period=month")
    assert r.status_code == 200
    data = r.json()
    # Only the allowed user's data appears.
    assert data["total_by_currency"].get("ARS", 0) == 1000.0
