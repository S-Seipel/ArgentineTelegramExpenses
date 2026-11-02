"""Local category learning with a conservative, per-user consensus gate.

The model is rebuilt from SQL aggregates for the requesting user. A familiar
name needs at least three active, normal expense examples and unanimous
category labels. New names, disagreements and ambiguous category names are
left to the normal AI path. Confidence describes observed agreement; it is
not a calibrated probability of correctness.

There is no global model cache, model file, dependency, mutation or network
call. Read queries explicitly disable autoflush so classification cannot
accidentally persist another caller's pending changes.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
import re
import unicodedata

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.categories.models import Category
from app.expenses.models import Expense


MIN_EXAMPLES = 3
_TRAINING_SOURCES = ("telegram", "legacy", "receipt")
_GENERIC_NAMES = frozenset({"gasto", "compra", "pago", "otros", "varios", "ticket", "recibo", "total"})
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class Prediction:
    category: str
    confidence: Decimal
    examples: int


def _canonical_name(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        return None
    text = "".join(char for char in unicodedata.normalize("NFKD", value).casefold()
                   if not unicodedata.combining(char))
    # Preserve word boundaries: "Bar Casa" and "Barcasa" cannot teach each
    # other's category merely because their compact grouping keys collide.
    key = " ".join(_WORDS.findall(text))
    if not key or key in _GENERIC_NAMES or not any(char.isalpha() for char in key):
        return None
    return key


def _evidence(session: Session, user_id: int):
    stmt = select(
        Expense.name,
        Category.id.label("category_id"),
        Category.name.label("category_name"),
        func.count(Expense.id).label("examples"),
    ).join(Category, Category.id == Expense.category_id).where(
        Expense.telegram_user_id == user_id,
        Expense.deleted_at.is_(None),
        Expense.source_type.in_(_TRAINING_SOURCES),
        Category.is_active.is_(True),
    ).group_by(Expense.name, Category.id, Category.name)
    ambiguous_stmt = select(Category.name).where(
        Category.is_active.is_(True),
    ).group_by(Category.name).having(func.count(Category.id) > 1)
    with session.no_autoflush:
        rows = session.execute(stmt).all()
        ambiguous_labels = set(session.execute(ambiguous_stmt).scalars())
    groups: dict[str, Counter[tuple[int, str]]] = defaultdict(Counter)
    for row in rows:
        key = _canonical_name(row.name)
        if key is not None:
            groups[key][(row.category_id, row.category_name)] += int(row.examples)
    return groups, ambiguous_labels


def _eligible(counts: Counter, ambiguous_labels: set[str]) -> bool:
    if len(counts) != 1:
        return False
    (_, category), examples = next(iter(counts.items()))
    return examples >= MIN_EXAMPLES and category not in ambiguous_labels


def classifier_status(session: Session, user_id: int) -> dict:
    """Return counts of this user's eligible labeled history and ready names."""
    groups, ambiguous_labels = _evidence(session, user_id)
    return {
        "examples": sum(sum(counts.values()) for counts in groups.values()),
        "categories": len({category_id for counts in groups.values()
                           for category_id, _ in counts}),
        "ready_names": sum(_eligible(counts, ambiguous_labels) for counts in groups.values()),
        "min_examples": MIN_EXAMPLES,
        "algorithm": "unanimous_name_consensus",
    }


def predict_category(session: Session, user_id: int, name: str) -> Prediction | None:
    """Learn from familiar names only; abstain on any uncertain classification."""
    key = _canonical_name(name)
    if key is None:
        return None
    groups, ambiguous_labels = _evidence(session, user_id)
    counts = groups.get(key)
    if not counts or not _eligible(counts, ambiguous_labels):
        return None
    (_, category), examples = next(iter(counts.items()))
    return Prediction(category=category, confidence=Decimal("1.000"), examples=examples)
