from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.ai.schemas import (
    AIError,
    ExtractedExpense,
    Intent,
    IntentType,
    ParsedMessage,
)
from app.ai.service import StubAIService
from app.bot.service import (
    BotRuntime,
    _process_message_async,
)
from app.config.settings import Settings
from app.utils.dates import parse_relative_date


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


@pytest.mark.asyncio
async def test_cafe_expense(runtime):
    runtime.ai = StubAIService(
        {
            "Gaste 10k en un cafe": {
                "type": "register_expense",
                "expenses": [
                    {
                        "name": "Café",
                        "amount": 10000,
                        "currency": "ARS",
                        "category": "Café",
                        "date": "2026-01-19",
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
    reply = await _process_message_async(
        runtime, "Gaste 10k en un cafe", user_id=1, today=date(2026, 1, 19)
    )
    assert "Gasto registrado" in reply
    assert "Café" in reply
    assert "10.000" in reply


@pytest.mark.asyncio
async def test_uber_lucas_amount(runtime):
    runtime.ai = StubAIService(
        {
            "gasté 10 lucas en Uber": {
                "type": "register_expense",
                "expenses": [
                    {
                        "name": "Uber",
                        "amount": 10000,
                        "currency": "ARS",
                        "category": "Uber",
                        "date": "2026-01-19",
                        "confidence": 0.95,
                        "needs_clarification": False,
                        "clarification_question": None,
                    }
                ],
                "query": None,
                "confidence": 0.95,
            }
        }
    )
    reply = await _process_message_async(
        runtime, "gasté 10 lucas en Uber", user_id=1, today=date(2026, 1, 19)
    )
    assert "Gasto registrado" in reply
    assert "Uber" in reply


@pytest.mark.asyncio
async def test_yesterday_date(runtime):
    payload = {
        "type": "register_expense",
        "expenses": [
            {
                "name": "Comida",
                "amount": 20000,
                "currency": "ARS",
                "category": "Comida",
                "date": "2026-01-18",
                "confidence": 0.8,
                "needs_clarification": False,
                "clarification_question": None,
            }
        ],
        "query": None,
        "confidence": 0.8,
    }
    runtime.ai = StubAIService({"ayer gasté 20k en comida": payload})
    reply = await _process_message_async(
        runtime,
        "ayer gasté 20k en comida",
        user_id=1,
        today=date(2026, 1, 19),
    )
    assert "Gasto registrado" in reply


@pytest.mark.asyncio
async def test_multiple_expenses(runtime):
    runtime.ai = StubAIService(
        {
            "hoy gasté 5k en café y 12k en Uber": {
                "type": "register_expense",
                "expenses": [
                    {
                        "name": "Café",
                        "amount": 5000,
                        "currency": "ARS",
                        "category": "Café",
                        "date": "2026-01-19",
                        "confidence": 0.9,
                        "needs_clarification": False,
                        "clarification_question": None,
                    },
                    {
                        "name": "Uber",
                        "amount": 12000,
                        "currency": "ARS",
                        "category": "Uber",
                        "date": "2026-01-19",
                        "confidence": 0.9,
                        "needs_clarification": False,
                        "clarification_question": None,
                    },
                ],
                "query": None,
                "confidence": 0.9,
            }
        }
    )
    reply = await _process_message_async(
        runtime,
        "hoy gasté 5k en café y 12k en Uber",
        user_id=1,
        today=date(2026, 1, 19),
    )
    assert "Gastos registrados" in reply
    assert "Café" in reply
    assert "Uber" in reply
    assert "Total" in reply


@pytest.mark.asyncio
async def test_usd_steam(runtime):
    runtime.ai = StubAIService(
        {
            "gasté 20 dólares en Steam": {
                "type": "register_expense",
                "expenses": [
                    {
                        "name": "Steam",
                        "amount": 20,
                        "currency": "USD",
                        "category": "Juegos",
                        "date": "2026-01-19",
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
    reply = await _process_message_async(
        runtime,
        "gasté 20 dólares en Steam",
        user_id=1,
        today=date(2026, 1, 19),
    )
    assert "Steam" in reply
    assert "USD" in reply


@pytest.mark.asyncio
async def test_missing_amount_asks_clarification(runtime):
    runtime.ai = StubAIService(
        {
            "gasté en McDonald's": {
                "type": "register_expense",
                "expenses": [
                    {
                        "name": "McDonald's",
                        "amount": None,
                        "currency": "ARS",
                        "category": "Comida rápida",
                        "date": "2026-01-19",
                        "confidence": 0.7,
                        "needs_clarification": True,
                        "clarification_question": "¿Cuánto gastaste en McDonald's?",
                    }
                ],
                "query": None,
                "confidence": 0.7,
            }
        }
    )
    reply = await _process_message_async(
        runtime,
        "gasté en McDonald's",
        user_id=1,
        today=date(2026, 1, 19),
    )
    assert "Cuánto gastaste" in reply


@pytest.mark.asyncio
async def test_invalid_amount_asks_clarification(runtime):
    runtime.ai = StubAIService(
        {
            "gasté": {
                "type": "register_expense",
                "expenses": [
                    {
                        "name": "Comida",
                        "amount": -5,
                        "currency": "ARS",
                        "category": "Comida",
                        "date": "2026-01-19",
                        "confidence": 0.5,
                        "needs_clarification": False,
                        "clarification_question": None,
                    }
                ],
                "query": None,
                "confidence": 0.5,
            }
        }
    )
    reply = await _process_message_async(
        runtime, "gasté", user_id=1, today=date(2026, 1, 19)
    )
    assert "monto" in reply.lower()


@pytest.mark.asyncio
async def test_unauthorized_user(runtime):
    runtime.settings.telegram_allowed_user_id = 99
    from app.bot.app import BotDependencies, _SilentStop
    from app.bot.handlers import _ensure_authorized
    from unittest.mock import MagicMock, AsyncMock

    message = MagicMock()
    message.reply_text = AsyncMock()
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_message = message
    deps = BotDependencies(runtime=runtime)
    with pytest.raises(_SilentStop):
        await _ensure_authorized(update, deps)


@pytest.mark.asyncio
async def test_invalid_llm_response_is_handled(runtime):
    class BrokenAI:
        async def interpret(self, message, **kwargs):
            raise AIError("nope")

    runtime.ai = BrokenAI()  # type: ignore[assignment]
    reply = await _process_message_async(
        runtime, "gasté 10k en café", user_id=1, today=date(2026, 1, 19)
    )
    assert "No pude entender" in reply or "mensaje" in reply.lower()


@pytest.mark.asyncio
async def test_postgres_unavailable(monkeypatch, runtime):
    from sqlalchemy.exc import OperationalError

    runtime.ai = StubAIService(
        {
            "gasté 10k en café": {
                "type": "register_expense",
                "expenses": [
                    {
                        "name": "Café",
                        "amount": 10000,
                        "currency": "ARS",
                        "category": "Café",
                        "date": "2026-01-19",
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

    def boom(*args, **kwargs):
        raise OperationalError("db down", {}, Exception())

    monkeypatch.setattr("app.bot.service.session_scope", boom)
    reply = await _process_message_async(
        runtime, "gasté 10k en café", user_id=1, today=date(2026, 1, 19)
    )
    assert "base de datos" in reply.lower() or "no pude" in reply.lower()


@pytest.mark.asyncio
async def test_query_total(runtime):
    runtime.ai = StubAIService(
        {
            "cuánto gasté hoy": {
                "type": "query",
                "expenses": [],
                "query": {
                    "period": "today",
                    "days": None,
                    "category": None,
                    "currency": "ARS",
                    "limit": None,
                    "order_by": None,
                    "raw_period_text": "hoy",
                },
                "confidence": 0.95,
            }
        }
    )
    reply = await _process_message_async(
        runtime, "cuánto gasté hoy", user_id=1, today=date(2026, 1, 19)
    )
    assert "gasto" in reply.lower()


@pytest.mark.asyncio
async def test_query_largest(runtime):
    runtime.ai = StubAIService(
        {
            "cuál fue mi gasto más grande este mes": {
                "type": "query",
                "expenses": [],
                "query": {
                    "period": "month",
                    "order_by": "amount_desc",
                    "limit": 1,
                },
                "confidence": 0.9,
            }
        }
    )
    reply = await _process_message_async(
        runtime,
        "cuál fue mi gasto más grande este mes",
        user_id=1,
        today=date(2026, 1, 19),
    )
    assert "gasto" in reply.lower()


@pytest.mark.asyncio
async def test_list_recent(runtime):
    runtime.ai = StubAIService(
        {
            "mostrame mis últimos 10 gastos": {
                "type": "query",
                "expenses": [],
                "query": {
                    "period": None,
                    "limit": 10,
                    "order_by": "date_desc",
                },
                "confidence": 0.8,
            }
        }
    )
    reply = await _process_message_async(
        runtime,
        "mostrame mis últimos 10 gastos",
        user_id=1,
        today=date(2026, 1, 19),
    )
    assert "ltimos gastos" in reply.lower()


def test_relative_dates():
    today = date(2026, 1, 19)
    assert parse_relative_date("hoy", today=today) == today
    assert parse_relative_date("ayer", today=today) == date(2026, 1, 18)
    assert parse_relative_date("anteayer", today=today) == date(2026, 1, 17)
    assert parse_relative_date(None, today=today) is None
    assert parse_relative_date("2026-01-15", today=today) == date(2026, 1, 15)
    assert parse_relative_date("15/01/2026", today=today) == date(2026, 1, 15)
