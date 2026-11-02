"""Closing-snapshot serializer/parser for the F1 money domain.

Pure module. No DB. No mutations. Owns:

- The v1 snapshot shape (locked — readers must reject anything else).
- ``serialize_snapshot`` for building the dict that gets persisted on
  :class:`app.money.models.FinancialCycle.closing_snapshot`.
- ``ensure_snapshot_version`` so readers can't silently interpret
  unknown schemas.
- ``build_closing_snapshot`` — convenience wrapper that returns the
  JSON-safe payload for a given ``CycleSummary``.

CycleService is the only caller that writes the produced payload back
to the DB; the snapshot module itself never touches a session.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from app.money.dto import (
    CarriedOverFixedOccurrence,
    CycleSummary,
    MoneyByCurrency,
)


CURRENT_SNAPSHOT_VERSION = 1


def serialize_snapshot(
    summary: CycleSummary,
    *,
    closed_at: datetime,
    closed_by: str,
) -> dict[str, Any]:
    """Render ``summary`` into a JSON-safe dict tagged with version 1.

    Pure function. Callers must persist the result themselves in the
    same transaction that captured ``summary``.
    """
    money = {
        "income": summary.base_income.to_dict(),
        "extra": summary.extra_income.to_dict(),
        "gross_income": summary.gross_income.to_dict(),
        "savings_target": summary.savings_target.to_dict(),
        "savings_reserved": summary.savings_reserved.to_dict(),
        "savings_realized": summary.savings_realized.to_dict(),
        "savings_committed": summary.savings_committed.to_dict(),
        "realized_expenses": summary.realized_expenses.to_dict(),
        "pending_fixed_obligations": (
            summary.pending_fixed_obligations.to_dict()
        ),
        "pending_recurring_obligations": (
            summary.pending_recurring_obligations.to_dict()
        ),
        "pending_reserved_obligations": (
            summary.pending_reserved_obligations.to_dict()
        ),
        "available_to_spend": summary.available_to_spend.to_dict(),
        "daily_safe_to_spend": (
            summary.daily_safe_to_spend.to_dict()
            if summary.daily_safe_to_spend is not None
            else None
        ),
        "warnings": list(summary.warnings),
    }
    return {
        "snapshot_version": CURRENT_SNAPSHOT_VERSION,
        "money": money,
        "closed_at": _iso(closed_at),
        "closed_by": closed_by,
        "carried_over_fixed": [
            _serialize_carried_over(occ) for occ in summary.carried_over_fixed
        ],
    }


def build_closing_snapshot(
    summary: CycleSummary,
    carried_over: Iterable[CarriedOverFixedOccurrence],
    *,
    closed_at: datetime,
    closed_by: str,
) -> dict[str, Any]:
    """Compose the persisted snapshot for a closing cycle.

    Combines the live read model (``summary``) with the explicit
    carry-over list produced by :mod:`app.money.fixed_obligations`
    and stamps the version/closed metadata. Pure function — the
    caller (typically :class:`CycleService`) is responsible for
    persisting the returned dict in the same transaction.
    """
    summary_with_carry = replace(
        summary,
        carried_over_fixed=tuple(carried_over),
        daily_safe_to_spend=None,
        remaining_days=None,
    )
    return serialize_snapshot(
        summary_with_carry, closed_at=closed_at, closed_by=closed_by
    )


def _serialize_carried_over(occ: CarriedOverFixedOccurrence) -> dict[str, Any]:
    return {
        "fixed_id": occ.fixed_id,
        "name": occ.name,
        "expected_amount": str(occ.expected_amount),
        "currency": occ.currency,
        "due_date": occ.due_date.isoformat(),
        "original_month_year": occ.original_month_year,
    }


def ensure_snapshot_version(payload: dict[str, Any]) -> int:
    """Validate the snapshot version. Return it on success.

    Raises ``SnapshotVersionMismatchError`` on missing/unknown versions
    so callers cannot accidentally consume a future schema they don't
    understand.
    """
    from app.money.engine import SnapshotVersionMismatchError

    if not isinstance(payload, dict):
        raise SnapshotVersionMismatchError(
            "closing_snapshot is not a dict"
        )
    raw = payload.get("snapshot_version")
    if raw is None:
        raise SnapshotVersionMismatchError(
            "closing_snapshot missing snapshot_version field"
        )
    # JSON booleans and fractional numbers must not become version 1 via
    # Python's int coercion: an unknown schema cannot be read as v1.
    if type(raw) is not int:
        raise SnapshotVersionMismatchError(
            f"closing_snapshot snapshot_version is not an int: {raw!r}"
        )
    version = raw
    if version != CURRENT_SNAPSHOT_VERSION:
        raise SnapshotVersionMismatchError(
            f"closing_snapshot version {version} is not supported "
            f"(engine understands version {CURRENT_SNAPSHOT_VERSION})"
        )
    return version


def deserialize_carried_over(payload: Any) -> tuple[CarriedOverFixedOccurrence, ...]:
    """Decode the ``carried_over_fixed`` block back into DTOs.

    The engine calls this to project a closed-cycle snapshot into the
    next cycle's ``CycleSummary.carried_over`` field without recomputing
    the original occurrence.
    """
    if not payload:
        return ()
    out: list[CarriedOverFixedOccurrence] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        out.append(
            CarriedOverFixedOccurrence(
                fixed_id=int(entry["fixed_id"]),
                name=str(entry["name"]),
                expected_amount=Decimal(str(entry["expected_amount"])),
                currency=str(entry["currency"]),
                due_date=date.fromisoformat(str(entry["due_date"])),
                original_month_year=str(entry["original_month_year"]),
            )
        )
    return tuple(out)


def deserialize_money_block(payload: dict[str, Any]) -> dict[str, MoneyByCurrency]:
    """Project the ``money`` block into a dict of ``MoneyByCurrency``."""
    out: dict[str, MoneyByCurrency] = {}
    for key, value in payload.items():
        if key == "warnings":
            continue
        if isinstance(value, dict):
            out[key] = MoneyByCurrency(
                {k: Decimal(v) for k, v in value.items()}
            )
    return out


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


__all__ = [
    "CURRENT_SNAPSHOT_VERSION",
    "build_closing_snapshot",
    "deserialize_carried_over",
    "deserialize_money_block",
    "ensure_snapshot_version",
    "serialize_snapshot",
]
