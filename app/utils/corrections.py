"""Parse user-typed corrections to a pending receipt extraction.

When the bot extracts fields from a receipt and the user taps 'Corregir',
their next free-text message is interpreted here. Supports field-specific
patterns (monto:, nombre:, fecha:) and a bare number heuristic for the
amount.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

_MONTO_RE = re.compile(
    r"^(?:monto|total|precio|cu[aá]nto)\s*[:=]?\s*"
    r"(\d+(?:[.,]\d{1,2})?)$",
    re.IGNORECASE,
)
_NOMBRE_RE = re.compile(
    r"^(?:nombre|comercio|lugar)\s*[:=]?\s*(.+)$",
    re.IGNORECASE,
)
_FECHA_RE = re.compile(
    r"^(?:fecha)\s*[:=]?\s*(\d{4}-\d{2}-\d{2})$",
    re.IGNORECASE,
)
_DATE_RELAXED = re.compile(
    r"^(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?$"
)


def _to_decimal(text: str) -> Decimal | None:
    cleaned = text.strip().replace(",", ".")
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def parse_correction(text: str, current: dict, today: date) -> dict:
    """Apply a user correction to a pending receipt draft.

    Patterns (in priority order):
    - ``monto: 19280.50`` or ``total: 19280``  -> amount
    - ``nombre: Carrefour``                    -> name
    - ``fecha: 2026-09-08``                   -> date
    - Bare number (e.g. ``19280.50``)         -> amount (heuristic)
    - Anything else                            -> name (catch-all)

    Returns a new dict with corrections applied.
    """
    text = (text or "").strip()
    if not text:
        return dict(current)
    result = dict(current)

    m = _FECHA_RE.match(text)
    if m:
        result["date"] = text.split(":", 1)[1].strip()
        return result

    m = _MONTO_RE.match(text)
    if m:
        amt = _to_decimal(m.group(1))
        if amt is not None and amt > 0:
            result["amount"] = amt
            result["confidence"] = 1.0
            return result

    m = _NOMBRE_RE.match(text)
    if m:
        result["name"] = m.group(1).strip()
        return result

    bare = _to_decimal(text)
    if bare is not None and bare > 0:
        result["amount"] = bare
        result["confidence"] = 1.0
        return result

    result["name"] = text
    return result
