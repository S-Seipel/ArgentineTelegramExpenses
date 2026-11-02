"""Durable user-scoped undo, revision fences and explicitly supplied location."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.categories.models import Category
from app.database.database import session_scope
from app.expenses.history import UndoConflictError
from app.expenses.models import Expense, ExpenseChange
from app.expenses.repository import ExpenseRepository
from app.expenses.schemas import ExpenseCreate
from app.expenses.service import ExpenseService, SystemOwnedExpenseError


def _expense(user=1, *, source="telegram", amount="100.01"):
    with session_scope() as session:
        category = session.execute(select(Category.id).limit(1)).scalar_one()
        return ExpenseRepository(session).create(ExpenseCreate(
            telegram_user_id=user, name="Café", amount=Decimal(amount), currency="ARS",
            category_id=category, expense_date=date(2026, 11, 2),
            original_message="unit fixture", ai_confidence=Decimal("0.900"),
            source_type=source,
        )).id


def _undo(user=1):
    with session_scope() as session:
        return ExpenseService(ExpenseRepository(session)).undo_last(user)


def test_delete_undo_survives_new_session_and_preserves_exact_money(in_memory_db):
    eid = _expense()
    with session_scope() as session:
        ExpenseRepository(session).delete(1, eid)
    with session_scope() as session:
        assert ExpenseRepository(session).get_by_id(1, eid) is None
        change = session.execute(select(ExpenseChange)).scalar_one()
        assert change.before_snapshot["amount"] == "100.01"
        assert change.action == "delete"
    result = _undo()
    assert result.action == "delete"
    assert result.expense.id == eid
    assert result.expense.amount == Decimal("100.01")
    assert result.expense.deleted_at is None
    assert result.expense.revision == 3
    assert _undo() is None


def test_multiple_undos_same_expense_rebase_revision_without_overwriting_audit(in_memory_db):
    eid = _expense()
    with session_scope() as session:
        repo = ExpenseRepository(session)
        repo.update_amount(1, eid, Decimal("200.02"))
        repo.update_name(1, eid, "Café y tostadas")
        repo.set_location(1, eid, "-34.603722", "-58.381592")
        repo.delete(1, eid)
        frozen = [(c.id, dict(c.before_snapshot), dict(c.after_snapshot), c.before_revision, c.after_revision)
                  for c in session.execute(select(ExpenseChange).order_by(ExpenseChange.id)).scalars()]
    assert [_undo().action for _ in range(4)] == ["delete", "location", "name", "amount"]
    with session_scope() as session:
        row = ExpenseRepository(session).get_by_id(1, eid)
        assert (row.name, row.amount, row.latitude, row.longitude, row.revision) == (
            "Café", Decimal("100.01"), None, None, 9,
        )
        after = [(c.id, dict(c.before_snapshot), dict(c.after_snapshot), c.before_revision, c.after_revision)
                 for c in session.execute(select(ExpenseChange).order_by(ExpenseChange.id)).scalars()]
        assert after == frozen
        assert {c.status for c in session.execute(select(ExpenseChange)).scalars()} == {"undone"}


def test_undo_and_edit_are_user_scoped(in_memory_db):
    first, second = _expense(1), _expense(2)
    with session_scope() as session:
        repo = ExpenseRepository(session)
        repo.delete(1, first)
        repo.update_name(2, second, "Usuario dos")
        assert repo.update_name(1, second, "Invalid") is None
        assert repo.set_location(1, second, 10, 20) is None
    assert _undo(3) is None
    assert _undo(1).expense.id == first
    with session_scope() as session:
        assert ExpenseRepository(session).get_by_id(2, second).name == "Usuario dos"
    assert _undo(2).expense.name == "Café"


@pytest.mark.parametrize("modify", ["revision", "amount", "source_type"])
def test_undo_rejects_untracked_changes_instead_of_overwriting(in_memory_db, modify):
    eid = _expense()
    with session_scope() as session:
        ExpenseRepository(session).update_name(1, eid, "Edited")
        row = session.get(Expense, eid)
        if modify == "revision":
            row.revision += 1
        elif modify == "amount":
            row.amount = Decimal("900.99")
        else:
            row.source_type = "receipt"
    with pytest.raises(UndoConflictError):
        _undo()
    with session_scope() as session:
        assert session.get(Expense, eid).name == "Edited"
        assert session.execute(select(ExpenseChange)).scalar_one().status == "pending"


def test_retains_only_ten_pending_actions_and_preserves_old_audit(in_memory_db):
    eid = _expense(amount="1")
    with session_scope() as session:
        repo = ExpenseRepository(session)
        for amount in range(2, 14):
            repo.update_amount(1, eid, Decimal(amount))
        changes = session.execute(select(ExpenseChange).order_by(ExpenseChange.id)).scalars().all()
        assert [change.status for change in changes] == ["expired"] * 2 + ["pending"] * 10
    for _ in range(10):
        assert _undo() is not None
    assert _undo() is None
    with session_scope() as session:
        assert ExpenseRepository(session).get_by_id(1, eid).amount == Decimal("3")
        assert session.query(ExpenseChange).count() == 12


@pytest.mark.parametrize("source", ["fixed_payment", "recurring"])
def test_system_mirrors_cannot_enter_undo_or_location(source, in_memory_db):
    eid = _expense(source=source)
    with session_scope() as session:
        repo = ExpenseRepository(session)
        for call in (lambda: repo.update_amount(1, eid, Decimal("5")),
                     lambda: repo.update_name(1, eid, "Bad"),
                     lambda: repo.set_location(1, eid, 0, 0),
                     lambda: repo.delete(1, eid)):
            with pytest.raises(SystemOwnedExpenseError):
                call()
        # Owning domains still use their low-level soft-delete/restore path.
        repo.soft_delete(1, eid)
        repo.restore(1, eid)
        assert session.query(ExpenseChange).count() == 0
    assert _undo() is None


def test_location_is_explicit_nullable_and_removal_can_be_undone(in_memory_db):
    eid = _expense(source="receipt")
    with session_scope() as session:
        service = ExpenseService(ExpenseRepository(session))
        initial = service.repo.get_by_id(1, eid)
        assert initial.latitude is initial.longitude is initial.location_recorded_at is None
        added = service.set_location(1, eid, "-34.6037221", "-58.3815922")
        assert added.latitude == Decimal("-34.6037221")
        assert added.longitude == Decimal("-58.3815922")
        assert added.location_recorded_at is not None
        removed = service.set_location(1, eid, None, None)
        assert removed.latitude is removed.longitude is removed.location_recorded_at is None
    restored = _undo().expense
    assert restored.latitude == Decimal("-34.6037221")
    assert restored.longitude == Decimal("-58.3815922")
    assert _undo().expense.latitude is None


@pytest.mark.parametrize("latitude,longitude", [
    (None, 0), (0, None), ("nan", 0), (0, "Infinity"), (91, 0),
    (-91, 0), (0, 181), (0, -181), ("bad", "value"), (True, False),
])
def test_invalid_location_has_no_side_effects(in_memory_db, latitude, longitude):
    eid = _expense()
    with session_scope() as session:
        with pytest.raises(ValueError):
            ExpenseService(ExpenseRepository(session)).set_location(1, eid, latitude, longitude)
        row = session.get(Expense, eid)
        assert row.latitude is row.longitude is None
        assert row.revision == 1
        assert session.query(ExpenseChange).count() == 0


def test_location_bounds_and_same_value_noop(in_memory_db):
    eid = _expense()
    with session_scope() as session:
        repo = ExpenseRepository(session)
        first = repo.set_location(1, eid, -90, 180)
        second = repo.set_location(1, eid, -90, 180)
        assert first.revision == second.revision == 2
        repo.update_amount(1, eid, Decimal("100.01"))
        repo.update_name(1, eid, "Café")
        assert session.query(ExpenseChange).count() == 1


def test_uncommitted_soft_delete_and_history_roll_back_together(in_memory_db):
    eid = _expense()
    with session_scope() as session:
        ExpenseRepository(session).soft_delete(1, eid, commit=False)
        assert session.query(ExpenseChange).count() == 1
        session.rollback()
    with session_scope() as session:
        assert ExpenseRepository(session).get_by_id(1, eid) is not None
        assert session.query(ExpenseChange).count() == 0


def test_cached_expense_is_refreshed_before_edit_and_undo(in_memory_db):
    eid = _expense()
    from app.database.database import get_session_factory
    factory = get_session_factory()
    with factory() as cached:
        old = cached.get(Expense, eid)
        assert old.amount == Decimal("100.01")
        with session_scope() as other:
            ExpenseRepository(other).update_amount(1, eid, Decimal("200.02"))
        repo = ExpenseRepository(cached)
        repo.update_name(1, eid, "Fresh edit")
        assert repo.undo_last(1).expense.amount == Decimal("200.02")
        assert repo.undo_last(1).expense.amount == Decimal("100.01")


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "0", "10000000000000000", "9999999999999999.999", "0.001"])
def test_invalid_amount_edit_never_adds_history(in_memory_db, amount):
    eid = _expense()
    with session_scope() as session:
        with pytest.raises(ValueError):
            ExpenseRepository(session).update_amount(1, eid, Decimal(amount))
        assert session.get(Expense, eid).amount == Decimal("100.01")
        assert session.query(ExpenseChange).count() == 0
