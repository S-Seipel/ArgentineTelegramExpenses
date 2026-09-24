"""Tests for the ``/config_payday`` Telegram handler (R1)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.app import BotDependencies
from app.bot.handlers import handle_command
from app.bot.service import BotRuntime
from app.ai.service import StubAIService
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
    return BotRuntime(settings=_settings(), ai=StubAIService())


def _make_update(user_id: int, message: MagicMock):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_message = message
    return update


def _run(runtime, user_id: int, text: str) -> str:
    message = MagicMock()
    message.text = text
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(user_id, message)
    deps = BotDependencies(runtime=runtime)
    import asyncio

    asyncio.run(handle_command(update, None, deps))
    return message.reply_text.call_args[0][0]


def test_config_payday_with_valid_day(runtime):
    reply = _run(runtime, 1, "/config_payday 15")
    assert "día *15*" in reply
    from app.database.database import session_scope
    from app.money.repository import UserSettingsRepository
    with session_scope() as s:
        row = UserSettingsRepository(s).get(1)
    assert row is not None
    assert row.payday_day == 15
    assert row.timezone == "UTC"


def test_config_payday_without_args(runtime):
    reply = _run(runtime, 1, "/config_payday")
    assert "Decime el día" in reply


def test_config_payday_rejects_zero(runtime):
    reply = _run(runtime, 1, "/config_payday 0")
    assert "1 y 31" in reply


def test_config_payday_rejects_32(runtime):
    reply = _run(runtime, 1, "/config_payday 32")
    assert "1 y 31" in reply


def test_config_payday_rejects_non_numeric(runtime):
    reply = _run(runtime, 1, "/config_payday quince")
    assert "número entero" in reply


def test_config_payday_updates_existing(runtime):
    _run(runtime, 1, "/config_payday 5")
    _run(runtime, 1, "/config_payday 20")
    from app.database.database import session_scope
    from app.money.repository import UserSettingsRepository
    with session_scope() as s:
        row = UserSettingsRepository(s).get(1)
    assert row.payday_day == 20
