"""CRUD over the ``categories`` table.

The first time the table is empty, ``ensure_seed`` populates it from the
shipped seed list. Otherwise the DB is authoritative.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.categories.models import Category
from app.categories.seed import DEFAULT_CATEGORY_TREE, iter_seed_rows


class CategoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, category_id: int) -> Optional[Category]:
        return self.session.get(Category, category_id)

    def by_name(self, name: str) -> Optional[Category]:
        return (
            self.session.query(Category)
            .filter(Category.name == name)
            .one_or_none()
        )

    def find_or_create(
        self,
        name: str,
        *,
        parent_name: str | None = None,
        icon: str | None = None,
        is_active: bool = True,
    ) -> Category:
        existing = self.by_name(name)
        if existing is not None:
            return existing

        parent: Optional[Category] = None
        if parent_name is not None:
            parent = self.by_name(parent_name)
            if parent is None:
                parent = self.find_or_create(parent_name, is_active=is_active)

        cat = Category(
            name=name,
            parent_id=parent.id if parent is not None else None,
            icon=icon,
            is_active=is_active,
        )
        self.session.add(cat)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing = self.by_name(name)
            if existing is None:
                raise
            return existing
        self.session.refresh(cat)
        return cat

    def list_active(self) -> list[Category]:
        return (
            self.session.query(Category)
            .filter(Category.is_active.is_(True))
            .order_by(Category.parent_id.nulls_first(), Category.name)
            .all()
        )

    def count(self) -> int:
        return self.session.query(Category).count()


def ensure_seed(repo: CategoryRepository) -> list[Category]:
    """Populate the ``categories`` table from the shipped seed if empty."""
    if repo.count() > 0:
        return repo.list_active()

    created: list[Category] = []
    parent_ids: dict[str, int] = {}
    for parent_name, name, icon in iter_seed_rows():
        if parent_name is None:
            cat = repo.find_or_create(name, icon=icon)
            parent_ids[name] = cat.id
            created.append(cat)
        else:
            parent = repo.by_name(parent_name)
            cat = Category(
                name=name,
                parent_id=parent.id if parent else None,
                icon=icon,
                is_active=True,
            )
            repo.session.add(cat)
            repo.session.commit()
            repo.session.refresh(cat)
            created.append(cat)
    return created
