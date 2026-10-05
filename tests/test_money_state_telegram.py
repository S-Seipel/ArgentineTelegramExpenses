"""F1 R3 Telegram command tests.

Each test wires ``handle_command`` with a stub Update and asserts the
reply text. The tests cover the spec §14 commands and the §15 target
syntax (``/target 20%`` vs ``/target 220000``).

The handlers run inside a real session against the in-memory DB so the
``CycleService.ensure_current_cycle`` rollover walk and the
``MoneyStateService`` UPSERT paths are exercised end-to-end.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.service import StubAIService
from app.bot.app import BotDependencies
from app.bot.handlers import handle_command
from app.bot.service import BotRuntime
from app.config.settings import Settings
from app.database.database import session_scope
from app.money import (
    CycleService,
    MoneyEngine,
    UserSettingsService,
)
from app.utils.dates import today_in_tz


# ---------------------------------------------------------------------------
# Stubs (mirrors tests/test_bot_commands.py conventions)
# ---------------------------------------------------------------------------


def _make_update(user_id: int, message: MagicMock) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    update = MagicMock()
    update.effective_message = message
    update.effective_user = user
    return update


def _run(coro):
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return asyncio.run(coro)
        return loop.run_until_complete(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)


@pytest.fixture
def runtime(in_memory_db):
    settings = Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_id=42,
        database_url="sqlite:///:memory:",
        ollama_base_url="http://localhost:11434",
        ollama_model="test-model",
        timezone="UTC",
    )
    return BotRuntime(settings=settings, ai=StubAIService())


def _seed_cycle(user_id: int, today: date, payday_day: int = 2):
    with session_scope() as s:
        UserSettingsService(s).set_payday_day(
            user_id=user_id, payday_day=payday_day
        )
        view = CycleService(s).open_initial_cycle(
            user_id=user_id, today=today
        )
    return view


def _send(runtime, text: str, *, chat_id: int = 7, message_id: int = 1,
          today=None):
    message = MagicMock()
    message.text = text
    message.message_id = message_id
    message.chat_id = chat_id
    message.chat.send_action = AsyncMock()
    message.reply_text = AsyncMock()
    update = _make_update(runtime.settings.telegram_allowed_user_id, message)
    deps = BotDependencies(runtime=runtime)
    effective_today = today or today_in_tz(runtime.settings.timezone)
    # The R3 handlers call ``today_in_tz`` inside the dispatcher; patch
    # so the same date is used in ``_seed_cycle`` and in the handler.
    with patch(
        "app.bot.handlers.today_in_tz", return_value=effective_today
    ):
        _run(handle_command(update, None, deps))
    return message.reply_text.call_args.args[0]


def _summary(runtime, cycle_id: int):
    user_id = runtime.settings.telegram_allowed_user_id
    with session_scope() as s:
        return MoneyEngine(s).get_cycle_summary(user_id, cycle_id)


@pytest.mark.parametrize(
    "command,model",
    [
        ("/extra", "CycleIncome"),
        ("/ahorro_realizado", "SavingsRealization"),
    ],
)
def test_tg_source_key_currency_conflict_is_friendly_and_keeps_one_row(
    runtime, in_memory_db, command, model
):
    from app.money import models

    today = today_in_tz(runtime.settings.timezone)
    view = _seed_cycle(runtime.settings.telegram_allowed_user_id, today)
    first = _send(runtime, f"{command} 50", message_id=77, today=today)
    assert "registrado" in first.lower()
    conflict = _send(runtime, f"{command} 60 USD", message_id=77, today=today)
    assert "otra moneda" in conflict
    assert "telegram:" not in conflict
    with session_scope() as s:
        rows = s.query(getattr(models, model)).filter_by(cycle_id=view.cycle_id).all()
        if model == "CycleIncome":
            rows = [row for row in rows if row.kind == "extra"]
        assert len(rows) == 1
        assert rows[0].currency == "ARS"


# ---------------------------------------------------------------------------
# /ingreso
# ---------------------------------------------------------------------------


def test_tg_ingreso_sets_base_income(runtime, in_memory_db):
    today = today_in_tz(runtime.settings.timezone)
    view = _seed_cycle(runtime.settings.telegram_allowed_user_id, today)
    reply = _send(runtime, "/ingreso 1100000", today=today)
    assert "Ingreso base" in reply
    summary = _summary(runtime, view.cycle_id)
    assert summary.base_income.get("ARS") == Decimal("1100000.00")


def test_tg_ingreso_rejects_missing_amount(runtime, in_memory_db):
    today = today_in_tz(runtime.settings.timezone)
    _seed_cycle(runtime.settings.telegram_allowed_user_id, today)
    reply = _send(runtime, "/ingreso", today=today)
    assert "Decime el monto" in reply


# ---------------------------------------------------------------------------
# /extra
# ---------------------------------------------------------------------------


def test_tg_extra_records_with_source_key(runtime, in_memory_db):
    today = today_in_tz(runtime.settings.timezone)
    view = _seed_cycle(runtime.settings.telegram_allowed_user_id, today)
    reply = _send(
        runtime, "/extra 50000 regalo", chat_id=7, message_id=42, today=today
    )
    assert "Extra registrado" in reply
    summary = _summary(runtime, view.cycle_id)
    assert summary.extra_income.get("ARS") == Decimal("50000.00")


def test_tg_extra_idempotent_on_retry(runtime, in_memory_db):
    today = today_in_tz(runtime.settings.timezone)
    view = _seed_cycle(runtime.settings.telegram_allowed_user_id, today)
    # Same chat+message_id → same source_key.
    _send(runtime, "/extra 50000", chat_id=7, message_id=42, today=today)
    _send(runtime, "/extra 70000", chat_id=7, message_id=42, today=today)
    summary = _summary(runtime, view.cycle_id)
    # Should still be one logical extra, updated to 70000.
    assert summary.extra_income.get("ARS") == Decimal("70000.00")


# ---------------------------------------------------------------------------
# /extra_editar + /extra_borrar
# ---------------------------------------------------------------------------


def test_tg_extra_editar(runtime, in_memory_db):
    today = today_in_tz(runtime.settings.timezone)
    view = _seed_cycle(runtime.settings.telegram_allowed_user_id, today)
    _send(runtime, "/extra 50", chat_id=7, message_id=1, today=today)
    from app.money.models import CycleIncome
    from sqlalchemy import select
    with session_scope() as s:
        extra_id = s.execute(
            select(CycleIncome.id).where(
                CycleIncome.cycle_id == view.cycle_id,
                CycleIncome.kind == "extra",
            )
        ).scalar_one()
    reply = _send(runtime, f"/extra_editar {extra_id} 75", today=today)
    assert "actualizado" in reply
    summary = _summary(runtime, view.cycle_id)
    assert summary.extra_income.get("ARS") == Decimal("75.00")


def test_tg_extra_borrar(runtime, in_memory_db):
    today = today_in_tz(runtime.settings.timezone)
    view = _seed_cycle(runtime.settings.telegram_allowed_user_id, today)
    _send(runtime, "/extra 50", chat_id=7, message_id=1, today=today)
    from app.money.models import CycleIncome
    from sqlalchemy import select
    with session_scope() as s:
        extra_id = s.execute(
            select(CycleIncome.id).where(
                CycleIncome.cycle_id == view.cycle_id,
                CycleIncome.kind == "extra",
            )
        ).scalar_one()
    reply = _send(runtime, f"/extra_borrar {extra_id}", today=today)
    assert "borrado" in reply
    summary = _summary(runtime, view.cycle_id)
    assert summary.extra_income.get("ARS") == Decimal("0")


# ---------------------------------------------------------------------------
# /target — explicit syntax
# ---------------------------------------------------------------------------


def test_tg_target_pct(runtime, in_memory_db):
    today = today_in_tz(runtime.settings.timezone)
    view = _seed_cycle(runtime.settings.telegram_allowed_user_id, today)
    reply = _send(runtime, "/target 20%", today=today)
    assert "Target" in reply
    from app.money.models import SavingsTarget
    with session_scope() as s:
        from sqlalchemy import select
        row = s.execute(
            select(SavingsTarget).where(
                SavingsTarget.cycle_id == view.cycle_id
            )
        ).scalar_one()
        assert row.target_pct == Decimal("0.2000")


def test_tg_target_amount(runtime, in_memory_db):
    today = today_in_tz(runtime.settings.timezone)
    view = _seed_cycle(runtime.settings.telegram_allowed_user_id, today)
    reply = _send(runtime, "/target 220000", today=today)
    assert "Target" in reply
    summary = _summary(runtime, view.cycle_id)
    assert summary.savings_target.get("ARS") == Decimal("220000.00")


def test_tg_target_bare_number_is_amount(runtime, in_memory_db):
    """A bare number is treated as an amount, never as a percentage.

    Per spec §15 the ``%`` suffix is the only thing that disambiguates
    a percentage; ``/target 20`` therefore sets ``target_amount = 20``,
    not a 20% pct. Without the suffix we never silently coerce.
    """
    today = today_in_tz(runtime.settings.timezone)
    view = _seed_cycle(runtime.settings.telegram_allowed_user_id, today)
    reply = _send(runtime, "/target 20", today=today)
    assert "Target" in reply
    summary = _summary(runtime, view.cycle_id)
    assert summary.savings_target.get("ARS") == Decimal("20.00")


# ---------------------------------------------------------------------------
# /reservar + /liberar
# ---------------------------------------------------------------------------


def test_tg_reservar(runtime, in_memory_db):
    today = today_in_tz(runtime.settings.timezone)
    view = _seed_cycle(runtime.settings.telegram_allowed_user_id, today)
    reply = _send(runtime, "/reservar 220000", today=today)
    assert "Reserva" in reply
    summary = _summary(runtime, view.cycle_id)
    assert summary.savings_reserved.get("ARS") == Decimal("220000.00")


def test_tg_liberar(runtime, in_memory_db):
    today = today_in_tz(runtime.settings.timezone)
    view = _seed_cycle(runtime.settings.telegram_allowed_user_id, today)
    _send(runtime, "/reservar 220000", today=today)
    reply = _send(runtime, "/liberar 100000", today=today)
    assert "liberada" in reply
    summary = _summary(runtime, view.cycle_id)
    assert summary.savings_reserved.get("ARS") == Decimal("120000.00")


# ---------------------------------------------------------------------------
# /ahorro_realizado + edit + delete
# ---------------------------------------------------------------------------


def test_tg_ahorro_realizado(runtime, in_memory_db):
    today = today_in_tz(runtime.settings.timezone)
    view = _seed_cycle(runtime.settings.telegram_allowed_user_id, today)
    reply = _send(
        runtime, "/ahorro_realizado 150000 transferencia",
        chat_id=7, message_id=99, today=today,
    )
    assert "Ahorro realizado" in reply
    summary = _summary(runtime, view.cycle_id)
    assert summary.savings_realized.get("ARS") == Decimal("150000.00")


def test_tg_ahorro_editar(runtime, in_memory_db):
    today = today_in_tz(runtime.settings.timezone)
    view = _seed_cycle(runtime.settings.telegram_allowed_user_id, today)
    _send(runtime, "/ahorro_realizado 50", chat_id=7, message_id=1, today=today)
    from app.money.models import SavingsRealization
    from sqlalchemy import select
    with session_scope() as s:
        rid = s.execute(
            select(SavingsRealization.id).where(
                SavingsRealization.cycle_id == view.cycle_id
            )
        ).scalar_one()
    reply = _send(runtime, f"/ahorro_editar {rid} 75", today=today)
    assert "actualizado" in reply
    summary = _summary(runtime, view.cycle_id)
    assert summary.savings_realized.get("ARS") == Decimal("75.00")


def test_tg_ahorro_borrar(runtime, in_memory_db):
    today = today_in_tz(runtime.settings.timezone)
    view = _seed_cycle(runtime.settings.telegram_allowed_user_id, today)
    _send(runtime, "/ahorro_realizado 50", chat_id=7, message_id=1, today=today)
    from app.money.models import SavingsRealization
    from sqlalchemy import select
    with session_scope() as s:
        rid = s.execute(
            select(SavingsRealization.id).where(
                SavingsRealization.cycle_id == view.cycle_id
            )
        ).scalar_one()
    reply = _send(runtime, f"/ahorro_borrar {rid}", today=today)
    assert "borrado" in reply
    summary = _summary(runtime, view.cycle_id)
    assert summary.savings_realized.get("ARS") == Decimal("0")
