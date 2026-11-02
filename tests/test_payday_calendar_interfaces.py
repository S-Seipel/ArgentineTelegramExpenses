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
        "payday_label": "día 1 de cada mes",
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
