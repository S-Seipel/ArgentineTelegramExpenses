"""End-to-end API contracts for reviewed receipts, undo, locations and metrics."""
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.bot import pending
from app.categories.models import Category
from app.config.settings import get_settings
from app.database.database import session_scope
from app.expenses.models import Expense, ExpenseChange
from app.expenses.repository import ExpenseRepository
from app.expenses.schemas import ExpenseCreate
from app.main import app
from app.media import ReceiptDraft
from app.metrics import Metrics
from app.users import UserService
from app.web import routes


USER = 123456


@pytest.fixture
def extras_client(in_memory_db, monkeypatch):
    import app.metrics as module
    with session_scope() as session:
        UserService(session, settings=get_settings()).ensure_owner()
    app.dependency_overrides[routes._today] = lambda: date(2026, 11, 2)
    monkeypatch.setattr(pending, "receipt_previews", pending.ReceiptPreviews())
    monkeypatch.setattr(module, "metrics", Metrics())
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _seed(user=USER, name="Synthetic expense", amount="100.25", source_type="telegram"):
    with session_scope() as session:
        category = session.execute(select(Category.id).where(Category.name == "Otros")).scalar_one()
        row = ExpenseRepository(session).create(ExpenseCreate(
            telegram_user_id=user, name=name, amount=Decimal(amount), currency="ARS",
            category_id=category, expense_date=date(2026, 11, 2), original_message="unit fixture",
            ai_confidence=Decimal("1"), source_type=source_type,
        ))
        return row.id


def _count():
    with session_scope() as session:
        return session.query(Expense).count()


def _preview(client, monkeypatch, *, amount=Decimal("1250.50"), data=b"synthetic receipt image"):
    from app.media.receipts import ReceiptService
    read = AsyncMock(return_value=ReceiptDraft(
        name="PRIVATE_MERCHANT_UNIT_EXAMPLE", amount=amount, currency="ARS",
        expense_date=date(2026, 10, 30), raw_text="PRIVATE_OCR_TEXT_UNIT_EXAMPLE",
    ))
    monkeypatch.setattr(ReceiptService, "analyze_receipt", read)
    response = client.post("/api/receipts/preview", content=data, headers={"Content-Type": "image/png"})
    assert response.status_code == 200, response.text
    return response.json(), read


def test_deleted_receipt_retry_does_not_claim_it_was_saved(extras_client, monkeypatch):
    preview, _ = _preview(extras_client, monkeypatch)
    payload = {'preview_id': preview['preview_id']}
    saved = extras_client.post('/api/receipts/confirm', json=payload).json()['id']
    with session_scope() as session:
        ExpenseRepository(session).delete(USER, saved)
    retry = extras_client.post('/api/receipts/confirm', json=payload)
    assert retry.status_code == 400
    assert '/undo' in retry.json()['detail']
    with session_scope() as session:
        assert session.get(Expense, saved).deleted_at is not None
        assert session.query(Expense).count() == 1


def test_receipt_preview_is_read_only_and_confirm_retries_are_idempotent(extras_client, monkeypatch):
    preview, read = _preview(extras_client, monkeypatch)
    assert _count() == 0
    with session_scope() as session:
        assert session.query(ExpenseChange).count() == 0
    assert preview["requires_confirmation"] is True
    assert preview["amount"] == "1250.50"
    assert "PRIVATE_OCR_TEXT" not in str(preview)
    assert read.await_args.kwargs["today"] == date(2026, 11, 2)
    request = {"preview_id": preview["preview_id"]}
    first = extras_client.post("/api/receipts/confirm", json=request)
    assert first.status_code == 200, first.text
    second = extras_client.post("/api/receipts/confirm", json=request)
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert _count() == 1
    # A retry of the same upload also keeps its original financial identity.
    redelivery, _ = _preview(extras_client, monkeypatch)
    third = extras_client.post("/api/receipts/confirm", json={"preview_id": redelivery["preview_id"]})
    assert third.status_code == 200, third.text
    assert third.json()["id"] == first.json()["id"]
    assert _count() == 1
    with session_scope() as session:
        row = session.get(Expense, first.json()["id"])
        assert row.amount == Decimal("1250.50")
        assert row.currency == "ARS"
        assert row.source_type == "receipt"
        assert row.telegram_user_id == USER


