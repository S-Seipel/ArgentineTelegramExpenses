from __future__ import annotations

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


def _json(payload: dict, status: int = 200) -> httpx.Response:
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
        vision_model="llava:7b",
    )
    transport = _MockTransport(responder)
    client = httpx.AsyncClient(transport=transport, timeout=cfg.http_timeout())
    return OllamaAIService(cfg, client=client)


# ----------------- transcribe_receipt -----------------


@pytest.mark.asyncio
async def test_transcribe_receipt_returns_text():
    captured = {}

    def responder(request: httpx.Request):
        captured["body"] = json.loads(request.content)
        return _json(
            {"message": {"content": "TIGRE 19280.00 2026-09-08"}}
        )

    svc = _service(responder)
    out = await svc.transcribe_receipt(b"fake-image-bytes")
    await svc.aclose()
    assert out == "TIGRE 19280.00 2026-09-08"
    # Uses vision model, not text model
    assert captured["body"]["model"] == "llava:7b"
    # No format=json (we want raw text, not JSON extraction)
    assert "format" not in captured["body"] or captured["body"].get("format") != "json"
    # Image is base64-encoded
    assert len(captured["body"]["messages"][0]["images"]) == 1


@pytest.mark.asyncio
async def test_transcribe_receipt_empty_raises():
    svc = _service(lambda r: _json({"x": 1}))
    with pytest.raises(AIError):
        await svc.transcribe_receipt(b"")
    await svc.aclose()


@pytest.mark.asyncio
async def test_transcribe_receipt_404_suggests_pull():
    def responder(request):
        return _json({"error": "model 'llava:7b' not found"}, status=404)

    svc = _service(responder)
    with pytest.raises(AIError) as exc:
        await svc.transcribe_receipt(b"x")
    await svc.aclose()
    assert "ollama pull" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_transcribe_receipt_connect_error():
    def responder(request):
        raise httpx.ConnectError("nope", request=request)

    svc = _service(responder)
    with pytest.raises(AIUnavailable):
        await svc.transcribe_receipt(b"x")
    await svc.aclose()


@pytest.mark.asyncio
async def test_transcribe_receipt_timeout():
    def responder(request):
        raise httpx.ReadTimeout("slow", request=request)

    svc = _service(responder)
    with pytest.raises(AIUnavailable):
        await svc.transcribe_receipt(b"x")
    await svc.aclose()


@pytest.mark.asyncio
async def test_transcribe_receipt_500_raises():
    def responder(request):
        return _json({"error": "boom"}, status=500)

    svc = _service(responder)
    with pytest.raises(AIError):
        await svc.transcribe_receipt(b"x")
    await svc.aclose()


# ----------------- parse_receipt_text -----------------


@pytest.mark.asyncio
async def test_parse_receipt_text_returns_dict():
    captured = {}

    def responder(request):
        captured["body"] = json.loads(request.content)
        return _json(
            {"message": {"content": json.dumps({
                "merchant": "TIGRE",
                "total": 19280.00,
                "currency": "ARS",
                "date": "2026-09-08",
                "category": "Supermercado",
                "confidence": 0.95,
            })}}
        )

    svc = _service(responder)
    out = await svc.parse_receipt_text("TIGRE 19280.00 2026-09-08")
    await svc.aclose()
    assert out["merchant"] == "TIGRE"
    assert out["total"] == 19280.00
    # Uses text model, not vision model
    assert captured["body"]["model"] == "qwen3:4b"
    # format=json so the model returns valid JSON
    assert captured["body"]["format"] == "json"


@pytest.mark.asyncio
async def test_parse_receipt_text_normalizes_missing():
    def responder(request):
        return _json({"message": {"content": json.dumps({})}})

    svc = _service(responder)
    out = await svc.parse_receipt_text("nothing")
    await svc.aclose()
    assert out["merchant"] is None
    assert out["total"] is None
    assert out["currency"] == "ARS"


@pytest.mark.asyncio
async def test_parse_receipt_text_empty_raises():
    svc = _service(lambda r: _json({"x": 1}))
    with pytest.raises(AIError):
        await svc.parse_receipt_text("")
    await svc.aclose()


@pytest.mark.asyncio
async def test_parse_receipt_text_invalid_json_raises():
    def responder(request):
        return _json({"message": {"content": "not json at all"}})

    svc = _service(responder)
    with pytest.raises(AIError) as exc:
        await svc.parse_receipt_text("garbage")
    await svc.aclose()
    assert "json" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_parse_receipt_text_non_object_raises():
    def responder(request):
        return _json({"message": {"content": "[1,2,3]"}})

    svc = _service(responder)
    with pytest.raises(AIError):
        await svc.parse_receipt_text("x")
    await svc.aclose()


@pytest.mark.asyncio
async def test_parse_receipt_text_500_raises():
    def responder(request):
        return _json({"error": "boom"}, status=500)

    svc = _service(responder)
    with pytest.raises(AIError):
        await svc.parse_receipt_text("x")
    await svc.aclose()
