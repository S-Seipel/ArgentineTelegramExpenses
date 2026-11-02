"""API integration: independent calendars, money, rollover and archive reads."""
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.categories.models import Category
from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.expenses.schemas import ExpenseCreate
from app.fixed_expenses.models import MonthlyBudget
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.service import FixedExpenseDraft, FixedExpenseService
from app.main import app
from app.money.models import CycleIncome, FinancialCycle
from app.web import routes


@pytest.fixture
def currency_client(in_memory_db):
    app.dependency_overrides[routes._today] = lambda: date(2026, 11, 20)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def test_independent_calendars_money_and_rollover(currency_client):
    client = currency_client
    _ok(client.patch("/api/settings/payday", json={
        "currency": "ARS", "pay_rule": "first_weekday", "payday_weekday": 4,
    }))
    _ok(client.patch("/api/settings/payday", json={
        "currency": "USD", "pay_rule": "fixed_day", "payday_day": 15,
    }))
    with session_scope() as session:
        assert session.query(FinancialCycle).count() == 0
        assert session.query(CycleIncome).count() == 0
    ars = _ok(client.post("/api/cycle/ensure?currency=ARS"))["cycle"]
    usd = _ok(client.post("/api/cycle/ensure?currency=USD"))["cycle"]
    assert ars["id"] != usd["id"]
    assert (ars["start_date"], ars["end_date"]) == ("2026-11-06", "2026-12-03")
    assert (usd["start_date"], usd["end_date"]) == ("2026-11-15", "2026-12-14")
    _ok(client.post("/api/cycles/income", json={"currency": "ARS", "amount": "1000"}))
    _ok(client.post("/api/cycles/income", json={"currency": "USD", "amount": "100"}))
    _ok(client.post("/api/cycles/savings/reserved", json={"currency": "ARS", "amount": "100"}))
    _ok(client.post("/api/cycles/savings/reserved", json={"currency": "USD", "amount": "10"}))

    with session_scope() as session:
        user_id = session.get(FinancialCycle, ars["id"]).telegram_user_id
        category_id = session.execute(select(Category.id).limit(1)).scalar_one()
        expense_repo = ExpenseRepository(session)
        for currency, amount, day in (("ARS", "250", 16), ("USD", "7", 16),
                                      ("ARS", "999", 1), ("USD", "50", 10)):
            expense_repo.create(ExpenseCreate(
                telegram_user_id=user_id, name="isolated expense",
                amount=Decimal(amount), currency=currency, category_id=category_id,
                expense_date=date(2026, 11, day), original_message="unit fixture",
                ai_confidence=Decimal("1"),
            ))
        fixed = FixedExpenseService(FixedExpenseRepository(session))
        for currency, amount in (("ARS", "50"), ("USD", "5")):
            fixed.add(user_id=user_id, draft=FixedExpenseDraft(
                name=f"bill {currency}", expected_amount=Decimal(amount),
                currency=currency, due_day_of_month=20,
            ))

    ars_summary = _ok(client.get("/api/cycle-summary?currency=ARS"))
    usd_summary = _ok(client.get("/api/cycle-summary?currency=USD"))
    assert ars_summary["money"]["available_to_spend"] == {"ARS": "600.00"}
    assert usd_summary["money"]["available_to_spend"] == {"USD": "78.00"}
    assert ars_summary["money"]["realized_expenses"] == {"ARS": "250.00"}
    assert usd_summary["money"]["realized_expenses"] == {"USD": "7.00"}

    conflict = client.post("/api/cycles/income", json={
        "cycle_id": ars["id"], "currency": "USD", "amount": "99999",
    })
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "cycle_currency_mismatch"
    assert _ok(client.get("/api/cycle-summary?currency=USD"))["money"]["income"] == {"USD": "100.00"}

    app.dependency_overrides[routes._today] = lambda: date(2026, 12, 5)
    successor = _ok(client.post("/api/cycle/ensure?currency=ARS"))["cycle"]
    assert successor["id"] != ars["id"]
    assert _ok(client.get("/api/cycle/current?currency=USD"))["cycle"]["id"] == usd["id"]
    history = _ok(client.get("/api/cycle-history?currency=ARS"))["items"]
    assert len(history) == 1
    assert history[0]["cycle_id"] == ars["id"]
    assert history[0]["money"]["available_to_spend"] == {"ARS": "600.00"}
    assert history[0]["money"]["daily_safe_to_spend"] is None
    assert _ok(client.get("/api/cycle-history?currency=USD"))["items"] == []


def test_legacy_month_can_be_read_without_creating_cycle(currency_client):
    client = currency_client
    with session_scope() as session:
        session.add(MonthlyBudget(
            telegram_user_id=123456, month_year="2025-03", income=Decimal("800"),
            extra=Decimal("20"), note="preserved historical data",
        ))
    history = _ok(client.get("/api/month-summary?month_year=2025-03"))
    assert history["month_year"] == "2025-03"
    assert history["income"] == 800
    assert history["extra"] == 20
    assert history["read_only"] is True
    assert history["source"] == "monthly_budget_legacy"
    assert client.get("/api/month-summary?month_year=2025-13").status_code in (400, 422)
    assert _ok(client.get("/api/cycle/current"))["cycle"] is None
    with session_scope() as session:
        assert session.query(CycleIncome).count() == 0
