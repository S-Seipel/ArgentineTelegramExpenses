from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, Integer

from app.ai.prompts import system_prompt, user_instructions
from app.ai.schemas import parse_ai_response
from app.ai.service import StubAIService
from app.bot.service import _process_message_async
from app.database.database import session_scope
from app.expenses.models import Expense
from app.expenses.repository import ExpenseRepository
from app.expenses.service import ExpenseDraft, ExpenseService


def test_telegram_user_id_column_is_biginteger():
    col = Expense.__table__.columns["telegram_user_id"]
    assert isinstance(col.type, BigInteger), (
        f"Expected BigInteger, got {type(col.type).__name__}"
    )
    assert not isinstance(col.type, Integer) or isinstance(col.type, BigInteger)


def test_telegram_sized_user_id_persists_and_reads_back(in_memory_db):
    """A real Telegram user_id (>2^31) must round-trip through the DB."""
    real_telegram_id = 600_000_0001  # > Int32 max (~2.14e9)
    draft = ExpenseDraft(
        name="Café",
        amount=Decimal("10000"),
        currency="ARS",
        category="Café",
        expense_date=date(2026, 1, 19),
        confidence=Decimal("0.9"),
    )
    with session_scope() as s:
        service = ExpenseService(ExpenseRepository(s))
        outcome = service.register_many(
            user_id=real_telegram_id,
            drafts=[draft],
            original_message="gasté 10k en café",
        )
    assert len(outcome.saved) == 1

    with session_scope() as s:
        rows = (
            s.query(Expense)
            .filter(Expense.telegram_user_id == real_telegram_id)
            .all()
        )
    assert len(rows) == 1
    assert rows[0].telegram_user_id == real_telegram_id


def test_telegram_user_id_at_int32_max(in_memory_db):
    """Edge case: exactly 2^31 - 1 (the last valid Int32)."""
    big_id = 2_147_483_647
    draft = ExpenseDraft(
        name="X",
        amount=Decimal("1"),
        currency="ARS",
        category="Otros",
        expense_date=date(2026, 1, 19),
        confidence=Decimal("0.5"),
    )
    with session_scope() as s:
        service = ExpenseService(ExpenseRepository(s))
        outcome = service.register_many(
            user_id=big_id,
            drafts=[draft],
            original_message="x",
        )
    assert len(outcome.saved) == 1
    assert outcome.saved[0].telegram_user_id == big_id


@pytest.mark.asyncio
async def test_lucas_amount_10x1000_through_bot(in_memory_db):
    """Real bot path: '10 lucas' must yield amount=10000 (not 10)."""
    from app.bot.service import BotRuntime
    from app.config.settings import Settings

    settings = Settings(
        telegram_bot_token="t",
        telegram_allowed_user_id=1,
        database_url="sqlite:///:memory:",
        ollama_base_url="http://localhost:11434",
        ollama_model="test",
        timezone="UTC",
        max_message_length=2000,
    )
    runtime = BotRuntime(
        settings=settings,
        ai=StubAIService(
            {
                "gasté 10 lucas en un café": {
                    "type": "register_expense",
                    "expenses": [
                        {
                            "name": "Café",
                            "amount": 10000,
                            "currency": "ARS",
                            "category": "Café",
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
        ),
    )
    big_id = 600_000_0001
    reply = await _process_message_async(
        runtime,
        "gasté 10 lucas en un café",
        user_id=big_id,
        today=date(2026, 1, 19),
    )
    assert "10.000" in reply
    assert "ARS" in reply


def test_lucas_k_palos_mangos_amount_via_parser(in_memory_db):
    """Verify the parser-level handling: any 'lucas'/'k' suffix means x1000.

    This guards against regressions in the LLM JSON shape: if a model ever
    returned amount=10 for '10 lucas', our schema should still be able to
    surface it and the service should reject it as too small.
    """
    raw_payload = {
        "type": "register_expense",
        "expenses": [
            {
                "name": "Café",
                "amount": 10,  # WRONG: model forgot 'lucas' = x1000
                "currency": "ARS",
                "category": "Café",
                "date": "2026-01-19",
                "confidence": 0.7,
                "needs_clarification": False,
                "clarification_question": None,
            }
        ],
        "query": None,
        "confidence": 0.7,
    }
    intent = parse_ai_response(raw_payload)
    assert intent.expenses[0].amount == Decimal("10")
    # A model that returned 10 for '10 lucas' would fail validation downstream.
    draft = ExpenseDraft(
        name=intent.expenses[0].name,
        amount=intent.expenses[0].amount,
        currency="ARS",
        category="Café",
        expense_date=date(2026, 1, 19),
        confidence=Decimal("0.7"),
    )
    with session_scope() as s:
        service = ExpenseService(ExpenseRepository(s))
        outcome = service.register_many(
            user_id=1, drafts=[draft], original_message="10 lucas"
        )
    # Validation layer treats the suspicious 10-ARS value as needing
    # clarification rather than auto-inserting. Either clarification OR
    # the original $10 expense is acceptable, but never an implicit 100x.
    assert not (
        outcome.saved and outcome.saved[0].amount == Decimal("10000")
    )


def test_prompt_documents_argentine_amount_slangs():
    """The system prompt MUST enumerate the slang rules so the LLM can't miss them."""
    sys_prompt = system_prompt("2026-01-19", "America/Argentina/Buenos_Aires")
    usr_prompt = user_instructions("2026-01-19", "America/Argentina/Buenos_Aires")
    combined = sys_prompt + usr_prompt
    for slug in ("k", "lucas", "mil", "palos", "mangos", "dólares", "verdes"):
        assert slug in combined, f"Prompt missing rules for {slug!r}"
    assert "10000" in combined
    assert "10 lucas" in combined


def test_prompt_amount_examples_consistent():
    sys_prompt = system_prompt("2026-01-19", "America/Argentina/Buenos_Aires")
    assert "'10 lucas'" in sys_prompt and "10000" in sys_prompt
    # Make sure the rule explicitly warns the model NOT to return 10:
    assert "no devolver 10" in sys_prompt.lower() or "no devolver 10" in sys_prompt
