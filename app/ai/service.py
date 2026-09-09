"""Pure async AI service abstraction + Ollama implementation.

The service is responsible ONLY for:

* calling the model
* parsing JSON
* retrying when the model produces invalid output
* returning validated Pydantic objects

It MUST NOT touch the database, generate SQL, or know about Telegram.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.ai.prompts import system_prompt, today_str, user_instructions
from app.ai.schemas import (
    AIError,
    AIUnavailable,
    Intent,
    ParsedMessage,
    parse_ai_response,
)
from app.config.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class AIServiceConfig:
    base_url: str
    model: str
    timeout: float
    max_retries: int
    timezone: str
    whisper_model_size: str = "base"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    vision_model: str = "llava:7b"
    vision_min_confidence: float = 0.5

    def http_timeout(self) -> httpx.Timeout:
        """Build an explicit httpx.Timeout with sane sub-budgets.

        Local inference on CPU/MPS is slow on first call (model load) and
        can take 30–120s on subsequent calls for large system prompts.
        We allocate most of the budget to the read phase, keep the connect
        budget low, and cap write/pool to prevent zombie sockets.
        """
        total = float(self.timeout)
        return httpx.Timeout(
            connect=min(30.0, total),
            read=total,
            write=min(60.0, total),
            pool=min(30.0, total),
        )


class AIService(Protocol):
    async def interpret(
        self, message: str, *, user_context: str | None = None
    ) -> ParsedMessage: ...

    async def transcribe(self, audio: bytes) -> str: ...

    async def describe_image(self, image: bytes) -> dict: ...


class OllamaAIService:
    """Concrete implementation that talks to a local Ollama instance."""

    def __init__(self, cfg: AIServiceConfig, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self._owns_client = client is None
        if client is None:
            timeout = cfg.http_timeout()
            self._client = httpx.AsyncClient(
                timeout=timeout,
                headers={"Content-Type": "application/json"},
            )
            logger.info(
                "OllamaAIService ready: url=%s model=%s whisper=%s/%s "
                "timeout=%s",
                cfg.base_url,
                cfg.model,
                cfg.whisper_model_size,
                cfg.whisper_compute_type,
                cfg.timeout,
            )
        else:
            self._client = client
        self._whisper_model = None
        self._whisper_model_override = None

    def set_whisper_model_for_testing(self, model) -> None:
        """Inject a pre-built WhisperModel-compatible object.

        Used by tests to bypass the real ``faster_whisper.WhisperModel`` and
        the model download that comes with it.
        """
        self._whisper_model_override = model

    def _get_whisper_model(self):
        if self._whisper_model_override is not None:
            return self._whisper_model_override
        if self._whisper_model is None:
            from faster_whisper import WhisperModel

            self._whisper_model = WhisperModel(
                self.cfg.whisper_model_size,
                device=self.cfg.whisper_device,
                compute_type=self.cfg.whisper_compute_type,
            )
            logger.info(
                "Whisper model loaded: size=%s device=%s compute=%s",
                self.cfg.whisper_model_size,
                self.cfg.whisper_device,
                self.cfg.whisper_compute_type,
            )
        return self._whisper_model

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "OllamaAIService":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def transcribe(self, audio: bytes) -> str:
        import os
        import tempfile

        if not audio:
            raise AIError("Empty audio payload")

        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".audio", delete=False
            ) as f:
                f.write(audio)
                tmp_path = f.name
            model = self._get_whisper_model()

            def _run() -> str:
                segments, _info = model.transcribe(tmp_path)
                return " ".join(seg.text for seg in segments).strip()

            try:
                text = await asyncio.to_thread(_run)
            except FileNotFoundError as exc:
                raise AIUnavailable(
                    "ffmpeg no disponible para decodificar el audio."
                ) from exc
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        if not text:
            raise AIError("Whisper returned empty transcription")
        return text

    async def describe_image(self, image: bytes) -> dict:
        """Send a receipt/ticket image to the vision model and parse the JSON.

        Returns a dict with at least ``confidence``, ``name``, ``amount``,
        ``currency``, ``date``, ``category`` and ``needs_clarification``.
        Raises ``AIUnavailable`` on transport failures and ``AIError`` for
        malformed responses or missing model.
        """
        import base64
        import time

        if not image:
            raise AIError("Empty image payload")
        encoded = base64.b64encode(image).decode("ascii")
        prompt = (
            "Sos un asistente que extrae datos de tickets / facturas de "
            "Argentina. Devolvés SOLO un objeto JSON con: name (comercio o "
            "descripción corta), amount (número decimal positivo), currency "
            "(ARS, USD u otra), date (YYYY-MM-DD o null si no se ve), "
            "category (una hoja válida del árbol: Comida, Restaurante, "
            "Café, Supermercado, Transporte, Uber, Taxi, Combustible, "
            "Transporte público, Juegos, Cine, Suscripciones, Salidas, "
            "Ropa, Tecnología, Salud, Educación, Hogar, Viajes, Regalos, "
            "Servicios, Otros), confidence (0-1), needs_clarification "
            "(boolean) y clarification_question (string|null). Si no podés "
            "leer algún campo, devolvé null y bajá confidence."
        )
        payload = {
            "model": self.cfg.vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [encoded],
                }
            ],
            "stream": False,
            "format": "json",
            "think": False,
            "keep_alive": "30m",
            "options": {
                "temperature": 0.1,
                "num_ctx": 2048,
                "num_predict": 512,
            },
        }
        url = f"{self.cfg.base_url.rstrip('/')}/api/chat"
        start = time.perf_counter()
        try:
            response = await self._client.post(url, json=payload)
        except httpx.ConnectError as exc:
            raise AIUnavailable(
                f"Ollama is unreachable at {self.cfg.base_url}: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            logger.warning(
                "Vision timeout after %.1fs", time.perf_counter() - start
            )
            raise AIUnavailable("Vision request timed out") from exc
        elapsed = time.perf_counter() - start
        if response.status_code == 404:
            raise AIError(
                f"Modelo de visión no encontrado. "
                f"Ejecutá: ollama pull {self.cfg.vision_model}"
            )
        if response.status_code != 200:
            raise AIError(
                f"Vision returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        data = response.json()
        content = (
            data.get("message", {}).get("content")
            or data.get("response")
        )
        if not content:
            raise AIError("Vision returned empty content")
        text = str(content).strip()
        try:
            parsed = _extract_json(text)
        except AIError as exc:
            raise AIError(f"Vision JSON parse failed: {exc}") from exc
        if not isinstance(parsed, dict):
            raise AIError("Vision response was not a JSON object")
        logger.info(
            "Vision ok: %.2fs (confidence=%s)",
            elapsed,
            parsed.get("confidence"),
        )
        # Normalize shape — accept missing keys gracefully.
        parsed.setdefault("name", None)
        parsed.setdefault("amount", None)
        parsed.setdefault("currency", "ARS")
        parsed.setdefault("date", None)
        parsed.setdefault("category", None)
        parsed.setdefault("confidence", 0.0)
        parsed.setdefault("needs_clarification", False)
        parsed.setdefault("clarification_question", None)
        return parsed

    async def interpret(
        self, message: str, *, user_context: str | None = None
    ) -> ParsedMessage:
        if not message or not message.strip():
            raise AIError("Empty message")

        today = today_str()
        system_msg = system_prompt(today, self.cfg.timezone)
        user_msg = user_instructions(today, self.cfg.timezone)
        if user_context:
            user_msg = f"{user_msg}\n\nContexto adicional: {user_context}"
        user_msg = f"{user_msg}\n\nMensaje del usuario:\n{message}"

        last_error: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                raw = await self._call_model(system_msg, user_msg)
                intent = parse_ai_response(raw)
                return ParsedMessage(intent=intent)
            except AIUnavailable:
                raise
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                last_error = exc
                logger.error("Ollama HTTP error: %s", exc)
                raise AIUnavailable(
                    f"Ollama is unreachable at {self.cfg.base_url}: {exc}"
                ) from exc
            except AIError as exc:
                last_error = exc
                logger.warning(
                    "AI returned invalid JSON (attempt %s/%s): %s",
                    attempt + 1,
                    self.cfg.max_retries + 1,
                    exc,
                )
                if attempt < self.cfg.max_retries:
                    repair_hint = (
                        "Tu respuesta anterior no fue un JSON válido que "
                        "cumpla el esquema. Devolvé únicamente un objeto JSON "
                        "válido, sin texto adicional, sin markdown."
                    )
                    user_msg = f"{user_msg}\n\n{repair_hint}"
                    continue
                break

        raise AIError(
            f"Could not get a valid AI response: {last_error}"
        ) from last_error

    async def _call_model(self, system: str, user: str) -> Any:
        import time

        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            "think": False,
            "keep_alive": "30m",
            "options": {
                "temperature": 0.1,
                "top_p": 0.9,
                "repeat_penalty": 1.1,
                "num_ctx": 2048,
                "num_predict": 512,
            },
        }
        url = f"{self.cfg.base_url.rstrip('/')}/api/chat"
        start = time.perf_counter()
        try:
            response = await self._client.post(url, json=payload)
        except httpx.ConnectError as exc:
            raise AIUnavailable(
                f"Ollama is unreachable at {self.cfg.base_url}: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            logger.warning(
                "Ollama timeout after %.1fs", time.perf_counter() - start
            )
            raise AIUnavailable("Ollama request timed out") from exc
        elapsed = time.perf_counter() - start
        if response.status_code != 200:
            raise AIError(
                f"Ollama returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        data = response.json()
        content = (
            data.get("message", {}).get("content")
            or data.get("response")
        )
        if not content:
            raise AIError("Ollama response did not contain content")

        text = str(content).strip()
        logger.info(
            "Ollama chat ok: %.2fs (prompt=%d, eval_count=%s)",
            elapsed,
            len(system) + len(user),
            data.get("eval_count"),
        )
        return _extract_json(text)


def _extract_json(text: str) -> Any:
    """Robust JSON extraction (handles stray markdown fences and chatter)."""
    text = text.strip()
    if not text:
        raise AIError("Empty model content")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence_match:
        candidate = fence_match.group(1)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        candidate = brace_match.group(0)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise AIError(f"Could not parse model JSON: {exc}") from exc

    raise AIError("Model did not return JSON")


def build_default_ai_service(settings: Settings) -> OllamaAIService:
    cfg = AIServiceConfig(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        timeout=settings.ollama_timeout,
        max_retries=settings.ollama_max_retries,
        timezone=settings.timezone,
        whisper_model_size=settings.whisper_model_size,
        whisper_device=settings.whisper_device,
        whisper_compute_type=settings.whisper_compute_type,
        vision_model=settings.vision_model,
        vision_min_confidence=settings.vision_min_confidence,
    )
    return OllamaAIService(cfg)


class StubAIService:
    """Deterministic AIService used by tests and as a fallback when Ollama is down."""

    def __init__(self, scripted: dict[str, Any] | None = None) -> None:
        self.scripted = scripted or {}
        self.calls: list[str] = []
        self.transcripts: list[bytes] = []
        self.descriptions: list[bytes] = []

    async def interpret(
        self, message: str, *, user_context: str | None = None
    ) -> ParsedMessage:
        self.calls.append(message)
        if message in self.scripted:
            payload = self.scripted[message]
            intent = parse_ai_response(payload)
            return ParsedMessage(intent=intent)
        default_payload = {
            "type": "unknown",
            "expenses": [],
            "query": None,
            "confidence": 0.0,
            "rationale": "stub-fallback",
        }
        intent = parse_ai_response(default_payload)
        return ParsedMessage(intent=intent)

    async def transcribe(self, audio: bytes) -> str:
        self.transcripts.append(audio)
        return ""

    async def describe_image(self, image: bytes) -> dict:
        self.descriptions.append(image)
        return {
            "needs_clarification": True,
            "confidence": 0.0,
        }

    async def aclose(self) -> None:
        return None

    async def __aenter__(self) -> "StubAIService":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None
