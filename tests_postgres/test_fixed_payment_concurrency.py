"""PostgreSQL integration: SELECT FOR UPDATE + fixed-payment concurrency.

These tests verify the atomicity invariants of ``FixedExpenseService``
against a real Postgres instance. Two concurrent transactions must
produce exactly one expense mirror, not two.
"""
from __future__ import annotations

import threading
import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from app.database.database import get_session_factory
from app.expenses.models import Expense
from app.fixed_expenses.models import FixedExpensePayment
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.service import FixedExpenseDraft, FixedExpenseService
from app.models_registry import register_all_models
from app.recurring.repository import RecurringRepository
from app.recurring.service import (
    RecurringDraft,
    RecurringService,
    recurring_occurrence_source_key,
)
from app.recurring.scheduler import _try_send_for_user
from tests_postgres.conftest import run_alembic


def _scheduler_runtime():
    from types import SimpleNamespace
    from app.config.settings import Settings
    return SimpleNamespace(settings=Settings(_env_file=None,
        TELEGRAM_BOT_TOKEN="test-token", TELEGRAM_ALLOWED_USER_ID=42, TIMEZONE="UTC"))


def _bootstrap(pg_database_url: str) -> None:
    """Fresh schema. The categories seed ships with migration 0003,
    so there's no need to insert duplicates here — doing so would
    create ``(parent_id=NULL, name)`` rows that coexist with the
    parent=parent_id rows from the migration and break
    ``ExpenseService._resolve_category_id``'s ``.one_or_none()``."""
    engine = create_engine(pg_database_url, future=True)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()

    run_alembic("upgrade", "head", database_url=pg_database_url)
    register_all_models()


@pytest.fixture
def fresh_db(pg_database_url):
    _bootstrap(pg_database_url)
    # Reload the in-process engine so it sees the freshly-migrated DB.
    import app.database.database as db_module

    db_module._engine = None
    db_module._SessionLocal = None
    yield pg_database_url


def test_mark_paid_is_idempotent_under_repeat(fresh_db):
    """Two sequential ``mark_paid`` calls yield one mirror + one payment."""
    from app.database.database import session_scope

    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            user_id=42,
            draft=FixedExpenseDraft(
                name="CASA", expected_amount=Decimal("90000"),
                payment_method="TRANSFERENCIA", due_day_of_month=10,
            ),
        )
        bid = bill.id

    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        svc.mark_paid(42, bid, "2026-09", actual_amount=Decimal("90000"))

    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        svc.mark_paid(42, bid, "2026-09", actual_amount=Decimal("95000"))

    with session_scope() as s:
        expenses = (
            s.query(Expense)
            .filter(Expense.source_key.is_not(None))
            .all()
        )
        payments = (
            s.query(FixedExpensePayment)
            .filter_by(fixed_expense_id=bid, month_year="2026-09")
            .all()
        )
        # Exactly one mirror, updated to the latest amount.
        assert len(expenses) == 1
        assert expenses[0].amount == Decimal("95000.00")
        # Created with revision=1, bumped on the second ``mark_paid``
        # (restore/update path).
        assert expenses[0].revision >= 2
        # Exactly one payment row.
        assert len(payments) == 1
        assert payments[0].expense_id == expenses[0].id


def test_recurring_occurrence_is_idempotent_on_postgres(fresh_db):
    from app.database.database import session_scope

    occurrence = date(2026, 9, 15)
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        template = service.create_from_draft(
            user_id=42,
            draft=RecurringDraft(
                name="Netflix",
                amount=Decimal("30000"),
                currency="ARS",
                category="Suscripciones",
                frequency="monthly",
                day_of_month=15,
                month_of_year=None,
                confidence=1.0,
            ),
            today=occurrence,
        )
        recurring_id = template.id

    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        _, first = service.register_occurrence(42, recurring_id, occurrence)
        _, second = service.register_occurrence(42, recurring_id, occurrence)
        assert first.id == second.id
        assert first.source_type == "recurring"
        assert first.source_key == recurring_occurrence_source_key(
            recurring_id, occurrence
        )
        assert s.query(Expense).filter(
            Expense.source_key == first.source_key
        ).count() == 1


