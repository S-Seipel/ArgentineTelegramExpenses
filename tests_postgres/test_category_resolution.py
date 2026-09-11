"""PostgreSQL integration: category resolution with duplicate names.

This test reproduces the original ``MultipleResultsFound`` scenario
under PostgreSQL — same-name categories under different parents —
and proves the new hierarchy-aware lookup picks the canonical row.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.expenses.service import ExpenseDraft, ExpenseService
from app.fixed_expenses.service import FixedExpenseDraft, FixedExpenseService
from app.fixed_expenses.repository import FixedExpenseRepository
from app.models_registry import register_all_models
from tests_postgres.conftest import run_alembic


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fresh_db(pg_database_url):
    engine = create_engine(pg_database_url, future=True)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()

    run_alembic("upgrade", "head", database_url=pg_database_url)
    register_all_models()
    # Hydrate the in-memory registry from the DB so the
    # hierarchy-aware category lookup can disambiguate by parent
    # name. In production this happens via ``refresh_from_db`` at
    # app startup; tests must opt in explicitly.
    from app.database.database import get_session_factory
    from app.categories.registry import refresh_from_db

    refresh_from_db(get_session_factory())
    yield pg_database_url


def test_mark_paid_with_null_category_resolves_to_top_level_otros(
    fresh_db,
):
    """A FixedExpense with ``category_id = NULL`` (its template carries
    no category) must still produce a working mark_paid against
    PostgreSQL — the migration-seeded top-level ``Otros`` row is the
    canonical fallback and there is exactly one such row."""
    from sqlalchemy import text
    from sqlalchemy.exc import MultipleResultsFound

    with session_scope() as s:
        # Verify there is exactly one top-level Otros row after the
        # migration seed. This is the precondition the lookup relies
        # on.
        otros_rows = (
            s.query(__import__(
                "app.categories.models",
                fromlist=["Category"],
            ).Category)
            .filter(
                __import__(
                    "app.categories.models",
                    fromlist=["Category"],
                ).Category.name == "Otros",
                __import__(
                    "app.categories.models",
                    fromlist=["Category"],
                ).Category.parent_id.is_(None),
            )
            .all()
        )
        assert len(otros_rows) == 1
        otros_id = otros_rows[0].id

    with session_scope() as s:
        service = FixedExpenseService(FixedExpenseRepository(s))
        bill = service.add(
            user_id=42,
            draft=FixedExpenseDraft(
                name="CLOUD-N", expected_amount=Decimal("5000"),
                category=None,
            ),
        )
        service.mark_paid(
            42, bill.id, "2026-09", actual_amount=Decimal("5000")
        )

    with session_scope() as s:
        from app.expenses.models import Expense
        mirror = (
            s.query(Expense)
            .filter(Expense.source_key.like("fixed-payment:%"))
            .one()
        )
        assert mirror.category_id == otros_id
        assert mirror.source_type == "fixed_payment"


def test_mark_paid_resolves_canonical_cafe_when_duplicate_exists(
    fresh_db,
):
    """Inject a SECOND ``Café`` row under a different parent (``Hogar``).

    The migration seed already has ``Café`` under ``Comida``. The
    registry says ``Café`` lives under ``Comida`` (the canonical
    parent), so the lookup must pick the ``(parent_id=Comida,
    name=Café)`` row and NOT the new ``(parent_id=Hogar, name=Café)``
    one.

    The old ``WHERE name = 'Café'`` lookup would have raised
    ``MultipleResultsFound`` on this fixture.
    """
    from app.categories.models import Category
    from app.expenses.models import Expense
    from sqlalchemy.exc import MultipleResultsFound

    with session_scope() as s:
        com_id = (
            s.query(Category).filter(Category.name == "Comida").one().id
        )
        hogar = (
            s.query(Category)
            .filter(Category.name == "Hogar", Category.parent_id.is_(None))
            .one()
        )
        canonical_cafe_id = (
            s.query(Category)
            .filter(Category.name == "Café", Category.parent_id == com_id)
            .one()
            .id
        )

    with session_scope() as s:
        s.add(
            Category(
                name="Café", parent_id=hogar.id, is_active=True,
            )
        )
    # Verify in a separate session that the second ``Café`` row is
    # actually persisted before we exercise the disambiguation path.
    with session_scope() as s:
        cafes = (
            s.query(Category).filter(Category.name == "Café").all()
        )
        assert len(cafes) == 2  # canonical + injected-under-Hogar

    with session_scope() as s:
        # This used to crash with ``MultipleResultsFound`` because the
        # lookup did ``WHERE name = 'Café'`` only. Now it picks the
        # canonical row via the registry's parent mapping.
        try:
            service = FixedExpenseService(FixedExpenseRepository(s))
            bill = service.add(
                user_id=43,
                draft=FixedExpenseDraft(
                    name="LATTE", expected_amount=Decimal("1500"),
                    category="Café",
                ),
            )
            service.mark_paid(
                43, bill.id, "2026-09", actual_amount=Decimal("1500")
            )
        except MultipleResultsFound as exc:
            pytest.fail(
                f"mark_paid raised MultipleResultsFound on duplicate "
                f"Café rows: {exc}"
            )

    with session_scope() as s:
        mirror = (
            s.query(Expense)
            .filter(Expense.source_key.like("fixed-payment:%"))
            .one()
        )
        assert mirror.category_id == canonical_cafe_id
