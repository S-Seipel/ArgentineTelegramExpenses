from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.service import StubAIService
from app.bot.app import BotDependencies
from app.bot.handlers import handle_command, handle_text
from app.bot.service import BotRuntime
from app.config.settings import Settings
from app.database.database import session_scope
from app.recurring.repository import RecurringRepository


def _settings() -> Settings:
    return Settings(
        telegram_bot_token="t",
        telegram_allowed_user_id=1,
        database_url="sqlite:///:memory:",
        ollama_base_url="http://localhost:11434",
        ollama_model="test",
        timezone="UTC",
        max_message_length=2000,
    )


@pytest.fixture
def runtime(in_memory_db):
    s = _settings()
    return BotRuntime(settings=s, ai=StubAIService())


def _make_update(user_id: int, message: MagicMock):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_message = message
    return update


def test_recurrente_add_creates_monthly(runtime):
    message = MagicMock()
    message.text = "/recurrente_add Netflix 30000 15"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    asyncio_run(handle_command(update, None, deps))
    reply = message.reply_text.call_args[0][0]
    assert "Recurrente registrado" in reply
    assert "Netflix" in reply
    with session_scope() as s:
        repo = RecurringRepository(s)
        items = repo.list_for_user(1)
    assert len(items) == 1
    assert items[0].name == "Netflix"
    assert items[0].day_of_month == 15
    assert items[0].frequency == "monthly"


def test_recurrente_add_with_k(runtime):
    message = MagicMock()
    message.text = "/recurrente_add Spotify 15k 5"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    asyncio_run(handle_command(update, None, deps))
    reply = message.reply_text.call_args[0][0]
    assert "Recurrente registrado" in reply
    with session_scope() as s:
        repo = RecurringRepository(s)
        items = repo.list_for_user(1)
    assert items[0].amount == Decimal("15000.00")
    assert items[0].day_of_month == 5


def test_recurrente_add_anual(runtime):
    message = MagicMock()
    message.text = "/recurrente_add_anual Dominio 15000 3 15"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    asyncio_run(handle_command(update, None, deps))
    with session_scope() as s:
        repo = RecurringRepository(s)
        items = repo.list_for_user(1)
    assert items[0].frequency == "yearly"
    assert items[0].month_of_year == 3
    assert items[0].day_of_month == 15


def test_recurrente_add_missing_args(runtime):
    message = MagicMock()
    message.text = "/recurrente_add Netflix"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    asyncio_run(handle_command(update, None, deps))
    reply = message.reply_text.call_args[0][0]
    assert "Uso" in reply or "uso" in reply.lower()


def test_recurrente_add_invalid_day(runtime):
    message = MagicMock()
    message.text = "/recurrente_add Netflix 10000 xxx"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    asyncio_run(handle_command(update, None, deps))
    reply = message.reply_text.call_args[0][0]
    assert "número" in reply.lower() or "dia" in reply.lower() or "día" in reply.lower()


def test_recurrentes_list(runtime):
    # Seed two recurring templates via the command.
    for cmd in ["/recurrente_add Netflix 30000 15", "/recurrente_add Spotify 5000 5"]:
        message = MagicMock()
        message.text = cmd
        message.reply_text = AsyncMock()
        message.chat.send_action = AsyncMock()
        update = _make_update(1, message)
        deps = BotDependencies(runtime=runtime)
        asyncio_run(handle_command(update, None, deps))

    message = MagicMock()
    message.text = "/recurrentes"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    asyncio_run(handle_command(update, None, deps))
    reply = message.reply_text.call_args[0][0]
    assert "Netflix" in reply
    assert "Spotify" in reply


def test_recurrente_del(runtime):
    # Seed first
    seed_msg = MagicMock()
    seed_msg.text = "/recurrente_add Netflix 30000 15"
    seed_msg.reply_text = AsyncMock()
    seed_msg.chat.send_action = AsyncMock()
    asyncio_run(handle_command(_make_update(1, seed_msg), None, BotDependencies(runtime=runtime)))

    with session_scope() as s:
        rid = RecurringRepository(s).list_for_user(1)[0].id

    message = MagicMock()
    message.text = f"/recurrente_del {rid}"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    asyncio_run(handle_command(update, None, deps))
    reply = message.reply_text.call_args[0][0]
    assert "eliminado" in reply.lower() or "pausado" in reply.lower()

    with session_scope() as s:
        items = RecurringRepository(s).list_for_user(1)
    assert items == []


