"""Financial interface regressions for zero balances and explicit rollover."""
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from app.bot.handlers import _handle_cycle_read_command
from app.config.settings import get_settings
from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.expenses.schemas import ExpenseCreate
from app.main import app
from app.money import CycleService, MoneyStateService
from app.money.models import FinancialCycle
from app.web.routes import _today


TODAY = date(2026, 10, 15)


def _seed_balances(*, with_usd=False):
    user = get_settings().telegram_allowed_user_id
    with session_scope() as session:
        cycle_id = CycleService(session).open_initial_cycle(user, TODAY).cycle_id
        state = MoneyStateService(session)
        state.set_income(user, cycle_id, "ARS", Decimal("100"))
        if with_usd:
            state.set_income(user, cycle_id, "USD", Decimal("20"))
        ExpenseRepository(session).create(ExpenseCreate(
            telegram_user_id=user, name="All ARS spent", amount=Decimal("100"),
            currency="ARS", category_id=1, expense_date=TODAY,
            original_message="seed", ai_confidence=Decimal("1"),
            source_type="telegram", revision=1,
        ))
    return user, cycle_id


def test_api_zero_balance_is_explicit_and_keeps_both_currencies(in_memory_db):
    _, cycle_id = _seed_balances(with_usd=True)
    app.dependency_overrides[_today] = lambda: TODAY
    try:
        response = TestClient(app).get(f"/api/cycle-summary?cycle_id={cycle_id}")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    money = response.json()["money"]
    assert money["available_to_spend"] == {"ARS": "0.00", "USD": "20.00"}
    assert money["daily_safe_to_spend"]["ARS"] == "0.00"
    assert money["extra"] == {"ARS": "0.00", "USD": "0.00"}
    assert money["savings_reserved"] == {"ARS": "0.00", "USD": "0.00"}


def test_telegram_zero_is_a_balance_not_missing_data(in_memory_db):
    user, _ = _seed_balances()
    available = _handle_cycle_read_command("/disponible", user_id=user, today=TODAY)
    daily = _handle_cycle_read_command("/gastar_diario", user_id=user, today=TODAY)
    assert "0 ARS" in available
    assert "datos suficientes" not in available
    assert "0 ARS" in daily
    assert "Sin ingresos" not in daily


def test_telegram_keeps_currency_with_zero_alongside_nonzero_currency(in_memory_db):
    user, _ = _seed_balances(with_usd=True)
    reply = _handle_cycle_read_command("/disponible", user_id=user, today=TODAY)
    assert "0 ARS" in reply
    assert "20 USD" in reply


def test_expired_cycle_rolls_only_after_explicit_post(in_memory_db):
    user, cycle_id = _seed_balances()
    app.dependency_overrides[_today] = lambda: date(2026, 11, 2)
    try:
        client = TestClient(app)
        assert client.get("/api/cycle/current").json() == {"cycle": None}
        with session_scope() as session:
            assert session.get(FinancialCycle, cycle_id).status == "open"
        result = client.post("/api/cycle/ensure")
        assert result.status_code == 200
        assert result.json()["cycle"]["start_date"] == "2026-11-01"
        assert result.json()["cycle"]["cycle_index"] == 2
        assert client.get("/api/cycle/current").json() == result.json()
        with session_scope() as session:
            assert session.get(FinancialCycle, cycle_id).status == "closed"
    finally:
        app.dependency_overrides.clear()


def test_cycle_failure_does_not_expose_internal_details(in_memory_db, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("SQL password=private-internal-detail")

    monkeypatch.setattr(CycleService, "ensure_current_cycle", unavailable)
    reply = _handle_cycle_read_command("/cycle", user_id=123456, today=TODAY)
    assert "No pude resolver" in reply
    assert "SQL" not in reply
    assert "private-internal-detail" not in reply


def test_manually_closed_current_cycle_is_a_clear_state_for_api_and_telegram(in_memory_db):
    user, cycle_id = _seed_balances()
    with session_scope() as session:
        CycleService(session).close_cycle(user, cycle_id, force=True)
    app.dependency_overrides[_today] = lambda: TODAY
    try:
        client = TestClient(app)
        for method, path, body in (
            ("POST", "/api/cycle/ensure", None),
            ("GET", "/api/cycle-summary", None),
            ("POST", "/api/cycles/income", {"amount": "200", "currency": "ARS"}),
        ):
            response = client.request(method, path, json=body)
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == "cycle_not_current"
        reply = _handle_cycle_read_command("/cycle", user_id=user, today=TODAY)
        assert "cerrado" in reply
        with session_scope() as session:
            assert session.query(FinancialCycle).count() == 1
            assert session.get(FinancialCycle, cycle_id).status == "closed"
    finally:
        app.dependency_overrides.clear()


def test_unknown_snapshot_is_explicit_for_summary_and_cycle_update(in_memory_db):
    user, cycle_id = _seed_balances()
    with session_scope() as session:
        CycleService(session).close_cycle(user, cycle_id, force=True)
        cycle = session.get(FinancialCycle, cycle_id)
        cycle.closing_snapshot = {**cycle.closing_snapshot, "snapshot_version": 999}
    app.dependency_overrides[_today] = lambda: date(2026, 11, 2)
    try:
        client = TestClient(app)
        response = client.get(f"/api/cycle-summary?cycle_id={cycle_id}")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "unsupported_snapshot_version"
    finally:
        app.dependency_overrides.clear()
