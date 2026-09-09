"""Categories service.

Two responsibilities, very deliberately split:

* ``registry`` — read-only views over the in-memory cache that the AI prompt
  uses. It is hydrated from the database at startup and refreshed whenever
  admin code mutates the ``categories`` table.
* ``repository`` — the only place that issues INSERT/UPDATE/DELETE on the
  ``categories`` table, used by Alembic seeds and any future admin tooling.

The hard-coded list lives in ``app.categories.seed``. The first run
hydrates that seed into the DB via Alembic, after which the DB is the
source of truth.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)


DEFAULT_CATEGORY = "Otros"


@dataclass(frozen=True)
class CategoryRow:
    name: str
    parent: str | None = None
    icon: str | None = None


_LOCK = threading.RLock()
_TREE: dict[str, list[str]] = {}
_NAME_TO_PARENT: dict[str, str | None] = {}
_ICON_BY_NAME: dict[str, str] = {}
_LEAF_NAMES: set[str] = set()


def hydrate(rows: Sequence[CategoryRow]) -> None:
    """Replace the in-memory cache. Called by ``refresh_from_db``."""
    with _LOCK:
        _TREE.clear()
        _NAME_TO_PARENT.clear()
        _ICON_BY_NAME.clear()
        _LEAF_NAMES.clear()
        for row in rows:
            _ICON_BY_NAME[row.name] = row.icon or ""
            if row.parent is None:
                _TREE.setdefault(row.name, [])
                _NAME_TO_PARENT.setdefault(row.name, None)
            else:
                _TREE.setdefault(row.parent, []).append(row.name)
                _NAME_TO_PARENT[row.name] = row.parent
        for parent, children in _TREE.items():
            _LEAF_NAMES.add(parent)
            _LEAF_NAMES.update(children)


def clear() -> None:
    with _LOCK:
        _TREE.clear()
        _NAME_TO_PARENT.clear()
        _ICON_BY_NAME.clear()
        _LEAF_NAMES.clear()


def refresh_from_db(session_factory) -> None:
    """Read all active categories from the DB and refresh the in-memory cache."""
    from app.categories.models import Category

    with session_factory() as session:
        rows = (
            session.query(Category)
            .filter(Category.is_active.is_(True))
            .all()
        )

    category_rows: list[CategoryRow] = []
    by_id_name: dict[tuple[int, str], CategoryRow] = {}
    for cat in rows:
        cr = CategoryRow(
            name=cat.name,
            icon=cat.icon,
            parent=None,
        )
        by_id_name[(cat.id, cat.name)] = cr
        category_rows.append(cr)

    for cat in rows:
        if cat.parent_id is None:
            continue
        for parent in rows:
            if parent.id == cat.parent_id:
                cr = by_id_name[(cat.id, cat.name)]
                object.__setattr__(cr, "parent", parent.name)
                break

    hydrate(category_rows)
    logger.info("Categories registry refreshed: %d rows", len(rows))


def get_categories() -> dict[str, list[str]]:
    with _LOCK:
        return {k: list(v) for k, v in _TREE.items()}


def get_category_tree() -> list["Category"]:
    """Return the cached tree in the legacy dataclass shape."""
    from app.categories.categories_legacy import Category  # noqa: F401

    tree: list[Category] = []
    with _LOCK:
        snapshot = {k: list(v) for k, v in _TREE.items()}
    for parent, children in snapshot.items():
        tree.append(Category(name=parent, children=children))
        for child in children:
            tree.append(Category(name=child, parent=parent))
    return tree


def get_leaf_names() -> list[str]:
    with _LOCK:
        return sorted(_LEAF_NAMES)


def get_parent_of(leaf: str) -> str | None:
    with _LOCK:
        return _NAME_TO_PARENT.get(leaf)


def is_valid_category(name: str | None) -> bool:
    if not name:
        return False
    with _LOCK:
        return name in _LEAF_NAMES


def normalize_category(name: str | None) -> str:
    """Return the closest valid category. Falls back to ``DEFAULT_CATEGORY``."""
    if not name:
        return DEFAULT_CATEGORY
    cleaned = name.strip()
    with _LOCK:
        leaves = list(_LEAF_NAMES)
    if not leaves:
        return DEFAULT_CATEGORY
    for leaf in leaves:
        if leaf.lower() == cleaned.lower():
            return leaf
    low = cleaned.lower()
    for leaf in leaves:
        if low in leaf.lower() or leaf.lower() in low:
            return leaf
    return DEFAULT_CATEGORY


def list_categories_text() -> str:
    """Human-readable, multi-line representation used in the AI prompt."""
    lines: list[str] = []
    tree = get_categories()
    for parent, children in tree.items():
        lines.append(parent)
        for child in children:
            lines.append(f"  - {child}")
    return "\n".join(lines)


def icon_for(name: str | None) -> str | None:
    if not name:
        return None
    with _LOCK:
        return _ICON_BY_NAME.get(name) or None
