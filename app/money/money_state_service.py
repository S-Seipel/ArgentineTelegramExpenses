"""F1 R3 MoneyStateService — mutations for the financial cycle state.

``MoneyEngine`` (in :mod:`app.money.engine`) is read-only; the lifecycle
methods on ``CycleService`` own cycle opening / closing / rollover.
``MoneyStateService`` is the third owner: the WRITE path for the
income / savings state the engine reads. The dependency arrow stays
one-way:

    MoneyEngine       → CycleService / MoneyStateService → snapshot module
    (read)            (write)

``MoneyStateService`` never calls ``MoneyEngine`` to decide what to
write. After a mutation the caller can hand the same session to
``MoneyEngine.get_cycle_summary`` to read the updated numbers; the
engine never reads what this service is about to write.

All mutations enforce three invariants:

1. The cycle exists and belongs to ``user_id``. A cycle owned by
   another user is rejected (no row leakage).
2. The cycle is ``status='open'``. Closed cycles raise
   :class:`CycleClosedError` so historical state is immutable.
3. One mutation, one currency. The service never crosses currency
   boundaries.

Idempotency
-----------

- ``set_income`` UPSERT on ``(cycle_id, currency) WHERE kind='income'``.
  Repeated calls update the same physical row.
- ``record_extra_income`` UPSERT on ``(cycle_id, source_key)``. The
  source_key identity survives soft-delete, so a re-issued command
  restores the previous row instead of creating a duplicate.
- ``set_savings_target`` UPSERT on ``(cycle_id, currency)``. Exactly
  one of ``target_pct`` / ``target_amount`` is non-null; both or
  neither is a 400.
- ``set_savings_reserved`` UPSERT on ``(cycle_id, currency)``.
  ``release_savings`` is a partial release; it never mutates
  ``SavingsRealization`` (committment is computed by the engine as
  ``max(reserved, realized)``).
- ``record_savings_realized`` mirrors extras: UPSERT on
  ``(cycle_id, source_key)``.

All inputs are ``Decimal``. ``float`` is rejected by the schema
(``NUMERIC(18,2)``). Round-trip is exact: ``0.01`` survives two
serialisations.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.money.models import (
    CycleIncome,
    FinancialCycle,
    SavingsRealization,
    SavingsReserved,
    SavingsTarget,
)
from app.money.repository import normalize_currency


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MoneyStateError(RuntimeError):
    """Base for every domain error raised by MoneyStateService."""


class CycleNotFoundError(MoneyStateError):
    """The requested cycle does not exist for ``user_id``."""


class CycleClosedError(MoneyStateError):
    """The cycle exists but is ``status='closed'`` — read-only snapshot."""


class CycleOwnershipMismatchError(MoneyStateError):
    """Defensive: a code path tried to mutate another user's cycle."""


class CycleCurrencyMismatchError(MoneyStateError):
    """A monetary mutation addressed a cycle in another currency."""

    def __init__(self, cycle_id: int, cycle_currency: str, requested_currency: str):
        self.cycle_id = cycle_id
        self.cycle_currency = cycle_currency
        self.requested_currency = requested_currency
        super().__init__(f"Cycle {cycle_id} uses {cycle_currency}; requested {requested_currency}")


class InvalidAmountError(MoneyStateError):
    """Amount is negative / zero / NaN / not a Decimal."""


class InvalidSourceKeyError(MoneyStateError):
    """``source_key`` is missing or empty."""


class MoneySourceCurrencyConflictError(MoneyStateError):
    """An existing source-keyed operation cannot change currency."""

    def __init__(
        self, source_key: str, existing_currency: str, requested_currency: str
    ) -> None:
        self.source_key = source_key
        self.existing_currency = existing_currency
        self.requested_currency = requested_currency
        super().__init__(
            f"source_key={source_key!r} has currency {existing_currency!r}; "
            f"requested {requested_currency!r}"
        )


class TargetXorViolationError(MoneyStateError):
    """Both or neither of ``target_pct`` / ``target_amount`` were passed."""


class InvalidTargetValueError(MoneyStateError):
    """``target_pct`` outside 0..1 or ``target_amount`` negative."""


class ExtraIncomeNotFoundError(MoneyStateError):
    """An ``update_extra_income`` / ``soft_delete_extra_income`` call
    targeted a row that does not exist (or was deleted) for ``user_id``."""


