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
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.service import FixedExpenseService


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


def test_gastofijo_add(runtime):
    message = MagicMock()
    message.text = "/gastofijo_add CASA 90000 10 transferencia"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args.args[0]
    assert "CASA" in reply
    assert "90.000" in reply or "90000" in reply
    assert "transferencia" in reply.lower() or "TRANSFERENCIA" in reply


def test_gastofijo_add_missing_args(runtime):
    message = MagicMock()
    message.text = "/gastofijo_add CASA"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args.args[0]
    assert "Uso" in reply or "uso" in reply.lower()


def test_gastosfijos_list_empty(runtime):
    message = MagicMock()
    message.text = "/gastosfijos"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args.args[0]
    assert "no tenés" in reply.lower() or "creá uno" in reply.lower()


def test_gastosfijos_list_with_bills(runtime):
    # Seed
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        service.add(user_id=1, draft=__import__(
            "app.fixed_expenses.service", fromlist=["FixedExpenseDraft"]
        ).FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000"),
            payment_method="TRANSFERENCIA", due_day_of_month=10))
        service.add(user_id=1, draft=__import__(
            "app.fixed_expenses.service", fromlist=["FixedExpenseDraft"]
        ).FixedExpenseDraft(
            name="GYM", expected_amount=Decimal("60000"),
            payment_method="EFECTIVO", due_day_of_month=15))
    message = MagicMock()
    message.text = "/gastosfijos"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args.args[0]
    assert "CASA" in reply
    assert "GYM" in reply
    assert "0/2" in reply or "Progreso" in reply


def test_pague_marks_as_paid(runtime):
    from app.fixed_expenses.service import FixedExpenseDraft
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        service.add(user_id=1, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000")))
    message = MagicMock()
    message.text = "/pague CASA"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args.args[0]
    assert "CASA" in reply
    assert "pagado" in reply.lower()


def test_pague_with_real_amount_shows_diff(runtime):
    from app.fixed_expenses.service import FixedExpenseDraft
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        service.add(user_id=1, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000")))
    message = MagicMock()
    message.text = "/pague CASA 95000"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args.args[0]
    assert "CASA" in reply
    assert "95.000" in reply or "95000" in reply
    assert "5.000" in reply or "5000" in reply  # diff


def test_pague_unknown_name(runtime):
    message = MagicMock()
    message.text = "/pague INEXISTENTE"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args.args[0]
    assert "no encontré" in reply.lower()


def test_salte_marks_as_skipped(runtime):
    from app.fixed_expenses.service import FixedExpenseDraft
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        service.add(user_id=1, draft=FixedExpenseDraft(
            name="DENTISTA", expected_amount=Decimal("32000")))
    message = MagicMock()
    message.text = "/salte DENTISTA"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args.args[0]
    assert "DENTISTA" in reply
    assert "salteado" in reply.lower()


def test_ingreso_and_liberado(runtime):
    from app.fixed_expenses.service import FixedExpenseDraft
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        service.add(user_id=1, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000")))
        service.mark_paid(1, service.list_active(1)[0].id, "2026-09")

    # Set income
    message = MagicMock()
    message.text = "/ingreso 1100000"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    assert "1.100.000" in message.reply_text.call_args.args[0] or "1100000" in message.reply_text.call_args.args[0]

    # Set extra
    message2 = MagicMock()
    message2.text = "/extra 75000"
    message2.reply_text = AsyncMock()
    message2.chat.send_action = AsyncMock()
    update2 = _make_update(1, message2)
    _run(handle_command(update2, None, deps))

    # Summary
    message3 = MagicMock()
    message3.text = "/liberado"
    message3.reply_text = AsyncMock()
    message3.chat.send_action = AsyncMock()
    update3 = _make_update(1, message3)
    _run(handle_command(update3, None, deps))
    reply = message3.reply_text.call_args.args[0]
    assert "Ingreso" in reply
    assert "Extra" in reply
    assert "Liberado" in reply
    # 1100000 + 75000 - 90000 = 1085000
    assert "1.085.000" in reply or "1085000" in reply


def test_gastofijo_del(runtime):
    from app.fixed_expenses.service import FixedExpenseDraft
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = service.add(user_id=1, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000")))
    message = MagicMock()
    message.text = f"/gastofijo_del {bill.id}"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    reply = message.reply_text.call_args.args[0]
    assert "eliminado" in reply.lower()
    assert "CASA" in reply


def test_gastofijo_off_then_on(runtime):
    from app.fixed_expenses.service import FixedExpenseDraft
    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = service.add(user_id=1, draft=FixedExpenseDraft(
            name="CASA", expected_amount=Decimal("90000")))

    message = MagicMock()
    message.text = f"/gastofijo_off {bill.id}"
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    update = _make_update(1, message)
    deps = BotDependencies(runtime=runtime)
    _run(handle_command(update, None, deps))
    assert "pausado" in message.reply_text.call_args.args[0].lower()

    with session_scope() as s:
        active = FixedExpenseService(FixedExpenseRepository(s)).list_active(1)
    assert active == []

    message2 = MagicMock()
    message2.text = f"/gastofijo_on {bill.id}"
    message2.reply_text = AsyncMock()
    message2.chat.send_action = AsyncMock()
    update2 = _make_update(1, message2)
    _run(handle_command(update2, None, deps))
    assert "reanudado" in message2.reply_text.call_args.args[0].lower()
