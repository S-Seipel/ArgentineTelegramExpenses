#!/usr/bin/env python3
"""Private, validated PostgreSQL backups through the existing Docker container.

The scheduled mode makes one automatic backup per 03:00 Buenos Aires
window, including a catch-up after sleep or login. Manual copies are kept
outside automatic retention. This program uses only the Python stdlib.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo


BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")
DEFAULT_DIRECTORY = (
    Path.home() / "Library" / "Application Support" / "telegram-expenses" / "backups"
)


class BackupError(RuntimeError):
    """A backup did not complete; previous validated copies remain intact."""


@dataclass(frozen=True)
class BackupConfig:
    directory: Path = DEFAULT_DIRECTORY
    docker: str = "docker"
    container: str = "telegram_expenses_postgres"
    user: str = "expenses"
    database: str = "expenses"
    retain: int = 7
    timeout: float = 300

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.container):
            raise ValueError("Invalid Docker container name")
        for value in (self.user, self.database):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$-]*", value):
                raise ValueError("Invalid PostgreSQL user or database name")
        if self.retain < 1 or self.timeout <= 0:
            raise ValueError("Retention and timeout must be positive")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Backup times must include a timezone")
    return value.astimezone(timezone.utc)


def scheduled_cutoff(now: datetime) -> datetime:
    """Latest daily 03:00 Buenos Aires boundary, independent of the Mac TZ."""
    local = _aware(now).astimezone(BUENOS_AIRES)
    cutoff = datetime.combine(local.date(), time(3), tzinfo=BUENOS_AIRES)
    if local < cutoff:
        cutoff -= timedelta(days=1)
    return cutoff.astimezone(timezone.utc)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_json(path: Path, payload: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


@contextmanager
def backup_lock(directory: Path):
    """Yield False when another backup owns the lock; never wait on it."""
    descriptor = os.open(directory / ".backup.lock", os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(descriptor, 0o600)
    locked = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            pass
        yield locked
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_current_success(config: BackupConfig, success: dict, cutoff: datetime) -> bool:
    if not isinstance(success, dict):
        return False
    if success.get("scheduled_for") != cutoff.isoformat():
        return False
    filename = success.get("filename", "")
    if not isinstance(filename, str) or Path(filename).name != filename:
        return False
    path = config.directory / filename
    try:
        return (
            filename.startswith(f"auto-{config.database}-")
            and not path.is_symlink()
            and path.is_file()
            and path.stat().st_size == success.get("bytes")
            and _sha256(path) == success.get("sha256")
        )
    except OSError:
        return False


def _prune_automatic(config: BackupConfig) -> tuple[list[str], list[str]]:
    """Prune only successful metadata/dump pairs in this exact namespace."""
    prefix = f"auto-{config.database}-"
    pattern = re.compile(re.escape(prefix) + r"\d{8}T\d{12}Z-[0-9a-f]{32}\.dump")
    successes, errors = [], []
    for metadata_path in config.directory.glob(f"{prefix}*.json"):
        if metadata_path.is_symlink():
            continue
        metadata = _read_json(metadata_path)
        filename = metadata.get("filename", "")
        if not isinstance(filename, str) or not pattern.fullmatch(filename):
            continue
        dump_path = config.directory / filename
        if (
            metadata.get("format_version") != 1
            or metadata.get("kind") != "automatic"
            or metadata.get("database") != config.database
            or metadata_path.name != dump_path.with_suffix(".json").name
            or dump_path.is_symlink()
            or not dump_path.is_file()
            or dump_path.stat().st_size != metadata.get("bytes")
            or not re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("sha256", "")))
        ):
            continue
        try:
            completed = _aware(datetime.fromisoformat(metadata["completed_at"]))
        except (KeyError, TypeError, ValueError):
            continue
        try:
            if _sha256(dump_path) != metadata["sha256"]:
                errors.append(f"{filename}: checksum_mismatch")
                continue
        except OSError as error:
            errors.append(f"{filename}: {type(error).__name__}")
            continue
        successes.append((completed, filename, dump_path, metadata_path))
    successes.sort(reverse=True)
    removed = []
    for _, filename, dump_path, metadata_path in successes[config.retain:]:
        try:
            dump_path.unlink()
            metadata_path.unlink()
            removed.append(filename)
        except OSError as error:
            errors.append(f"{filename}: {type(error).__name__}")
    return removed, errors


def perform_backup(
    config: BackupConfig,
    *,
    scheduled: bool = False,
    now: datetime | None = None,
    run: Callable = subprocess.run,
) -> dict:
    config.validate()
    config.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    config.directory.chmod(0o700)
    started = _aware(now or datetime.now(timezone.utc))
    cutoff = scheduled_cutoff(started)
    with backup_lock(config.directory) as locked:
        if not locked:
            return {"status": "skipped", "reason": "backup_in_progress"}
        status_path = config.directory / "backup-status.json"
        state = _read_json(status_path)
        if scheduled and _is_current_success(
            config, state.get("last_automatic_success", {}), cutoff
        ):
            return {"status": "skipped", "reason": "already_completed"}

        kind = "automatic" if scheduled else "manual"
        prefix = "auto" if scheduled else "manual"
        identifier = started.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex
        filename = f"{prefix}-{config.database}-{identifier}.dump"
        destination = config.directory / filename
        fd, temporary = tempfile.mkstemp(prefix=f".{filename}.", suffix=".partial", dir=config.directory)
        temporary_path = Path(temporary)
        state.update({"format_version": 1, "last_attempt_at": started.isoformat()})
        try:
            with os.fdopen(fd, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                result = run(
                    [config.docker, "exec", config.container, "pg_dump", "--format=custom",
                     "--no-password", "--username", config.user, "--dbname", config.database],
                    stdout=output, stderr=subprocess.PIPE, timeout=config.timeout, check=False,
                )
                if result.returncode:
                    raise BackupError(f"pg_dump failed (exit {result.returncode}); check Docker and the database")
                output.flush()
                os.fsync(output.fileno())
            if temporary_path.stat().st_size == 0:
                raise BackupError("pg_dump produced an empty backup")
            with temporary_path.open("rb") as source:
                result = run(
                    [config.docker, "exec", "-i", config.container, "pg_restore", "--list"],
                    stdin=source, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    timeout=config.timeout, check=False,
                )
            if result.returncode:
                raise BackupError(f"pg_restore validation failed (exit {result.returncode})")
            completed = _aware(now or datetime.now(timezone.utc))
            metadata = {
                "format_version": 1, "kind": kind, "database": config.database,
                "container": config.container, "filename": filename,
                "started_at": started.isoformat(), "completed_at": completed.isoformat(),
                "scheduled_for": cutoff.isoformat(), "bytes": temporary_path.stat().st_size,
                "sha256": _sha256(temporary_path), "validated": "pg_restore --list",
            }
            os.replace(temporary_path, destination)
            _atomic_json(destination.with_suffix(".json"), metadata)
            state.update({"status": "success", "last_success": metadata})
            if scheduled:
                state["last_automatic_success"] = metadata
            _atomic_json(status_path, state)
            removed, errors = _prune_automatic(config)
            state.update({"retention": {"keep": config.retain, "removed": removed, "errors": errors}})
            _atomic_json(status_path, state)
            return {"status": "success", "backup": str(destination), "sha256": metadata["sha256"],
                    "bytes": metadata["bytes"], "retention_removed": len(removed),
                    "retention_errors": errors}
        except Exception as error:
            state.update({"status": "failed", "last_failure": {
                "at": started.isoformat(), "type": type(error).__name__,
                "message": str(error) if isinstance(error, BackupError) else "Backup did not complete",
            }})
            try:
                _atomic_json(status_path, state)
            except OSError:
                pass  # Preserve the previous atomic status if the disk is unavailable.
            if isinstance(error, BackupError):
                raise
            raise BackupError(f"Backup did not complete ({type(error).__name__})") from error
        finally:
            temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--container", default="telegram_expenses_postgres")
    parser.add_argument("--user", default="expenses")
    parser.add_argument("--database", default="expenses")
    parser.add_argument("--retain", type=int, default=7)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--scheduled", action="store_true", help="One automatic copy per 03:00 Buenos Aires window")
    args = parser.parse_args()
    config = BackupConfig(directory=args.directory.expanduser(), docker=args.docker,
                          container=args.container, user=args.user, database=args.database,
                          retain=args.retain, timeout=args.timeout)
    try:
        result = perform_backup(config, scheduled=args.scheduled)
    except (BackupError, OSError, ValueError) as error:
        print(json.dumps({"status": "failed", "message": str(error)}), file=sys.stderr)
        return 1
    if not (args.scheduled and result["status"] == "skipped"):
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
