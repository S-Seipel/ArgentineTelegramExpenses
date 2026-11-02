"""Deterministic final F1 cycle, history and currency integration checks."""
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from app.config.settings import get_settings
from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.expenses.schemas import ExpenseCreate
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.service import FixedExpenseDraft, FixedExpenseService
from app.main import app
from app.money import CycleService, MoneyEngine, MoneyStateService, UserSettingsService
from app.money.models import FinancialCycle
from app.money.fixed_obligations import resolve_current_obligations
from app.web.routes import DASHBOARD_HTML


def _expense(session, user, amount, currency, when):
    ExpenseRepository(session).create(ExpenseCreate(
        telegram_user_id=user, name="Normal", amount=Decimal(amount),
        currency=currency, category_id=1, expense_date=when,
        original_message="normal", ai_confidence=Decimal("1"),
        source_type="telegram", revision=1,
    ))


def _money(summary, field, currency):
    return Decimal(getattr(summary, field).to_dict().get(currency, "0"))


def test_final_ars_cycle_and_immutable_history(in_memory_db):
    user = get_settings().telegram_allowed_user_id
    today = date(2026, 10, 15)
    with session_scope() as session:
        UserSettingsService(session).set_payday_day(user, 1)
        cycle = CycleService(session).open_initial_cycle(user, today).cycle_id
        state = MoneyStateService(session)
        state.set_income(user, cycle, "ARS", Decimal("1100000"))
        state.set_savings_reserved(user, cycle, "ARS", Decimal("220000"))
        state.record_savings_realized(user, cycle, "ARS", Decimal("150000"), "initial")
        _expense(session, user, "400000", "ARS", today)
        fixed = FixedExpenseService(FixedExpenseRepository(session))
        ids = {}
        for name, amount in (("Netflix", "50000"), ("Edesur", "80000"), ("Gimnasio", "30000")):
            ids[name] = fixed.add(user, FixedExpenseDraft(
                name=name, expected_amount=Decimal(amount), due_day_of_month=5
            )).id
    with session_scope() as session:
        summary = MoneyEngine(session).get_cycle_summary(user, cycle, today=today)
        assert _money(summary, "gross_income", "ARS") == 1100000
        assert _money(summary, "savings_committed", "ARS") == 220000
        assert _money(summary, "realized_expenses", "ARS") == 400000
        assert _money(summary, "pending_reserved_obligations", "ARS") == 160000
        assert _money(summary, "available_to_spend", "ARS") == 320000
        fixed = FixedExpenseService(FixedExpenseRepository(session))
        fixed.mark_paid(user, ids["Netflix"], "2026-10", paid_on=today)
    with session_scope() as session:
        summary = MoneyEngine(session).get_cycle_summary(user, cycle, today=today)
        assert _money(summary, "realized_expenses", "ARS") == 450000
        assert _money(summary, "pending_reserved_obligations", "ARS") == 110000
        assert _money(summary, "available_to_spend", "ARS") == 320000
        FixedExpenseService(FixedExpenseRepository(session)).mark_paid(
            user, ids["Edesur"], "2026-10", actual_amount=Decimal("92000"), paid_on=today
        )
    with session_scope() as session:
        summary = MoneyEngine(session).get_cycle_summary(user, cycle, today=today)
        assert _money(summary, "realized_expenses", "ARS") == 542000
        assert _money(summary, "pending_reserved_obligations", "ARS") == 30000
        assert _money(summary, "available_to_spend", "ARS") == 308000
        MoneyStateService(session).record_savings_realized(
            user, cycle, "ARS", Decimal("300000"), "initial"
        )
    with session_scope() as session:
        summary = MoneyEngine(session).get_cycle_summary(user, cycle, today=today)
        assert _money(summary, "savings_committed", "ARS") == 300000
        assert _money(summary, "available_to_spend", "ARS") == 228000
        closed = CycleService(session).close_cycle(user, cycle, force=True)
        assert _money(closed, "available_to_spend", "ARS") == 228000
        assert closed.daily_safe_to_spend is None
        assert [(item.name, item.expected_amount, item.original_month_year)
                for item in closed.carried_over_fixed] == [
                    ("Gimnasio", Decimal("30000"), "2026-10")
                ]
        frozen = session.get(FinancialCycle, cycle).closing_snapshot
    with session_scope() as session:
        next_cycle = CycleService(session).ensure_current_cycle(user, date(2026, 11, 5))
        next_summary = MoneyEngine(session).get_cycle_summary(
            user, next_cycle.id, today=date(2026, 11, 5)
        )
        pending_before = _money(next_summary, "pending_reserved_obligations", "ARS")
        assert pending_before >= 30000
        FixedExpenseService(FixedExpenseRepository(session)).mark_paid(
            user, ids["Gimnasio"], "2026-10", paid_on=date(2026, 11, 5)
        )
    with session_scope() as session:
        next_summary = MoneyEngine(session).get_cycle_summary(
            user, next_cycle.id, today=date(2026, 11, 5)
        )
        occurrences = resolve_current_obligations(
            session, user, session.get(FinancialCycle, next_cycle.id)
        ).occurrences
        old_gym = [item for item in occurrences if
                   item.fixed_expense_id == ids["Gimnasio"]
                   and item.original_month_year == "2026-10"]
        assert len(old_gym) == 1 and old_gym[0].paid
        assert _money(next_summary, "pending_reserved_obligations", "ARS") == pending_before
        assert session.get(FinancialCycle, cycle).closing_snapshot == frozen
        assert _money(MoneyEngine(session).get_cycle_summary(user, cycle),
                      "available_to_spend", "ARS") == 228000
    response = TestClient(app).get("/api/cycle-history")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    item = response.json()["items"][0]
    assert item["money"]["available_to_spend"]["ARS"] == "228000.00"
    assert item["money"]["daily_safe_to_spend"] is None


