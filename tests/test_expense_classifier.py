"""Local consensus learns only the caller's normal, active expense labels."""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.categories.models import Category
from app.database.database import get_engine, session_scope
from app.expenses.classifier import classifier_status, predict_category
from app.expenses.models import Expense
from app.expenses.repository import ExpenseRepository
from app.expenses.schemas import ExpenseCreate


def _seed(name="Starbucks", category="Café", count=3, *, user=1, source="telegram", deleted=False):
    with session_scope() as session:
        category_id = session.execute(select(Category.id).where(Category.name == category)).scalar_one()
        repo = ExpenseRepository(session)
        rows = repo.create_many([ExpenseCreate(
            telegram_user_id=user, name=name, amount=Decimal("100.01"),
            currency="ARS", category_id=category_id, expense_date=date(2026, 11, 2),
            original_message="isolated classifier fixture", ai_confidence=Decimal("0.900"),
            source_type=source,
        ) for _ in range(count)])
        if deleted:
            for row in rows:
                row.deleted_at = datetime.now(timezone.utc)
        return [row.id for row in rows]


def _predict(name="Starbucks", user=1):
    with session_scope() as session:
        return predict_category(session, user, name)


def test_three_unanimous_examples_return_exact_observed_category(in_memory_db):
    _seed()
    result = _predict()
    assert result.category == "Café"
    assert result.examples == 3
    assert result.confidence == Decimal("1.000")
    with session_scope() as session:
        assert classifier_status(session, 1) == {
            "examples": 3, "categories": 1, "ready_names": 1,
            "min_examples": 3, "algorithm": "unanimous_name_consensus",
        }


@pytest.mark.parametrize("count", [0, 1, 2])
def test_insufficient_evidence_abstains(in_memory_db, count):
    _seed(count=count)
    assert _predict() is None


def test_any_category_disagreement_abstains_even_with_large_majority(in_memory_db):
    _seed(count=20)
    _seed(category="Supermercado", count=1)
    assert _predict() is None
    with session_scope() as session:
        status = classifier_status(session, 1)
        assert status["examples"] == 21
        assert status["categories"] == 2
        assert status["ready_names"] == 0


def test_user_histories_never_train_or_poison_one_another(in_memory_db):
    _seed(user=1)
    assert _predict(user=2) is None
    _seed(user=2, category="Supermercado")
    assert _predict(user=1).category == "Café"
    assert _predict(user=2).category == "Supermercado"
    with session_scope() as session:
        assert classifier_status(session, 3)["examples"] == 0


@pytest.mark.parametrize("source", ["fixed_payment", "recurring", "internal", "unknown", "classifier"])
def test_system_and_unknown_sources_never_train(in_memory_db, source):
    _seed(source=source)
    assert _predict() is None
    with session_scope() as session:
        assert classifier_status(session, 1)["examples"] == 0


@pytest.mark.parametrize("source", ["telegram", "legacy", "receipt"])
def test_normal_user_sources_are_eligible(in_memory_db, source):
    _seed(source=source)
    assert _predict().category == "Café"


def test_excluded_rows_cannot_count_towards_minimum_or_poison_consensus(in_memory_db):
    _seed(count=2)
    _seed(count=8, deleted=True)
    _seed(count=3, source="recurring")
    assert _predict() is None
    _seed(count=1)
    _seed(category="Supermercado", deleted=True)
    _seed(category="Supermercado", source="fixed_payment")
    result = _predict()
    assert result.category == "Café" and result.examples == 3


def test_inactive_category_is_not_learned(in_memory_db):
    _seed()
    with session_scope() as session:
        session.execute(select(Category).where(Category.name == "Café")).scalar_one().is_active = False
    assert _predict() is None
    with session_scope() as session:
        assert classifier_status(session, 1)["categories"] == 0


def test_category_name_ambiguous_in_tree_abstains(in_memory_db):
    _seed()
    with session_scope() as session:
        session.add(Category(name="Café", parent_id=None, is_active=True))
    assert _predict() is None
    with session_scope() as session:
        assert classifier_status(session, 1)["ready_names"] == 0


def test_case_accents_spacing_and_terminal_punctuation_share_evidence(in_memory_db):
    _seed("CAFÉ  DEL CENTRO", count=1)
    _seed("Café del centro!", count=1)
    _seed("cafe del centro", count=1)
    result = _predict(" Café del Centro. ")
    assert result.category == "Café" and result.examples == 3


def test_word_boundaries_and_unknown_names_do_not_generalize_unsafely(in_memory_db):
    _seed("Bar Casa")
    assert _predict("Barcasa") is None
    assert _predict("Bar Casa delivery") is None
    assert _predict("Un comercio desconocido") is None


@pytest.mark.parametrize("name", ["", "   ", "!!!", "1234", "Gasto", "COMPRA", "Ticket", None, "x" * 201])
def test_empty_generic_or_invalid_names_never_predict(in_memory_db, name):
    _seed(name=name or "Gasto")
    assert _predict(name) is None


def test_model_is_rebuilt_after_edits_and_keeps_no_stale_cache(in_memory_db):
    ids = _seed()
    assert _predict().category == "Café"
    with session_scope() as session:
        category = session.execute(select(Category.id).where(Category.name == "Supermercado")).scalar_one()
        session.get(Expense, ids[0]).category_id = category
    assert _predict() is None


def test_classifier_reads_issue_no_writes_and_do_not_autoflush(in_memory_db):
    _seed()
    engine = get_engine()
    writes = []

    def inspect_statement(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    with Session(engine, autoflush=True) as session:
        category = session.execute(select(Category.id).limit(1)).scalar_one()
        pending = Expense(
            telegram_user_id=1, name="Pending", amount=Decimal("1"), currency="ARS",
            category_id=category, expense_date=date(2026, 11, 2), original_message="not saved",
        )
        session.add(pending)
        event.listen(engine, "before_cursor_execute", inspect_statement)
        try:
            assert predict_category(session, 1, "Starbucks").examples == 3
            assert classifier_status(session, 1)["examples"] == 3
            assert pending in session.new and pending.id is None
        finally:
            event.remove(engine, "before_cursor_execute", inspect_statement)
            session.rollback()
    assert writes == []
