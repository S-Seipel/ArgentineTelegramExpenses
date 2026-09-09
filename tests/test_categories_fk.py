from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.categories.models import Category
from app.categories.repository import CategoryRepository, ensure_seed
from app.database.database import session_scope


def test_categories_table_seeded(in_memory_db):
    """The categories table should be populated from the shipped seed."""
    with session_scope() as s:
        rows = (
            s.query(Category)
            .filter(Category.is_active.is_(True))
            .all()
        )
    names = {r.name for r in rows}
    for must in {"Comida", "Café", "Uber", "Otros", "Supermercado", "Comida rápida"}:
        assert must in names, f"Missing seed category {must!r}"


def test_categories_have_parent_links(in_memory_db):
    from sqlalchemy.orm import joinedload
    with session_scope() as s:
        rows = (
            s.query(Category)
            .options(joinedload(Category.parent))
            .filter(Category.parent_id.isnot(None))
            .all()
        )
    assert len(rows) > 0
    by_name = {r.name: r for r in rows}
    assert by_name["Café"].parent.name == "Comida"
    assert by_name["Uber"].parent.name == "Transporte"


def test_expense_has_category_id_fk(in_memory_db):
    from app.expenses.models import Expense
    from sqlalchemy import ForeignKey

    fk_cols = {
        col.name: list(col.foreign_keys)
        for col in Expense.__table__.columns
        if col.foreign_keys
    }
    assert "category_id" in fk_cols
    fks = fk_cols["category_id"]
    assert len(fks) == 1
    assert fks[0].column.table.name == "categories"


def test_expense_category_string_column_removed(in_memory_db):
    from app.expenses.models import Expense

    column_names = {c.name for c in Expense.__table__.columns}
    assert "category" not in column_names
    assert "category_id" in column_names


def test_expense_insert_requires_category_id(in_memory_db):
    import sqlalchemy.exc

    with session_scope() as s:
        cat = s.query(Category).filter(Category.name == "Café").one()
        s.add(
            __make_expense_for_test(
                name="Café", category_id=cat.id, amount=Decimal("100")
            )
        )


def test_expense_with_invalid_category_id_fails(in_memory_db):
    import sqlalchemy.exc

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with session_scope() as s:
            s.add(
                __make_expense_for_test(
                    name="X",
                    category_id=999999,  # does not exist
                    amount=Decimal("100"),
                )
            )


def test_unknown_category_falls_back_to_otros(in_memory_db):
    """Service: if the AI returns a category not in the table, use Otros."""
    from app.expenses.repository import ExpenseRepository
    from app.expenses.schemas import ExpenseCreate, ExpenseSummary
    from app.expenses.service import ExpenseDraft, ExpenseService

    with session_scope() as s:
        service = ExpenseService(ExpenseRepository(s))
        outcome = service.register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Cosa rara",
                    amount=Decimal("500"),
                    currency="ARS",
                    category="CategoriaInventada",
                    expense_date=date(2026, 1, 19),
                    confidence=Decimal("0.9"),
                )
            ],
            original_message="gasté 500 en cosa rara",
        )
    assert len(outcome.saved) == 1
    cat_name = outcome.saved[0].category.name
    assert cat_name == "Otros"


def test_runtime_added_category_is_usable(in_memory_db):
    """Adding a category at runtime should be available for new expenses."""
    from app.expenses.repository import ExpenseRepository
    from app.expenses.schemas import ExpenseCreate  # noqa: F401
    from app.expenses.service import ExpenseDraft, ExpenseService

    with session_scope() as s:
        CategoryRepository(s).find_or_create(
            "Mascotas",
            parent_name="Hogar",
        )

    with session_scope() as s:
        rows = s.query(Category).filter(Category.name == "Mascotas").all()
        assert len(rows) == 1
        new_id = rows[0].id

    with session_scope() as s:
        service = ExpenseService(ExpenseRepository(s))
        outcome = service.register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name="Alimento perro",
                    amount=Decimal("3000"),
                    currency="ARS",
                    category="Mascotas",
                    expense_date=date(2026, 1, 19),
                    confidence=Decimal("0.9"),
                )
            ],
            original_message="gasté 3k en alimento perro",
        )
    assert len(outcome.saved) == 1
    assert outcome.saved[0].category_id == new_id
    assert outcome.saved[0].category.name == "Mascotas"


def test_unique_constraint_prevents_duplicate_leaf(in_memory_db):
    """Trying to create two leaves with same parent must fail.

    Note: SQL's three-valued logic says two NULL values are never equal,
    which is why the schema uses parent_id as part of the unique key. A
    second leaf with same (parent_id, name) collides; we test the leaf case.
    """
    import sqlalchemy.exc

    with session_scope() as s:
        s.add(Category(name="PadreTest", parent_id=None, is_active=True))

    with session_scope() as s:
        padre = s.query(Category).filter(Category.name == "PadreTest").one()
        s.add(
            Category(
                name="HijoDuplicado",
                parent_id=padre.id,
                is_active=True,
            )
        )

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with session_scope() as s:
            padre = s.query(Category).filter(Category.name == "PadreTest").one()
            s.add(
                Category(
                    name="HijoDuplicado",
                    parent_id=padre.id,
                    is_active=True,
                )
            )


def test_ensure_seed_is_idempotent(in_memory_db):
    """Calling ensure_seed twice does not duplicate rows."""
    with session_scope() as s:
        first = CategoryRepository(s).count()
    with session_scope() as s:
        ensure_seed(CategoryRepository(s))
    with session_scope() as s:
        second = CategoryRepository(s).count()
    assert first == second


def __make_expense_for_test(*, name: str, category_id: int, amount: Decimal):
    from app.expenses.models import Expense

    return Expense(
        telegram_user_id=1,
        name=name,
        amount=amount,
        currency="ARS",
        category_id=category_id,
        expense_date=date(2026, 1, 19),
        original_message="test",
        ai_confidence=Decimal("0.9"),
    )