def test_currency_partition_and_history(in_memory_db):
    user = get_settings().telegram_allowed_user_id
    today = date(2026, 10, 15)
    with session_scope() as session:
        UserSettingsService(session).set_payday_day(user, 1)
        cycle = CycleService(session).open_initial_cycle(user, today).cycle_id
        state = MoneyStateService(session)
        fixed = FixedExpenseService(FixedExpenseRepository(session))
        for currency, income, saving, expense, pending in (
            ("ARS", "1000", "100", "200", "50"),
            ("USD", "500", "80", "20", "30"),
        ):
            state.set_income(user, cycle, currency, Decimal(income))
            state.set_savings_reserved(user, cycle, currency, Decimal(saving))
            _expense(session, user, expense, currency, today)
            fixed.add(user, FixedExpenseDraft(
                name="Bill " + currency, expected_amount=Decimal(pending),
                currency=currency, due_day_of_month=5
            ))
    with session_scope() as session:
        engine = MoneyEngine(session)
        summary = engine.get_cycle_summary(user, cycle, today=today)
        assert summary.gross_income.to_dict() == {"ARS": "1000.00", "USD": "500.00"}
        assert summary.available_to_spend.to_dict() == {"ARS": "650.00", "USD": "370.00"}
        CycleService(session).close_cycle(user, cycle, force=True)
    history = TestClient(app).get("/api/cycle-history").json()["items"]
    assert history[0]["money"]["available_to_spend"] == {
        "ARS": "650.00", "USD": "370.00"
    }
    assert history[0]["money"]["daily_safe_to_spend"] is None


def test_history_rejects_unknown_snapshot_version(in_memory_db):
    user = get_settings().telegram_allowed_user_id
    with session_scope() as session:
        cycle = CycleService(session).open_initial_cycle(user, date(2026, 10, 15)).cycle_id
        CycleService(session).close_cycle(user, cycle, force=True)
        row = session.get(FinancialCycle, cycle)
        row.closing_snapshot = {**row.closing_snapshot, "snapshot_version": 999}
    response = TestClient(app).get("/api/cycle-history")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "unsupported_snapshot_version"


def test_active_cycle_dashboard_uses_server_money_fields():
    assert "Ciclo activo" in DASHBOARD_HTML
    assert "/api/cycle/current" in DASHBOARD_HTML
    assert "/api/cycle-summary?cycle_id=" in DASHBOARD_HTML
    assert "money[key][currency]" in DASHBOARD_HTML
    assert "gross_income -" not in DASHBOARD_HTML
    assert "income - savings" not in DASHBOARD_HTML
