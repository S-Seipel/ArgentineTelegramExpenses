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


def test_api_fixed_expenses_empty(in_memory_db, client):
    r = client.get("/api/fixed-expenses")
    assert r.status_code == 200
    assert r.json() == {"items": []}


def test_api_fixed_expenses_returns_status(in_memory_db, client):
    from app.fixed_expenses.service import FixedExpenseDraft
    from app.fixed_expenses.repository import FixedExpenseRepository
    from app.fixed_expenses.service import FixedExpenseService
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        casa = svc.add(user_id=123456, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000"),
            payment_method="TRANSFERENCIA", due_day_of_month=10))
        svc.mark_paid(user_id=123456, fixed_id=casa.id,
                      month_year=today_in_tz("UTC").strftime("%Y-%m"))
    r = client.get("/api/fixed-expenses")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "CASA"
    assert items[0]["status"] == "paid_exact"


def test_api_month_summary_no_budget(in_memory_db, client):
    r = client.get("/api/month-summary")
    assert r.status_code == 200
    data = r.json()
    assert data["month_year"] == today_in_tz("UTC").strftime("%Y-%m")
    assert data["income"] == 0.0
    assert data["liberado"] == 0.0


def test_api_month_summary_with_income(in_memory_db, client):
    from app.fixed_expenses.service import FixedExpenseDraft
    from app.fixed_expenses.repository import FixedExpenseRepository
    from app.fixed_expenses.service import FixedExpenseService
    my = today_in_tz("UTC").strftime("%Y-%m")
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        svc.set_budget(123456, my, income=Decimal("1000000"),
                       extra=Decimal("50000"))
    r = client.get("/api/month-summary")
    data = r.json()
    assert data["income"] == 1000000.0
    assert data["extra"] == 50000.0