def test_recurrente_off_then_on(runtime):
    seed_msg = MagicMock()
    seed_msg.text = "/recurrente_add Netflix 30000 15"
    seed_msg.reply_text = AsyncMock()
    seed_msg.chat.send_action = AsyncMock()
    asyncio_run(handle_command(_make_update(1, seed_msg), None, BotDependencies(runtime=runtime)))

    with session_scope() as s:
        rid = RecurringRepository(s).list_for_user(1)[0].id

    message = MagicMock()
    message.text = f"/recurrente_off {rid}"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    asyncio_run(handle_command(update, None, deps))
    assert " pausado" in message.reply_text.call_args[0][0].lower()

    with session_scope() as s:
        items = RecurringRepository(s).list_for_user(1, include_inactive=True)
    assert items[0].is_active is False

    message2 = MagicMock()
    message2.text = f"/recurrente_on {rid}"
    message2.reply_text = AsyncMock()
    message2.chat.send_action = AsyncMock()
    asyncio_run(handle_command(_make_update(1, message2), None, deps))
    assert "reanudado" in message2.reply_text.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_handle_text_si_response_registers_expense(runtime):
    # Seed a recurring template due today.
    with session_scope() as s:
        service = __import__(
            "app.recurring.service", fromlist=["RecurringService"]
        ).RecurringService(RecurringRepository(s))
        from app.recurring.service import RecurringDraft
        template = service.create_from_draft(
            user_id=1,
            draft=RecurringDraft(
                name="Netflix",
                amount=Decimal("30000"),
                currency="ARS",
                category="Suscripciones",
                frequency="monthly",
                day_of_month=15,
                month_of_year=None,
                confidence=1.0,
            ),
            today=date(2026, 8, 15),
        )
        rid = template.id

    runtime.pending_reminders[1] = {
        "recurring_id": rid,
        "name": "Netflix",
        "amount": Decimal("30000"),
        "currency": "ARS",
    }

    message = MagicMock()
    message.text = "sí"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    await handle_text(update, None, deps)

    reply = message.reply_text.call_args[0][0]
    assert "Listo" in reply or "registrado" in reply.lower()
    assert runtime.pending_reminders == {}


@pytest.mark.asyncio
async def test_handle_text_no_response_skips(runtime):
    with session_scope() as s:
        service = __import__(
            "app.recurring.service", fromlist=["RecurringService"]
        ).RecurringService(RecurringRepository(s))
        from app.recurring.service import RecurringDraft
        template = service.create_from_draft(
            user_id=1,
            draft=RecurringDraft(
                name="Netflix",
                amount=Decimal("30000"),
                currency="ARS",
                category="Suscripciones",
                frequency="monthly",
                day_of_month=15,
                month_of_year=None,
                confidence=1.0,
            ),
            today=date(2026, 8, 15),
        )
        rid = template.id

    runtime.pending_reminders[1] = {
        "recurring_id": rid,
        "name": "Netflix",
        "amount": Decimal("30000"),
        "currency": "ARS",
    }

    message = MagicMock()
    message.text = "no"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    await handle_text(update, None, deps)

    reply = message.reply_text.call_args[0][0]
    assert "Saltado" in reply or "saltado" in reply.lower()


@pytest.mark.asyncio
async def test_handle_text_eliminar_response_deactivates(runtime):
    with session_scope() as s:
        service = __import__(
            "app.recurring.service", fromlist=["RecurringService"]
        ).RecurringService(RecurringRepository(s))
        from app.recurring.service import RecurringDraft
        template = service.create_from_draft(
            user_id=1,
            draft=RecurringDraft(
                name="Netflix",
                amount=Decimal("30000"),
                currency="ARS",
                category="Suscripciones",
                frequency="monthly",
                day_of_month=15,
                month_of_year=None,
                confidence=1.0,
            ),
            today=date(2026, 8, 15),
        )
        rid = template.id

    runtime.pending_reminders[1] = {
        "recurring_id": rid,
        "name": "Netflix",
        "amount": Decimal("30000"),
        "currency": "ARS",
    }

    message = MagicMock()
    message.text = "eliminar"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    await handle_text(update, None, deps)

    reply = message.reply_text.call_args[0][0]
    assert "eliminado" in reply.lower()
    with session_scope() as s:
        items = RecurringRepository(s).list_for_user(1)
    assert items == []


def asyncio_run(coro):
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)