def test_receipt_total_and_date_can_be_corrected_before_saving(extras_client, monkeypatch):
    preview, _ = _preview(extras_client, monkeypatch, amount=None)
    missing = extras_client.post("/api/receipts/confirm", json={"preview_id": preview["preview_id"]})
    assert missing.status_code == 400
    assert _count() == 0
    corrected = extras_client.post("/api/receipts/confirm", json={
        "preview_id": preview["preview_id"], "amount": "42.75", "currency": "USD",
        "name": "Corrected receipt", "expense_date": "2026-10-29",
    })
    assert corrected.status_code == 200, corrected.text
    with session_scope() as session:
        row = session.query(Expense).one()
        assert row.amount == Decimal("42.75")
        assert row.currency == "USD"
        assert row.name == "Corrected receipt"
        assert row.expense_date == date(2026, 10, 29)


@pytest.mark.parametrize("payload", [
    {}, {"preview_id": 1}, {"preview_id": "missing", "unrecognized": "value"},
])
def test_invalid_receipt_confirmation_has_no_writes(extras_client, payload):
    assert extras_client.post("/api/receipts/confirm", json=payload).status_code == 400
    assert _count() == 0


def test_missing_or_foreign_receipt_preview_is_not_leaked(extras_client):
    foreign = pending.receipt_previews.put(USER + 1, None, ReceiptDraft(
        name="PRIVATE_OTHER_USER", amount=Decimal("999"), currency="USD", expense_date=None,
    ), "receipt:other")
    for key in ("does-not-exist", foreign):
        response = extras_client.post("/api/receipts/confirm", json={"preview_id": key})
        assert response.status_code == 404
        assert "PRIVATE_OTHER_USER" not in response.text
    assert _count() == 0


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "0", "1.001", True, "10000000000000000"])
def test_invalid_receipt_amount_does_not_consume_or_write_preview(extras_client, monkeypatch, amount):
    preview, _ = _preview(extras_client, monkeypatch)
    response = extras_client.post("/api/receipts/confirm", json={"preview_id": preview["preview_id"], "amount": amount})
    assert response.status_code == 400
    assert _count() == 0
    assert pending.receipt_previews.get(preview["preview_id"], USER, None) is not None


def test_receipt_upload_rejects_unsupported_type_or_oversized_body_before_ocr(extras_client, monkeypatch):
    from app.media.receipts import ReceiptService
    read = AsyncMock()
    monkeypatch.setattr(ReceiptService, "analyze_receipt", read)
    unsupported = extras_client.post("/api/receipts/preview", content=b"not a photo", headers={"Content-Type": "application/pdf"})
    assert unsupported.status_code == 400
    too_big = extras_client.post("/api/receipts/preview", content=b"x" * (8 * 1024 * 1024 + 1), headers={"Content-Type": "image/jpeg"})
    assert too_big.status_code == 413
    read.assert_not_awaited()
    assert _count() == 0


def test_undo_empty_and_soft_deleted_expense(extras_client):
    assert extras_client.post("/api/undo").json() == {"undone": False, "result": None}
    identifier = _seed()
    with session_scope() as session:
        ExpenseRepository(session).delete(USER, identifier)
    response = extras_client.post("/api/undo")
    assert response.status_code == 200, response.text
    assert response.json()["undone"] is True
    assert response.json()["result"]["expense_id"] == identifier
    assert response.json()["result"]["action"] == "delete"
    assert Decimal(response.json()["result"]["amount"]) == Decimal("100.25")
    with session_scope() as session:
        assert session.get(Expense, identifier).deleted_at is None
    assert extras_client.post("/api/undo").json()["undone"] is False


def test_undo_only_targets_the_current_user(extras_client):
    mine, other = _seed(), _seed(user=USER + 1)
    with session_scope() as session:
        repo = ExpenseRepository(session)
        repo.delete(USER, mine)
        repo.delete(USER + 1, other)
    response = extras_client.post("/api/undo")
    assert response.json()["result"]["expense_id"] == mine
    with session_scope() as session:
        assert session.get(Expense, other).deleted_at is not None


