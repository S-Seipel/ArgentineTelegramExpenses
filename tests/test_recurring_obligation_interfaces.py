"""F1.5 opt-in and reminder replies through the public interfaces."""
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.ai.service import StubAIService
from app.bot.app import BotDependencies
from app.bot.handlers import _maybe_handle_reminder_reply, handle_command
from app.bot.service import BotRuntime
from app.config.settings import get_settings
from app.database.database import session_scope
from app.main import app
from app.money import CycleService, MoneyEngine, MoneyStateService
from app.recurring.models import RecurringExpense, RecurringReminder
from app.recurring.repository import RecurringRepository
from app.recurring.service import RecurringDraft, RecurringService
from app.web.routes import _today


TODAY = date(2026, 10, 31)


@pytest.fixture
def context(in_memory_db):
    settings = get_settings()
    user = settings.telegram_allowed_user_id
    runtime = BotRuntime(settings=settings, ai=StubAIService())
    with session_scope() as session:
        service = RecurringService(RecurringRepository(session))
        item = service.create_from_draft(user, RecurringDraft(
            name="Internet", amount=Decimal("100"), currency="ARS",
            category="Otros", frequency="monthly", day_of_month=31,
            month_of_year=None, confidence=1.0,
        ), today=TODAY)
        recurring_id = item.id
        cycle_id = CycleService(session).open_initial_cycle(user, TODAY).cycle_id
        MoneyStateService(session).set_income(user, cycle_id, "ARS", Decimal("1000"))
    app.dependency_overrides[_today] = lambda: TODAY
    yield user, recurring_id, cycle_id, BotDependencies(runtime=runtime)
    app.dependency_overrides.clear()


def _summary(user, cycle_id):
    with session_scope() as session:
        return MoneyEngine(session).get_cycle_summary(user, cycle_id, today=TODAY)


def test_api_opt_in_changes_available_and_exposes_canonical_due_date(context):
    user, recurring_id, cycle_id, _ = context
    client = TestClient(app)
    before = client.get("/api/recurring").json()["items"][0]
    assert before["is_obligation"] is False
    assert _summary(user, cycle_id).available_to_spend.get("ARS") == Decimal("1000")
    response = client.patch(f"/api/recurring/{recurring_id}/obligation", json={"is_obligation": True})
    assert response.status_code == 200
    assert response.json()["item"]["is_obligation"] is True
    assert response.json()["item"]["obligation_due_date"] == "2026-10-31"
    assert _summary(user, cycle_id).available_to_spend.get("ARS") == Decimal("900")
    client.patch(f"/api/recurring/{recurring_id}/obligation", json={"is_obligation": False})
    assert _summary(user, cycle_id).available_to_spend.get("ARS") == Decimal("1000")


@pytest.mark.parametrize("payload", [
    {}, {"is_obligation": "false"}, {"is_obligation": 1},
    {"is_obligation": True, "currency": "USD"},
])
def test_api_rejects_ambiguous_or_extra_obligation_inputs(context, payload):
    _, recurring_id, _, _ = context
    response = TestClient(app).patch(f"/api/recurring/{recurring_id}/obligation", json=payload)
    assert response.status_code == 400
    with session_scope() as session:
        assert session.get(RecurringExpense, recurring_id).is_obligation is False


def test_api_cannot_change_another_users_recurring(context):
    user, recurring_id, _, _ = context
    with session_scope() as session:
        session.get(RecurringExpense, recurring_id).telegram_user_id = user + 1
    client = TestClient(app)
    assert client.get("/api/recurring").json()["items"] == []
    response = client.patch(f"/api/recurring/{recurring_id}/obligation", json={"is_obligation": True})
    assert response.status_code == 404
    with session_scope() as session:
        assert session.get(RecurringExpense, recurring_id).is_obligation is False


@pytest.mark.asyncio
async def test_telegram_opt_in_and_listing(context):
    user, recurring_id, cycle_id, deps = context
    message = MagicMock()
    message.chat.send_action = AsyncMock()
    message.reply_text = AsyncMock()
    update = MagicMock()
    update.effective_user.id = user
    update.effective_message = message
    message.text = f"/recurrente_obligacion {recurring_id} on"
    await handle_command(update, None, deps)
    assert "descuenta" in message.reply_text.call_args.args[0]
    assert _summary(user, cycle_id).pending_recurring_obligations.get("ARS") == Decimal("100")
    message.text = "/recurrentes"
    await handle_command(update, None, deps)
    assert "obligación" in message.reply_text.call_args.args[0]
    message.text = f"/recurrente_obligacion {recurring_id} off"
    await handle_command(update, None, deps)
    assert "no reserva dinero" in message.reply_text.call_args.args[0]


def _prepare_reminder(context):
    user, recurring_id, _, _ = context
    with session_scope() as session:
        repo = RecurringRepository(session)
        RecurringService(repo).set_obligation(user, recurring_id, True)
        reminder = repo.get_or_create_reminder(user, recurring_id, TODAY)
        reminder.telegram_message_id = 777
        reminder.delivery_status = "sent"
        reminder_id = reminder.id
    return reminder_id


@pytest.mark.asyncio
async def test_snooze_at_cycle_boundary_keeps_same_pending_reminder_and_reservation(context):
    user, recurring_id, cycle_id, deps = context
    reminder_id = _prepare_reminder(context)
    reply = await _maybe_handle_reminder_reply(deps, user, "posponer", TODAY, reply_to_message_id=777)
    assert "mañana" in reply
    with session_scope() as session:
        item = session.get(RecurringExpense, recurring_id)
        reminder = session.get(RecurringReminder, reminder_id)
        assert item.next_due_date == date(2026, 11, 1)
        assert item.obligation_due_date == TODAY
        assert reminder.occurrence_date == TODAY
        assert reminder.status == "pending"
        assert reminder.delivery_status == "pending"
        assert reminder.telegram_message_id is None
        assert session.query(RecurringReminder).count() == 1
    assert _summary(user, cycle_id).pending_recurring_obligations.get("ARS") == Decimal("100")


@pytest.mark.asyncio
async def test_skipping_obligation_releases_its_reservation(context):
    user, recurring_id, cycle_id, deps = context
    reminder_id = _prepare_reminder(context)
    reply = await _maybe_handle_reminder_reply(deps, user, "no", TODAY, reply_to_message_id=777)
    assert "Saltado" in reply
    with session_scope() as session:
        assert session.get(RecurringReminder, reminder_id).status == "resolved"
        assert session.get(RecurringExpense, recurring_id).obligation_due_date == date(2026, 11, 30)
    assert _summary(user, cycle_id).pending_recurring_obligations.get("ARS") == Decimal("0")
    assert _summary(user, cycle_id).available_to_spend.get("ARS") == Decimal("1000")


@pytest.mark.asyncio
async def test_confirming_obligation_swaps_reservation_for_one_real_expense(context):
    user, recurring_id, cycle_id, deps = context
    _prepare_reminder(context)
    reply = await _maybe_handle_reminder_reply(deps, user, "sí", TODAY, reply_to_message_id=777)
    assert "registrado" in reply
    summary = _summary(user, cycle_id)
    assert summary.pending_recurring_obligations.get("ARS") == Decimal("0")
    assert summary.realized_expenses.get("ARS") == Decimal("100")
    assert summary.available_to_spend.get("ARS") == Decimal("900")
