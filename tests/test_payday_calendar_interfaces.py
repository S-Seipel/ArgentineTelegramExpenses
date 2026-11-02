"""Payday interfaces preserve calendar history and never invent income."""
from datetime import date

from fastapi.testclient import TestClient
import pytest

from app.bot.handlers import _handle_config_payday_command, _handle_cycle_read_command
from app.config.settings import get_settings
from app.database.database import session_scope
from app.main import app
from app.money.models import CycleIncome, FinancialCycle, UserSettings
from app.web.routes import _today


TODAY = date(2026, 11, 6)
FIRST_FRIDAY = {"pay_rule": "first_weekday", "payday_weekday": 4}


@pytest.fixture
def client(in_memory_db):
    app.dependency_overrides[_today] = lambda: TODAY
    yield TestClient(app)
    app.dependency_overrides.clear()


def _assert_no_cycle_or_income():
    with session_scope() as session:
        assert session.query(FinancialCycle).count() == 0
        assert session.query(CycleIncome).count() == 0


def test_reading_default_calendar_is_read_only(client):
    response = client.get("/api/settings/payday")
    assert response.status_code == 200
    assert response.json()["payday"] == {
        "pay_rule": "fixed_day", "payday_day": 1,
        "payday_weekday": None, "payday_ordinal": None,
        "payday_label": "día 1 de cada mes", "currency": "ARS", "inherited": False,
    }
    with session_scope() as session:
        assert session.query(UserSettings).count() == 0
    _assert_no_cycle_or_income()


@pytest.mark.parametrize("command,weekday,label", [
    ("primer_viernes", 4, "primer viernes"),
    ("first_friday", 4, "primer viernes"),
    ("primer_lunes", 0, "primer lunes"),
])
def test_telegram_configuration_does_not_open_cycle(client, command, weekday, label):
    user = get_settings().telegram_allowed_user_id
    reply = _handle_config_payday_command([command], timezone="UTC", user_id=user)
    assert label in reply and "próximo ciclo" in reply
    payday = client.get("/api/settings/payday").json()["payday"]
    assert payday["pay_rule"] == "first_weekday"
    assert payday["payday_weekday"] == weekday
    assert payday["payday_ordinal"] == 1
    _assert_no_cycle_or_income()


def test_fixed_day_http_contract_and_switching_calendar_remain_compatible(client):
    assert client.patch("/api/settings/payday", json={"payday_day": 15}).status_code == 200
    first_friday = client.patch("/api/settings/payday", json=FIRST_FRIDAY)
    assert first_friday.status_code == 200
    assert first_friday.json()["payday"]["payday_day"] == 15
    assert first_friday.json()["payday"]["payday_label"] == "primer viernes de cada mes"
    fixed = client.patch("/api/settings/payday", json={"payday_day": 5})
    assert fixed.json()["payday"]["pay_rule"] == "fixed_day"
    assert fixed.json()["payday"]["payday_weekday"] is None
    assert fixed.json()["payday"]["payday_label"] == "día 5 de cada mes"
    _assert_no_cycle_or_income()


@pytest.mark.parametrize("payload", [
    {}, {"payday_day": True}, {"payday_day": 32},
    {"pay_rule": "first_weekday", "payday_weekday": True},
    {"pay_rule": "first_weekday", "payday_weekday": 7},
    {"pay_rule": "first_weekday", "payday_weekday": "4"},
    {**FIRST_FRIDAY, "payday_day": 1}, {**FIRST_FRIDAY, "ordinal": 2},
    {"pay_rule": "unknown"},
])
def test_invalid_calendar_never_writes_settings_or_opens_cycle(client, payload):
    assert client.patch("/api/settings/payday", json=payload).status_code == 400
    with session_scope() as session:
        assert session.query(UserSettings).count() == 0
    _assert_no_cycle_or_income()


