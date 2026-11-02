"""Bounded, cancellable subprocesses without shell or sensitive diagnostics."""
from __future__ import annotations

import asyncio
import os
import weakref


_LOOP_SLOTS = weakref.WeakKeyDictionary()


def _process_slots() -> asyncio.Semaphore:
    # Share the native-process cap even when an HTTP handler creates a fresh
    # adapter per request. Weak references avoid retaining short-lived test
    # event loops; active/waiting callers themselves keep the semaphore alive.
    loop = asyncio.get_running_loop()
    reference = _LOOP_SLOTS.get(loop)
    slots = reference() if reference is not None else None
    if slots is None:
        slots = asyncio.Semaphore(2)
        _LOOP_SLOTS[loop] = weakref.ref(slots)
    return slots


class ProcessFailure(RuntimeError):
    def __init__(self, returncode: int):
        self.returncode = returncode
        super().__init__("Local media process failed")


async def _run_local_process(
    args: list[str], *, timeout: float, input_bytes: bytes | None = None,
) -> None:
    # Native OCR/speech tools do not need Telegram tokens or provider keys.
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C.UTF-8",
        "OMP_THREAD_LIMIT": "2",
    }
    if os.environ.get("TESSDATA_PREFIX"):
        env["TESSDATA_PREFIX"] = os.environ["TESSDATA_PREFIX"]
    process = await asyncio.create_subprocess_exec(
        *args, stdin=(asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    try:
        await asyncio.wait_for(process.communicate(input_bytes), timeout=timeout)
    except BaseException:
        # Timeouts and cancellation must release the native process before
        # the TemporaryDirectory containing financial text/images is removed.
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        raise
    if process.returncode:
        raise ProcessFailure(process.returncode)


async def run_local_process(
    args: list[str], *, timeout: float, input_bytes: bytes | None = None,
) -> None:
    async with _process_slots():
        await _run_local_process(args, timeout=timeout, input_bytes=input_bytes)
