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
    return BotRuntime(settings=_settings(), ai=StubAIService())


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


def _seed():
    today = date(2026, 8, 21)
    with session_scope() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Starbucks Palermo",
                    amount=Decimal("4500"),
                    currency="ARS",
                    category="Café",
                    expense_date=today,
                    confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="Uber Centro",
                    amount=Decimal("3200"),
                    currency="ARS",
                    category="Transporte",
                    expense_date=today,
                    confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )


def test_buscar_with_match(runtime):
    _seed()
    message = MagicMock()
    message.text = "/buscar starbucks"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args.args[0]
    assert "Starbucks Palermo" in reply
    assert "Resultado" in reply or "Resultados" in reply
    assert "4500" in reply or "4.500" in reply


def test_buscar_no_match(runtime):
    _seed()
    message = MagicMock()
    message.text = "/buscar carrefour"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args.args[0]
    assert "no encontré" in reply.lower() or "no encontr" in reply.lower()


def test_buscar_missing_query(runtime):
    message = MagicMock()
    message.text = "/buscar"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args.args[0]
    assert "decime" in reply.lower() or "uso" in reply.lower() or "❓" in reply


def test_buscar_multi_word(runtime):
    _seed()
    message = MagicMock()
    message.text = "/buscar starbucks palermo"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args.args[0]
    assert "Starbucks Palermo" in reply
    assert "Starbucks Centro" not in reply.replace("Starbucks Palermo", "")


def test_buscar_empty_results_shown_cleanly(runtime):
    message = MagicMock()
    message.text = "/buscar xyzabc"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args.args[0]
    assert "🔍" in reply