def test_api_create_fixed_expense(in_memory_db, client):
    r = client.post(
        "/api/fixed-expenses",
        json={
            "name": "CASA",
            "expected_amount": 90000,
            "payment_method": "TRANSFERENCIA",
            "due_day_of_month": 10,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name"] == "CASA"
    assert data["expected_amount"] == 90000.0
    assert data["currency"] == "ARS"
    assert data["payment_method"] == "TRANSFERENCIA"
    assert data["due_day_of_month"] == 10

    # Should appear in the GET endpoint
    r = client.get("/api/fixed-expenses")
    assert len(r.json()["items"]) == 1


def test_api_create_fixed_expense_invalid(in_memory_db, client):
    # Missing name
    r = client.post("/api/fixed-expenses", json={
        "name": "",
        "expected_amount": 1000,
    })
    assert r.status_code == 400

    # Zero amount
    r = client.post("/api/fixed-expenses", json={
        "name": "X",
        "expected_amount": 0,
    })
    assert r.status_code == 400


def test_api_pay_fixed_expense(in_memory_db, client):
    from app.fixed_expenses.service import FixedExpenseDraft
    from app.fixed_expenses.repository import FixedExpenseRepository
    from app.fixed_expenses.service import FixedExpenseService
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(user_id=123456, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000"),
            payment_method="TRANSFERENCIA", due_day_of_month=10))
    fid = bill.id

    # Mark as paid with explicit amount
    r = client.post(f"/api/fixed-expenses/{fid}/pay",
                    json={"actual_amount": 92000})
    assert r.status_code == 200

    # Verify it shows paid with diff
    r = client.get("/api/fixed-expenses")
    items = r.json()["items"]
    assert items[0]["status"] == "paid_more"
    assert items[0]["actual_amount"] == 92000.0
    assert items[0]["diff"] == 2000.0


def test_api_pay_fixed_expense_exact(in_memory_db, client):
    from app.fixed_expenses.service import FixedExpenseDraft
    from app.fixed_expenses.repository import FixedExpenseRepository
    from app.fixed_expenses.service import FixedExpenseService
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(user_id=123456, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000")))
    fid = bill.id

    r = client.post(f"/api/fixed-expenses/{fid}/pay",
                    json={})  # no actual_amount -> uses expected
    assert r.status_code == 200
    r = client.get("/api/fixed-expenses")
    assert r.json()["items"][0]["status"] == "paid_exact"


def test_api_pay_fixed_expense_invalid_amount(in_memory_db, client):
    from app.fixed_expenses.service import FixedExpenseDraft
    from app.fixed_expenses.repository import FixedExpenseRepository
    from app.fixed_expenses.service import FixedExpenseService
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(user_id=123456, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000")))
    r = client.post(f"/api/fixed-expenses/{bill.id}/pay",
                    json={"actual_amount": 0})
    assert r.status_code == 400


def test_api_pay_fixed_expense_not_found(client):
    r = client.post("/api/fixed-expenses/99999/pay", json={})
    assert r.status_code == 404


def test_api_skip_fixed_expense(in_memory_db, client):
    from app.fixed_expenses.service import FixedExpenseDraft
    from app.fixed_expenses.repository import FixedExpenseRepository
    from app.fixed_expenses.service import FixedExpenseService
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(user_id=123456, draft=FixedExpenseDraft(
            name="DENTISTA", expected_amount=Decimal("32000")))
    r = client.post(f"/api/fixed-expenses/{bill.id}/skip", json={})
    assert r.status_code == 200
    r = client.get("/api/fixed-expenses")
    assert r.json()["items"][0]["status"] == "skipped"


def test_api_unpay_fixed_expense(in_memory_db, client):
    from app.fixed_expenses.service import FixedExpenseDraft
    from app.fixed_expenses.repository import FixedExpenseRepository
    from app.fixed_expenses.service import FixedExpenseService
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(user_id=123456, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000")))
        svc.mark_paid(user_id=123456, fixed_id=bill.id,
                      month_year=today_in_tz("UTC").strftime("%Y-%m"))

    r = client.post(f"/api/fixed-expenses/{bill.id}/unpay", json={})
    assert r.status_code == 200
    r = client.get("/api/fixed-expenses")
    assert r.json()["items"][0]["status"] == "pending"


def test_api_full_flow_via_dashboard(in_memory_db, client):
    """E2E: create via POST, mark paid with diff, verify summary."""
    from app.fixed_expenses.repository import FixedExpenseRepository
    from app.fixed_expenses.service import FixedExpenseService
    # Create two bills
    r1 = client.post("/api/fixed-expenses", json={
        "name": "CASA", "expected_amount": 90000,
        "payment_method": "TRANSFERENCIA", "due_day_of_month": 10,
    })
    r2 = client.post("/api/fixed-expenses", json={
        "name": "GYM", "expected_amount": 60000,
        "payment_method": "EFECTIVO", "due_day_of_month": 15,
    })
    assert r1.status_code == 200 and r2.status_code == 200

    casa_id = r1.json()["id"]
    gym_id = r2.json()["id"]

    # Set income
    my = today_in_tz("UTC").strftime("%Y-%m")
    client.post("/api/month-summary")  # warmup
    # Set income via service directly
    with session_scope() as s:
        FixedExpenseService(FixedExpenseRepository(s)).set_budget(
            123456, my, income=Decimal("1100000"),
            extra=Decimal("75000"))

    # Pay CASA exact, GYM more
    client.post(f"/api/fixed-expenses/{casa_id}/pay", json={})
    client.post(f"/api/fixed-expenses/{gym_id}/pay",
                json={"actual_amount": 70000})

    # Verify summary reflects paid amounts
    r = client.get("/api/month-summary")
    summary = r.json()
    assert summary["income"] == 1100000.0
    assert summary["extra"] == 75000.0
    # Total paid: 90000 + 70000 = 160000
    assert summary["total_fixed_paid"] == 160000.0
    # Liberado = 1100000 + 75000 - 160000 = 1015000
    assert summary["liberado"] == 1015000.0


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


def test_api_trend_accepts_period(client):
    """Default still works for backwards compat."""
    r = client.get("/api/trend?period=week")
    assert r.status_code == 200
    assert len(r.json()["points"]) == 7


def test_api_trend_period_today(client):
    r = client.get("/api/trend?period=today")
    assert r.status_code == 200
    assert len(r.json()["points"]) == 1


def test_api_trend_period_week(client):
    r = client.get("/api/trend?period=week")
    assert r.status_code == 200
    assert len(r.json()["points"]) == 7


def test_api_trend_period_year(client):
    r = client.get("/api/trend?period=year")
    assert r.status_code == 200
    assert len(r.json()["points"]) == 365


def test_api_comparison_accepts_period(in_memory_db, client):
    today = today_in_tz("UTC")
    last_month = today.replace(day=1)
    if last_month.month == 1:
        prev = last_month.replace(year=last_month.year - 1, month=12)
    else:
        prev = last_month.replace(month=last_month.month - 1)
    with session_scope() as s:
        ExpenseService(ExpenseRepository(s)).register_many(
            user_id=123456,
            drafts=[
                _draft(name="A", amount=Decimal("10000"), category="Café",
                       expense_date=today),
                _draft(name="B", amount=Decimal("5000"), category="Café",
                       expense_date=prev),
            ],
            original_message="seed",
        )
    r = client.get("/api/comparison?period=month")
    assert r.status_code == 200
    data = r.json()
    assert data["current"]["total"] == 10000.0
    assert data["previous"]["total"] == 5000.0


def test_api_comparison_period_today(in_memory_db, client):
    today = today_in_tz("UTC")
    yesterday = today.replace(day=max(1, today.day - 1))
    with session_scope() as s:
        ExpenseService(ExpenseRepository(s)).register_many(
            user_id=123456,
            drafts=[
                _draft(name="A", amount=Decimal("1000"), category="Café",
                       expense_date=today),
                _draft(name="B", amount=Decimal("500"), category="Café",
                       expense_date=yesterday),
            ],
            original_message="seed",
        )
    r = client.get("/api/comparison?period=today")
    assert r.status_code == 200
    data = r.json()
    assert data["current"]["total"] == 1000.0
    assert data["previous"]["total"] == 500.0


def test_api_projection_accepts_period(in_memory_db, client):
    today = today_in_tz("UTC")
    with session_scope() as s:
        ExpenseService(ExpenseRepository(s)).register_many(
            user_id=123456,
            drafts=[
                _draft(name="A", amount=Decimal("5000"), category="Café",
                       expense_date=today),
            ],
            original_message="seed",
        )
    r = client.get("/api/projection?period=month")
    assert r.status_code == 200
    data = r.json()
    assert data["applies"] is True
    assert data["spent"] == 5000.0
    assert data["projected_total"] >= 5000.0


def test_api_projection_today_does_not_apply(client):
    r = client.get("/api/projection?period=today")
    assert r.status_code == 200
    data = r.json()
    assert data["applies"] is False


def test_api_budgets_accepts_period(in_memory_db, client):
    today = today_in_tz("UTC")
    with session_scope() as s:
        ExpenseService(ExpenseRepository(s)).register_many(
            user_id=123456,
            drafts=[
                _draft(name="A", amount=Decimal("45000"), category="Café",
                       expense_date=today),
            ],
            original_message="seed",
        )
        BudgetService(BudgetRepository(s)).upsert_from_draft(
            user_id=123456,
            draft=BudgetDraft(category="Café", monthly_limit=Decimal("50000"),
                              currency=None),
        )
    # Month period: percent = 90
    r = client.get("/api/budgets?period=month")
    data = r.json()
    assert data["items"][0]["percent"] == 90.0
    assert data["items"][0]["effective_limit"] == 50000.0
    # Week period: limit scaled to ~7/30 = 11666, spent is full 45000
    r = client.get("/api/budgets?period=week")
    data = r.json()
    assert data["items"][0]["effective_limit"] < 50000.0
    assert data["items"][0]["percent"] > 90.0


def test_resolve_period_yesterday():
    from app.web.queries import resolve_period
    from app.utils.dates import today_in_tz
    today = today_in_tz("UTC")
    window = resolve_period("yesterday", today)
    assert window.start == window.end
    assert window.days_in_period == 1


def test_resolve_period_last_week():
    from app.web.queries import resolve_period
    from app.utils.dates import today_in_tz
    today = today_in_tz("UTC")
    window = resolve_period("last_week", today)
    assert (window.end - window.start).days == 6
    assert window.days_in_period == 7


def test_resolve_period_last_year():
    from app.web.queries import resolve_period
    from app.utils.dates import today_in_tz
    today = today_in_tz("UTC")
    window = resolve_period("last_year", today)
    assert window.start.year == today.year - 1
    assert window.end.year == today.year - 1


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
