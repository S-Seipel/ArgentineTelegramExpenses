"""Stable keys used to group expense names without changing user text."""
from __future__ import annotations

import unicodedata


def normalize_expense_name(name: str | None) -> str:
    """Return a compact, case/accent-insensitive grouping key."""
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name).casefold()
    return "".join(char for char in text if char.isalnum())
