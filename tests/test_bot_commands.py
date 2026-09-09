from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.service import StubAIService
from app.bot.app import BotDependencies
from app.bot.handlers import handle_command
from app.bot.service import BotRuntime
from app.config.settings import Settings
from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.expenses.service import ExpenseDraft, ExpenseService
from app.utils.dates import today_in_tz


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


def _draft(**overrides) -> ExpenseDraft:
    today = today_in_tz("UTC")
    base = dict(
        name="Café",
        amount=Decimal("10000"),
        currency="ARS",
        category="Café",
        expense_date=today,
        confidence=Decimal("0.9"),
    )
    base.update(overrides)
    return ExpenseDraft(**base)


@pytest.fixture
def runtime(in_memory_db):
    s = _settings()
    return BotRuntime(settings=s, ai=StubAIService())


def _seed(user_id: int = 1) -> dict:
    today = today_in_tz("UTC")
    with session_scope() as s:
        repo = ExpenseRepository(s)
        service = ExpenseService(repo)
        outcome = service.register_many(
            user_id=user_id,
            drafts=[
                _draft(name="Café", amount=Decimal("5000"), category="Café", expense_date=today),
                _draft(name="Uber", amount=Decimal("12000"), category="Uber", expense_date=today),
            ],
            original_message="seed",
        )
    return {e.name: e.id for e in outcome.saved}


def _make_update(user_id: int, message: MagicMock):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_message = message
    return update


@pytest.mark.asyncio
async def test_borrar_ultimo_deletes_last(runtime):
    ids = _seed()
    message = MagicMock()
    message.text = "/borrar_ultimo"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    await handle_command(update, None, deps)
    message.reply_text.assert_called_once()
    reply = message.reply_text.call_args[0][0]
    assert "Café" in reply or "Uber" in reply
    assert "borrado" in reply.lower()
    with session_scope() as s:
        repo = ExpenseRepository(s)
        # The remaining expense is the one not deleted.
        remaining = repo.list_recent(1, limit=10)
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_borrar_with_id(runtime):
    ids = _seed()
    message = MagicMock()
    message.text = f"/borrar {ids['Uber']}"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    await handle_command(update, None, deps)
    reply = message.reply_text.call_args[0][0]
    assert "Uber" in reply
    assert "borrado" in reply.lower()
    with session_scope() as s:
        repo = ExpenseRepository(s)
        assert repo.get_by_id(1, ids["Uber"]) is None
        assert repo.get_by_id(1, ids["Café"]) is not None


@pytest.mark.asyncio
async def test_borrar_invalid_id(runtime):
    _seed()
    message = MagicMock()
    message.text = "/borrar abc"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    await handle_command(update, None, deps)
    reply = message.reply_text.call_args[0][0]
    assert "número" in reply.lower() or "entero" in reply.lower()


@pytest.mark.asyncio
async def test_borrar_missing_id(runtime):
    _seed()
    message = MagicMock()
    message.text = "/borrar"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    await handle_command(update, None, deps)
    reply = message.reply_text.call_args[0][0]
    assert "ID" in reply or "id" in reply.lower()


@pytest.mark.asyncio
async def test_borrar_nonexistent_id(runtime):
    _seed()
    message = MagicMock()
    message.text = "/borrar 9999"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    await handle_command(update, None, deps)
    reply = message.reply_text.call_args[0][0]
    assert "9999" in reply or "no encontr" in reply.lower()


@pytest.mark.asyncio
async def test_borrar_ultimo_when_empty(runtime):
    message = MagicMock()
    message.text = "/borrar_ultimo"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    await handle_command(update, None, deps)
    reply = message.reply_text.call_args[0][0]
    assert "no tenés gastos" in reply.lower()


@pytest.mark.asyncio
async def test_editar_ultimo_amount(runtime):
    ids = _seed()
    message = MagicMock()
    message.text = "/editar_ultimo 7500"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    await handle_command(update, None, deps)
    reply = message.reply_text.call_args[0][0]
    assert "actualizado" in reply.lower()
    assert "7.500" in reply
    with session_scope() as s:
        repo = ExpenseRepository(s)
        latest = repo.get_latest(1)
    assert latest.amount == Decimal("7500.00")


@pytest.mark.asyncio
async def test_editar_with_id_and_amount(runtime):
    ids = _seed()
    message = MagicMock()
    message.text = f"/editar {ids['Café']} 15k"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    await handle_command(update, None, deps)
    reply = message.reply_text.call_args[0][0]
    assert "actualizado" in reply.lower()
    with session_scope() as s:
        repo = ExpenseRepository(s)
        row = repo.get_by_id(1, ids["Café"])
    assert row.amount == Decimal("15000.00")


@pytest.mark.asyncio
async def test_editar_ultimo_nombre(runtime):
    _seed()
    message = MagicMock()
    message.text = "/editar_ultimo nombre: Café con leche"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    await handle_command(update, None, deps)
    reply = message.reply_text.call_args[0][0]
    assert "actualizado" in reply.lower()
    with session_scope() as s:
        repo = ExpenseRepository(s)
        latest = repo.get_latest(1)
    assert latest.name == "Café con leche"


@pytest.mark.asyncio
async def test_editar_invalid_amount(runtime):
    _seed()
    message = MagicMock()
    message.text = "/editar_ultimo lucas"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    await handle_command(update, None, deps)
    reply = message.reply_text.call_args[0][0]
    # Could be "no pude interpretar" or fall through to numeric validation.
    assert "monto" in reply.lower() or "interpretar" in reply.lower()


@pytest.mark.asyncio
async def test_editar_missing_args(runtime):
    _seed()
    message = MagicMock()
    message.text = "/editar_ultimo"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    await handle_command(update, None, deps)
    reply = message.reply_text.call_args[0][0]
    assert "monto" in reply.lower() or "valor" in reply.lower()


@pytest.mark.asyncio
async def test_desglose_command_returns_categories(runtime):
    _seed()
    message = MagicMock()
    message.text = "/desglose"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    await handle_command(update, None, deps)
    reply = message.reply_text.call_args[0][0]
    assert "Desglose" in reply or "desglose" in reply.lower()
    assert "Café" in reply
    assert "Uber" in reply


@pytest.mark.asyncio
async def test_desglose_when_empty(runtime):
    message = MagicMock()
    message.text = "/desglose"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    await handle_command(update, None, deps)
    reply = message.reply_text.call_args[0][0]
    assert "no tenés" in reply.lower() or "no hay" in reply.lower()


@pytest.mark.asyncio
async def test_exportar_sends_csv_document(runtime):
    _seed()
    message = MagicMock()
    message.text = "/exportar"
    message.reply_text = AsyncMock()
    message.reply_document = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    # The export handler reads settings from update.application.bot_data.
    update.application.bot_data = {"settings": runtime.settings}
    deps = BotDependencies(runtime=runtime)
    await handle_command(update, None, deps)
    message.reply_document.assert_called_once()
    kwargs = message.reply_document.call_args.kwargs
    assert "caption" in kwargs
    assert "gastos" in kwargs["caption"].lower()
    document = kwargs["document"]
    # InputFile wraps bytes; check filename endswith .csv
    assert document.filename.endswith(".csv")
    payload = document.input_file_content
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    text = payload.decode("utf-8")
    assert "id,fecha,nombre,monto,moneda,categoria" in text
    assert "Café" in text
    assert "Uber" in text