def test_first_friday_cycle_has_real_dates_and_rule_in_api_and_telegram(client):
    user = get_settings().telegram_allowed_user_id
    client.patch("/api/settings/payday", json=FIRST_FRIDAY)
    opened = client.post("/api/cycle/open")
    assert opened.status_code == 200
    cycle = opened.json()["cycle"]
    assert cycle["start_date"] == "2026-11-06"
    assert cycle["end_date"] == "2026-12-03"
    assert cycle["pay_rule"] == "first_weekday"
    assert cycle["payday_weekday"] == 4 and cycle["payday_ordinal"] == 1
    assert cycle["payday_label"] == "primer viernes de cada mes"
    assert client.get("/api/cycle/current").json()["cycle"] == cycle
    summary = client.get(f"/api/cycle-summary?cycle_id={cycle['id']}").json()
    assert summary["payday_label"] == cycle["payday_label"]
    reply = _handle_cycle_read_command("/cycle", user_id=user, today=TODAY)
    assert "primer viernes de cada mes" in reply
    assert "2026-11-06 → 2026-12-03" in reply
    assert "día 1 de cada mes" not in reply
    with session_scope() as session:
        assert session.query(CycleIncome).count() == 0


def test_calendar_change_does_not_relabel_active_or_closed_cycle(client):
    opened = client.post("/api/cycle/open").json()["cycle"]
    client.patch("/api/settings/payday", json=FIRST_FRIDAY)
    assert client.get("/api/settings/payday").json()["payday"]["payday_label"] == "primer viernes de cada mes"
    current = client.get("/api/cycle/current").json()["cycle"]
    assert current["start_date"] == "2026-11-01" and current["end_date"] == "2026-11-30"
    assert current["payday_label"] == "día 1 de cada mes"
    client.post(f"/api/cycle/{opened['id']}/close?force=true")
    history = client.get("/api/cycle-history").json()["items"]
    assert history[0]["pay_rule"] == "fixed_day"
    assert history[0]["payday_label"] == "día 1 de cada mes"


def test_first_friday_history_keeps_calendar_after_settings_change(client):
    client.patch("/api/settings/payday", json=FIRST_FRIDAY)
    opened = client.post("/api/cycle/open").json()["cycle"]
    client.post(f"/api/cycle/{opened['id']}/close?force=true")
    client.patch("/api/settings/payday", json={"payday_day": 15})
    history = client.get("/api/cycle-history").json()["items"]
    assert history[0]["pay_rule"] == "first_weekday"
    assert history[0]["payday_weekday"] == 4
    assert history[0]["payday_label"] == "primer viernes de cada mes"
    assert history[0]["start_date"] == "2026-11-06"
    assert history[0]["end_date"] == "2026-12-03"


def test_telegram_currency_calendars_are_independent_without_creating_cycles(client):
    user = get_settings().telegram_allowed_user_id
    ars = _handle_config_payday_command(["primer_viernes", "ARS"], timezone="UTC", user_id=user)
    usd = _handle_config_payday_command(["15", "usd"], timezone="UTC", user_id=user)
    assert "ARS" in ars and "USD" in usd
    assert client.get("/api/settings/payday").json()["payday"]["payday_weekday"] == 4
    usd_settings = client.get("/api/settings/payday?currency=USD").json()["payday"]
    assert usd_settings["currency"] == "USD"
    assert usd_settings["payday_day"] == 15 and usd_settings["pay_rule"] == "fixed_day"
    _assert_no_cycle_or_income()


@pytest.mark.parametrize("args", [["primer_viernes", "USDD"], ["15", "12"], ["15", "USD", "extra"]])
def test_telegram_rejects_invalid_currency_without_writing(client, args):
    user = get_settings().telegram_allowed_user_id
    reply = _handle_config_payday_command(args, timezone="UTC", user_id=user)
    assert "❓" in reply
    with session_scope() as session:
        assert session.query(UserSettings).count() == 0
    _assert_no_cycle_or_income()


