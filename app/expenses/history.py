"""Transaction-local expense snapshots and the durable undo stack."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.expenses.models import Expense, ExpenseChange


class UndoConflictError(RuntimeError):
    """The expense changed outside this undo chain; no data was overwritten."""


def lock_user_history(session: Session, user_id: int) -> None:
    """Serialize only a user's expense edits and undo operations on PG."""
    if session.get_bind().dialect.name == "postgresql":
        digest = hashlib.sha256(f"expense-history:{user_id}".encode()).digest()
        session.execute(select(func.pg_advisory_xact_lock(int.from_bytes(digest[:8], "big", signed=True))))


def _datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def snapshot(row: Expense) -> dict:
    """Exact JSON values; neither floats nor ORM representations are stored."""
    return {
        "name": row.name, "name_key": row.name_key,
        "amount": format(Decimal(row.amount).quantize(Decimal("0.01")), "f"),
        "currency": row.currency, "category_id": row.category_id,
        "expense_date": row.expense_date.isoformat(),
        "original_message": row.original_message or "",
        "ai_confidence": format(Decimal(row.ai_confidence).quantize(Decimal("0.001")), "f"),
        "source_type": row.source_type, "source_key": row.source_key,
        "telegram_chat_id": row.telegram_chat_id, "telegram_message_id": row.telegram_message_id,
        "deleted_at": _datetime(row.deleted_at),
        "latitude": format(Decimal(row.latitude).quantize(Decimal("0.0000001")), "f") if row.latitude is not None else None,
        "longitude": format(Decimal(row.longitude).quantize(Decimal("0.0000001")), "f") if row.longitude is not None else None,
        "location_recorded_at": _datetime(row.location_recorded_at),
    }


def restore_snapshot(row: Expense, values: dict) -> None:
    for field in ("name", "name_key", "currency", "category_id", "original_message", "source_type", "source_key", "telegram_chat_id", "telegram_message_id"):
        setattr(row, field, values[field])
    row.amount = Decimal(values["amount"])
    row.ai_confidence = Decimal(values["ai_confidence"])
    row.expense_date = date.fromisoformat(values["expense_date"])
    for field in ("deleted_at", "location_recorded_at"):
        setattr(row, field, datetime.fromisoformat(values[field]) if values[field] is not None else None)
    for field in ("latitude", "longitude"):
        setattr(row, field, Decimal(values[field]) if values[field] is not None else None)


def record_change(session: Session, row: Expense, action: str, before: dict, before_revision: int) -> None:
    after = snapshot(row)
    if after == before:
        return
    session.add(ExpenseChange(
        telegram_user_id=row.telegram_user_id, expense_id=row.id, action=action,
        before_snapshot=before, after_snapshot=after,
        before_revision=before_revision, after_revision=row.revision,
        expected_revision=row.revision,
    ))
    session.flush()
    older = session.execute(select(ExpenseChange).where(
        ExpenseChange.telegram_user_id == row.telegram_user_id,
        ExpenseChange.status == "pending",
    ).order_by(ExpenseChange.id.desc()).offset(10)).scalars().all()
    for change in older:
        change.status = "expired"
