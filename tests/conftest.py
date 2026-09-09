"""Shared pytest fixtures."""
from __future__ import annotations

import importlib
import os
from datetime import date
from decimal import Decimal
from typing import Iterator

import pytest


os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "123456")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434")
os.environ.setdefault("OLLAMA_MODEL", "test-model")


@pytest.fixture
def settings(monkeypatch):
    from app.config import settings as settings_module

    importlib.reload(settings_module)
    settings_module.get_settings.cache_clear()
    return settings_module.get_settings()


@pytest.fixture
def in_memory_db(monkeypatch, settings):
    """Rebuild SQLAlchemy engine against a shared in-memory SQLite for tests.

    Uses ``StaticPool`` so every checkout from the pool reuses the same
    single connection. Otherwise, with plain ``:memory:`` SQLite, each
    new connection (e.g., FastAPI dependency injection in TestClient)
    would see a fresh empty database.
    """
    from sqlalchemy import create_engine
    from sqlalchemy import event
    from sqlalchemy.pool import StaticPool

    import app.database.database as db_module

    def _enable_fks(dbapi_con, _):
        cur = dbapi_con.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    engine_ = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    event.listen(engine_, "connect", _enable_fks)

    db_module._engine = engine_
    db_module._SessionLocal = None
    db_module.init_db()

    from app.categories.models import Category  # noqa: F401
    from app.categories.registry import hydrate
    from app.expenses.models import Expense  # noqa: F401
    from app.categories.seed import iter_seed_rows

    with db_module.get_session_factory()() as session:
        from app.categories.repository import CategoryRepository, ensure_seed

        repo = CategoryRepository(session)
        ensure_seed(repo)

        rows = (
            session.query(Category)
            .filter(Category.is_active.is_(True))
            .all()
        )

    from app.categories.categories import CategoryRow

    by_name: dict[tuple, CategoryRow] = {}
    for cat in rows:
        by_name[(cat.id, cat.name)] = CategoryRow(
            name=cat.name,
            icon=cat.icon,
            parent=None,
        )
    for cat in rows:
        if cat.parent_id is None:
            continue
        for parent in rows:
            if parent.id == cat.parent_id:
                cr = by_name[(cat.id, cat.name)]
                object.__setattr__(cr, "parent", parent.name)
                break

    hydrate(list(by_name.values()))

    yield db_module

    db_module._engine = None
    db_module._SessionLocal = None
