from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.service import StubAIService
from app.bot.app import BotDependencies
from app.bot.handlers import handle_photo, handle_vision_callback
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
        vision_model="llava:7b",
        vision_min_confidence=0.5,
    )


@pytest.fixture
def runtime(in_memory_db):
    s = _settings()
    return BotRuntime(settings=s, ai=StubAIService())


class _FakeBot:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[str] = []

    async def get_file(self, file_id: str):
        self.calls.append(file_id)
        file = MagicMock()

        async def _download():
            return bytearray(self.payload)

        file.download_as_bytearray = _download
        return file


def _make_photo_update(user_id: int, payload: bytes, mime: str = "image/jpeg"):
    message = MagicMock()
    photo = [
        MagicMock(file_id="small", width=90, height=90),
        MagicMock(file_id="med", width=320, height=320),
        MagicMock(file_id="big", width=800, height=800),
    ]
    message.photo = photo
    message.audio = None
    message.voice = None
    message.reply_text = AsyncMock()
    message.edit_message_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_message = message
    return update, message


class _ScriptedAI(StubAIService):
    def __init__(self, response: dict):
        super().__init__()
        self._response = response

    async def transcribe_receipt(self, image):
        self.ocr_calls.append(image)
        return "TIGRE 19280.00 2026-09-08"

    async def parse_receipt_text(self, raw_text):
        self.parse_calls.append(raw_text)
        return self._response


@pytest.mark.asyncio
async def test_handle_photo_extracts_and_shows_preview(runtime):
    runtime.ai = _ScriptedAI(
        {
            "merchant": "Starbucks",
            "total": 4500,
            "currency": "ARS",
            "category": "Café",
            "date": "2026-09-09",
            "confidence": 0.85,
        }
    )
    bot = _FakeBot(b"\x89PNGfake")
    update, message = _make_photo_update(1, b"image")
    context = MagicMock()
    context.bot = bot
    deps = BotDependencies(runtime=runtime)
    await handle_photo(update, context, deps)

    assert bot.calls == ["big"]
    assert 1 in runtime.pending_visions
    pending = runtime.pending_visions[1]
    assert pending["name"] == "Starbucks"
    assert pending["amount"] == 4500
    reply = message.reply_text.call_args.kwargs
    assert "reply_markup" in reply
    text = message.reply_text.call_args.args[0]
    assert "Starbucks" in text
    assert "4.500" in text or "4500" in text


@pytest.mark.asyncio
async def test_handle_photo_missing_amount_asks_correction(runtime):
    runtime.ai = _ScriptedAI(
        {"merchant": "X", "total": None, "currency": "ARS"}
    )
    bot = _FakeBot(b"image")
    update, message = _make_photo_update(1, b"image")
    context = MagicMock()
    context.bot = bot
    deps = BotDependencies(runtime=runtime)
    await handle_photo(update, context, deps)
    reply = message.reply_text.call_args.args[0]
    assert "monto" in reply.lower()
    assert "raw_text" in runtime.pending_visions[1] or \
           runtime.pending_visions[1].get("state") == "editing_amount"


@pytest.mark.asyncio
async def test_handle_photo_ocr_text_too_short(runtime):
    runtime.ai = _ScriptedAI({})  # parse response doesn't matter

    class _StubOCR(StubAIService):
        async def transcribe_receipt(self, image):
            return ""

    runtime.ai = _StubOCR()
    bot = _FakeBot(b"image")
    update, message = _make_photo_update(1, b"image")
    context = MagicMock()
    context.bot = bot
    deps = BotDependencies(runtime=runtime)
    await handle_photo(update, context, deps)
    reply = message.reply_text.call_args.args[0]
    assert "no pude leer" in reply.lower()


@pytest.mark.asyncio
async def test_handle_photo_no_photo_ignored(runtime):
    message = MagicMock()
    message.photo = None
    message.reply_text = AsyncMock()
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_message = message
    context = MagicMock()
    context.bot = _FakeBot(b"x")
    deps = BotDependencies(runtime=runtime)
    await handle_photo(update, context, deps)
    message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_handle_vision_callback_confirm_registers_expense(runtime):
    # Seed pending extraction.
    runtime.pending_visions[1] = {
        "name": "Starbucks",
        "amount": 4500,
        "currency": "ARS",
        "category": "Café",
        "date": "2026-09-09",
        "confidence": 0.85,
        "state": "pending",
    }
    runtime.ai = StubAIService()

    query = MagicMock()
    query.data = "vision:confirm"
    query.from_user.id = 1
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock()
    update = MagicMock()
    update.callback_query = query
    context = MagicMock()
    deps = BotDependencies(runtime=runtime)
    await handle_vision_callback(update, context, deps)

    assert runtime.pending_visions == {}
    query.answer.assert_called_once()
    assert query.edit_message_text.called
    reply = query.edit_message_text.call_args.args[0]
    assert "registrado" in reply.lower() or "gasto" in reply.lower()


@pytest.mark.asyncio
async def test_handle_vision_callback_cancel(runtime):
    runtime.pending_visions[1] = {
        "name": "X", "amount": 100, "currency": "ARS",
        "category": "Otros", "date": None, "confidence": 0.9,
    }
    query = MagicMock()
    query.data = "vision:cancel"
    query.from_user.id = 1
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock()
    update = MagicMock()
    update.callback_query = query
    context = MagicMock()
    deps = BotDependencies(runtime=runtime)
    await handle_vision_callback(update, context, deps)
    assert runtime.pending_visions == {}
    assert "descartado" in query.edit_message_text.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_handle_vision_callback_edit_requeues(runtime):
    runtime.pending_visions[1] = {
        "name": "X", "amount": 100, "currency": "ARS",
        "category": "Otros", "date": None, "confidence": 0.9,
    }
    query = MagicMock()
    query.data = "vision:edit"
    query.from_user.id = 1
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock()
    update = MagicMock()
    update.callback_query = query
    context = MagicMock()
    deps = BotDependencies(runtime=runtime)
    await handle_vision_callback(update, context, deps)
    # Re-queued so the next text message can correct it.
    assert 1 in runtime.pending_visions


@pytest.mark.asyncio
async def test_handle_vision_callback_no_pending(runtime):
    query = MagicMock()
    query.data = "vision:confirm"
    query.from_user.id = 1
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    context = MagicMock()
    deps = BotDependencies(runtime=runtime)
    await handle_vision_callback(update, context, deps)
    reply = query.edit_message_text.call_args.args[0]
    assert "expir" in reply.lower()
