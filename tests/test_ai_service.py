from __future__ import annotations

import asyncio
import json
import os
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.ai.schemas import AIError, parse_ai_response
from app.ai.schemas import SpendingAnalysis
from app.ai.service import (
    AIServiceConfig,
    MiniMaxAIService,
    OllamaAIService,
    StubAIService,
    _extract_json,
    build_default_ai_service,
)
from app.config.settings import Settings


def _build_settings() -> Settings:
    return Settings(
        telegram_bot_token="t",
        telegram_allowed_user_id=1,
        database_url="sqlite:///:memory:",
        ollama_base_url="http://localhost:11434",
        ollama_model="test-model",
    )


def test_extract_json_plain() -> None:
    data = {"a": 1}
    assert _extract_json('{"a": 1}') == data


def test_extract_json_code_fence() -> None:
    raw = "```json\n{\"a\": 2}\n```"
    assert _extract_json(raw) == {"a": 2}


def test_extract_json_chatter() -> None:
    raw = "Hago una cosa: {\"a\": 3} espero que sirva."
    assert _extract_json(raw) == {"a": 3}


def test_extract_json_invalid() -> None:
    with pytest.raises(AIError):
        _extract_json("no json here")


def test_parse_ai_response_validates_payload() -> None:
    payload = {
        "type": "register_expense",
        "expenses": [
            {
                "name": "Café",
                "amount": 10000,
                "currency": "ARS",
                "category": "Café",
                "date": "2026-01-01",
                "confidence": 0.9,
                "needs_clarification": False,
                "clarification_question": None,
            }
        ],
        "query": None,
        "confidence": 0.9,
    }
    intent = parse_ai_response(payload)
    assert intent.type == "register_expense"
    assert intent.expenses[0].name == "Café"
    assert intent.expenses[0].amount == Decimal("10000")


def test_parse_ai_response_invalid_amount_becomes_none() -> None:
    """Invalid amounts are normalized to None (downstream asks clarification)."""
    payload = {
        "type": "register_expense",
        "expenses": [
            {
                "name": "Café",
                "amount": "not-a-number",
                "currency": "ARS",
                "category": "Café",
                "date": "2026-01-01",
                "confidence": 0.9,
                "needs_clarification": False,
                "clarification_question": None,
            }
        ],
    }
    intent = parse_ai_response(payload)
    assert intent.expenses[0].amount is None


@pytest.mark.asyncio
async def test_ollama_service_unreachable_returns_ai_error() -> None:
    from app.ai.schemas import AIUnavailable

    cfg = AIServiceConfig(
        base_url="http://does-not-exist:11434",
        model="x",
        timeout=0.5,
        max_retries=0,
        timezone="UTC",
    )
    svc = OllamaAIService(cfg)
    with pytest.raises(AIUnavailable):
        await svc.interpret("gasté 10k en café")


@pytest.mark.asyncio
async def test_ollama_service_timeout_raises_ai_unavailable() -> None:
    """TimeoutException at the HTTP layer surfaces as AIUnavailable."""
    from app.ai.schemas import AIUnavailable

    class TimeoutClient:
        async def post(self, url, json=None):
            raise httpx.ConnectTimeout("timed out")

    cfg = AIServiceConfig(
        base_url="http://fake:11434",
        model="x",
        timeout=0.1,
        max_retries=0,
        timezone="UTC",
    )
    svc = OllamaAIService(cfg, client=TimeoutClient())
    with pytest.raises(AIUnavailable):
        await svc.interpret("test")


@pytest.mark.asyncio
async def test_ollama_service_retries_on_invalid_json_then_succeeds() -> None:
    import json as json_mod

    calls = {"n": 0}

    class FakeClient:
        async def post(self, url, json=None):
            calls["n"] += 1

            class R:
                status_code = 200

                def json(_self):
                    if calls["n"] == 1:
                        return {"message": {"content": "no json here"}}
                    return {
                        "message": {
                            "content": json_mod.dumps(
                                {
                                    "type": "register_expense",
                                    "expenses": [
                                        {
                                            "name": "Café",
                                            "amount": 10000,
                                            "currency": "ARS",
                                            "category": "Café",
                                            "date": "2026-01-01",
                                            "confidence": 0.9,
                                            "needs_clarification": False,
                                            "clarification_question": None,
                                        }
                                    ],
                                    "query": None,
                                    "confidence": 0.9,
                                }
                            )
                        }
                    }

                @property
                def text(_self):
                    return ""

            return R()

    cfg = AIServiceConfig(
        base_url="http://fake:11434",
        model="x",
        timeout=1.0,
        max_retries=2,
        timezone="UTC",
    )
    svc = OllamaAIService(cfg, client=FakeClient())
    parsed = await svc.interpret("gasté 10k en café")
    assert parsed.intent.expenses[0].name == "Café"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_minimax_service_uses_openai_compatible_response() -> None:
    class FakeClient:
        headers = {}

        async def post(self, url, json=None):
            assert url == "https://api.minimax.io/v1/chat/completions"
            assert self.headers["Authorization"] == "Bearer subscription-key"
            assert json["thinking"] == {"type": "disabled"}

            class Response:
                status_code = 200
                text = ""

                def json(self):
                    return {
                        "choices": [
                            {
                                "message": {
                                    "content": '{"type":"unknown","expenses":[]}'
                                }
                            }
                        ]
                    }

            return Response()

    cfg = AIServiceConfig(
        base_url="https://api.minimax.io/v1",
        model="MiniMax-M3",
        timeout=1.0,
        max_retries=0,
        timezone="UTC",
        provider="MiniMax",
        api_key="subscription-key",
    )
    parsed = await MiniMaxAIService(cfg, client=FakeClient()).interpret("hola")
    assert parsed.intent.type == "unknown"


