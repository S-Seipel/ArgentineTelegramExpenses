from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.service import StubAIService
from app.bot.app import BotDependencies
from app.bot.handlers import handle_command
from app.bot.service import BotRuntime
from app.config.settings import Settings


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


def _run(coro):
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def test_presupuesto_creates_budget(runtime):
    message = MagicMock()
    message.text = "/presupuesto comida 50000"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args[0][0]
    assert "Presupuesto creado" in reply
    assert "Comida" in reply or "Comida" in reply


def test_presupuesto_with_currency(runtime):
    message = MagicMock()
    message.text = "/presupuesto salidas 100 USD"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args[0][0]
    assert "creado" in reply.lower()
    assert "USD" in reply


def test_presupuesto_with_k_suffix(runtime):
    message = MagicMock()
    message.text = "/presupuesto comida 50k"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args[0][0]
    assert "creado" in reply.lower()
    # 50k → 50000 in service


def test_presupuesto_missing_args(runtime):
    message = MagicMock()
    message.text = "/presupuesto comida"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args[0][0]
    assert "Uso" in reply or "uso" in reply.lower()


def test_presupuesto_invalid_amount(runtime):
    message = MagicMock()
    message.text = "/presupuesto comida lucas"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args[0][0]
    assert "monto" in reply.lower() or "interpretar" in reply.lower()


def test_presupuestos_lists_existing(runtime):
    # Create two budgets first.
    for cmd in ["/presupuesto comida 50000", "/presupuesto transporte 10000"]:
        m = MagicMock()
        m.text = cmd
        m.reply_text = AsyncMock()
        m.chat.send_action = AsyncMock()
        _run(handle_command(_make_update(1, m), None, BotDependencies(runtime=runtime)))

    # List them.
    message = MagicMock()
    message.text = "/presupuestos"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args[0][0]
    assert "Comida" in reply
    assert "Transporte" in reply


def test_presupuestos_empty(runtime):
    message = MagicMock()
    message.text = "/presupuestos"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args[0][0]
    assert "no tenés" in reply.lower()


def test_presupuesto_del(runtime):
    # Seed
    m = MagicMock()
    m.text = "/presupuesto comida 50000"
    m.reply_text = AsyncMock()
    m.chat.send_action = AsyncMock()
    _run(handle_command(_make_update(1, m), None, BotDependencies(runtime=runtime)))

    # List to get the ID
    from app.database.database import session_scope
    from app.budgets.repository import BudgetRepository
    with session_scope() as s:
        bid = BudgetRepository(s).list_for_user(1)[0].id

    # Delete
    message = MagicMock()
    message.text = f"/presupuesto_del {bid}"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args[0][0]
    assert "eliminado" in reply.lower()


def test_presupuesto_off_then_on(runtime):
    m = MagicMock()
    m.text = "/presupuesto comida 50000"
    m.reply_text = AsyncMock()
    m.chat.send_action = AsyncMock()
    _run(handle_command(_make_update(1, m), None, BotDependencies(runtime=runtime)))

    from app.database.database import session_scope
    from app.budgets.repository import BudgetRepository
    with session_scope() as s:
        bid = BudgetRepository(s).list_for_user(1)[0].id

    message = MagicMock()
    message.text = f"/presupuesto_off {bid}"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    assert "pausado" in message.reply_text.call_args[0][0].lower()

    message2 = MagicMock()
    message2.text = f"/presupuesto_on {bid}"
    message2.reply_text = AsyncMock()
    message2.chat.send_action = AsyncMock()
    _run(handle_command(_make_update(1, message2), None, deps))
    assert "reanudado" in message2.reply_text.call_args[0][0].lower()
