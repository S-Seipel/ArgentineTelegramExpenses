from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.schemas import AIError, AIUnavailable
from app.ai.service import StubAIService
from app.bot.app import BotDependencies
from app.bot.handlers import handle_voice
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
        whisper_model_size="base",
        whisper_device="cpu",
        whisper_compute_type="int8",
    )


@pytest.fixture
def runtime(in_memory_db):
    s = _settings()
    return BotRuntime(settings=s, ai=StubAIService())


def _make_update(user_id: int, *, voice_id: str | None = None, audio_id: str | None = None, mime: str | None = None):
    message = MagicMock()
    message.text = None
    message.caption = None

    if voice_id is not None:
        voice = MagicMock()
        voice.file_id = voice_id
        voice.mime_type = mime or "audio/ogg"
        message.voice = voice
        message.audio = None
    elif audio_id is not None:
        audio = MagicMock()
        audio.file_id = audio_id
        audio.mime_type = mime or "audio/mpeg"
        message.voice = None
        message.audio = audio
    else:
        message.voice = None
        message.audio = None

    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_message = message
    return update, message


class _FakeFile:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def download_as_bytearray(self):
        return bytearray(self.payload)


class _FakeBot:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[str] = []

    async def get_file(self, file_id: str):
        self.calls.append(file_id)
        return _FakeFile(self.payload)


class _RecordingAI(StubAIService):
    def __init__(self, *, transcript: str = "", raise_exc: Exception | None = None):
        super().__init__()
        self._transcript = transcript
        self._raise = raise_exc
        self.received: list[bytes] = []

    async def transcribe(self, audio: bytes) -> str:
        self.received.append(audio)
        if self._raise is not None:
            raise self._raise
        return self._transcript


@pytest.mark.asyncio
async def test_handle_voice_downloads_and_runs_text_flow(runtime):
    payload = b"\x00\x01\x02audio"
    runtime.ai = StubAIService(
        scripted={
            "gasté diez lucas en café": {
                "type": "register_expense",
                "expenses": [
                    {
                        "name": "Café",
                        "amount": 10000,
                        "currency": "ARS",
                        "category": "Café",
                        "date": "2026-08-21",
                        "confidence": 0.9,
                        "needs_clarification": False,
                        "clarification_question": None,
                    }
                ],
                "query": None,
                "confidence": 0.9,
            }
        }
    )
    original_transcribe = _RecordingAI.transcribe
    runtime.ai.received = []

    async def fake_transcribe(audio):
        runtime.ai.received.append(audio)
        return "gasté diez lucas en café"

    runtime.ai.transcribe = fake_transcribe  # type: ignore[assignment]

    bot = _FakeBot(payload)
    update, message = _make_update(1, voice_id="file-1")
    update.effective_message = message
    context = MagicMock()
    context.bot = bot
    deps = BotDependencies(runtime=runtime)
    await handle_voice(update, context, deps)

    assert bot.calls == ["file-1"]
    assert runtime.ai.received == [payload]
    reply = message.reply_text.call_args[0][0]
    assert "gasté diez lucas en café" in reply
    assert "Gasto registrado" in reply or "Gastos registrados" in reply


@pytest.mark.asyncio
async def test_handle_voice_with_audio_attachment(runtime):
    payload = b"mp3-bytes"
    runtime.ai = StubAIService(
        scripted={
            "ayer gasté 20 dólares en Steam": {
                "type": "register_expense",
                "expenses": [
                    {
                        "name": "Steam",
                        "amount": 20,
                        "currency": "USD",
                        "category": "Juegos",
                        "date": "2026-08-20",
                        "confidence": 0.9,
                        "needs_clarification": False,
                        "clarification_question": None,
                    }
                ],
                "query": None,
                "confidence": 0.9,
            }
        }
    )
    runtime.ai.received = []

    async def fake_transcribe(audio):
        runtime.ai.received.append(audio)
        return "ayer gasté 20 dólares en Steam"

    runtime.ai.transcribe = fake_transcribe  # type: ignore[assignment]

    bot = _FakeBot(payload)
    update, message = _make_update(1, audio_id="file-audio")
    update.effective_message = message
    context = MagicMock()
    context.bot = bot
    deps = BotDependencies(runtime=runtime)
    await handle_voice(update, context, deps)

    assert bot.calls == ["file-audio"]
    reply = message.reply_text.call_args[0][0]
    assert "ayer gasté 20 dólares en Steam" in reply


@pytest.mark.asyncio
async def test_handle_voice_empty_transcript(runtime):
    runtime.ai = _RecordingAI(transcript="   ")
    bot = _FakeBot(b"x")
    update, message = _make_update(1, voice_id="file-2")
    context = MagicMock()
    context.bot = bot
    deps = BotDependencies(runtime=runtime)
    await handle_voice(update, context, deps)
    reply = message.reply_text.call_args[0][0]
    assert "no pude entender" in reply.lower()


@pytest.mark.asyncio
async def test_handle_voice_unavailable(runtime):
    runtime.ai = _RecordingAI(raise_exc=AIUnavailable("whisper down"))
    bot = _FakeBot(b"x")
    update, message = _make_update(1, voice_id="file-3")
    context = MagicMock()
    context.bot = bot
    deps = BotDependencies(runtime=runtime)
    await handle_voice(update, context, deps)
    reply = message.reply_text.call_args[0][0]
    assert "whisper" in reply.lower()
    assert "ffmpeg" in reply.lower() or "modelo" in reply.lower()


@pytest.mark.asyncio
async def test_handle_voice_ai_error(runtime):
    runtime.ai = _RecordingAI(raise_exc=AIError("modelo no encontrado"))
    bot = _FakeBot(b"x")
    update, message = _make_update(1, voice_id="file-4")
    context = MagicMock()
    context.bot = bot
    deps = BotDependencies(runtime=runtime)
    await handle_voice(update, context, deps)
    reply = message.reply_text.call_args[0][0]
    assert "modelo no encontrado" in reply


@pytest.mark.asyncio
async def test_handle_voice_no_audio_no_reply(runtime):
    update, message = _make_update(1)
    context = MagicMock()
    context.bot = _FakeBot(b"x")
    deps = BotDependencies(runtime=runtime)
    await handle_voice(update, context, deps)
    message.reply_text.assert_not_called()