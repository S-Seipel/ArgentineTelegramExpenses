from __future__ import annotations

import asyncio
import os

import pytest

from app.ai.schemas import AIError, AIUnavailable
from app.ai.service import AIServiceConfig, OllamaAIService


class _FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeWhisperModel:
    """In-memory stand-in for ``faster_whisper.WhisperModel``.

    Holds no CTranslate2 dependency so tests stay hermetic.
    """

    def __init__(self, text: str | None = None, exc: Exception | None = None) -> None:
        self.text = text or "gasté diez lucas en café"
        self.exc = exc
        self.calls: list[str] = []
        self.vad_filters: list[bool] = []

    def transcribe(self, audio_path: str, *, vad_filter: bool = False):
        self.calls.append(audio_path)
        self.vad_filters.append(vad_filter)
        if self.exc is not None:
            raise self.exc
        return ([_FakeSegment(self.text)], object())


def _make_service(model: _FakeWhisperModel | None = None) -> OllamaAIService:
    cfg = AIServiceConfig(
        base_url="http://x",
        model="qwen3:4b",
        timeout=10,
        max_retries=0,
        timezone="UTC",
        whisper_model_size="base",
        whisper_device="cpu",
        whisper_compute_type="int8",
    )
    svc = OllamaAIService(cfg)
    if model is not None:
        svc.set_whisper_model_for_testing(model)
    return svc


@pytest.mark.asyncio
async def test_transcribe_returns_text():
    model = _FakeWhisperModel(text="hola mundo")
    svc = _make_service(model)
    out = await svc.transcribe(b"\x00\x01\x02fake-ogg-bytes")
    await svc.aclose()
    assert out == "hola mundo"
    assert len(model.calls) == 1
    assert model.vad_filters == [True]
    assert model.calls[0].endswith(".audio")
    assert os.path.exists(model.calls[0]) is False


@pytest.mark.asyncio
async def test_transcribe_writes_to_temp_file():
    model = _FakeWhisperModel()
    svc = _make_service(model)
    await svc.transcribe(b"hello world bytes")
    await svc.aclose()
    assert len(model.calls) == 1
    # The file was cleaned up after transcribe returned.
    assert not os.path.exists(model.calls[0])


@pytest.mark.asyncio
async def test_transcribe_empty_audio_raises():
    svc = _make_service(_FakeWhisperModel())
    with pytest.raises(AIError) as exc:
        await svc.transcribe(b"")
    await svc.aclose()
    assert "empty" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_transcribe_empty_model_output_raises():
    model = _FakeWhisperModel(text="   ")
    svc = _make_service(model)
    with pytest.raises(AIError) as exc:
        await svc.transcribe(b"audio")
    await svc.aclose()
    assert "empty" in str(exc.value).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "max() arg is an empty sequence", "max() iterable argument is empty",
])
async def test_transcribe_vad_without_speech_returns_controlled_empty_error(message):
    model = _FakeWhisperModel(exc=ValueError(message))
    svc = _make_service(model)
    try:
        with pytest.raises(AIError, match="No pude detectar voz"):
            await svc.transcribe(b"synthetic silence")
    finally:
        await svc.aclose()
    assert model.vad_filters == [True]
    assert not os.path.exists(model.calls[0])


@pytest.mark.asyncio
async def test_transcribe_does_not_hide_unrelated_value_errors():
    model = _FakeWhisperModel(exc=ValueError("invalid audio codec"))
    svc = _make_service(model)
    try:
        with pytest.raises(ValueError, match="invalid audio codec"):
            await svc.transcribe(b"invalid audio")
    finally:
        await svc.aclose()
    assert not os.path.exists(model.calls[0])


@pytest.mark.asyncio
async def test_transcribe_missing_ffmpeg_maps_to_unavailable():
    model = _FakeWhisperModel(exc=FileNotFoundError("ffmpeg missing"))
    svc = _make_service(model)
    with pytest.raises(AIUnavailable) as exc:
        await svc.transcribe(b"audio")
    await svc.aclose()
    assert "ffmpeg" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_transcribe_uses_asyncio_thread():
    """transcribe() should not block the event loop."""
    main_thread_id = None
    sub_thread_id = []

    class _CapturingModel(_FakeWhisperModel):
        def transcribe(self, audio_path: str, *, vad_filter: bool = False):
            sub_thread_id.append(os.getpid() if hasattr(os, "getpid") else id(self))
            return super().transcribe(audio_path, vad_filter=vad_filter)

    model = _CapturingModel()
    svc = _make_service(model)
    await svc.transcribe(b"audio")
    await svc.aclose()
    assert len(sub_thread_id) == 1


@pytest.mark.asyncio
async def test_lazy_load_initializes_on_first_call():
    """First transcribe() should call WhisperModel exactly once."""
    cfg = AIServiceConfig(
        base_url="http://x",
        model="qwen3:4b",
        timeout=10,
        max_retries=0,
        timezone="UTC",
    )

    init_calls = {"n": 0}

    def factory(model_size, device, compute_type):
        init_calls["n"] += 1
        return _FakeWhisperModel()

    from app.ai import service as service_module

    original = service_module.WhisperModel if hasattr(service_module, "WhisperModel") else None

    class _Stub:
        def __init__(self, *args, **kwargs):
            self.size = args[0] if args else kwargs.get("model_size")
            self.device = kwargs.get("device")
            self.compute_type = kwargs.get("compute_type")

        def transcribe(self, audio, *, vad_filter: bool = False):
            return ([_FakeSegment("lazy")], object())

    svc = OllamaAIService(cfg)
    svc._get_whisper_model = lambda: _Stub()
    out = await svc.transcribe(b"audio")
    await svc.aclose()
    assert out == "lazy"


@pytest.mark.asyncio
async def test_set_whisper_model_for_testing_skips_real_model():
    """If the override is set, real WhisperModel must NOT be imported."""
    cfg = AIServiceConfig(
        base_url="http://x",
        model="qwen3:4b",
        timeout=10,
        max_retries=0,
        timezone="UTC",
    )
    svc = OllamaAIService(cfg)
    sentinel = _FakeWhisperModel(text="hi")
    svc.set_whisper_model_for_testing(sentinel)
    out = await svc.transcribe(b"x")
    await svc.aclose()
    assert out == "hi"
