"""Offline voice replies through eSpeak NG and FFmpeg Opus encoding."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
from pathlib import Path
import re
import tempfile
import unicodedata

from app.media._process import ProcessFailure, run_local_process


MAX_VOICE_CHARS = 1200
MAX_VOICE_BYTES = 2 * 1024 * 1024
MAX_WAVE_BYTES = 25 * 1024 * 1024
_VOICES = {"es": "es", "en": "en-us", "pt": "pt-br"}


class VoiceError(ValueError):
    """Text/locale cannot be synthesized safely."""


class VoiceUnavailable(VoiceError):
    """The local speech backend is unavailable; the text reply still works."""


@dataclass(frozen=True)
class VoiceReply:
    audio: bytes
    language: str
    truncated: bool = False
    filename: str = "reply.ogg"
    mime_type: str = "audio/ogg"


def prepare_voice_text(text: str) -> tuple[str, bool]:
    if not isinstance(text, str) or not text.strip():
        raise VoiceError("No hay texto para leer.")
    # Prefer link labels to private URLs. Markdown decoration and emojis
    # should not make a numeric confirmation unnecessarily noisy.
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = "".join(char for char in cleaned if unicodedata.category(char) not in {"So", "Cs", "Cc"} or char in "\n\t")
    cleaned = re.sub(r"[`*_#]", "", cleaned)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        raise VoiceError("No hay texto legible para leer.")
    truncated = len(cleaned) > MAX_VOICE_CHARS
    if truncated:
        cleaned = cleaned[:MAX_VOICE_CHARS].rsplit(" ", 1)[0]
    return cleaned, truncated


class LocalVoiceService:
    """No network calls; the caller must check the user's voice preference."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("Speech timeout must be between 0 and 60 seconds")
        self.timeout = timeout
        self._slots = asyncio.Semaphore(1)

    async def synthesize(self, text: str, language: str = "es") -> VoiceReply:
        if language not in _VOICES:
            raise VoiceError("Elegí español, inglés o portugués para la voz.")
        spoken, truncated = prepare_voice_text(text)
        async with self._slots:
            with tempfile.TemporaryDirectory(prefix="tge-voice-") as directory:
                wave = Path(directory) / "speech.wav"
                encoded = Path(directory) / "reply.ogg"
                try:
                    await run_local_process(
                        ["espeak-ng", "-v", _VOICES[language], "-s", "165", "-w", str(wave), "--stdin"],
                        timeout=self.timeout, input_bytes=spoken.encode("utf-8"),
                    )
                    if not wave.is_file() or not 44 <= wave.stat().st_size <= MAX_WAVE_BYTES:
                        raise VoiceUnavailable("La síntesis local no produjo un audio válido.")
                    await run_local_process(
                        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(wave),
                         "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "32k", "-application", "voip",
                         "-t", "120", str(encoded)],
                        timeout=self.timeout,
                    )
                except FileNotFoundError as exc:
                    raise VoiceUnavailable("La voz local necesita eSpeak NG y FFmpeg instalados. La respuesta de texto sigue disponible.") from exc
                except asyncio.TimeoutError as exc:
                    raise VoiceUnavailable("La voz local tardó demasiado. La respuesta de texto sigue disponible.") from exc
                except ProcessFailure as exc:
                    raise VoiceUnavailable("No pude generar la voz local. La respuesta de texto sigue disponible.") from exc
                if not encoded.is_file() or not 4 < encoded.stat().st_size <= MAX_VOICE_BYTES:
                    raise VoiceUnavailable("El audio generado no tiene un tamaño válido.")
                audio = encoded.read_bytes()
                if not audio.startswith(b"OggS"):
                    raise VoiceUnavailable("El audio generado no es un OGG válido.")
            return VoiceReply(audio=audio, language=language, truncated=truncated)
