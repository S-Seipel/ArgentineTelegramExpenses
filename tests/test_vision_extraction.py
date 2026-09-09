from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.ai.schemas import AIError, AIUnavailable
from app.ai.service import AIServiceConfig, OllamaAIService


class _MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, responder):
        self.responder = responder
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return self.responder(request)


def _json_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )


def _service(responder) -> OllamaAIService:
    cfg = AIServiceConfig(
        base_url="http://x",
        model="qwen3:4b",
        timeout=10,
        max_retries=0,
        timezone="UTC",
        vision_model="qwen2-vl:7b",
    )
    transport = _MockTransport(responder)
    client = httpx.AsyncClient(transport=transport, timeout=cfg.http_timeout())
    return OllamaAIService(cfg, client=client)


@pytest.mark.asyncio
async def test_describe_image_returns_dict():
    captured = {}

    def responder(request: httpx.Request):
        captured["body"] = json.loads(request.content)
        return _json_response(
            {
                "message": {
                    "content": json.dumps(
                        {
                            "name": "Starbucks",
                            "amount": 4500,
                            "currency": "ARS",
                            "date": "2026-09-09",
                            "category": "Café",
                            "confidence": 0.85,
                            "needs_clarification": False,
                        }
                    )
                }
            }
        )

    svc = _service(responder)
    out = await svc.describe_image(b"\x89PNG\r\nfake-bytes")
    await svc.aclose()
    assert out["name"] == "Starbucks"
    assert out["amount"] == 4500
    assert out["confidence"] == 0.85
    assert captured["body"]["model"] == "qwen2-vl:7b"
    assert captured["body"]["format"] == "json"
    assert captured["body"]["think"] is False
    assert len(captured["body"]["messages"][0]["images"]) == 1
    img = captured["body"]["messages"][0]["images"][0]
    assert base64.b64decode(img) == b"\x89PNG\r\nfake-bytes"


@pytest.mark.asyncio
async def test_describe_image_normalizes_missing_keys():
    def responder(request: httpx.Request):
        return _json_response(
            {"message": {"content": json.dumps({"amount": 1000})}}
        )

    svc = _service(responder)
    out = await svc.describe_image(b"x")
    await svc.aclose()
    assert out["amount"] == 1000
    assert out["name"] is None
    assert out["currency"] == "ARS"
    assert out["confidence"] == 0.0


@pytest.mark.asyncio
async def test_describe_image_empty_raises():
    svc = _service(lambda r: _json_response({"ok": True}))
    with pytest.raises(AIError):
        await svc.describe_image(b"")
    await svc.aclose()


@pytest.mark.asyncio
async def test_describe_image_404_suggests_pull():
    def responder(request: httpx.Request):
        return _json_response(
            {"error": "model 'qwen2-vl:7b' not found"}, status=404
        )

    svc = _service(responder)
    with pytest.raises(AIError) as exc:
        await svc.describe_image(b"x")
    await svc.aclose()
    assert "ollama pull" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_describe_image_connect_error():
    def responder(request: httpx.Request):
        raise httpx.ConnectError("nope", request=request)

    svc = _service(responder)
    with pytest.raises(AIUnavailable):
        await svc.describe_image(b"x")
    await svc.aclose()


@pytest.mark.asyncio
async def test_describe_image_timeout():
    def responder(request: httpx.Request):
        raise httpx.ReadTimeout("slow", request=request)

    svc = _service(responder)
    with pytest.raises(AIUnavailable):
        await svc.describe_image(b"x")
    await svc.aclose()


@pytest.mark.asyncio
async def test_describe_image_invalid_json():
    def responder(request: httpx.Request):
        return _json_response(
            {"message": {"content": "not json at all"}}
        )

    svc = _service(responder)
    with pytest.raises(AIError) as exc:
        await svc.describe_image(b"x")
    await svc.aclose()
    assert "json" in str(exc.value).lower()
