"""F1.5 migration compatibility and recurring occurrence serialization."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import create_engine, inspect, select, text

from app.database.database import session_scope
from app.expenses.models import Expense
from app.money import CycleService, MoneyEngine
from app.recurring.models import RecurringExpense
from app.recurring.repository import RecurringRepository
from app.recurring.service import (
    RecurringDraft,
    RecurringService,
    recurring_occurrence_source_key,
)
from tests_postgres.conftest import run_alembic
from tests_postgres.test_fixed_payment_concurrency import fresh_db  # noqa: F401


@pytest.mark.parametrize("actions", [
    ("pay", "pay"),
    ("pay", "skip"),
    ("pay", "postpone"),
])
def test_concurrent_obligation_responses_resolve_once(fresh_db, actions):
    """Both workers cache the old row before taking the canonical row lock."""
    user_id = 42
    occurrence = date(2026, 10, 15)
    next_occurrence = date(2026, 11, 15)
    with session_scope() as session:
        cycle = CycleService(session).open_initial_cycle(
            user_id=user_id, today=date(2026, 10, 1), currency="USD"
        )
        cycle_id = cycle.cycle_id
        service = RecurringService(RecurringRepository(session))
        template = service.create_from_draft(user_id, RecurringDraft(
            name="Concurrent USD obligation", amount=Decimal("10.25"),
            currency="USD", category="Otros", frequency="monthly",
            day_of_month=15, month_of_year=None, confidence=1.0,
        ), today=date(2026, 10, 1))
        recurring_id = template.id
        service.set_obligation(user_id, recurring_id, True)
        summary = MoneyEngine(session).get_cycle_summary(
            user_id, cycle_id, today=occurrence
        )
        assert summary.pending_recurring_obligations.get("USD") == Decimal("10.25")
        before_available = summary.available_to_spend.get("USD")

    barrier = Barrier(2)

    def respond(action):
        with session_scope() as session:
            # Preload in both identity maps. The locking query must refresh
            # this stale anchor after waiting for the other transaction.
            cached = session.get(RecurringExpense, recurring_id)
            assert cached is not None
            session.execute(text("SET LOCAL lock_timeout = '10s'"))
            session.execute(text("SET LOCAL statement_timeout = '15s'"))
            barrier.wait(timeout=10)
            service = RecurringService(RecurringRepository(session))
            if action == "pay":
                _, expense = service.register_occurrence(
                    user_id, recurring_id, occurrence,
                    occurrence_date=occurrence, commit=False,
                )
                return expense.id if expense is not None else None
            if action == "skip":
                service.skip_occurrence(
                    user_id, recurring_id, occurrence,
                    occurrence_date=occurrence, commit=False,
                )
            else:
                service.postpone_occurrence(
                    user_id, recurring_id,
                    occurrence_date=occurrence, commit=False,
                )
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(respond, action) for action in actions]
        results = [future.result(timeout=30) for future in futures]

    with session_scope() as session:
        template = session.get(RecurringExpense, recurring_id)
        assert template.obligation_due_date == next_occurrence
        assert template.next_due_date == next_occurrence
        assert template.currency == "USD"
        mirrors = session.execute(select(Expense).where(
            Expense.telegram_user_id == user_id,
            Expense.source_key == recurring_occurrence_source_key(
                recurring_id, occurrence
            ),
        )).scalars().all()
        if actions == ("pay", "pay"):
            assert len(mirrors) == 1
            assert results == [mirrors[0].id, mirrors[0].id]
        elif "postpone" in actions:
            assert len(mirrors) == 1
        else:
            # The first response wins: skipping cannot be undone by the
            # stale payment, and skipping a paid occurrence is a no-op.
            assert len(mirrors) == (1 if results[0] is not None else 0)
        assert all(
            mirror.currency == "USD" and mirror.amount == Decimal("10.25")
            for mirror in mirrors
        )
        summary = MoneyEngine(session).get_cycle_summary(
            user_id, cycle_id, today=occurrence
        )
        assert summary.pending_recurring_obligations.get("USD") == 0
        assert summary.pending_recurring_obligations.get("ARS") == 0
        assert summary.realized_expenses.get("USD") == (
            Decimal("10.25") if mirrors else 0
        )
        assert summary.available_to_spend.get("USD") == (
            before_available if mirrors else 0
        )


def test_existing_recurring_data_survives_obligation_upgrade_and_downgrade(
    pg_database_url,
):
    """Enabling the feature never opts existing reminders into reservations."""
    engine = create_engine(pg_database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        run_alembic(
            "upgrade", "0013_money_engine_state", database_url=pg_database_url
        )
        with engine.begin() as connection:
            category_id = connection.execute(text(
                "SELECT min(id) FROM categories WHERE name = 'Otros'"
            )).scalar_one()
            recurring_id = connection.execute(text(
                "INSERT INTO recurring_expenses "
                "(telegram_user_id, name, amount, currency, category_id, "
                "frequency, day_of_month, next_due_date, is_active) "
                "VALUES (42, 'Existing reminder', 10.25, 'USD', :category_id, "
                "'monthly', 15, '2026-11-15', true) RETURNING id"
            ), {"category_id": category_id}).scalar_one()
            expense_id = connection.execute(text(
                "INSERT INTO expenses "
                "(telegram_user_id, name, amount, currency, category_id, "
                "expense_date, original_message, ai_confidence, source_type, "
                "source_key, revision) VALUES "
                "(42, 'Existing payment', 10.25, 'USD', :category_id, "
                "'2026-10-15', '', 1, 'recurring', :source_key, 1) RETURNING id"
            ), {
                "category_id": category_id,
                "source_key": f"recurring:{recurring_id}:2026-10-15",
            }).scalar_one()

        run_alembic("upgrade", "head", database_url=pg_database_url)
        with engine.connect() as connection:
            row = connection.execute(text(
                "SELECT is_obligation, obligation_due_date, next_due_date "
                "FROM recurring_expenses WHERE id = :id"
            ), {"id": recurring_id}).one()
            assert tuple(row) == (False, None, date(2026, 11, 15))

        run_alembic(
            "downgrade", "0013_money_engine_state", database_url=pg_database_url
        )
        columns = {
            column["name"]
            for column in inspect(engine).get_columns("recurring_expenses")
        }
        assert "is_obligation" not in columns
        assert "obligation_due_date" not in columns
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT next_due_date FROM recurring_expenses WHERE id = :id"
            ), {"id": recurring_id}).scalar_one() == date(2026, 11, 15)
            assert tuple(connection.execute(text(
                "SELECT currency, amount FROM expenses WHERE id = :id"
            ), {"id": expense_id}).one()) == ("USD", Decimal("10.25"))

        run_alembic("upgrade", "head", database_url=pg_database_url)
        with engine.connect() as connection:
            assert tuple(connection.execute(text(
                "SELECT is_obligation, obligation_due_date FROM "
                "recurring_expenses WHERE id = :id"
            ), {"id": recurring_id}).one()) == (False, None)
            assert connection.execute(text(
                "SELECT count(*) FROM expenses WHERE id = :id"
            ), {"id": expense_id}).scalar_one() == 1
    finally:
        engine.dispose()
