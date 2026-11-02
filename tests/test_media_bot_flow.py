"""Telegram photos only become expenses after an explicit scoped confirmation."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ai.service import StubAIService
from app.bot.app import BotDependencies
from app.bot.extensions import handle_extension_command, handle_photo, reply
from app.bot import pending
from app.bot.service import BotRuntime
from app.config.settings import Settings
from app.database.database import session_scope
from app.expenses.models import Expense
from app.media import ReceiptDraft, VoiceReply, VoiceUnavailable
from app.users import UserService


@pytest.fixture
def media_deps(in_memory_db, monkeypatch):
    settings = Settings(
        telegram_bot_token="test-token", telegram_allowed_user_id=123456,
        database_url="sqlite:///:memory:", timezone="America/Argentina/Buenos_Aires",
    )
    deps = BotDependencies(runtime=BotRuntime(settings, StubAIService()))
    with session_scope() as session:
        UserService(session, settings=settings).ensure_owner()
    monkeypatch.setattr(pending, "receipt_previews", pending.ReceiptPreviews())
    return deps


def _update(*, text="", user=123456, chat=42, message_id=71, photo=False, file_size=100):
    message = SimpleNamespace(
        text=text, caption="Compra de ejemplo", chat_id=chat, message_id=message_id,
        photo=[SimpleNamespace(file_id="photo-example", file_size=file_size)] if photo else [],
        reply_text=AsyncMock(), reply_voice=AsyncMock(),
    )
    return SimpleNamespace(effective_user=SimpleNamespace(id=user), effective_message=message)


def _context():
    file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"synthetic photo")))
    return SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=file)))


def _draft(amount=Decimal("3750.00")):
    return ReceiptDraft(name="SUPERMERCADO DEMO", amount=amount, currency="ARS", expense_date=date(2026, 11, 2))


def _count():
    with session_scope() as session:
        return session.query(Expense).count()


def _token(update):
    answer = update.effective_message.reply_text.call_args.args[0]
    return answer.split("/ticket_confirmar ", 1)[1].split()[0]


@pytest.mark.asyncio
async def test_photo_preview_does_not_write_then_confirmation_saves_once(media_deps, monkeypatch):
    from app.media.receipts import ReceiptService
    read = AsyncMock(return_value=_draft())
    monkeypatch.setattr(ReceiptService, "analyze_receipt", read)
    update, context = _update(photo=True), _context()
    await handle_photo(update, context, media_deps)
    assert _count() == 0
    assert read.await_count == 1
    assert update.effective_message.reply_text.call_args.kwargs["parse_mode"] is None
    token = _token(update)
    command = _update(text=f"/ticket_confirmar {token}")
    assert await handle_extension_command(command, context, media_deps) is True
    assert _count() == 1
    with session_scope() as session:
        expense = session.query(Expense).one()
        assert expense.amount == Decimal("3750.00")
        assert expense.source_key == "receipt:42:71"
        assert expense.source_type == "receipt"
        assert expense.telegram_user_id == 123456
    # Repeating a command or receiving a redelivery of the same photo
    # cannot create another expense with a new random preview token.
    await handle_extension_command(command, context, media_deps)
    assert _count() == 1
    again = _update(photo=True)
    await handle_photo(again, context, media_deps)
    await handle_extension_command(_update(text=f"/ticket_confirmar {_token(again)}"), context, media_deps)
    assert _count() == 1


@pytest.mark.asyncio
async def test_other_user_or_chat_cannot_confirm_anothers_preview(media_deps, monkeypatch):
    from app.media.receipts import ReceiptService
    monkeypatch.setattr(ReceiptService, "analyze_receipt", AsyncMock(return_value=_draft()))
    update, context = _update(photo=True), _context()
    await handle_photo(update, context, media_deps)
    token = _token(update)
    for user, chat in ((222222, 42), (123456, 999)):
        attempt = _update(text=f"/ticket_confirmar {token}", user=user, chat=chat)
        await handle_extension_command(attempt, context, media_deps)
        assert "borrador" in attempt.effective_message.reply_text.call_args.args[0].lower()
        assert _count() == 0
    await handle_extension_command(_update(text=f"/ticket_confirmar {token}"), context, media_deps)
    assert _count() == 1


@pytest.mark.asyncio
async def test_missing_total_needs_explicit_amount_override(media_deps, monkeypatch):
    from app.media.receipts import ReceiptService
    monkeypatch.setattr(ReceiptService, "analyze_receipt", AsyncMock(return_value=_draft(None)))
    update, context = _update(photo=True), _context()
    await handle_photo(update, context, media_deps)
    token = _token(update)
    missing = _update(text=f"/ticket_confirmar {token}")
    await handle_extension_command(missing, context, media_deps)
    assert _count() == 0
    assert "total" in missing.effective_message.reply_text.call_args.args[0].lower()
    invalid = _update(text=f"/ticket_confirmar {token} -10")
    await handle_extension_command(invalid, context, media_deps)
    assert _count() == 0
    corrected = _update(text=f"/ticket_confirmar {token} 4.000,25 USD")
    await handle_extension_command(corrected, context, media_deps)
    with session_scope() as session:
        row = session.query(Expense).one()
        assert row.amount == Decimal("4000.25")
        assert row.currency == "USD"


@pytest.mark.asyncio
async def test_cancelled_or_expired_preview_cannot_be_confirmed(media_deps, monkeypatch):
    from app.media.receipts import ReceiptService
    monkeypatch.setattr(ReceiptService, "analyze_receipt", AsyncMock(return_value=_draft()))
    clock = [0.0]
    monkeypatch.setattr(pending, "receipt_previews", pending.ReceiptPreviews(ttl=10, clock=lambda: clock[0]))
    context = _context()
    for expired in (False, True):
        update = _update(photo=True, message_id=71 + int(expired))
        await handle_photo(update, context, media_deps)
        token = _token(update)
        if expired:
            clock[0] = 11
        else:
            await handle_extension_command(_update(text=f"/ticket_cancelar {token}"), context, media_deps)
        await handle_extension_command(_update(text=f"/ticket_confirmar {token}"), context, media_deps)
        assert _count() == 0


@pytest.mark.asyncio
async def test_oversized_or_unauthorized_photo_never_runs_ocr(media_deps, monkeypatch):
    from app.media.receipts import ReceiptService
    read = AsyncMock(return_value=_draft())
    monkeypatch.setattr(ReceiptService, "analyze_receipt", read)
    context = _context()
    await handle_photo(_update(photo=True, file_size=9 * 1024 * 1024), context, media_deps)
    await handle_photo(_update(photo=True, user=999999), context, media_deps)
    read.assert_not_awaited()
    context.bot.get_file.assert_not_awaited()
    assert _count() == 0


@pytest.mark.asyncio
async def test_ocr_failure_is_friendly_and_creates_no_expense(media_deps, monkeypatch):
    from app.media.receipts import ReceiptService
    monkeypatch.setattr(ReceiptService, "analyze_receipt", AsyncMock(side_effect=ValueError("untrusted private OCR data")))
    update = _update(photo=True)
    await handle_photo(update, _context(), media_deps)
    answer = update.effective_message.reply_text.call_args.args[0]
    assert "No pude leer" in answer
    assert "private" not in answer
    assert _count() == 0


@pytest.mark.asyncio
async def test_voice_is_disabled_by_default_and_keeps_text_reply(media_deps, monkeypatch):
    from app.media.voice import LocalVoiceService
    speak = AsyncMock(return_value=VoiceReply(audio=b"OggS-test", language="es"))
    monkeypatch.setattr(LocalVoiceService, "synthesize", speak)
    update = _update()
    await reply(update, media_deps, "✅ Mensaje de ejemplo")
    update.effective_message.reply_text.assert_awaited_once()
    update.effective_message.reply_voice.assert_not_awaited()
    speak.assert_not_awaited()


@pytest.mark.asyncio
async def test_enabled_voice_uses_language_preference_and_local_speech(media_deps, monkeypatch):
    from app.media.voice import LocalVoiceService
    with session_scope() as session:
        UserService(session, settings=media_deps.runtime.settings).set_preferences(123456, voice_enabled=True, language="pt")
    speak = AsyncMock(return_value=VoiceReply(audio=b"OggS-local-opus", language="pt"))
    monkeypatch.setattr(LocalVoiceService, "synthesize", speak)
    update = _update()
    await reply(update, media_deps, "Disponible 100 ARS")
    assert speak.await_args.kwargs["language"] == "pt"
    update.effective_message.reply_text.assert_awaited_once()
    update.effective_message.reply_voice.assert_awaited_once()
    assert update.effective_message.reply_voice.call_args.kwargs["voice"].getvalue() == b"OggS-local-opus"


@pytest.mark.asyncio
async def test_voice_failure_falls_back_to_text_without_exposing_failure_details(media_deps, monkeypatch):
    from app.media.voice import LocalVoiceService
    with session_scope() as session:
        UserService(session, settings=media_deps.runtime.settings).set_preferences(123456, voice_enabled=True)
    monkeypatch.setattr(LocalVoiceService, "synthesize", AsyncMock(side_effect=VoiceUnavailable("private backend detail")))
    update = _update()
    await reply(update, media_deps, "Respuesta principal")
    answers = [call.args[0] for call in update.effective_message.reply_text.await_args_list]
    assert answers[0] == "Respuesta principal"
    assert len(answers) == 2
    assert all("private" not in answer for answer in answers)
    update.effective_message.reply_voice.assert_not_awaited()