def test_scheduler_repeated_runs_persist_one_reminder(fresh_db):
    from app.database.database import session_scope
    from app.recurring.models import RecurringReminder

    occurrence = date(2026, 9, 15)
    with session_scope() as s:
        template = RecurringService(RecurringRepository(s)).create_from_draft(
            user_id=42,
            draft=RecurringDraft(
                name="Netflix",
                amount=Decimal("30000"),
                currency="ARS",
                category="Suscripciones",
                frequency="monthly",
                day_of_month=15,
                month_of_year=None,
                confidence=1.0,
            ),
            today=occurrence,
        )

    class _Message:
        message_id = 901

    class _Bot:
        def __init__(self):
            self.sent = 0

        async def send_message(self, *, chat_id, text):
            self.sent += 1
            return _Message()

    class _App:
        def __init__(self):
            self.bot = _Bot()

    app = _App()
    asyncio.run(
        _try_send_for_user(
            app,
            _scheduler_runtime(),
            42,
            occurrence,
            occurrence.isoformat(),
            {},
        )
    )
    asyncio.run(
        _try_send_for_user(
            app,
            _scheduler_runtime(),
            42,
            occurrence,
            occurrence.isoformat(),
            {},
        )
    )
    asyncio.run(
        _try_send_for_user(
            app,
            _scheduler_runtime(),
            42,
            occurrence,
            occurrence.isoformat(),
            {},
        )
    )

    with session_scope() as s:
        reminders = s.query(RecurringReminder).filter(
            RecurringReminder.recurring_id == template.id,
            RecurringReminder.occurrence_date == occurrence,
        ).all()
    assert app.bot.sent == 1
    assert len(reminders) == 1
    assert reminders[0].telegram_message_id == 901
    assert reminders[0].delivery_status == "sent"
    assert reminders[0].attempt_count == 1
    assert reminders[0].sent_at is not None


def test_scheduler_failure_then_retry_delivers_once(fresh_db):
    from app.database.database import session_scope
    from app.recurring.models import RecurringReminder

    occurrence = date(2026, 9, 15)
    with session_scope() as s:
        template = RecurringService(RecurringRepository(s)).create_from_draft(
            user_id=42,
            draft=RecurringDraft(
                name="Spotify",
                amount=Decimal("5000"),
                currency="ARS",
                category="Suscripciones",
                frequency="monthly",
                day_of_month=15,
                month_of_year=None,
                confidence=1.0,
            ),
            today=occurrence,
        )

    class _Message:
        message_id = 902

    class _Bot:
        def __init__(self):
            self.attempts = 0
            self.successes = 0

        async def send_message(self, *, chat_id, text):
            self.attempts += 1
            if self.attempts == 1:
                raise TimeoutError("telegram timeout")
            self.successes += 1
            return _Message()

    class _App:
        def __init__(self):
            self.bot = _Bot()

    app = _App()
    for _ in range(3):
        asyncio.run(
            _try_send_for_user(
                app,
                _scheduler_runtime(),
                42,
                occurrence,
                occurrence.isoformat(),
                {},
            )
        )

    with session_scope() as s:
        reminder = s.query(RecurringReminder).filter(
            RecurringReminder.recurring_id == template.id,
            RecurringReminder.occurrence_date == occurrence,
        ).one()
    assert app.bot.attempts == 2
    assert app.bot.successes == 1
    assert reminder.delivery_status == "sent"
    assert reminder.telegram_message_id == 902
    assert reminder.attempt_count == 2


def test_scheduler_reclaims_crashed_delivery_claim(fresh_db):
    from app.database.database import session_scope
    from app.recurring.models import RecurringReminder
    from app.recurring.repository import REMINDER_CLAIM_TIMEOUT

    occurrence = date(2026, 9, 15)
    with session_scope() as s:
        template = RecurringService(RecurringRepository(s)).create_from_draft(
            user_id=42,
            draft=RecurringDraft(
                name="HBO", amount=Decimal("7000"), currency="ARS",
                category="Suscripciones", frequency="monthly", day_of_month=15,
                month_of_year=None, confidence=1.0,
            ),
            today=occurrence,
        )
        repo = RecurringRepository(s)
        reminder = repo.get_or_create_reminder(42, template.id, occurrence)
        assert repo.claim_reminder(
            reminder,
            now=datetime.utcnow() - REMINDER_CLAIM_TIMEOUT - timedelta(seconds=1),
        )

    class _Message:
        message_id = 903

    class _Bot:
        def __init__(self):
            self.attempts = 0

        async def send_message(self, *, chat_id, text):
            self.attempts += 1
            return _Message()

    class _App:
        def __init__(self):
            self.bot = _Bot()

    app = _App()
    asyncio.run(
        _try_send_for_user(
            app,
            _scheduler_runtime(),
            42,
            occurrence,
            occurrence.isoformat(),
            {},
        )
    )

    with session_scope() as s:
        reminder = s.query(RecurringReminder).filter(
            RecurringReminder.recurring_id == template.id,
            RecurringReminder.occurrence_date == occurrence,
        ).one()
    assert app.bot.attempts == 1
    assert reminder.delivery_status == "sent"
    assert reminder.telegram_message_id == 903


