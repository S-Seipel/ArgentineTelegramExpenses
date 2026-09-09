from __future__ import annotations

from app.categories.categories import (
    DEFAULT_CATEGORY,
    get_categories,
    get_leaf_names,
    get_parent_of,
    is_valid_category,
    normalize_category,
)


def test_default_categories_loaded() -> None:
    leaves = get_leaf_names()
    assert "Café" in leaves
    assert "Uber" in leaves
    assert DEFAULT_CATEGORY in leaves


def test_parent_resolution() -> None:
    assert get_parent_of("Café") == "Comida"
    assert get_parent_of("Uber") == "Transporte"
    assert get_parent_of("Otros") is None


def test_is_valid_category() -> None:
    assert is_valid_category("Café")
    assert is_valid_category("Otros")
    assert not is_valid_category("Auto eléctrico")
    assert not is_valid_category(None)


def test_normalize_category_falls_back_to_otros() -> None:
    assert normalize_category("Café") == "Café"
    assert normalize_category("café") == "Café"
    assert normalize_category("uber") == "Uber"
    assert normalize_category("Auto eléctrico") == DEFAULT_CATEGORY
    assert normalize_category(None) == DEFAULT_CATEGORY


def test_categories_structure() -> None:
    tree = get_categories()
    assert "Comida" in tree
    assert "Café" in tree["Comida"]
