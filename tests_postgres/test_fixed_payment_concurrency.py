"""PostgreSQL integration: SELECT FOR UPDATE + fixed-payment concurrency.

These tests verify the atomicity invariants of ``FixedExpenseService``
against a real Postgres instance. Two concurrent transactions must
produce exactly one expense mirror, not two.
"""
from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from app.database.database import get_session_factory
from app.expenses.models import Expense
from app.fixed_expenses.models import FixedExpensePayment
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.service import FixedExpenseDraft, FixedExpenseService
from app.models_registry import register_all_models
from tests_postgres.conftest import run_alembic


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