def test_concurrent_mark_paid_yields_one_mirror(fresh_db):
    """Two threads racing on the same ``(bill, month)``.

    ``SELECT FOR UPDATE`` on the template + the unique constraint on
    ``(fixed_expense_id, month_year)`` + the partial unique index on
    ``(telegram_user_id, source_key)`` together guarantee that the
    final state has exactly one live mirror and one payment row.
    """
    from app.database.database import session_scope

    user_id = 7
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            user_id=user_id,
            draft=FixedExpenseDraft(
                name="NETFLIX", expected_amount=Decimal("5500"),
                payment_method="TARJETA", due_day_of_month=15,
            ),
        )
        bid = bill.id

    session_factory = get_session_factory()
    errors: list[BaseException] = []

    def worker(amount: Decimal) -> None:
        try:
            with session_scope() as s:
                svc = FixedExpenseService(FixedExpenseRepository(s))
                svc.mark_paid(user_id, bid, "2026-09", actual_amount=amount)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=(Decimal("5500"),))
    t2 = threading.Thread(target=worker, args=(Decimal("5700"),))
    t1.start(); t2.start()
    t1.join(); t2.join()

    # Both threads must succeed: the second one finds the existing
    # payment and updates it, not duplicates it.
    assert not errors, errors

    with session_scope() as s:
        mirrors = (
            s.query(Expense)
            .filter(Expense.source_key.is_not(None))
            .all()
        )
        payments = (
            s.query(FixedExpensePayment)
            .filter_by(fixed_expense_id=bid, month_year="2026-09")
            .all()
        )
        assert len(mirrors) == 1, [m.source_key for m in mirrors]
        assert len(payments) == 1
        assert payments[0].expense_id == mirrors[0].id


def test_rollback_when_payment_step_fails(fresh_db, monkeypatch):
    """A simulated failure inside ``mark_paid`` must leave the DB clean."""
    from app.database.database import session_scope

    from app.fixed_expenses import service as fx_service

    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            user_id=11,
            draft=FixedExpenseDraft(
                name="GYM", expected_amount=Decimal("60000"),
            ),
        )
        bid = bill.id

    real_paid_at = fx_service.business_now

    def boom():
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(fx_service, "business_now", boom)

    # Use a raw session + manual rollback so we can ``assert raises``
    # WITHOUT hiding the exception from the context manager (otherwise
    # ``session_scope`` would commit instead of rolling back).
    from app.database.database import get_session_factory

    factory = get_session_factory()
    s = factory()
    try:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        with pytest.raises(RuntimeError):
            svc.mark_paid(11, bid, "2026-09", actual_amount=Decimal("60000"))
        s.rollback()
    finally:
        s.close()

    monkeypatch.setattr(fx_service, "business_now", real_paid_at)

    with session_scope() as s:
        assert (
            s.query(FixedExpensePayment)
            .filter_by(fixed_expense_id=bid, month_year="2026-09")
            .count()
            == 0
        )
        assert (
            s.query(Expense)
            .filter(Expense.source_key.is_not(None))
            .count()
            == 0
        )


def test_fk_behavior_ondelete_set_null(fresh_db):
    """Deleting an Expense that is linked from ``fixed_expense_payments``
    must NULL the FK (``ON DELETE SET NULL``)."""
    from app.database.database import session_scope

    user_id = 21
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            user_id=user_id,
            draft=FixedExpenseDraft(
                name="EDESUR", expected_amount=Decimal("15000"),
            ),
        )
        svc.mark_paid(user_id, bill.id, "2026-09")
        bid = bill.id

    with session_scope() as s:
        payment = (
            s.query(FixedExpensePayment)
            .filter_by(fixed_expense_id=bid, month_year="2026-09")
            .one()
        )
        linked_id = payment.expense_id
        assert linked_id is not None

    with session_scope() as s:
        # Bypass the generic CRUD (which would block) by issuing the
        # DELETE through raw SQL. The FK uses ``ON DELETE SET NULL``,
        # so this is allowed and clears the link.
        s.execute(
            text("DELETE FROM expenses WHERE id = :i"),
            {"i": linked_id},
        )

    with session_scope() as s:
        payment = (
            s.query(FixedExpensePayment)
            .filter_by(fixed_expense_id=bid, month_year="2026-09")
            .one()
        )
        assert payment.expense_id is None
        assert payment.skipped is False
        # When the caller doesn't specify an actual amount, the payment
        # row keeps ``actual_amount = NULL`` so ``status`` renders as
        # ``paid_exact`` regardless.
        assert payment.actual_amount is None