@pytest.mark.asyncio
async def test_default_service_can_select_minimax() -> None:
    settings = Settings(
        telegram_bot_token="t",
        telegram_allowed_user_id=1,
        database_url="sqlite:///:memory:",
        ai_provider="minimax",
        minimax_api_key="subscription-key",
    )
    service = build_default_ai_service(settings)
    assert isinstance(service, MiniMaxAIService)
    await service.aclose()


@pytest.mark.asyncio
async def test_minimax_service_validates_spending_analysis() -> None:
    class FakeClient:
        headers = {}

        async def post(self, url, json=None):
            class Response:
                status_code = 200
                text = ""

                def json(self):
                    return {
                        "choices": [{
                            "message": {
                                "content": (
                                    '{"summary":"Bien",'
                                    '"observations":["Café pesa mucho"],'
                                    '"recommendations":[{"category":"Café",'
                                    '"reason":"alto","action":"comparar precios"}],'
                                    '"caveat":null}'
                                )
                            }
                        }]
                    }

            return Response()

    cfg = AIServiceConfig(
        base_url="https://api.minimax.io/v1",
        model="MiniMax-M3",
        timeout=1.0,
        max_retries=0,
        timezone="UTC",
        provider="MiniMax",
        api_key="subscription-key",
    )
    result = await MiniMaxAIService(cfg, client=FakeClient()).analyze_spending(
        '{"periodo_actual":{}}'
    )
    assert isinstance(result, SpendingAnalysis)
    assert result.recommendations[0].action == "comparar precios"


@pytest.mark.asyncio
async def test_spending_analysis_retries_truncated_json() -> None:
    class FakeClient:
        headers = {}
        calls = 0

        async def post(self, url, json=None):
            self.calls += 1

            class Response:
                status_code = 200
                text = ""

                def __init__(self, content):
                    self._content = content

                def json(self):
                    return {"choices": [{"message": {"content": self._content}}]}

            if self.calls == 1:
                return Response('{"summary":"respuesta cortada"')
            return Response(
                '{"summary":"Ok", "observations":[], '
                '"recommendations":[], "caveat":null}'
            )

    client = FakeClient()
    cfg = AIServiceConfig(
        base_url="https://api.minimax.io/v1",
        model="MiniMax-M3",
        timeout=1.0,
        max_retries=1,
        timezone="UTC",
        provider="MiniMax",
        api_key="subscription-key",
    )
    result = await MiniMaxAIService(cfg, client=client).analyze_spending("{}")
    assert result.summary == "Ok"
    assert client.calls == 2


@pytest.mark.asyncio
async def test_stub_ai_returns_scripted() -> None:
    payload = {
        "type": "register_expense",
        "expenses": [
            {
                "name": "X",
                "amount": 1,
                "currency": "ARS",
                "category": "Café",
                "date": "2026-01-01",
                "confidence": 0.5,
                "needs_clarification": False,
                "clarification_question": None,
            }
        ],
    }
    stub = StubAIService({"hola": payload})
    parsed = await stub.interpret("hola")
    assert parsed.intent.expenses[0].name == "X"
    parsed_default = await stub.interpret("chau")
    assert parsed_default.intent.type in ("unknown",)


def test_http_timeout_sub_budgets():
    """The httpx.Timeout must allocate the read budget for slow inference
    while keeping connect/write/pool short to fail fast on network issues.
    """
    cfg = AIServiceConfig(
        base_url="http://x",
        model="x",
        timeout=180,
        max_retries=2,
        timezone="UTC",
    )
    t = cfg.http_timeout()
    assert t.read == 180
    assert t.connect == 30
    assert t.write == 60
    assert t.pool == 30


def test_http_timeout_handles_small_budgets():
    cfg = AIServiceConfig(
        base_url="http://x",
        model="x",
        timeout=5,
        max_retries=2,
        timezone="UTC",
    )
    t = cfg.http_timeout()
    assert t.read == 5
    assert t.connect == 5
    assert t.write == 5
    assert t.pool == 5


@pytest.mark.asyncio
async def test_ollama_service_uses_explicit_timeout():
    """Constructor wires the explicit sub-budget timeout into the client."""
    captured = {}

    class FakeClient:
        def __init__(self, *, timeout, headers):
            captured["timeout"] = timeout
            captured["headers"] = headers

        async def post(self, *args, **kwargs):
            raise AssertionError("not used")

        async def aclose(self):
            pass

    cfg = AIServiceConfig(
        base_url="http://x",
        model="qwen3:8b",
        timeout=180,
        max_retries=2,
        timezone="UTC",
    )
    OllamaAIService(cfg, client=FakeClient(timeout=cfg.http_timeout(), headers={}))
    assert captured["timeout"].read == 180