class SavingsRealizationNotFoundError(MoneyStateError):
    """An ``update_savings_realized`` / ``soft_delete_savings_realized``
    call targeted a row that does not exist (or was deleted) for
    ``user_id``."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_currency(currency: str) -> str:
    try:
        return normalize_currency(currency)
    except ValueError as exc:
        raise InvalidAmountError(f"invalid currency: {currency!r}") from exc


def _validate_amount_positive_or_zero(amount, *, name: str) -> Decimal:
    if amount is None:
        raise InvalidAmountError(f"{name} is required")
    if not isinstance(amount, Decimal):
        if isinstance(amount, int):
            amount = Decimal(amount)
        else:
            try:
                amount = Decimal(str(amount))
            except Exception as exc:
                raise InvalidAmountError(
                    f"{name} must be a Decimal-compatible value, got "
                    f"{type(amount).__name__}"
                ) from exc
    if not amount.is_finite():
        raise InvalidAmountError(f"{name} must be a finite number")
    return amount


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _require_source_currency(row, currency: str, source_key: str) -> None:
    if row.currency != currency:
        raise MoneySourceCurrencyConflictError(
            source_key, row.currency, currency
        )


# ---------------------------------------------------------------------------
# MoneyStateService
# ---------------------------------------------------------------------------


class MoneyStateService:
    """Mutations for ``CycleIncome`` / ``SavingsTarget`` /
    ``SavingsReserved`` / ``SavingsRealization``.

    Cycle lifecycle (open / close / rollover) stays on
    :class:`app.money.cycle_service.CycleService`. The expense domain
    stays on ``ExpenseService``. This service touches ONE table per
    method, never crossing tables in a single mutation.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # ==================================================================
    # Cycle gate (shared by every mutation)
    # ==================================================================

    def _require_open_cycle(
        self, user_id: int, cycle_id: int, *, currency: Optional[str] = None
    ) -> FinancialCycle:
        """Return the cycle if it exists, belongs to ``user_id`` and
        is open. Raise otherwise.
        """
        # Share the cycle row lock with close_cycle. Refresh even when this
        # Session cached an earlier "open" object before another writer closed
        # it; checking the identity-map object alone is not a concurrency gate.
        cycle = self.session.execute(
            select(FinancialCycle)
            .where(FinancialCycle.id == cycle_id)
            .with_for_update(of=FinancialCycle)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if cycle is None:
            raise CycleNotFoundError(
                f"Cycle {cycle_id} not found"
            )
        if cycle.telegram_user_id != user_id:
            raise CycleOwnershipMismatchError(
                f"Cycle {cycle_id} does not belong to user {user_id}"
            )
        if cycle.status != "open":
            raise CycleClosedError(
                f"Cycle {cycle_id} is closed (status={cycle.status!r}); "
                "historical state is immutable"
            )
        if currency is not None and cycle.currency != _validate_currency(currency):
            raise CycleCurrencyMismatchError(cycle_id, cycle.currency, _validate_currency(currency))
        # Preserve this transaction's own pending money changes before the
        # fresh per-row reads below. Never flush a mutation before checking
        # the current, locked lifecycle state.
        self.session.flush()
        return cycle

    def validate_source_currency(
        self, user_id: int, source_key: str, currency: str, *, kind: str,
    ) -> None:
        """Read-only replay precheck; record methods repeat it under a lock."""
        currency = _validate_currency(currency)
        if not source_key or not str(source_key).strip():
            raise InvalidSourceKeyError("source_key is required")
        source_key = str(source_key).strip()
        if kind == "extra":
            model = CycleIncome
        elif kind == "savings_realized":
            model = SavingsRealization
        else:
            raise InvalidSourceKeyError("unknown source-key operation kind")
        stmt = select(model.currency).join(
            FinancialCycle, FinancialCycle.id == model.cycle_id,
        ).where(
            FinancialCycle.telegram_user_id == user_id,
            model.source_key == source_key,
            model.currency != currency,
        )
        if kind == "extra":
            stmt = stmt.where(CycleIncome.kind == "extra")
        previous = self.session.execute(stmt.limit(1)).scalar_one_or_none()
        if previous is not None:
            raise MoneySourceCurrencyConflictError(source_key, previous, currency)

    def _lock_source_currency(self, user_id: int, source_key: str, currency: str, *, kind: str) -> None:
        # Cycle locks are intentionally independent across currencies. This
        # narrow key lock makes the cross-cycle identity check atomic without
        # serializing unrelated financial operations or adding a ledger.
        if self.session.get_bind().dialect.name == "postgresql":
            digest = hashlib.sha256(f"money:{user_id}:{kind}:{source_key}".encode()).digest()
            lock_id = int.from_bytes(digest[:8], "big", signed=True)
            self.session.execute(select(func.pg_advisory_xact_lock(lock_id)))
        self.validate_source_currency(user_id, source_key, currency, kind=kind)

    # ==================================================================
    # BASE INCOME
    # ==================================================================

    def set_income(
        self,
        user_id: int,
        cycle_id: int,
        currency: str,
        amount: Decimal,
    ) -> CycleIncome:
        """Upsert the base income for ``(cycle_id, currency)``.

        ``amount`` may be zero (a user can park a cycle at zero income
        temporarily) but never negative. Repeating the call with the
        same ``(cycle_id, currency)`` updates the existing row; no
        second physical row is created.
        """
        self._require_open_cycle(user_id, cycle_id, currency=currency)
        currency = _validate_currency(currency)
        amount = _validate_amount_positive_or_zero(amount, name="amount")
        if amount < 0:
            raise InvalidAmountError(
                f"base income amount must be >= 0, got {amount}"
            )

        now = _now_utc()
        existing = self.session.execute(
            select(CycleIncome).where(
                CycleIncome.cycle_id == cycle_id,
                CycleIncome.kind == "income",
                CycleIncome.currency == currency,
                CycleIncome.deleted_at.is_(None),
            )
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

        if existing is not None:
            if existing.amount != amount:
                existing.amount = amount
                existing.revision = (existing.revision or 1) + 1
                existing.updated_at = now
            self.session.commit()
            self.session.refresh(existing)
            return existing

        row = CycleIncome(
            cycle_id=cycle_id,
            currency=currency,
            kind="income",
            amount=amount,
            source_key=None,
            note=None,
            revision=1,
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError:
            # Concurrent winner created the row first; roll back and
            # fall through to the re-read + update path. We do not
            # touch ``row`` again — it has been detached by rollback.
            self.session.rollback()
            self._require_open_cycle(user_id, cycle_id, currency=currency)
        # Re-read after rollback to capture the canonical row. After a
        # concurrent insert we end up here; on the happy path the just-
        # flushed row is still in the session so this read returns it.
        canonical = self.session.execute(
            select(CycleIncome).where(
                CycleIncome.cycle_id == cycle_id,
                CycleIncome.kind == "income",
                CycleIncome.currency == currency,
                CycleIncome.deleted_at.is_(None),
            )
            .execution_options(populate_existing=True)
        ).scalar_one()
        if canonical.amount != amount:
            canonical.amount = amount
            canonical.revision = (canonical.revision or 1) + 1
            canonical.updated_at = now
        self.session.commit()
        self.session.refresh(canonical)
        return canonical

    # ==================================================================
    # EXTRA INCOME
    # ==================================================================

    def record_extra_income(
        self,
        user_id: int,
        cycle_id: int,
        currency: str,
        amount: Decimal,
        source_key: str,
        note: Optional[str] = None,
    ) -> CycleIncome:
        """Upsert an extra income identified by ``source_key``.

        The ``(cycle_id, source_key)`` identity survives soft-delete;
        a re-issued command with the same key restores the previous
        row instead of creating a duplicate. Concurrent callers on the
        same key observe one physical row.
        """
        self._require_open_cycle(user_id, cycle_id, currency=currency)
        currency = _validate_currency(currency)
        amount = _validate_amount_positive_or_zero(amount, name="amount")
        if amount <= 0:
            raise InvalidAmountError(
                f"extra income amount must be > 0, got {amount}"
            )
        if not source_key or not str(source_key).strip():
            raise InvalidSourceKeyError(
                "extra income source_key is required and must be non-empty"
            )
        source_key = str(source_key).strip()

        self._lock_source_currency(user_id, source_key, currency, kind="extra")

        now = _now_utc()
        existing = self._find_extra(user_id, cycle_id, source_key)
        if existing is not None:
            _require_source_currency(existing, currency, source_key)
            changed = False
            if existing.deleted_at is not None:
                existing.deleted_at = None
                changed = True
            if existing.amount != amount:
                existing.amount = amount
                changed = True
            if (existing.note or None) != (note or None):
                existing.note = note
                changed = True
            if changed:
                existing.revision = (existing.revision or 1) + 1
                existing.updated_at = now
            self.session.commit()
            self.session.refresh(existing)
            return existing

        row = CycleIncome(
            cycle_id=cycle_id,
            currency=currency,
            kind="extra",
            amount=amount,
            source_key=source_key,
            note=note,
            revision=1,
        )
        self.session.add(row)
        try:
            self.session.commit()
            self.session.refresh(row)
        except IntegrityError:
            self.session.rollback()
            self._require_open_cycle(user_id, cycle_id, currency=currency)
            self._lock_source_currency(user_id, source_key, currency, kind="extra")
            canonical = self._find_extra(user_id, cycle_id, source_key)
            if canonical is None:
                raise
            _require_source_currency(canonical, currency, source_key)
            if canonical.deleted_at is not None:
                canonical.deleted_at = None
            if canonical.amount != amount:
                canonical.amount = amount
            if (canonical.note or None) != (note or None):
                canonical.note = note
            canonical.revision = (canonical.revision or 1) + 1
            canonical.updated_at = now
            self.session.commit()
            self.session.refresh(canonical)
            return canonical
        return row

    def update_extra_income(
        self,
        user_id: int,
        extra_id: int,
        amount: Optional[Decimal] = None,
        note: Optional[str] = None,
    ) -> CycleIncome:
        """Update an extra income by ``extra_id``.

        Ownership is checked via the cycle's ``telegram_user_id`` —
        another user's extra is invisible (raises
        :class:`ExtraIncomeNotFoundError`, not a generic 404, so a
        malicious caller cannot probe for id existence).

        ``revision`` is bumped only when an actual change happens
        (``amount`` or ``note``). Setting ``note=None`` explicitly
        clears the note; omitting ``note`` leaves it untouched.
        """
        row = self._find_extra_by_id(user_id, extra_id)
        if row is None or row.deleted_at is not None:
            raise ExtraIncomeNotFoundError(
                f"extra income {extra_id} not found"
            )
        # Closed-cycle guard: the row itself survives, but no
        # mutation is allowed. ``_require_open_cycle`` raises
        # ``CycleClosedError`` on closed cycles.
        self._require_open_cycle(user_id, row.cycle_id, currency=row.currency)
        row = self._find_extra_by_id(user_id, extra_id, refresh=True)
        if row is None or row.deleted_at is not None:
            raise ExtraIncomeNotFoundError(f"extra income {extra_id} not found")
        changed = False
        if amount is not None:
            amount = _validate_amount_positive_or_zero(amount, name="amount")
            if amount <= 0:
                raise InvalidAmountError(
                    f"extra income amount must be > 0, got {amount}"
                )
            if row.amount != amount:
                row.amount = amount
                changed = True
        if note is not None and (row.note or None) != note:
            row.note = note
            changed = True
        if changed:
            row.revision = (row.revision or 1) + 1
            row.updated_at = _now_utc()
            self.session.commit()
            self.session.refresh(row)
        return row

    def soft_delete_extra_income(
        self, user_id: int, extra_id: int
    ) -> CycleIncome:
        """Soft-delete an extra. Idempotent: a second call on an
        already-deleted row bumps the revision by 1 (so callers
        observing the row know it was re-touched) but produces no
        other effect.
        """
        row = self._find_extra_by_id(user_id, extra_id)
        if row is None:
            raise ExtraIncomeNotFoundError(
                f"extra income {extra_id} not found"
            )
        self._require_open_cycle(user_id, row.cycle_id, currency=row.currency)
        row = self._find_extra_by_id(user_id, extra_id, refresh=True)
        if row is None:
            raise ExtraIncomeNotFoundError(f"extra income {extra_id} not found")
        if row.deleted_at is not None:
            row.revision = (row.revision or 1) + 1
            row.updated_at = _now_utc()
            self.session.commit()
            self.session.refresh(row)
            return row
        row.deleted_at = _now_utc()
        row.revision = (row.revision or 1) + 1
        row.updated_at = row.deleted_at
        self.session.commit()
        self.session.refresh(row)
        return row

    def _find_extra(
        self, user_id: int, cycle_id: int, source_key: str
    ) -> Optional[CycleIncome]:
        stmt = (
            select(CycleIncome)
            .join(FinancialCycle, FinancialCycle.id == CycleIncome.cycle_id)
            .where(
                CycleIncome.cycle_id == cycle_id,
                CycleIncome.kind == "extra",
                CycleIncome.source_key == source_key,
                FinancialCycle.telegram_user_id == user_id,
            )
            .execution_options(populate_existing=True)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def _find_extra_by_id(
        self, user_id: int, extra_id: int, *, refresh: bool = False
    ) -> Optional[CycleIncome]:
        stmt = (
            select(CycleIncome)
            .join(FinancialCycle, FinancialCycle.id == CycleIncome.cycle_id)
            .where(
                CycleIncome.id == extra_id,
                CycleIncome.kind == "extra",
                FinancialCycle.telegram_user_id == user_id,
            )
            .execution_options(populate_existing=refresh)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    # ==================================================================
    # SAVINGS TARGET
    # ==================================================================

    def set_savings_target(
        self,
        user_id: int,
        cycle_id: int,
        currency: str,
        *,
        target_pct: Optional[Decimal] = None,
        target_amount: Optional[Decimal] = None,
    ) -> SavingsTarget:
        """Upsert the savings target for ``(cycle_id, currency)``.

        XOR: exactly one of ``target_pct`` / ``target_amount`` must be
        non-None. Both or neither is a :class:`TargetXorViolationError`.
        ``target_pct`` must lie in ``[0, 1]``; ``target_amount`` must
        be non-negative. R3 does NOT auto-insert a 20% row on read —
        the soft default lives in the engine.
        """
        self._require_open_cycle(user_id, cycle_id, currency=currency)
        currency = _validate_currency(currency)

        has_pct = target_pct is not None
        has_amt = target_amount is not None
        if has_pct == has_amt:
            raise TargetXorViolationError(
                "exactly one of target_pct / target_amount is required"
            )

        now = _now_utc()
        if has_pct:
            pct = _validate_amount_positive_or_zero(
                target_pct, name="target_pct"
            )
            if pct < 0 or pct > 1:
                raise InvalidTargetValueError(
                    f"target_pct must be in [0, 1], got {pct}"
                )
            new_pct = pct.quantize(Decimal("0.0001"))
            new_amt: Optional[Decimal] = None
        else:
            amt = _validate_amount_positive_or_zero(
                target_amount, name="target_amount"
            )
            if amt < 0:
                raise InvalidTargetValueError(
                    f"target_amount must be >= 0, got {amt}"
                )
            new_pct = None
            new_amt = amt.quantize(Decimal("0.01"))

        existing = self.session.get(SavingsTarget, (cycle_id, currency), populate_existing=True)
        if existing is None:
            row = SavingsTarget(
                cycle_id=cycle_id,
                currency=currency,
                target_pct=new_pct,
                target_amount=new_amt,
            )
            self.session.add(row)
            self.session.commit()
            self.session.refresh(row)
            return row

        if existing.target_pct != new_pct or existing.target_amount != new_amt:
            existing.target_pct = new_pct
            existing.target_amount = new_amt
            existing.updated_at = now
            self.session.commit()
            self.session.refresh(existing)
        return existing

    # ==================================================================
    # SAVINGS RESERVED
    # ==================================================================

    def set_savings_reserved(
        self,
        user_id: int,
        cycle_id: int,
        currency: str,
        amount: Decimal,
    ) -> SavingsReserved:
        """Set the reserved amount absolutely (not increment).

        The amount is replaced wholesale: a previous reservation of
        220 followed by ``set_savings_reserved(100)`` yields 100.
        ``amount`` may be 0; negatives are rejected. ``reserved``
        may legally sit BELOW ``realized`` — the engine emits a
        warning, never an error.
        """
        self._require_open_cycle(user_id, cycle_id, currency=currency)
        currency = _validate_currency(currency)
        amount = _validate_amount_positive_or_zero(amount, name="amount")
        if amount < 0:
            raise InvalidAmountError(
                f"reserved amount must be >= 0, got {amount}"
            )

        new_amount = amount.quantize(Decimal("0.01"))
        existing = self.session.get(SavingsReserved, (cycle_id, currency), populate_existing=True)
        if existing is None:
            row = SavingsReserved(
                cycle_id=cycle_id, currency=currency, amount=new_amount
            )
            self.session.add(row)
            self.session.commit()
            self.session.refresh(row)
            return row
        if existing.amount != new_amount:
            existing.amount = new_amount
            existing.updated_at = _now_utc()
            self.session.commit()
            self.session.refresh(existing)
        return existing

    def release_savings(
        self,
        user_id: int,
        cycle_id: int,
        currency: str,
        amount: Decimal,
    ) -> SavingsReserved:
        """Reduce the reserved amount by ``amount`` (clamped at zero).

        ``release_savings`` only mutates ``savings_reserved``; it
        NEVER inserts a negative realization. If reserved sits below
        realized the engine still computes
        ``committed = max(reserved, realized)`` — no transfer
        bookkeeping is invented here.
        """
        self._require_open_cycle(user_id, cycle_id, currency=currency)
        currency = _validate_currency(currency)
        amount = _validate_amount_positive_or_zero(amount, name="amount")
        if amount <= 0:
            raise InvalidAmountError(
                f"release amount must be > 0, got {amount}"
            )

        delta = amount.quantize(Decimal("0.01"))
        existing = self.session.get(SavingsReserved, (cycle_id, currency), populate_existing=True)
        if existing is None:
            row = SavingsReserved(
                cycle_id=cycle_id, currency=currency, amount=Decimal("0.00")
            )
            self.session.add(row)
            self.session.commit()
            self.session.refresh(row)
            return row
        current = Decimal(existing.amount)
        new_amount = max(Decimal("0"), current - delta).quantize(
            Decimal("0.01")
        )
        if existing.amount != new_amount:
            existing.amount = new_amount
            existing.updated_at = _now_utc()
            self.session.commit()
            self.session.refresh(existing)
        return existing

    # ==================================================================
    # SAVINGS REALIZED
    # ==================================================================

    def record_savings_realized(
        self,
        user_id: int,
        cycle_id: int,
        currency: str,
        amount: Decimal,
        source_key: str,
        note: Optional[str] = None,
    ) -> SavingsRealization:
        """Upsert a savings realization identified by ``source_key``.

        Same identity / soft-delete semantics as
        :meth:`record_extra_income`. ``amount > 0`` is enforced both
        here and at the schema CHECK level.
        """
        self._require_open_cycle(user_id, cycle_id, currency=currency)
        currency = _validate_currency(currency)
        amount = _validate_amount_positive_or_zero(amount, name="amount")
        if amount <= 0:
            raise InvalidAmountError(
                f"realized amount must be > 0, got {amount}"
            )
        if not source_key or not str(source_key).strip():
            raise InvalidSourceKeyError(
                "realized source_key is required and must be non-empty"
            )
        source_key = str(source_key).strip()

        self._lock_source_currency(user_id, source_key, currency, kind="savings_realized")

        now = _now_utc()
        new_amount = amount.quantize(Decimal("0.01"))
        existing = self._find_realization(user_id, cycle_id, source_key)
        if existing is not None:
            _require_source_currency(existing, currency, source_key)
            changed = False
            if existing.deleted_at is not None:
                existing.deleted_at = None
                changed = True
            if existing.amount != new_amount:
                existing.amount = new_amount
                changed = True
            if (existing.note or None) != (note or None):
                existing.note = note
                changed = True
            if changed:
                existing.revision = (existing.revision or 1) + 1
                existing.updated_at = now
            self.session.commit()
            self.session.refresh(existing)
            return existing

        row = SavingsRealization(
            cycle_id=cycle_id,
            currency=currency,
            amount=new_amount,
            source_key=source_key,
            note=note,
            revision=1,
        )
        self.session.add(row)
        try:
            self.session.commit()
            self.session.refresh(row)
        except IntegrityError:
            self.session.rollback()
            self._require_open_cycle(user_id, cycle_id, currency=currency)
            self._lock_source_currency(user_id, source_key, currency, kind="savings_realized")
            canonical = self._find_realization(user_id, cycle_id, source_key)
            if canonical is None:
                raise
            _require_source_currency(canonical, currency, source_key)
            if canonical.deleted_at is not None:
                canonical.deleted_at = None
            if canonical.amount != new_amount:
                canonical.amount = new_amount
            if (canonical.note or None) != (note or None):
                canonical.note = note
            canonical.revision = (canonical.revision or 1) + 1
            canonical.updated_at = now
            self.session.commit()
            self.session.refresh(canonical)
            return canonical
        return row

    def update_savings_realized(
        self,
        user_id: int,
        realization_id: int,
        amount: Optional[Decimal] = None,
        note: Optional[str] = None,
    ) -> SavingsRealization:
        """Update a realization by id (with ownership check)."""
        row = self._find_realization_by_id(user_id, realization_id)
        if row is None or row.deleted_at is not None:
            raise SavingsRealizationNotFoundError(
                f"savings realization {realization_id} not found"
            )
        self._require_open_cycle(user_id, row.cycle_id, currency=row.currency)
        row = self._find_realization_by_id(user_id, realization_id, refresh=True)
        if row is None or row.deleted_at is not None:
            raise SavingsRealizationNotFoundError(f"savings realization {realization_id} not found")
        changed = False
        if amount is not None:
            amount = _validate_amount_positive_or_zero(amount, name="amount")
            if amount <= 0:
                raise InvalidAmountError(
                    f"realized amount must be > 0, got {amount}"
                )
            new_amount = amount.quantize(Decimal("0.01"))
            if row.amount != new_amount:
                row.amount = new_amount
                changed = True
        if note is not None and (row.note or None) != note:
            row.note = note
            changed = True
        if changed:
            row.revision = (row.revision or 1) + 1
            row.updated_at = _now_utc()
            self.session.commit()
            self.session.refresh(row)
        return row

    def soft_delete_savings_realized(
        self, user_id: int, realization_id: int
    ) -> SavingsRealization:
        """Soft-delete a realization. Idempotent."""
        row = self._find_realization_by_id(user_id, realization_id)
        if row is None:
            raise SavingsRealizationNotFoundError(
                f"savings realization {realization_id} not found"
            )
        self._require_open_cycle(user_id, row.cycle_id, currency=row.currency)
        row = self._find_realization_by_id(user_id, realization_id, refresh=True)
        if row is None:
            raise SavingsRealizationNotFoundError(f"savings realization {realization_id} not found")
        if row.deleted_at is not None:
            row.revision = (row.revision or 1) + 1
            row.updated_at = _now_utc()
            self.session.commit()
            self.session.refresh(row)
            return row
        now = _now_utc()
        row.deleted_at = now
        row.revision = (row.revision or 1) + 1
        row.updated_at = now
        self.session.commit()
        self.session.refresh(row)
        return row

    def _find_realization(
        self, user_id: int, cycle_id: int, source_key: str
    ) -> Optional[SavingsRealization]:
        stmt = (
            select(SavingsRealization)
            .join(
                FinancialCycle,
                FinancialCycle.id == SavingsRealization.cycle_id,
            )
            .where(
                SavingsRealization.cycle_id == cycle_id,
                SavingsRealization.source_key == source_key,
                FinancialCycle.telegram_user_id == user_id,
            )
            .execution_options(populate_existing=True)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def _find_realization_by_id(
        self, user_id: int, realization_id: int, *, refresh: bool = False
    ) -> Optional[SavingsRealization]:
        stmt = (
            select(SavingsRealization)
            .join(
                FinancialCycle,
                FinancialCycle.id == SavingsRealization.cycle_id,
            )
            .where(
                SavingsRealization.id == realization_id,
                FinancialCycle.telegram_user_id == user_id,
            )
            .execution_options(populate_existing=refresh)
        )
        return self.session.execute(stmt).scalar_one_or_none()


__all__ = [
    "CycleClosedError",
    "CycleCurrencyMismatchError",
    "CycleNotFoundError",
    "CycleOwnershipMismatchError",
    "ExtraIncomeNotFoundError",
    "InvalidAmountError",
    "InvalidSourceKeyError",
    "InvalidTargetValueError",
    "MoneyStateError",
    "MoneySourceCurrencyConflictError",
    "MoneyStateService",
    "SavingsRealizationNotFoundError",
    "TargetXorViolationError",
]
