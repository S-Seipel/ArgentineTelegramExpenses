from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config.settings import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _build_engine(url: str | None = None) -> Engine:
    target = url or get_settings().database_url
    connect_args: dict = {}
    if target.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(
        target,
        pool_pre_ping=True,
        future=True,
        connect_args=connect_args,
    )


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            future=True,
            expire_on_commit=False,
        )
    return _SessionLocal


def SessionLocal() -> Session:
    return get_session_factory()()


def get_db() -> Iterator[Session]:
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = get_session_factory()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    from app.categories.models import Category  # noqa: F401
    from app.expenses.models import Expense  # noqa: F401
    from app.recurring.models import RecurringExpense  # noqa: F401
    from app.budgets.models import Budget  # noqa: F401
    from app.fixed_expenses.models import (  # noqa: F401
        FixedExpense,
        FixedExpensePayment,
        MonthlyBudget,
    )

    Base.metadata.create_all(bind=get_engine())


engine = get_engine


__all__ = [
    "Base",
    "SessionLocal",
    "get_engine",
    "get_session_factory",
    "session_scope",
    "get_db",
    "init_db",
]
