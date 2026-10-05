"""F1 R3 Web API tests for the income / savings mutation endpoints.

The endpoints are tested as a unit (status codes + payload shape) and
as an integration (full request → response). Each endpoint is wired
to :class:`MoneyStateService`; the assertions here focus on the HTTP
contract:

- 200 with the entity + updated summary on success
- 400 on validation errors (negative amount, missing source_key, XOR)
- 404 on cycle-not-found and entity-not-found (cross-user invisible)
- 409 on closed-cycle rejection

The service-level semantics (UPSERT, soft-delete / restore,
committed formula) are covered by
:mod:`tests.test_money_state_service` and the engine read tests; the
HTTP layer just maps the same exceptions to status codes.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config.settings import get_settings
from app.database.database import session_scope
from app.main import app
from app.money import (
    CycleService,
    MoneyEngine,
    UserSettingsService,
)


@pytest.fixture
def api_client(in_memory_db):
    """FastAPI TestClient against the in-memory SQLite DB."""
    return TestClient(app)


def _open_cycle(user_id: int, *, payday_day: int = 1, today: date):
    with session_scope() as s:
        UserSettingsService(s).set_payday_day(
            user_id=user_id, payday_day=payday_day
        )
        view = CycleService(s).open_initial_cycle(
            user_id=user_id, today=today
        )
    return view


def _auth_user_id() -> int:
    return get_settings().telegram_allowed_user_id


def _post(client: TestClient, path: str, payload: dict):
    return client.post(path, json=payload)


@pytest.mark.parametrize(
    "path", ["/api/cycles/extra", "/api/cycles/savings/realized"]
)
def test_api_source_key_rejects_currency_change(api_client, in_memory_db, path):
    user_id = _auth_user_id()
    view = _open_cycle(user_id, payday_day=2, today=date(2026, 10, 5))
    payload = {
        "cycle_id": view.cycle_id, "currency": "ARS",
        "amount": "50", "source_key": "X",
    }
    assert _post(api_client, path, payload).status_code == 200
    conflict = _post(api_client, path, {**payload, "currency": "USD"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "source_currency_conflict"
    assert "X" not in conflict.json()["detail"]["message"]
    with session_scope() as s:
        from app.money.models import CycleIncome, SavingsRealization

        model = CycleIncome if path.endswith("extra") else SavingsRealization
        rows = s.query(model).filter_by(cycle_id=view.cycle_id, source_key="X").all()
        assert len(rows) == 1
        assert rows[0].currency == "ARS"


# ---------------------------------------------------------------------------
# /api/cycles/income
# ---------------------------------------------------------------------------


def test_api_set_income_creates(api_client, in_memory_db):
    user_id = _auth_user_id()
    today = date(2026, 10, 5)
    view = _open_cycle(user_id, payday_day=2, today=today)
    response = _post(
        api_client,
        "/api/cycles/income",
        {
            "cycle_id": view.cycle_id,
            "currency": "ARS",
            "amount": "1100000.00",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["income"]["amount"] == "1100000.00"
    assert body["income"]["kind"] == "income"
    assert body["summary"]["money"]["income"] == {"ARS": "1100000.00"}


def test_api_set_income_rejects_negative(api_client, in_memory_db):
    user_id = _auth_user_id()
    today = date(2026, 10, 5)
    view = _open_cycle(user_id, payday_day=2, today=today)
    response = _post(
        api_client,
        "/api/cycles/income",
        {
            "cycle_id": view.cycle_id,
            "currency": "ARS",
            "amount": "-1",
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "invalid_argument"


def test_api_set_income_cycle_not_found(api_client, in_memory_db):
    response = _post(
        api_client,
        "/api/cycles/income",
        {"cycle_id": 999, "currency": "ARS", "amount": "100"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "cycle_not_found"


def test_api_set_income_closed_cycle_rejected(api_client, in_memory_db):
    user_id = _auth_user_id()
    today = date(2026, 10, 5)
    view = _open_cycle(user_id, payday_day=2, today=today)
    # Close the cycle.
    with session_scope() as s:
        CycleService(s).close_cycle(
            user_id=user_id, cycle_id=view.cycle_id,
            force=True, closed_by="test",
        )
    response = _post(
        api_client,
        "/api/cycles/income",
        {"cycle_id": view.cycle_id, "currency": "ARS", "amount": "100"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "cycle_closed"


# ---------------------------------------------------------------------------
# /api/cycles/extra
# ---------------------------------------------------------------------------


def test_api_record_extra(api_client, in_memory_db):
    user_id = _auth_user_id()
    today = date(2026, 10, 5)
    view = _open_cycle(user_id, payday_day=2, today=today)
    response = _post(
        api_client,
        "/api/cycles/extra",
        {
            "cycle_id": view.cycle_id,
            "currency": "ARS",
            "amount": "50000",
            "source_key": "k1",
            "note": "regalo",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["extra"]["amount"] == "50000.00"
    assert body["extra"]["source_key"] == "k1"


def test_api_record_extra_same_source_returns_same_id(api_client, in_memory_db):
    user_id = _auth_user_id()
    today = date(2026, 10, 5)
    view = _open_cycle(user_id, payday_day=2, today=today)
    first = _post(
        api_client,
        "/api/cycles/extra",
        {
            "cycle_id": view.cycle_id, "currency": "ARS",
            "amount": "50", "source_key": "X",
        },
    )
    second = _post(
        api_client,
        "/api/cycles/extra",
        {
            "cycle_id": view.cycle_id, "currency": "ARS",
            "amount": "70", "source_key": "X",
        },
    )
    assert first.json()["extra"]["id"] == second.json()["extra"]["id"]
    assert second.json()["extra"]["amount"] == "70.00"


def test_api_record_extra_rejects_missing_source(api_client, in_memory_db):
    user_id = _auth_user_id()
    today = date(2026, 10, 5)
    view = _open_cycle(user_id, payday_day=2, today=today)
    response = _post(
        api_client,
        "/api/cycles/extra",
        {
            "cycle_id": view.cycle_id, "currency": "ARS",
            "amount": "50", "source_key": "",
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "invalid_argument"


def test_api_update_extra(api_client, in_memory_db):
    user_id = _auth_user_id()
    today = date(2026, 10, 5)
    view = _open_cycle(user_id, payday_day=2, today=today)
    created = _post(
        api_client,
        "/api/cycles/extra",
        {
            "cycle_id": view.cycle_id, "currency": "ARS",
            "amount": "50", "source_key": "X",
        },
    )
    extra_id = created.json()["extra"]["id"]
    response = api_client.patch(
        f"/api/cycles/extra/{extra_id}",
        json={"amount": "75"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["extra"]["amount"] == "75.00"


def test_api_soft_delete_extra(api_client, in_memory_db):
    user_id = _auth_user_id()
    today = date(2026, 10, 5)
    view = _open_cycle(user_id, payday_day=2, today=today)
    created = _post(
        api_client,
        "/api/cycles/extra",
        {
            "cycle_id": view.cycle_id, "currency": "ARS",
            "amount": "50", "source_key": "X",
        },
    )
    extra_id = created.json()["extra"]["id"]
    response = api_client.delete(f"/api/cycles/extra/{extra_id}")
    assert response.status_code == 200, response.text
    assert response.json()["extra"]["deleted_at"] is not None


# ---------------------------------------------------------------------------
# /api/cycles/savings/target
# ---------------------------------------------------------------------------


def test_api_set_target_pct(api_client, in_memory_db):
    user_id = _auth_user_id()
    today = date(2026, 10, 5)
    view = _open_cycle(user_id, payday_day=2, today=today)
    response = _post(
        api_client,
        "/api/cycles/savings/target",
        {
            "cycle_id": view.cycle_id,
            "currency": "ARS",
            "target_pct": "0.20",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["target"]["target_pct"] == "0.2000"


def test_api_set_target_both_rejected(api_client, in_memory_db):
    user_id = _auth_user_id()
    today = date(2026, 10, 5)
    view = _open_cycle(user_id, payday_day=2, today=today)
    response = _post(
        api_client,
        "/api/cycles/savings/target",
        {
            "cycle_id": view.cycle_id, "currency": "ARS",
            "target_pct": "0.20", "target_amount": "100",
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "invalid_argument"


# ---------------------------------------------------------------------------
# /api/cycles/savings/reserved + release
# ---------------------------------------------------------------------------


def test_api_set_reserved(api_client, in_memory_db):
    user_id = _auth_user_id()
    today = date(2026, 10, 5)
    view = _open_cycle(user_id, payday_day=2, today=today)
    response = _post(
        api_client,
        "/api/cycles/savings/reserved",
        {
            "cycle_id": view.cycle_id, "currency": "ARS",
            "amount": "220000",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["reserved"]["amount"] == "220000.00"


def test_api_release_savings(api_client, in_memory_db):
    user_id = _auth_user_id()
    today = date(2026, 10, 5)
    view = _open_cycle(user_id, payday_day=2, today=today)
    _post(
        api_client,
        "/api/cycles/savings/reserved",
        {"cycle_id": view.cycle_id, "currency": "ARS", "amount": "220"},
    )
    response = _post(
        api_client,
        "/api/cycles/savings/release",
        {"cycle_id": view.cycle_id, "currency": "ARS", "amount": "100"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["reserved"]["amount"] == "120.00"


# ---------------------------------------------------------------------------
# /api/cycles/savings/realized
# ---------------------------------------------------------------------------


def test_api_record_realized(api_client, in_memory_db):
    user_id = _auth_user_id()
    today = date(2026, 10, 5)
    view = _open_cycle(user_id, payday_day=2, today=today)
    response = _post(
        api_client,
        "/api/cycles/savings/realized",
        {
            "cycle_id": view.cycle_id, "currency": "ARS",
            "amount": "150000", "source_key": "R1",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["realization"]["amount"] == "150000.00"


def test_api_update_realized(api_client, in_memory_db):
    user_id = _auth_user_id()
    today = date(2026, 10, 5)
    view = _open_cycle(user_id, payday_day=2, today=today)
    created = _post(
        api_client,
        "/api/cycles/savings/realized",
        {
            "cycle_id": view.cycle_id, "currency": "ARS",
            "amount": "50000", "source_key": "R1",
        },
    )
    rid = created.json()["realization"]["id"]
    response = api_client.patch(
        f"/api/cycles/savings/realized/{rid}",
        json={"amount": "75000"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["realization"]["amount"] == "75000.00"


def test_api_soft_delete_realized(api_client, in_memory_db):
    user_id = _auth_user_id()
    today = date(2026, 10, 5)
    view = _open_cycle(user_id, payday_day=2, today=today)
    created = _post(
        api_client,
        "/api/cycles/savings/realized",
        {
            "cycle_id": view.cycle_id, "currency": "ARS",
            "amount": "50000", "source_key": "R1",
        },
    )
    rid = created.json()["realization"]["id"]
    response = api_client.delete(f"/api/cycles/savings/realized/{rid}")
    assert response.status_code == 200, response.text
    assert response.json()["realization"]["deleted_at"] is not None
