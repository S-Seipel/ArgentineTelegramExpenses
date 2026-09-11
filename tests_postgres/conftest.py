"""Bootstrap for the PostgreSQL integration suite.

These tests need a real PostgreSQL with a fresh schema. They are
opt-in: they're skipped unless ``DATABASE_URL`` points to a PostgreSQL
URL AND ``TGE_PG_INTEGRATION=1`` is set, so the regular SQLite unit
suite never accidentally talks to Postgres.

Run locally with::

    TGE_PG_INTEGRATION=1 \
    DATABASE_URL=postgresql+psycopg://expenses:expenses@/expenses?host=/tmp&port=5432 \
    pytest tests_postgres -q

In CI the dedicated ``ci-postgres`` job takes care of provisioning.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest


PG_INTEGRATION_ENV = "TGE_PG_INTEGRATION"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _pg_integration_enabled() -> bool:
    return os.environ.get(PG_INTEGRATION_ENV) == "1"


def _is_postgres_url(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://", "postgresql+"))


@pytest.fixture(scope="session")
def pg_integration_enabled() -> bool:
    if not _pg_integration_enabled():
        pytest.skip(
            f"{PG_INTEGRATION_ENV}=1 not set; PostgreSQL integration tests "
            "are opt-in"
        )
    url = os.environ.get("DATABASE_URL", "")
    if not _is_postgres_url(url):
        pytest.skip("DATABASE_URL is not a PostgreSQL URL")
    return True


@pytest.fixture(scope="session")
def pg_database_url(pg_integration_enabled) -> str:
    return os.environ["DATABASE_URL"]


@pytest.fixture(scope="session")
def alembic_config_file() -> Path:
    return REPO_ROOT / "alembic.ini"


def run_alembic(*args: str, database_url: str) -> None:
    """Invoke the alembic CLI in-process via the current Python.

    Using ``sys.executable`` keeps this working in CI containers where
    the bare ``python`` may not be on PATH.
    """
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO_ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
