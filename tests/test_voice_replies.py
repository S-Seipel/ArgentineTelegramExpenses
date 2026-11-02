"""Optional local voice synthesis never sends text to an external provider."""
import asyncio
import os
from pathlib import Path
import sys

import pytest

from app.media._process import ProcessFailure, run_local_process
from app.media.voice import LocalVoiceService, VoiceError, VoiceUnavailable, prepare_voice_text


def test_voice_text_removes_decoration_and_links_without_losing_amounts():
    spoken, truncated = prepare_voice_text("✅ *Guardado*: café $ 1.234,50 ARS. [Detalle](https://local.test/private)")
    assert spoken == "Guardado: café $ 1.234,50 ARS. Detalle"
    assert not truncated


def test_long_voice_reply_is_bounded_and_reports_truncation():
    spoken, truncated = prepare_voice_text("palabra " * 300)
    assert len(spoken) <= 1200
    assert truncated is True


@pytest.mark.parametrize("text", ["", "   ", "✅✅", None])
def test_empty_or_unreadable_voice_text_is_rejected(text):
    with pytest.raises(VoiceError):
        prepare_voice_text(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("language,voice", [("es", "es"), ("en", "en-us"), ("pt", "pt-br")])
async def test_synthesis_is_local_opus_and_cleans_files(monkeypatch, language, voice):
    from app.media import voice as module
    calls, paths = [], []
    async def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "espeak-ng":
            path = Path(args[args.index("-w") + 1])
            path.write_bytes(b"RIFF" + b"\x00" * 100)
        else:
            path = Path(args[-1])
            path.write_bytes(b"OggS" + b"test opus payload")
        paths.append(path)
    monkeypatch.setattr(module, "run_local_process", run)
    reply = await LocalVoiceService().synthesize("Disponible 100 ARS", language)
    assert reply.audio.startswith(b"OggS")
    assert reply.language == language
    assert reply.filename == "reply.ogg"
    assert reply.mime_type == "audio/ogg"
    assert calls[0][0][1:3] == ["-v", voice]
    assert calls[0][1]["input_bytes"] == b"Disponible 100 ARS"
    assert "Disponible 100 ARS" not in calls[0][0]  # Financial text is not an argv diagnostic.
    assert "libopus" in calls[1][0]
    assert "voip" in calls[1][0]
    assert all(not path.exists() for path in paths)


@pytest.mark.asyncio
async def test_invalid_locale_does_not_run_any_process(monkeypatch):
    from app.media import voice as module
    async def unexpected(*args, **kwargs):
        raise AssertionError("No process may run for invalid input")
    monkeypatch.setattr(module, "run_local_process", unexpected)
    with pytest.raises(VoiceError):
        await LocalVoiceService().synthesize("hola", "es;rm -rf")


@pytest.mark.asyncio
@pytest.mark.parametrize("error,expected", [(FileNotFoundError(), VoiceUnavailable), (asyncio.TimeoutError(), VoiceUnavailable), (ProcessFailure(1), VoiceUnavailable), (asyncio.CancelledError(), asyncio.CancelledError)])
async def test_speech_errors_and_cancellation_cleanup(monkeypatch, error, expected):
    from app.media import voice as module
    paths = []
    async def run(args, **kwargs):
        path = Path(args[args.index("-w") + 1])
        path.write_bytes(b"RIFF" + b"\x00" * 100)
        paths.append(path)
        raise error
    monkeypatch.setattr(module, "run_local_process", run)
    with pytest.raises(expected):
        await LocalVoiceService().synthesize("hola")
    assert paths and not paths[0].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"", b"NOT OGG", b"OggS" + b"x" * (2 * 1024 * 1024)])
async def test_invalid_or_oversized_encoded_audio_is_rejected(monkeypatch, payload):
    from app.media import voice as module
    async def run(args, **kwargs):
        if args[0] == "espeak-ng":
            Path(args[args.index("-w") + 1]).write_bytes(b"RIFF" + b"\x00" * 100)
        else:
            Path(args[-1]).write_bytes(payload)
    monkeypatch.setattr(module, "run_local_process", run)
    with pytest.raises(VoiceUnavailable):
        await LocalVoiceService().synthesize("hola")


@pytest.mark.asyncio
async def test_native_runner_timeout_kills_process_and_redacts_environment(tmp_path, monkeypatch):
    pid_file = tmp_path / "child.pid"
    environment = tmp_path / "env.txt"
    monkeypatch.setenv("MEDIA_TEST_PRIVATE_KEY", "do-not-inherit-this")
    code = ("import os,time;from pathlib import Path;"
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()));"
            f"Path({str(environment)!r}).write_text(os.getenv('MEDIA_TEST_PRIVATE_KEY','absent'));"
            "time.sleep(10)")
    with pytest.raises(asyncio.TimeoutError):
        await run_local_process([sys.executable, "-c", code], timeout=0.5)
    assert environment.read_text() == "absent"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


@pytest.mark.asyncio
async def test_native_runner_cancellation_kills_process(tmp_path):
    pid_file = tmp_path / "child.pid"
    code = ("import os,time;from pathlib import Path;"
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()));time.sleep(10)")
    task = asyncio.create_task(run_local_process([sys.executable, "-c", code], timeout=30))
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


@pytest.mark.asyncio
async def test_native_processing_limit_is_shared_across_adapter_instances(monkeypatch):
    import app.media._process as module
    current, peak = 0, 0
    async def bounded_job(*args, **kwargs):
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        try:
            await asyncio.sleep(0.01)
        finally:
            current -= 1
    monkeypatch.setattr(module, "_run_local_process", bounded_job)
    await asyncio.gather(*(run_local_process(["native"], timeout=1) for _ in range(8)))
    assert peak == 2