def test_undo_conflict_does_not_overwrite_later_external_change(extras_client):
    identifier = _seed()
    with session_scope() as session:
        ExpenseRepository(session).update_amount(USER, identifier, Decimal("200"))
    with session_scope() as session:
        row = session.get(Expense, identifier)
        row.amount = Decimal("300")
        row.revision += 1
    response = extras_client.post("/api/undo")
    assert response.status_code == 409
    with session_scope() as session:
        assert session.get(Expense, identifier).amount == Decimal("300")


def test_location_is_exact_scoped_removable_and_undoable(extras_client):
    identifier = _seed()
    payload = {"latitude": "-34.1234567", "longitude": "-58.7654321"}
    saved = extras_client.patch(f"/api/expenses/{identifier}/location", json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"id": identifier, **payload}
    rows = extras_client.get("/api/locations").json()["items"]
    assert len(rows) == 1 and rows[0]["id"] == identifier
    removed = extras_client.patch(f"/api/expenses/{identifier}/location", json={"latitude": None, "longitude": None})
    assert removed.status_code == 200
    assert extras_client.get("/api/locations").json()["items"] == []
    undone = extras_client.post("/api/undo")
    assert undone.status_code == 200
    assert undone.json()["result"]["expense_id"] == identifier
    assert extras_client.get("/api/locations").json()["items"][0]["latitude"] == payload["latitude"]


@pytest.mark.parametrize("payload", [
    {"latitude": 10}, {"latitude": 10, "longitude": None},
    {"latitude": "NaN", "longitude": "10"},
    {"latitude": "Infinity", "longitude": "10"},
    {"latitude": "90.0000001", "longitude": "10"},
    {"latitude": "10", "longitude": "-180.0000001"},
    {"latitude": True, "longitude": "10"},
    {"latitude": "10", "longitude": "20", "unrecognized": True},
])
def test_invalid_location_does_not_write_audit_or_coordinates(extras_client, payload):
    identifier = _seed()
    response = extras_client.patch(f"/api/expenses/{identifier}/location", json=payload)
    assert response.status_code == 400
    with session_scope() as session:
        assert session.get(Expense, identifier).latitude is None
        assert session.query(ExpenseChange).count() == 0


def test_location_rejects_foreign_and_system_owned_expenses(extras_client):
    foreign = _seed(user=USER + 1, name="PRIVATE_OTHER_EXPENSE")
    protected = _seed(source_type="recurring")
    payload = {"latitude": "10", "longitude": "20"}
    rejected = extras_client.patch(f"/api/expenses/{foreign}/location", json=payload)
    assert rejected.status_code == 404
    assert "PRIVATE_OTHER_EXPENSE" not in rejected.text
    assert extras_client.patch(f"/api/expenses/{protected}/location", json=payload).status_code == 409
    with session_scope() as session:
        assert session.query(ExpenseChange).count() == 0


def test_location_list_excludes_other_users_and_deleted_expenses(extras_client):
    mine, other, deleted = _seed(), _seed(user=USER + 1), _seed()
    with session_scope() as session:
        repo = ExpenseRepository(session)
        for user, identifier in ((USER, mine), (USER + 1, other), (USER, deleted)):
            repo.set_location(user, identifier, "10", "20")
        repo.delete(USER, deleted)
    assert [row["id"] for row in extras_client.get("/api/locations").json()["items"]] == [mine]


def test_metrics_exposes_route_templates_without_sensitive_content(extras_client, monkeypatch):
    preview, _ = _preview(extras_client, monkeypatch)
    identifier = _seed(name="PRIVATE_NAME_UNIT_MARKER")
    extras_client.patch(f"/api/expenses/{identifier}/location", json={"latitude": "-34.1234567", "longitude": "-58.7654321"})
    extras_client.get("/PRIVATE_UNKNOWN_ROUTE_UNIT_MARKER?text=PRIVATE_QUERY_UNIT_MARKER")
    response = extras_client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'route="/api/expenses/{expense_id}/location"' in response.text
    assert 'route="unmatched"' in response.text
    assert 'operation="receipt"' in response.text
    assert "# TYPE tge_operation_duration_seconds histogram" in response.text
    for private in ("PRIVATE", str(USER), preview["preview_id"], "1250.50", "-34.1234567", "-58.7654321"):
        assert private not in response.text
