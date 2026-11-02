"""Read-only value objects for the F1 R2 MoneyEngine.

Every monetary quantity is a ``Decimal`` and currency-partitioned by
design. ``MoneyByCurrency`` is a frozen mapping; the engine never
exposes a "total" across currencies because silently summing ARS and
USD is the exact bug the multi-currency contract forbids.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional


ZERO = Decimal("0")


class MoneyByCurrency:
    """Currency-partitioned monetary values.

    Adding values across currencies is impossible: ``add`` requires
    both operands be ``MoneyByCurrency`` and the operation is per-currency.
    Callers that legitimately need a single-currency total call
    ``total()``, which returns ``None`` whenever more than one currency
    is present — that is the explicit signal that the operation is
    ambiguous.
    """

    __slots__ = ("_amounts",)

    def __init__(self, amounts: Optional[dict[str, Decimal]] = None) -> None:
        # Normalize to ``Decimal`` and drop zero entries so equal
        # ``MoneyByCurrency`` instances compare equal regardless of the
        # order in which currencies appeared in the input.
        normalized: dict[str, Decimal] = {}
        for currency, value in (amounts or {}).items():
            key = str(currency).upper()
            d = value if isinstance(value, Decimal) else Decimal(str(value))
            if d == ZERO:
                continue
            normalized[key] = d
        object.__setattr__(self, "_amounts", normalized)

    # ---- read ----------------------------------------------------------

    def get(self, currency: str) -> Decimal:
        return self._amounts.get(str(currency).upper(), ZERO)

    def currencies(self) -> list[str]:
        return sorted(self._amounts.keys())

    def is_empty(self) -> bool:
        return not self._amounts

    def items(self) -> list[tuple[str, Decimal]]:
        return sorted(self._amounts.items())

    def to_dict(self) -> dict[str, str]:
        """JSON-friendly representation. Decimal precision preserved.

        Values are rendered as their natural string form (``"50"`` for
        ``Decimal("50")``, ``"50.75"`` for ``Decimal("50.75")``). The
        downstream consumer re-parses them with ``Decimal(...)`` so
        no precision is lost.
        """
        return {k: str(v) for k, v in self.items()}

    def total(self) -> Optional[Decimal]:
        """Return the single-currency total. ``None`` for multi-currency.

        The ``None`` return is the explicit contract: callers that try
        to sum cross-currency values must handle the ambiguity. See
        design v5 FINAL §I.2.
        """
        if len(self._amounts) == 0:
            return ZERO
        if len(self._amounts) > 1:
            return None
        return next(iter(self._amounts.values()))

    # ---- mutate (immutable; returns a new instance) -------------------

    def add(self, currency: str, amount: Decimal | int | str) -> "MoneyByCurrency":
        cur = str(currency).upper()
        delta = (
            amount if isinstance(amount, Decimal) else Decimal(str(amount))
        )
        if delta == ZERO:
            return self
        new = dict(self._amounts)
        new[cur] = new.get(cur, ZERO) + delta
        return MoneyByCurrency(new)

    def subtract(
        self, currency: str, amount: Decimal | int | str
    ) -> "MoneyByCurrency":
        cur = str(currency).upper()
        delta = (
            amount if isinstance(amount, Decimal) else Decimal(str(amount))
        )
        if delta == ZERO:
            return self
        new = dict(self._amounts)
        new[cur] = new.get(cur, ZERO) - delta
        return MoneyByCurrency(new)

    def union(self, other: "MoneyByCurrency") -> "MoneyByCurrency":
        merged: dict[str, Decimal] = dict(self._amounts)
        for cur, amount in other._amounts.items():
            merged[cur] = merged.get(cur, ZERO) + amount
        return MoneyByCurrency(merged)

    @classmethod
    def from_pairs(
        cls, pairs: Iterable[tuple[str, Decimal | int | str]]
    ) -> "MoneyByCurrency":
        out: dict[str, Decimal] = {}
        for cur, value in pairs:
            key = str(cur).upper()
            d = value if isinstance(value, Decimal) else Decimal(str(value))
            out[key] = out.get(key, ZERO) + d
        return cls(out)

    # ---- equality / repr ---------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MoneyByCurrency):
            return NotImplemented
        return self._amounts == other._amounts

    def __hash__(self) -> int:
        return hash(tuple(sorted(self._amounts.items())))

    def __repr__(self) -> str:
        return f"MoneyByCurrency({self.to_dict()})"


# ---------------------------------------------------------------------------
# Cycle summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CarriedOverFixedOccurrence:
    """A fixed obligation that carried into the current cycle.

    ``original_month_year`` is the YYYY-MM string where the obligation
    originated (the month when its ``due_day_of_month`` fell first).
    The expected amount is frozen: changing ``FixedExpense.expected_amount``
    afterwards never alters this historical entry.
    """

    fixed_id: int
    name: str
    expected_amount: Decimal
    currency: str
    due_date: "object"  # ``date``; declared as object to avoid early import
    original_month_year: str

    def source_key(self) -> str:
        # Stable identity used by MoneyEngine / invariant detection.
        # Mirrors the convention ``FixedExpenseService.fixed_payment_source_key``.
        return f"fixed-payment:{self.fixed_id}:{self.original_month_year}"


@dataclass(frozen=True)
class CycleSummary:
    """Read-only view of one financial cycle.

    Every monetary field is a ``MoneyByCurrency``. ``daily_safe_to_spend``
    is ``None`` for closed cycles or when ``remaining_days <= 0`` (see
    spec §16). ``warnings`` is a list of human-readable strings; the
    engine never raises on warnings, only on invariant violations.
    """

    cycle_id: int
    cycle_index: int
    status: str  # 'open' | 'closed'
    start_date: "object"  # date
    end_date: "object"  # date
    payday_day: int
    snapshot_version: int

    base_income: MoneyByCurrency
    extra_income: MoneyByCurrency
    gross_income: MoneyByCurrency
    savings_target: MoneyByCurrency
    savings_reserved: MoneyByCurrency
    savings_realized: MoneyByCurrency
    savings_committed: MoneyByCurrency
    realized_expenses: MoneyByCurrency
    pending_fixed_obligations: MoneyByCurrency
    pending_recurring_obligations: MoneyByCurrency
    pending_reserved_obligations: MoneyByCurrency
    available_to_spend: MoneyByCurrency
    daily_safe_to_spend: Optional[MoneyByCurrency]
    remaining_days: Optional[int]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    carried_over_fixed: tuple[CarriedOverFixedOccurrence, ...] = field(
        default_factory=tuple
    )
    pay_rule: str = "fixed_day"
    payday_weekday: Optional[int] = None
    currency: str = "ARS"
    legacy_mixed_currency: bool = False


# ---------------------------------------------------------------------------
# Invariant detection
# ---------------------------------------------------------------------------


class InvariantViolationKind:
    """Names of the invariant violations the engine can detect.

    Each ``FixedExpensePayment`` row with ``paid_at IS NOT NULL AND
    skipped = false`` is checked independently. The five violation
    kinds are mutually exclusive on a single row: an Expense can be
    missing, soft-deleted, of the wrong source_type, with a mismatched
    source_key, or ``expense_id`` can be NULL — never multiple at once
    in the canonical F1 design. Tests assert each in isolation.
    """

    EXPENSE_ID_NULL = "fixed_payment.expense_id_is_null"
    EXPENSE_ROW_MISSING = "fixed_payment.expense_row_missing"
    EXPENSE_SOFT_DELETED = "fixed_payment.expense_soft_deleted"
    EXPENSE_WRONG_SOURCE_TYPE = "fixed_payment.expense_wrong_source_type"
    EXPENSE_SOURCE_KEY_MISMATCH = "fixed_payment.expense_source_key_mismatch"


@dataclass(frozen=True)
class InvariantViolation:
    """One detected invariant violation.

    Identified by the originating ``FixedExpensePayment`` and the kind
    of inconsistency. The engine reports only — it never mutates rows.
    """

    fixed_payment_id: int
    fixed_expense_id: int
    month_year: str
    kind: str
    detail: str = ""
