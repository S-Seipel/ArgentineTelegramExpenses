#!/usr/bin/env python3
"""Render or explicitly install the user's private macOS backup LaunchAgent.

The default invocation only prints the plan. --install writes the durable
script/plist, and --enable additionally bootstraps this one LaunchAgent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile


LABEL = "com.telegram-expenses.database-backup"
DEFAULT_SUPPORT = Path.home() / "Library" / "Application Support" / "telegram-expenses"


def launch_agent(python: str, docker: str, support: Path, *, docker_context: str | None = None) -> dict:
    """Wake cheaply; the script, rather than the Mac TZ, owns the daily gate."""
    agent = {
        "Label": LABEL,
        "ProgramArguments": [python, str(support / "bin" / "backup_database.py"),
                             "--scheduled", "--directory", str(support / "backups"),
                             "--docker", docker, "--retain", "7"],
        "RunAtLoad": True,
        "StartCalendarInterval": [{"Minute": minute} for minute in (0, 15, 30, 45)],
        "ProcessType": "Background", "LowPriorityIO": True, "Nice": 10,
        "Umask": 0o077,
        "EnvironmentVariables": {
            "TZ": "America/Argentina/Buenos_Aires",
            "PATH": str(Path(docker).parent) + ":/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "StandardOutPath": str(support / "logs" / "database-backup.stdout.log"),
        "StandardErrorPath": str(support / "logs" / "database-backup.stderr.log"),
    }
    if docker_context:
        agent["EnvironmentVariables"]["DOCKER_CONTEXT"] = docker_context
    return agent


def current_docker_context(docker: str) -> str:
    result = subprocess.run([docker, "context", "show"], capture_output=True, text=True, check=True)
    context = result.stdout.strip()
    if not context or "\n" in context or "\x00" in context:
        raise ValueError("Docker did not report a valid active context")
    return context


def _atomic_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def install(
    *, python: str, docker: str, support: Path, launch_agents: Path,
    source: Path | None = None, enable: bool = False, docker_context: str | None = None,
) -> dict:
    """Called only by the explicitly selected installation CLI mode."""
    for executable in (python, docker):
        if not Path(executable).is_absolute() or not os.access(executable, os.X_OK):
            raise ValueError("Python and Docker must be existing absolute executables")
    subprocess.run([python, "-c", "import fcntl, sys; from zoneinfo import ZoneInfo; assert sys.version_info >= (3, 11); ZoneInfo('America/Argentina/Buenos_Aires')"], check=True)
    docker_context = docker_context or current_docker_context(docker)
    source = source or Path(__file__).with_name("backup_database.py")
    for directory in (support, support / "bin", support / "backups", support / "logs"):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    launch_agents.mkdir(parents=True, exist_ok=True)
    script = support / "bin" / "backup_database.py"
    plist = launch_agents / f"{LABEL}.plist"
    data = source.read_bytes()
    compile(data, str(script), "exec")
    _atomic_bytes(script, data)
    _atomic_bytes(plist, plistlib.dumps(launch_agent(python, docker, support, docker_context=docker_context), sort_keys=True))
    if enable:
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True, check=False)
        subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    return {"label": LABEL, "script": str(script), "script_sha256": hashlib.sha256(data).hexdigest(),
            "plist": str(plist), "backup_directory": str(support / "backups"),
            "enabled": enable, "docker_context": docker_context, "timezone": "America/Argentina/Buenos_Aires",
            "schedule": "03:00 daily, with catch-up on login/wake", "retention": 7}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=shutil.which("python3") or sys.executable)
    parser.add_argument("--docker", default=shutil.which("docker") or "/usr/local/bin/docker")
    parser.add_argument("--docker-context", help="Pin a Docker context; installation otherwise captures the active one")
    parser.add_argument("--support-directory", type=Path, default=DEFAULT_SUPPORT)
    parser.add_argument("--launch-agents-directory", type=Path, default=Path.home() / "Library" / "LaunchAgents")
    parser.add_argument("--output", type=Path, help="Write a reviewable plist without installing")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--enable", action="store_true")
    args = parser.parse_args()
    if args.enable and not args.install:
        parser.error("--enable requires --install")
    support = args.support_directory.expanduser().absolute()
    launch_agents = args.launch_agents_directory.expanduser().absolute()
    if args.output:
        context = args.docker_context or current_docker_context(args.docker)
        _atomic_bytes(args.output.expanduser(), plistlib.dumps(launch_agent(args.python, args.docker, support, docker_context=context), sort_keys=True))
    if args.install:
        result = install(python=args.python, docker=args.docker, support=support,
                         launch_agents=launch_agents, enable=args.enable, docker_context=args.docker_context)
    else:
        result = {"installed": False, "label": LABEL,
                  "script": str(support / "bin" / "backup_database.py"),
                  "plist": str(launch_agents / f"{LABEL}.plist"),
                  "backup_directory": str(support / "backups"),
                  "timezone": "America/Argentina/Buenos_Aires",
                  "schedule": "03:00 daily; catch-up on login/wake", "retention": 7}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
