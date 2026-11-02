"""Fixed-expense occurrence resolution for the MoneyEngine.

Two responsibilities:

1. **Current cycle occurrences.** For every active ``FixedExpense`` of
   the user, emit an occurrence whose ``due_day_of_month`` falls in the
   open cycle window. If a ``FixedExpensePayment`` row exists for that
   ``(fixed_expense_id, month_year)`` it resolves the occurrence to one
   of three states: ``paid`` (counted as realized via the expense
   mirror, excluded from pending), ``skipped`` (excluded entirely), or
   ``pending`` (counted in pending_fixed_obligations with the
   ``expected_amount``).

2. **Carry-over.** When a pending occurrence from a previous cycle
   was never paid, it propagates to the next cycle as a single
   obligation regardless of how many cycles elapsed. Identity is
   ``(fixed_expense_id, original_month_year)``; we de-duplicate via
   that key so a 4-cycle-old LUZ bill stays at 80, never 80+80+80+80.

The module is pure SQL/ORM work. No mutation, no engine state.
``MoneyEngine`` calls ``resolve_current_obligations`` and
``propagate_carry_over`` to build the ``pending_fixed_obligations`` and
``carried_over_fixed`` buckets.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.fixed_expenses.models import (
    FixedExpense,
    FixedExpensePayment,
)
from app.money.dto import CarriedOverFixedOccurrence
from app.money.models import FinancialCycle


def occurrence_month_year(due_date: date) -> str:
    """``YYYY-MM`` string for the given due date."""
    return f"{due_date.year:04d}-{due_date.month:02d}"


def fixed_payment_source_key(fixed_id: int, month_year: str) -> str:
    """Stable source_key for a (bill, month) occurrence. Mirrors
    ``FixedExpenseService.fixed_payment_source_key`` so the engine's
    invariant detection agrees with the domain that produces the rows.
    """
    return f"fixed-payment:{fixed_id}:{month_year}"


@dataclass(frozen=True)
class FixedOccurrence:
    """One occurrence of a fixed expense inside a cycle window."""

    fixed_expense_id: int
    name: str
    expected_amount: Decimal
    currency: str
    due_date: date
    month_year: str  # 'YYYY-MM' of the due_date
    payment_id: Optional[int]
    paid: bool
    skipped: bool
    original_month_year: str  # frozen; == month_year for fresh occurrences


@dataclass(frozen=True)
class ResolvedFixedObligations:
    """All occurrences for a cycle (current + carry-over) and the
    currency-partitioned totals the engine needs.
    """

    occurrences: tuple[FixedOccurrence, ...]
    pending_by_currency: dict[str, Decimal]
    carried_over: tuple[CarriedOverFixedOccurrence, ...]


# Maximum number of cycles to walk backwards when resolving carry-over.
# Bounds the recursive scan; in practice an unpaid LUZ bill does not
# survive more than a handful of cycles, but we cap defensively.
_MAX_CARRY_OVER_SCAN = 24


def resolve_current_obligations(
    session: Session,
    user_id: int,
    cycle: FinancialCycle,
) -> ResolvedFixedObligations:
    """Resolve every fixed obligation that affects ``cycle``.

    Steps:

    A. Compute the previous closed cycle (the one immediately
       preceding ``cycle`` by ``cycle_index``). If it has a snapshot
       and that snapshot lists carried-over occurrences, fold them in
       as ``carried_over`` history.
    B. Generate the fresh occurrences for ``cycle``: for every active
       ``FixedExpense`` of the user, every ``due_day_of_month`` that
       falls in ``[start_date, end_date]`` becomes one occurrence.
    C. Combine A + B and deduplicate by
       ``(fixed_expense_id, original_month_year)`` so a single unpaid
       obligation is counted exactly once. The carry-over identity
       wins: a carried-over Sep obligation is the same obligation as
       a hypothetical fresh Sep occurrence, so the fresh one is
       suppressed (the carried-over amount is frozen at the
       historical value, NOT the current template's
       ``expected_amount``).
    D. Cross-reference each occurrence with ``FixedExpensePayment`` to
       classify it as paid/skipped/pending.
    """
    pending: dict[tuple[int, str], FixedOccurrence] = {}

    # --- (A) carry-over from the previous cycle ---
    carried_over: list[CarriedOverFixedOccurrence] = []
    prev_cycle = _previous_cycle(session, user_id, cycle)
    if prev_cycle is not None and prev_cycle.closing_snapshot:
        from app.money.snapshot import ensure_snapshot_version

        ensure_snapshot_version(prev_cycle.closing_snapshot)
        for entry in prev_cycle.closing_snapshot.get("carried_over_fixed", []):
            occ = CarriedOverFixedOccurrence(
                fixed_id=int(entry["fixed_id"]),
                name=str(entry["name"]),
                expected_amount=Decimal(str(entry["expected_amount"])),
                currency=str(entry["currency"]),
                due_date=_parse_iso_date(entry["due_date"]),
                original_month_year=str(entry["original_month_year"]),
            )
            carried_over.append(occ)

    # Seed the pending map from carry-over entries. They retain their
    # original_month_year so later cycles see the same identity.
    for occ in carried_over:
        key = (occ.fixed_id, occ.original_month_year)
        payment = _lookup_payment(
            session, occ.fixed_id, occ.original_month_year
        )
        payment_id = payment.id if payment else None
        paid = bool(
            payment
            and payment.paid_at is not None
            and not payment.skipped
        )
        skipped = bool(payment and payment.skipped)
        pending[key] = FixedOccurrence(
            fixed_expense_id=occ.fixed_id,
            name=occ.name,
            expected_amount=occ.expected_amount,
            currency=occ.currency,
            due_date=occ.due_date,
            month_year=occ.original_month_year,
            payment_id=payment_id,
            paid=paid,
            skipped=skipped,
            original_month_year=occ.original_month_year,
        )

    # --- (B) fresh occurrences for the current cycle ---
    # A carry-over entry "occupies" the fixed_id slot: while the carry-over
    # is unresolved, the fresh occurrence that would normally be generated
    # for this cycle is suppressed. The carried-over entry is the canonical
    # obligation (with its historical frozen amount) until it is paid or
    # skipped. Spec §7 example: Sep LUZ 80 unpaid → Oct pending 80 (not
    # 160), so the fresh Oct occurrence must NOT be created on top of the
    # carry-over.
    blocked_fixed_ids: set[int] = set()
    for occ in carried_over:
        payment = _lookup_payment(
            session, occ.fixed_id, occ.original_month_year
        )
        resolved = bool(
            payment and (payment.paid_at is not None or payment.skipped)
        )
        if not resolved:
            blocked_fixed_ids.add(occ.fixed_id)

    templates = list(
        session.execute(
            select(FixedExpense).where(
                FixedExpense.telegram_user_id == user_id,
                FixedExpense.is_active.is_(True),
            )
        ).scalars()
    )
    for tmpl in templates:
        if tmpl.id in blocked_fixed_ids:
            # Carry-over for this template is still pending — do NOT
            # emit a fresh occurrence. The carried-over obligation
            # stands in for it (with the frozen historical amount).
            continue
        for due in _due_dates_in_window(tmpl, cycle):
            month_year = occurrence_month_year(due)
            key = (tmpl.id, month_year)
            if key in pending:
                continue
            payment = _lookup_payment(session, tmpl.id, month_year)
            payment_id = payment.id if payment else None
            paid = bool(
                payment
                and payment.paid_at is not None
                and not payment.skipped
            )
            skipped = bool(payment and payment.skipped)
            pending[key] = FixedOccurrence(
                fixed_expense_id=tmpl.id,
                name=tmpl.name,
                expected_amount=tmpl.expected_amount,
                currency=tmpl.currency,
                due_date=due,
                month_year=month_year,
                payment_id=payment_id,
                paid=paid,
                skipped=skipped,
                original_month_year=month_year,
            )

    # --- (C)+(D) aggregate ---
    pending_by_currency: dict[str, Decimal] = {}
    for occ in pending.values():
        if occ.paid or occ.skipped:
            continue
        cur = occ.currency.upper()
        pending_by_currency[cur] = pending_by_currency.get(
            cur, Decimal("0")
        ) + occ.expected_amount

    return ResolvedFixedObligations(
        occurrences=tuple(pending.values()),
        pending_by_currency=pending_by_currency,
        carried_over=tuple(carried_over),
    )


def resolve_carry_over_for_close(
    session: Session,
    user_id: int,
    cycle: FinancialCycle,
    *,
    previous_carried_over: tuple[CarriedOverFixedOccurrence, ...],
) -> tuple[CarriedOverFixedOccurrence, ...]:
    """Build the carried-over list that goes into the closing snapshot.

    Mirrors the live computation but stops at "unresolved" occurrences
    (no paid/skipped payment) so the next cycle picks up exactly the
    debt that survived. ``previous_carried_over`` is the snapshot of
    the immediately previous cycle so this function can do its own
    transitive merge without reading the previous cycle row again.

    A template that already has an unresolved carry-over does NOT
    contribute a fresh occurrence to the closing snapshot: the
    carried-over entry is the canonical obligation and the fresh one
    would be a duplicate.
    """
    pending: dict[tuple[int, str], CarriedOverFixedOccurrence] = {}

    blocked_fixed_ids: set[int] = set()
    for occ in previous_carried_over:
        payment = _lookup_payment(
            session, occ.fixed_id, occ.original_month_year
        )
        if payment and (payment.paid_at is not None or payment.skipped):
            continue
        key = (occ.fixed_id, occ.original_month_year)
        pending[key] = occ
        blocked_fixed_ids.add(occ.fixed_id)

    templates = list(
        session.execute(
            select(FixedExpense).where(
                FixedExpense.telegram_user_id == user_id,
                FixedExpense.is_active.is_(True),
            )
        ).scalars()
    )
    for tmpl in templates:
        if tmpl.id in blocked_fixed_ids:
            continue
        for due in _due_dates_in_window(tmpl, cycle):
            month_year = occurrence_month_year(due)
            key = (tmpl.id, month_year)
            if key in pending:
                continue
            payment = _lookup_payment(session, tmpl.id, month_year)
            if payment and (payment.paid_at is not None or payment.skipped):
                continue
            pending[key] = CarriedOverFixedOccurrence(
                fixed_id=tmpl.id,
                name=tmpl.name,
                expected_amount=tmpl.expected_amount,
                currency=tmpl.currency,
                due_date=due,
                original_month_year=month_year,
            )

    return tuple(pending.values())


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _previous_cycle(
    session: Session, user_id: int, cycle: FinancialCycle
) -> Optional[FinancialCycle]:
    stmt = (
        select(FinancialCycle)
        .where(
            FinancialCycle.telegram_user_id == user_id,
            FinancialCycle.cycle_index < cycle.cycle_index,
        )
        .order_by(FinancialCycle.cycle_index.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def _lookup_payment(
    session: Session, fixed_expense_id: int, month_year: str
) -> Optional[FixedExpensePayment]:
    stmt = select(FixedExpensePayment).where(
        FixedExpensePayment.fixed_expense_id == fixed_expense_id,
        FixedExpensePayment.month_year == month_year,
    )
    return session.execute(stmt).scalar_one_or_none()


def _due_dates_in_window(
    tmpl: FixedExpense, cycle: FinancialCycle
) -> list[date]:
    """Return the list of due dates ``tmpl.due_day_of_month`` produces
    within ``[cycle.start_date, cycle.end_date]``.

    A financial cycle is at most one month long (payday-driven, 28-31
    days) but can straddle two calendar months (e.g. payday=15 ⇒ cycle
    ``Nov 15..Dec 14`` spans both Nov and Dec). We therefore enumerate
    each calendar month overlapping the window and check whether the
    clamped due_day falls inside the cycle. Clamping matches the
    payday convention in :func:`app.money.cycle_resolution.payday_for`:
    ``due_day=31`` in February becomes the last day of February.
    """
    from app.utils.dates import last_day_of_month

    start = cycle.start_date
    end = cycle.end_date
    due_day = max(1, min(31, int(tmpl.due_day_of_month or 1)))
    if start > end:
        return []

    out: list[date] = []
    # First month overlapping the window.
    cursor_year, cursor_month = start.year, start.month
    safety = 0
    last_month_index = (end.year * 12) + (end.month - 1)
    while safety < _MAX_CARRY_OVER_SCAN:
        cursor_index = (cursor_year * 12) + (cursor_month - 1)
        if cursor_index > last_month_index:
            break
        last_day = last_day_of_month(cursor_year, cursor_month)
        day = min(due_day, last_day)
        candidate = date(cursor_year, cursor_month, day)
        if start <= candidate <= end:
            out.append(candidate)
        cursor_year, cursor_month = _next_month_year(
            cursor_year, cursor_month
        )
        safety += 1
    return out


def _next_month_year(year: int, month: int) -> tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def _parse_iso_date(value: object) -> date:
    """Parse an ISO date string (or pass through a ``date`` instance).

    Kept private because the snapshot dict is owned by us — no need to
    worry about exotic formats.
    """
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


__all__ = [
    "CarriedOverFixedOccurrence",
    "FixedOccurrence",
    "ResolvedFixedObligations",
    "fixed_payment_source_key",
    "occurrence_month_year",
    "resolve_carry_over_for_close",
    "resolve_current_obligations",
]
