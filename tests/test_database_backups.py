"""Backup failures, retention boundaries, scheduling, and installation safety."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest

from scripts.backup_database import (
    BackupConfig, BackupError, perform_backup, scheduled_cutoff,
)
from scripts import install_backups


class FakeDocker:
    def __init__(self, fail=None):
        self.fail = fail
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if "pg_dump" in command:
            kwargs["stdout"].write(b"PGDMP-private-test-data")
            return subprocess.CompletedProcess(command, 1 if self.fail == "dump" else 0)
        assert command[-2:] == ["pg_restore", "--list"]
        assert kwargs["stdin"].read().startswith(b"PGDMP")
        return subprocess.CompletedProcess(command, 1 if self.fail == "validation" else 0)


def _day(number=0):
    return datetime(2026, 11, 2, 9, tzinfo=timezone.utc) + timedelta(days=number)


def test_validated_backup_is_private_and_has_a_matching_sha(tmp_path, monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "secret-must-not-be-an-argument")
    docker = FakeDocker()
    config = BackupConfig(directory=tmp_path / "backups")
    result = perform_backup(config, scheduled=True, now=_day(), run=docker)
    backup = Path(result["backup"])
    metadata = json.loads(backup.with_suffix(".json").read_text())
    status = json.loads((config.directory / "backup-status.json").read_text())
    assert result["status"] == "success"
    assert metadata["validated"] == "pg_restore --list"
    assert metadata["sha256"] == hashlib.sha256(backup.read_bytes()).hexdigest()
    assert status["last_success"] == status["last_automatic_success"] == metadata
    for path in (backup, backup.with_suffix(".json"), config.directory / "backup-status.json"):
        assert path.stat().st_mode & 0o777 == 0o600
    assert config.directory.stat().st_mode & 0o777 == 0o700
    assert len(docker.commands) == 2
    assert all("secret-must-not-be-an-argument" not in " ".join(command) for command in docker.commands)
    assert "--no-password" in docker.commands[0]
    assert not list(config.directory.glob("*.partial"))


@pytest.mark.parametrize("failure", ["dump", "validation"])
def test_failed_backup_preserves_the_last_valid_copy_and_status(tmp_path, failure):
    config = BackupConfig(directory=tmp_path / "backups", retain=1)
    first = perform_backup(config, scheduled=True, now=_day(), run=FakeDocker())
    old_path = Path(first["backup"])
    old_bytes = old_path.read_bytes()
    old_success = json.loads((config.directory / "backup-status.json").read_text())["last_success"]
    with pytest.raises(BackupError):
        perform_backup(config, scheduled=True, now=_day(1), run=FakeDocker(fail=failure))
    state = json.loads((config.directory / "backup-status.json").read_text())
    assert state["status"] == "failed"
    assert state["last_success"] == state["last_automatic_success"] == old_success
    assert old_path.read_bytes() == old_bytes
    assert list(config.directory.glob("*.dump")) == [old_path]
    assert not list(config.directory.glob(".*.partial"))


def test_retention_keeps_seven_successes_and_never_prunes_manual_or_foreign_files(tmp_path):
    config = BackupConfig(directory=tmp_path / "backups")
    manual = Path(perform_backup(config, now=_day(-1), run=FakeDocker())["backup"])
    foreign = config.directory / "user-maintained.dump"
    foreign.write_bytes(b"unrelated")
    misleading = config.directory / "auto-expenses-not-our-backup.dump"
    misleading.write_bytes(b"not a validated backup pair")
    other_database = config.directory / "auto-other-database.dump"
    other_database.write_bytes(b"other database")
    automatic = [
        Path(perform_backup(config, scheduled=True, now=_day(day), run=FakeDocker())["backup"])
        for day in range(9)
    ]
    assert [path.exists() for path in automatic] == [False, False] + [True] * 7
    assert all(path.with_suffix(".json").exists() for path in automatic[-7:])
    assert not any(path.with_suffix(".json").exists() for path in automatic[:2])
    assert manual.exists() and manual.with_suffix(".json").exists()
    assert foreign.read_bytes() == b"unrelated"
    assert misleading.read_bytes() == b"not a validated backup pair"
    assert other_database.read_bytes() == b"other database"


def test_a_second_process_cannot_run_a_backup_while_the_lock_is_held(tmp_path):
    directory = tmp_path / "backups"
    directory.mkdir()
    child = subprocess.Popen([
        sys.executable, "-c",
        "import fcntl,sys; from pathlib import Path; "
        "lock=(Path(sys.argv[1])/'.backup.lock').open('a+b'); "
        "fcntl.flock(lock,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.read()",
        str(directory),
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        docker = FakeDocker()
        result = perform_backup(BackupConfig(directory=directory), scheduled=True, run=docker)
        assert result == {"status": "skipped", "reason": "backup_in_progress"}
        assert docker.commands == []
    finally:
        child.communicate(input="release", timeout=5)
    assert child.returncode == 0


def test_scheduled_mode_catches_up_and_runs_once_per_buenos_aires_boundary(tmp_path):
    config = BackupConfig(directory=tmp_path / "backups")
    docker = FakeDocker()
    before_three = datetime(2026, 11, 2, 5, 59, tzinfo=timezone.utc)
    first = perform_backup(config, scheduled=True, now=before_three, run=docker)
    assert first["status"] == "success"  # Catch up on the previous due window.
    assert scheduled_cutoff(before_three) == datetime(2026, 11, 1, 6, tzinfo=timezone.utc)
    at_three_elsewhere = datetime(2026, 11, 2, 19, tzinfo=ZoneInfo("Pacific/Auckland"))
    assert scheduled_cutoff(at_three_elsewhere) == datetime(2026, 11, 2, 6, tzinfo=timezone.utc)
    second = perform_backup(config, scheduled=True, now=at_three_elsewhere, run=docker)
    assert second["status"] == "success"
    assert second["backup"] != first["backup"]
    assert perform_backup(config, scheduled=True, now=_day(), run=docker) == {
        "status": "skipped", "reason": "already_completed"
    }
    assert len(docker.commands) == 4


def test_missing_last_automatic_copy_is_replaced_in_the_same_window(tmp_path):
    config = BackupConfig(directory=tmp_path / "backups")
    docker = FakeDocker()
    first = perform_backup(config, scheduled=True, now=_day(), run=docker)
    Path(first["backup"]).unlink()
    replacement = perform_backup(config, scheduled=True, now=_day(), run=docker)
    assert replacement["status"] == "success"
    assert Path(replacement["backup"]).is_file()
    assert replacement["backup"] != first["backup"]


def test_same_size_corruption_cannot_suppress_a_new_scheduled_copy(tmp_path):
    config = BackupConfig(directory=tmp_path / "backups")
    docker = FakeDocker()
    first = perform_backup(config, scheduled=True, now=_day(), run=docker)
    corrupted = Path(first["backup"])
    original = corrupted.read_bytes()
    corrupted.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    assert corrupted.stat().st_size == first["bytes"]
    replacement = perform_backup(config, scheduled=True, now=_day(), run=docker)
    assert replacement["status"] == "success"
    assert replacement["backup"] != first["backup"]
    assert len(docker.commands) == 4
    assert corrupted.name + ": checksum_mismatch" in replacement["retention_errors"]


def test_corrupt_copies_do_not_count_toward_successful_retention(tmp_path):
    config = BackupConfig(directory=tmp_path / "backups")
    copies = [
        Path(perform_backup(config, scheduled=True, now=_day(day), run=FakeDocker())["backup"])
        for day in range(7)
    ]
    corrupt = copies[-1]
    content = corrupt.read_bytes()
    corrupt.write_bytes(bytes([content[0] ^ 1]) + content[1:])
    newest = perform_backup(config, scheduled=True, now=_day(7), run=FakeDocker())
    assert newest["retention_removed"] == 0
    assert all(path.exists() for path in copies)  # Six old good + one corrupt retained for inspection.
    assert copies[0].exists()  # A corrupt seventh copy must not evict this good one.
    assert corrupt.name + ": checksum_mismatch" in newest["retention_errors"]


def test_launch_agent_uses_durable_paths_and_an_explicit_timezone_guard(tmp_path):
    support = tmp_path / "Application Support" / "telegram-expenses"
    agent = install_backups.launch_agent("/usr/local/bin/python3", "/usr/local/bin/docker", support,
                                        docker_context="orbstack")
    assert agent["RunAtLoad"] is True
    assert agent["StartCalendarInterval"] == [{"Minute": value} for value in (0, 15, 30, 45)]
    assert agent["EnvironmentVariables"]["TZ"] == "America/Argentina/Buenos_Aires"
    assert agent["EnvironmentVariables"]["DOCKER_CONTEXT"] == "orbstack"
    assert agent["ProgramArguments"][:2] == ["/usr/local/bin/python3", str(support / "bin" / "backup_database.py")]
    assert "--scheduled" in agent["ProgramArguments"]
    assert agent["Umask"] == 0o077


def test_install_is_durable_private_and_does_not_enable_by_default(tmp_path):
    support = tmp_path / "support"
    launch_agents = tmp_path / "LaunchAgents"
    result = install_backups.install(
        python=sys.executable, docker="/bin/echo", support=support,
        launch_agents=launch_agents, docker_context="test-context",
    )
    assert result["enabled"] is False
    installed = Path(result["script"])
    assert installed.read_bytes() == Path(install_backups.__file__).with_name("backup_database.py").read_bytes()
    assert installed.stat().st_mode & 0o777 == 0o600
    plist = Path(result["plist"])
    assert plist.stat().st_mode & 0o777 == 0o600
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"][1] == str(installed)
    assert plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["DOCKER_CONTEXT"] == "test-context"
    assert (support / "backups").stat().st_mode & 0o777 == 0o700


def test_default_installer_only_previews_and_never_activates(tmp_path, monkeypatch, capsys):
    support = tmp_path / "support"
    agents = tmp_path / "LaunchAgents"
    monkeypatch.setattr(sys, "argv", ["install_backups.py", "--support-directory", str(support),
                                     "--launch-agents-directory", str(agents)])
    assert install_backups.main() == 0
    assert json.loads(capsys.readouterr().out)["installed"] is False
    assert not support.exists() and not agents.exists()
    monkeypatch.setattr(sys, "argv", ["install_backups.py", "--enable"])
    with pytest.raises(SystemExit) as error:
        install_backups.main()
    assert error.value.code == 2