@pytest.mark.parametrize("endpoint,method,payload", [
    ("/api/settings/payday?currency=USDD", "get", None),
    ("/api/cycle/current?currency=USDD", "get", None),
    ("/api/cycle/ensure?currency=USDD", "post", None),
    ("/api/cycle/open?currency=USDD", "post", None),
    ("/api/settings/payday", "patch", {**FIRST_FRIDAY, "currency": "USDD"}),
    ("/api/cycles/income", "post", {"amount": "100", "currency": "USDD"}),
])
def test_invalid_api_currency_does_not_create_financial_history(client, endpoint, method, payload):
    response = client.request(method, endpoint, json=payload)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "invalid_currency"
    _assert_no_cycle_or_income()


def test_api_settings_query_currency_and_body_agree_without_writing_cycle(client):
    result = client.patch("/api/settings/payday?currency=USD", json={"payday_day": 15})
    assert result.status_code == 200
    assert result.json()["payday"]["currency"] == "USD"
    assert client.get("/api/settings/payday").json()["payday"]["payday_day"] == 1
    conflict = client.patch("/api/settings/payday?currency=USD", json={"payday_day": 10, "currency": "ARS"})
    assert conflict.status_code == 400
    assert client.get("/api/settings/payday?currency=USD").json()["payday"]["payday_day"] == 15
    _assert_no_cycle_or_income()


@pytest.mark.parametrize("command", ["/cycle", "/disponible", "/liberado", "/gastar_diario"])
def test_telegram_cycle_read_rejects_invalid_currency_before_open(client, command):
    reply = _handle_cycle_read_command(command, user_id=get_settings().telegram_allowed_user_id, today=TODAY, args=["USDD"])
    assert "❓" in reply
    _assert_no_cycle_or_income()


def test_archived_monthly_income_is_read_only_and_explicitly_legacy(client):
    from decimal import Decimal
    from app.fixed_expenses.models import MonthlyBudget

    with session_scope() as session:
        session.add(MonthlyBudget(
            telegram_user_id=get_settings().telegram_allowed_user_id,
            month_year="2025-09", income=Decimal("123"), extra=Decimal("4"),
        ))
    result = client.get("/api/month-summary?month_year=2025-09")
    assert result.status_code == 200
    archived = result.json()
    assert archived["month_year"] == "2025-09"
    assert archived["income"] == 123 and archived["extra"] == 4
    assert archived["source"] == "monthly_budget_legacy"
    assert archived["read_only"] is True and archived["currency"] == "ARS"
    _assert_no_cycle_or_income()


@pytest.mark.parametrize("month", ["2026-00", "2026-13", "0000-09", "2026-9", "unknown"])
def test_invalid_archive_month_is_a_client_error_without_financial_writes(client, month):
    assert client.get("/api/month-summary", params={"month_year": month}).status_code == 400
    _assert_no_cycle_or_income()


@pytest.mark.parametrize("endpoint,payload", [
    ("income", {"amount": "-1"}),
    ("income", {"amount": "NaN"}),
    ("income", {"amount": "Infinity"}),
    ("income", {}),
    ("income", {"amount": "100", "cycle_id": "bad"}),
    ("extra", {"amount": "10"}),
    ("extra", {"amount": "0", "source_key": "bad-zero"}),
    ("savings/reserved", {"amount": "-1"}),
    ("savings/release", {"amount": "0"}),
    ("savings/realized", {"amount": "10"}),
    ("savings/target", {}),
    ("savings/target", {"target_pct": ".2", "target_amount": "10"}),
    ("savings/target", {"target_pct": "1.1"}),
    ("savings/target", {"target_amount": "-1"}),
])
def test_invalid_money_request_does_not_open_cycle_or_create_income(client, endpoint, payload):
    response = client.post("/api/cycles/" + endpoint, json={**payload, "currency": "USD"})
    assert response.status_code == 400, response.text
    _assert_no_cycle_or_income()
