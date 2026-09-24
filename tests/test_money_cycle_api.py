"""API tests for the R1 cycle endpoints."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.database.database import session_scope
from app.main import app
from app.money import CycleRolloverRequired, CycleService, UserSettingsService


@pytest.fixture
def client(in_memory_db):
    return TestClient(app)


def _override_today(monkeypatch, fixed: str) -> None:
    """Patch the FastAPI ``today`` dependency to return ``fixed``."""
    from datetime import date as _date

    from app.web import routes as routes_module

    def _dep() -> _date:
        return _date.fromisoformat(fixed)

    app.dependency_overrides[routes_module._today] = _dep
    monkeypatch.setattr(app, "dependency_overrides", app.dependency_overrides)


@pytest.fixture(autouse=True)
def _reset_overrides():
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def test_api_cycle_current_returns_null_when_no_cycles(client):
    r = client.get("/api/cycle/current")
    assert r.status_code == 200
    assert r.json() == {"cycle": None}


def test_api_cycle_open_then_current(client, monkeypatch):
    _override_today(monkeypatch, "2026-10-15")
    r = client.post("/api/cycle/open")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cycle"]["cycle_index"] == 1
    assert body["cycle"]["start_date"] == "2026-10-01"
    assert body["cycle"]["end_date"] == "2026-10-31"
    assert body["cycle"]["payday_day"] == 1

    r2 = client.get("/api/cycle/current")
    assert r2.status_code == 200
    assert r2.json()["cycle"]["cycle_index"] == 1


def test_api_cycle_open_returns_409_on_rollover_required(client, monkeypatch):
    """Pre-seed history; opening again must signal rollover instead of writing."""
    _override_today(monkeypatch, "2026-07-01")
    client.post("/api/cycle/open")
    _override_today(monkeypatch, "2026-10-01")
    r = client.post("/api/cycle/open")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "cycle_rollover_required"
    assert detail["latest_cycle_index"] == 1


def test_api_cycle_current_returns_null_outside_window(client, monkeypatch):
    """Open in October, ask for it in November — read-only must NOT close it."""
    _override_today(monkeypatch, "2026-10-15")
    client.post("/api/cycle/open")
    _override_today(monkeypatch, "2026-11-15")
    r = client.get("/api/cycle/current")
    assert r.status_code == 200
    assert r.json() == {"cycle": None}
    # The October cycle is still open in the DB.
    statuses = []
    indices = []
    with session_scope() as s:
        from app.money.repository import FinancialCycleRepository
        rows = FinancialCycleRepository(s).list_cycles(
            user_id=Settings().telegram_allowed_user_id
        )
        statuses = [row.status for row in rows]
        indices = [row.cycle_index for row in rows]
    assert indices == [1]
    assert statuses == ["open"]
