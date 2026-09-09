"""End-to-end tests where the scripted AI returns WRONG amounts for slang,
and we verify the deterministic normalizer fixes them before they hit the DB."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.ai.service import StubAIService
from app.bot.service import BotRuntime, _process_message_async
from app.config.settings import Settings


def _runtime(in_memory_db, ai: StubAIService) -> BotRuntime:
    s = Settings(
        telegram_bot_token="t",
        telegram_allowed_user_id=1,
        database_url="sqlite:///:memory:",
        ollama_base_url="http://localhost:11434",
        ollama_model="test",
        timezone="UTC",
        max_message_length=2000,
    )
    return BotRuntime(settings=s, ai=ai)


@pytest.mark.asyncio
async def test_real_run_10_lucas_saves_10000(in_memory_db):
    """Reproduces the user's exact failure path with the broken amount=10."""
    ai = StubAIService(
        {
            "gaste 10 lucas en un pancho": {
                "type": "register_expense",
                "expenses": [
                    {
                        "name": "Pancho",
                        "amount": 10,  # what the LLM wrongly returned
                        "currency": "ARS",
                        "category": "Comida rápida",
                        "date": "2026-08-19",
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
    runtime = _runtime(in_memory_db, ai)
    reply = await _process_message_async(
        runtime,
        "gaste 10 lucas en un pancho",
        user_id=6000000001,
        today=date(2026, 8, 19),
    )
    assert "10.000" in reply
    assert "Pancho" in reply
    assert "ARS" in reply


@pytest.mark.asyncio
async def test_real_run_10k_saves_10000(in_memory_db):
    ai = StubAIService(
        {
            "gasté 10k en Uber": {
                "type": "register_expense",
                "expenses": [
                    {
                        "name": "Uber",
                        "amount": 10,
                        "currency": "ARS",
                        "category": "Uber",
                        "date": "2026-08-19",
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
    runtime = _runtime(in_memory_db, ai)
    reply = await _process_message_async(
        runtime,
        "gasté 10k en Uber",
        user_id=6000000001,
        today=date(2026, 8, 19),
    )
    assert "10.000" in reply
    assert "Uber" in reply


@pytest.mark.asyncio
async def test_real_run_5k_y_12k_pairwise(in_memory_db):
    """The user's exact multi-expense case."""
    ai = StubAIService(
        {
            "hoy gasté 5k en café y 12k en Uber": {
                "type": "register_expense",
                "expenses": [
                    {
                        "name": "Café",
                        "amount": 5,
                        "currency": "ARS",
                        "category": "Café",
                        "date": "2026-08-19",
                        "confidence": 0.9,
                        "needs_clarification": False,
                        "clarification_question": None,
                    },
                    {
                        "name": "Uber",
                        "amount": 12,
                        "currency": "ARS",
                        "category": "Uber",
                        "date": "2026-08-19",
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
    runtime = _runtime(in_memory_db, ai)
    reply = await _process_message_async(
        runtime,
        "hoy gasté 5k en café y 12k en Uber",
        user_id=6000000001,
        today=date(2026, 8, 19),
    )
    assert "Café" in reply and "Uber" in reply
    assert "5.000" in reply and "12.000" in reply


@pytest.mark.asyncio
async def test_real_run_dolares_no_multiplier(in_memory_db):
    ai = StubAIService(
        {
            "gasté 20 dólares en Steam": {
                "type": "register_expense",
                "expenses": [
                    {
                        "name": "Steam",
                        "amount": 20,
                        "currency": "USD",
                        "category": "Juegos",
                        "date": "2026-08-19",
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
    runtime = _runtime(in_memory_db, ai)
    reply = await _process_message_async(
        runtime,
        "gasté 20 dólares en Steam",
        user_id=6000000001,
        today=date(2026, 8, 19),
    )
    assert "20" in reply and "USD" in reply


@pytest.mark.asyncio
async def test_real_run_persists_big_telegram_id(in_memory_db):
    """End-to-end: huge Telegram user_id round-trips through the bot."""
    from app.database.database import session_scope
    from app.expenses.models import Expense

    big_id = 7937812665  # exactly the value from the user's failed log
    ai = StubAIService(
        {
            "gasté 10 lucas en un pancho": {
                "type": "register_expense",
                "expenses": [
                    {
                        "name": "Pancho",
                        "amount": 10,
                        "currency": "ARS",
                        "category": "Comida rápida",
                        "date": "2026-08-19",
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
    runtime = _runtime(in_memory_db, ai)
    await _process_message_async(
        runtime,
        "gasté 10 lucas en un pancho",
        user_id=big_id,
        today=date(2026, 8, 19),
    )
    with session_scope() as s:
        rows = (
            s.query(Expense)
            .filter(Expense.telegram_user_id == big_id)
            .all()
        )
    assert len(rows) == 1
    assert rows[0].telegram_user_id == big_id
    assert rows[0].amount == Decimal("10000.00")
    assert rows[0].name == "Pancho"
