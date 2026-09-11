"""PostgreSQL integration: Decimal / Numeric semantics.

Confirms that the Phase-0 backend never silently rounds currency
amounts: sums and JSON roundtrips preserve the 2-decimal precision.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from app.database.database import get_session_factory
from app.expenses.models import Expense
from app.expenses.repository import ExpenseRepository
from app.expenses.service import ExpenseDraft, ExpenseService
from app.models_registry import register_all_models
from app.web.queries import _money
from tests_postgres.conftest import run_alembic


@pytest.fixture
def fresh_db(pg_database_url):
    """Drop + recreate the public schema. The categories seed ships
    with migration 0003 so we don't insert duplicates here."""
    engine = create_engine(pg_database_url, future=True)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()

    run_alembic("upgrade", "head", database_url=pg_database_url)

    register_all_models()
    import app.database.database as db_module

    db_module._engine = None
    db_module._SessionLocal = None
    yield pg_database_url


def test_numeric_precision_preserved(fresh_db):
    """Sum of 0.1 + 0.2 stays 0.30 — no float drift in the SQL layer."""
    factory = get_session_factory()
    with factory() as s:
        svc = ExpenseService(ExpenseRepository(s))
        svc.register_many(
            user_id=99,
            drafts=[
                ExpenseDraft(
                    name="x", amount=Decimal("0.10"),
                    currency="ARS", category="Café",
                    expense_date=date(2026, 9, 10), confidence=Decimal("1"),
                ),
                ExpenseDraft(
                    name="y", amount=Decimal("0.20"),
                    currency="ARS", category="Café",
                    expense_date=date(2026, 9, 11), confidence=Decimal("1"),
                ),
            ],
            original_message="seed",
        )
    with factory() as s:
        amounts = sorted(
            Decimal(a)
            for (a,) in s.query(Expense)
            .filter(Expense.telegram_user_id == 99)
            .with_entities(Expense.amount)
            .all()
        )
        assert amounts == [Decimal("0.10"), Decimal("0.20")]
        repo = ExpenseRepository(s)
        out = repo.sum_by_period(99, date(2026, 9, 1), date(2026, 9, 30))
        assert out == {"ARS": Decimal("0.30")}


def test_json_serialization_preserves_two_decimals():
    """Money roundtripped through JSON stays a clean 2-decimal float.

    The dashboard's chart libs require a JSON number, so we serialize
    Decimal via ``float(v.quantize(Decimal('0.01')))`` at the API
    boundary. The Decimal arithmetic stays in the backend; this is the
    only place where a float ever appears.
    """
    raw = Decimal("1234.5")
    serialized = json.loads(json.dumps(_money(raw)))
    assert abs(serialized - 1234.5) < 1e-9
