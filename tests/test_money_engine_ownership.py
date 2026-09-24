"""R2 ownership + invariant detection tests.

Covers spec §10 (system-owned Expense guard) and §11 (invariant
detection). The five violation kinds are reported independently — each
test exercises a single violation in isolation so the failure mode is
unambiguous.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete

from app.database.database import session_scope
from app.expenses.models import Expense
from app.expenses.repository import ExpenseRepository
from app.expenses.schemas import ExpenseCreate
from app.expenses.service import (
    LinkedFixedPaymentError,
    SystemOwnedExpenseError,
)
from app.fixed_expenses.models import (
    FixedExpense,
    FixedExpensePayment,
)
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.service import (
    FixedExpenseDraft,
    FixedExpenseService,
    fixed_payment_source_key,
    current_month_year,
)
from app.money import MoneyEngine
from app.money.dto import (
    InvariantViolationKind,
)


# ---------------------------------------------------------------------------
# Ownership guard
# ---------------------------------------------------------------------------


def _make_recurring_expense_mirror(user_id: int) -> int:
    """Insert an Expense row that LOOKS like a recurring mirror but
    without the corresponding reminder; useful for triggering the
    ``source_type='recurring'`` branch of the guard.
    """
    with session_scope() as s:
        row = ExpenseRepository(s).create(
            ExpenseCreate(
                telegram_user_id=user_id,
                name="spotify",
                amount=Decimal("5000"),
                currency="ARS",
                category_id=1,
                expense_date=date(2026, 10, 10),
                original_message="[recurring] spotify",
                ai_confidence=Decimal("0"),
                source_type="recurring",
                source_key="recurring:42:2026-10-10",
                revision=1,
            )
        )
        return row.id


def _make_normal_expense(user_id: int) -> int:
    with session_scope() as s:
        row = ExpenseRepository(s).create(
            ExpenseCreate(
                telegram_user_id=user_id,
                name="cafe",
                amount=Decimal("2500"),
                currency="ARS",
                category_id=1,
                expense_date=date(2026, 10, 10),
                original_message="",
                ai_confidence=Decimal("0"),
                source_type="telegram",
                revision=1,
            )
        )
        return row.id


def test_recurring_source_type_delete_rejected(in_memory_db):
    """Spec §10: source_type='recurring' → SystemOwnedExpenseError."""
    eid = _make_recurring_expense_mirror(1)
    with session_scope() as s:
        repo = ExpenseRepository(s)
        with pytest.raises(SystemOwnedExpenseError):
            repo.delete(1, eid)


def test_fixed_payment_source_type_delete_still_raises_linked_error(
    in_memory_db,
):
    """Spec §10 partial overlap with the existing
    ``LinkedFixedPaymentError``: a fixed_payment mirror that still has
    the FK link produces the historical error (kept for back-compat).
    The system-owned guard kicks in when the FK is gone.
    """
    today = date(2026, 10, 15)
    my = current_month_year(today)
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            1,
            FixedExpenseDraft(
                name="CASA",
                expected_amount=Decimal("90000"),
                due_day_of_month=10,
            ),
        )
        svc.mark_paid(1, bill.id, my, paid_on=today)
        linked = (
            s.query(FixedExpensePayment)
            .filter_by(fixed_expense_id=bill.id, month_year=my)
            .one()
        )
        eid = linked.expense_id
    with session_scope() as s:
        repo = ExpenseRepository(s)
        with pytest.raises(LinkedFixedPaymentError):
            repo.delete(1, eid)


def test_fixed_payment_source_type_update_amount_rejected_when_no_link(
    in_memory_db,
):
    """When the FK is gone, the generic system-owned guard catches it."""
    today = date(2026, 10, 15)
    my = current_month_year(today)
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            1,
            FixedExpenseDraft(
                name="LUZ",
                expected_amount=Decimal("80"),
                due_day_of_month=10,
            ),
        )
        svc.mark_paid(1, bill.id, my, paid_on=today)
        # Strip the FK link so the new guard (not the legacy one)
        # has to fire.
        s.query(FixedExpensePayment).filter_by(
            fixed_expense_id=bill.id, month_year=my
        ).update({"expense_id": None})
        s.commit()
        eid = (
            s.query(FixedExpensePayment)
            .filter_by(fixed_expense_id=bill.id, month_year=my)
            .one()
            .expense_id
        )
    # The expense exists; find it.
    with session_scope() as s:
        eid = s.query(Expense).filter_by(source_type="fixed_payment").one().id

    with session_scope() as s:
        repo = ExpenseRepository(s)
        with pytest.raises(SystemOwnedExpenseError):
            repo.update_amount(1, eid, Decimal("100"))
        with pytest.raises(SystemOwnedExpenseError):
            repo.update_name(1, eid, "NEW NAME")


def test_normal_expense_unchanged(in_memory_db):
    eid = _make_normal_expense(1)
    with session_scope() as s:
        repo = ExpenseRepository(s)
        summary = repo.update_amount(1, eid, Decimal("3000"))
    assert summary is not None
    assert summary.amount == Decimal("3000")


# ---------------------------------------------------------------------------
# Invariant detection — each violation reported independently
# ---------------------------------------------------------------------------


def _setup_paid_payment_without_expense_id(user_id: int) -> int:
    """Insert a FixedExpensePayment marked paid but with expense_id=NULL."""
    today = date(2026, 10, 15)
    my = current_month_year(today)
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            user_id,
            FixedExpenseDraft(
                name="X",
                expected_amount=Decimal("100"),
                due_day_of_month=5,
            ),
        )
        payment_id = svc.mark_paid(
            user_id, bill.id, my, paid_on=today
        )[1].id
        # Strip the expense_id to create the violation.
        s.execute(
            delete(Expense).where(Expense.source_key.like("fixed-payment:%"))
        )
        s.query(FixedExpensePayment).filter_by(id=payment_id).update(
            {"expense_id": None}
        )
        s.commit()
    return payment_id


def _setup_paid_payment_with_missing_expense(user_id: int) -> int:
    """Mark paid, then delete the underlying expense row while
    leaving the payment.expense_id pointing at the now-missing id.

    We use a raw sqlite3 connection (with FKs disabled) so the
    deletion can proceed despite the FK link. This simulates real
    production corruption the engine must still detect.
    """
    today = date(2026, 10, 15)
    my = current_month_year(today)
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            user_id,
            FixedExpenseDraft(
                name="Y",
                expected_amount=Decimal("100"),
                due_day_of_month=5,
            ),
        )
        payment_id = svc.mark_paid(
            user_id, bill.id, my, paid_on=today
        )[1].id
        linked_eid = (
            s.query(FixedExpensePayment).filter_by(id=payment_id).one().expense_id
        )

    # Bypass ORM FK: hit the raw connection with FKs off, drop the
    # expense, then re-enable. The payment.expense_id still points
    # at the now-missing row.
    import sqlite3
    from app.database.database import get_engine
    raw_engine = get_engine().raw_connection()
    try:
        cur = raw_engine.cursor()
        cur.execute("PRAGMA foreign_keys = OFF")
        cur.execute("DELETE FROM expenses WHERE id = ?", (linked_eid,))
        raw_engine.commit()
        cur.execute("PRAGMA foreign_keys = ON")
        raw_engine.commit()
    finally:
        raw_engine.close()
    return payment_id


def _setup_paid_payment_with_soft_deleted_expense(user_id: int) -> int:
    today = date(2026, 10, 15)
    my = current_month_year(today)
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            user_id,
            FixedExpenseDraft(
                name="Z",
                expected_amount=Decimal("100"),
                due_day_of_month=5,
            ),
        )
        payment_id = svc.mark_paid(
            user_id, bill.id, my, paid_on=today
        )[1].id
        # Soft-delete the linked expense.
        s.query(Expense).filter_by(
            source_key=fixed_payment_source_key(bill.id, my)
        ).update({"deleted_at": datetime.now(timezone.utc)})
        s.commit()
    return payment_id


def _setup_paid_payment_with_wrong_source_type(user_id: int) -> int:
    today = date(2026, 10, 15)
    my = current_month_year(today)
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            user_id,
            FixedExpenseDraft(
                name="W",
                expected_amount=Decimal("100"),
                due_day_of_month=5,
            ),
        )
        payment_id = svc.mark_paid(
            user_id, bill.id, my, paid_on=today
        )[1].id
        # Flip source_type on the mirror.
        s.query(Expense).filter_by(
            source_key=fixed_payment_source_key(bill.id, my)
        ).update({"source_type": "telegram"})
        s.commit()
    return payment_id


def _setup_paid_payment_with_wrong_source_key(user_id: int) -> int:
    today = date(2026, 10, 15)
    my = current_month_year(today)
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            user_id,
            FixedExpenseDraft(
                name="V",
                expected_amount=Decimal("100"),
                due_day_of_month=5,
            ),
        )
        payment_id = svc.mark_paid(
            user_id, bill.id, my, paid_on=today
        )[1].id
        s.query(Expense).filter_by(
            source_key=fixed_payment_source_key(bill.id, my)
        ).update({"source_key": "tampered"})
        s.commit()
    return payment_id


def test_invariant_expense_id_null(in_memory_db):
    _setup_paid_payment_without_expense_id(1)
    with session_scope() as s:
        engine = MoneyEngine(s)
        v = engine.detect_invariant_violations(1)
    kinds = {x.kind for x in v}
    assert InvariantViolationKind.EXPENSE_ID_NULL in kinds


def test_invariant_expense_row_missing(in_memory_db):
    _setup_paid_payment_with_missing_expense(1)
    with session_scope() as s:
        engine = MoneyEngine(s)
        v = engine.detect_invariant_violations(1)
    kinds = {x.kind for x in v}
    assert InvariantViolationKind.EXPENSE_ROW_MISSING in kinds


def test_invariant_expense_soft_deleted(in_memory_db):
    _setup_paid_payment_with_soft_deleted_expense(1)
    with session_scope() as s:
        engine = MoneyEngine(s)
        v = engine.detect_invariant_violations(1)
    kinds = {x.kind for x in v}
    assert InvariantViolationKind.EXPENSE_SOFT_DELETED in kinds


def test_invariant_expense_wrong_source_type(in_memory_db):
    _setup_paid_payment_with_wrong_source_type(1)
    with session_scope() as s:
        engine = MoneyEngine(s)
        v = engine.detect_invariant_violations(1)
    kinds = {x.kind for x in v}
    assert InvariantViolationKind.EXPENSE_WRONG_SOURCE_TYPE in kinds


def test_invariant_expense_source_key_mismatch(in_memory_db):
    _setup_paid_payment_with_wrong_source_key(1)
    with session_scope() as s:
        engine = MoneyEngine(s)
        v = engine.detect_invariant_violations(1)
    kinds = {x.kind for x in v}
    assert InvariantViolationKind.EXPENSE_SOURCE_KEY_MISMATCH in kinds


def test_invariant_no_violations_for_clean_state(in_memory_db):
    """A well-formed mark_paid produces zero violations."""
    today = date(2026, 10, 15)
    my = current_month_year(today)
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            1,
            FixedExpenseDraft(
                name="OK",
                expected_amount=Decimal("100"),
                due_day_of_month=5,
            ),
        )
        svc.mark_paid(1, bill.id, my, paid_on=today)
    with session_scope() as s:
        engine = MoneyEngine(s)
        v = engine.detect_invariant_violations(1)
    assert v == []


def test_invariant_no_violations_for_pending_payment(in_memory_db):
    """Pending payments (paid_at IS NULL) are not inspected."""
    today = date(2026, 10, 15)
    my = current_month_year(today)
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            1,
            FixedExpenseDraft(
                name="PENDING",
                expected_amount=Decimal("100"),
                due_day_of_month=5,
            ),
        )
        # Don't mark paid: only a FixedExpensePayment row when paid.
        # So no violation here even if the row stays empty.
        _ = bill
    with session_scope() as s:
        engine = MoneyEngine(s)
        v = engine.detect_invariant_violations(1)
    assert v == []


def test_invariant_skipped_payment_not_inspected(in_memory_db):
    """Skipped payments (skipped=true) are excluded by design."""
    today = date(2026, 10, 15)
    my = current_month_year(today)
    with session_scope() as s:
        svc = FixedExpenseService(FixedExpenseRepository(s))
        bill = svc.add(
            1,
            FixedExpenseDraft(
                name="SKIP",
                expected_amount=Decimal("100"),
                due_day_of_month=5,
            ),
        )
        svc.mark_skipped(1, bill.id, my)
    with session_scope() as s:
        engine = MoneyEngine(s)
        v = engine.detect_invariant_violations(1)
    assert v == []
