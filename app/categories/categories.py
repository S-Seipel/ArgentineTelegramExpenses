"""Backward-compatible façade over the registry.

The legacy module-level names are kept here so existing call sites
(``from app.categories.categories import normalize_category``) keep working.
"""
from app.categories.registry import (
    DEFAULT_CATEGORY,
    CategoryRow,
    clear,
    get_categories,
    get_category_tree,
    get_leaf_names,
    get_parent_of,
    icon_for,
    is_valid_category,
    list_categories_text,
    normalize_category,
    refresh_from_db,
)
from app.categories.categories_legacy import Category

__all__ = [
    "Category",
    "CategoryRow",
    "DEFAULT_CATEGORY",
    "clear",
    "get_categories",
    "get_category_tree",
    "get_leaf_names",
    "get_parent_of",
    "icon_for",
    "is_valid_category",
    "list_categories_text",
    "normalize_category",
    "refresh_from_db",
]
