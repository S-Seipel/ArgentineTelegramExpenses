"""R2 MoneyEngine — read-only view over the financial cycle.

The engine is the single source of truth for "what can the user spend
right now?" It does not mutate any row, open any transaction, or hold
any locks. All persistence is owned by ``CycleService``.

Public contract (read-only):

- ``get_cycle_summary(user_id, cycle_id, *, today)`` → ``CycleSummary``
- ``get_available_to_spend(user_id, cycle_id, *, today)`` → ``MoneyByCurrency``
- ``get_daily_safe_to_spend(user_id, cycle_id, *, today)`` → ``MoneyByCurrency | None``
- ``detect_invariant_violations(user_id)`` → list of ``InvariantViolation``

Cycle lifecycle (close, rollover, catch-up, open) is owned by
``CycleService``; ``MoneyEngine`` exposes a single read helper
(``get_cycle_summary``) that ``CycleService`` calls inside the same
SQLAlchemy session so the ``SELECT … FOR UPDATE`` lock and the
snapshot calculation share one transaction.

Formula (per currency ``c``):

```
gross_income_c       = base_income_c + extra_income_c
savings_target_c     = explicit row | gross_income * target_pct | soft default 20%
savings_reserved_c   = explicit row | 0
savings_realized_c   = SUM(savings_realizations.amount WHERE live AND cycle AND c)
savings_committed_c  = max(savings_reserved_c, savings_realized_c)
realized_expenses_c  = SUM(expenses.amount WHERE live AND user AND date IN cycle AND c)
                        − fixed_payment mirrors (counted via the payment row)
pending_fixed_c      = sum(expected_amount of unresolved fixed occurrences)
pending_recurring_c  = 0 in R2 (F1 ledger)
pending_reserved_c   = pending_fixed_c + pending_recurring_c
available_c          = gross_income_c − savings_committed_c − realized_expenses_c
                       − pending_reserved_c
```

``available_to_spend`` is allowed to be negative: over-spending is a
state, not a bug. The UI is responsible for showing it as such.

Money is always currency-partitioned via :class:`MoneyByCurrency`.
Cross-currency aggregation is impossible by design.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.expenses.models import Expense
from app.fixed_expenses.models import (
    FixedExpense,
    FixedExpensePayment,
)
from app.fixed_expenses.service import fixed_payment_source_key
from app.money.dto import (
    CycleSummary,
    InvariantViolation,
    InvariantViolationKind,
    MoneyByCurrency,
)
from app.money.fixed_obligations import (
    resolve_current_obligations,
)
from app.money.models import (
    CycleIncome,
    FinancialCycle,
    SavingsRealization,
    SavingsReserved,
    SavingsTarget,
)
from app.money.snapshot import (
    CURRENT_SNAPSHOT_VERSION,
    deserialize_carried_over,
    deserialize_money_block,
    ensure_snapshot_version,
)


logger = logging.getLogger(__name__)


DEFAULT_SAVINGS_PCT = Decimal("0.20")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SnapshotVersionMismatchError(RuntimeError):
    """Raised when a stored snapshot is missing or has an unknown version."""


class CycleHasPendingError(RuntimeError):
    """Raised by ``CycleService.close_cycle`` when pending obligations
    remain and the caller did not pass ``force=True``.
    """

    def __init__(self, pending_by_currency: dict[str, Decimal]) -> None:
        self.pending_by_currency = pending_by_currency
        super().__init__(
            "Cycle has unresolved obligations: "
            + ", ".join(
                f"{cur}={amt}" for cur, amt in pending_by_currency.items()
            )
        )


class CycleCatchUpLimitExceeded(RuntimeError):
    """Raised by ``CycleService.ensure_current_cycle`` when the rollover
    walk exceeds the defensive maximum (12 by default).
    """


class FixedExpenseHasOutstandingDebtError(RuntimeError):
    """Raised when deleting a ``FixedExpense`` template would erase an
    unresolved historical obligation.
    """


class SystemOwnedExpenseError(RuntimeError):
    """Raised when a generic Expense mutation targets a row owned by the
    fixed-expense or recurring subsystems.
    """


_SYSTEM_OWNED_SOURCE_TYPES = frozenset({"fixed_payment", "recurring"})


# ---------------------------------------------------------------------------
# MoneyEngine
# ---------------------------------------------------------------------------


class MoneyEngine:
    """Read-only money engine.

    Pure SELECTs. No INSERT/UPDATE/DELETE. No transaction boundary
    management (the session is owned by the caller). Cycle lifecycle
    lives in :class:`app.money.cycle_service.CycleService`.
    """

    _CATCH_UP_LIMIT = 12

    def __init__(self, session: Session) -> None:
        self.session = session

    # ==================================================================
    # READS
    # ==================================================================

    def get_cycle_summary(
        self,
        user_id: int,
        cycle_id: int,
        *,
        today: Optional[date] = None,
    ) -> CycleSummary:
        """Return the snapshot-equivalent read model for ``cycle_id``.

        For closed cycles the snapshot is the source of truth — the
        engine does NOT recompute historical numbers. The
        ``carried_over_fixed`` block from the snapshot is restored
        verbatim so callers can render the debt.

        Pure read. Does not mutate any row, lock any row, or begin a
        transaction. The caller owns the session state.
        """
        cycle = self._require_cycle(user_id, cycle_id)
        if cycle.status == "closed":
            return self._summary_from_snapshot(cycle)
        return self._compute_open_summary(user_id, cycle, today)

    def get_available_to_spend(
        self,
        user_id: int,
        cycle_id: int,
        *,
        today: Optional[date] = None,
    ) -> MoneyByCurrency:
        summary = self.get_cycle_summary(user_id, cycle_id, today=today)
        return summary.available_to_spend

    def get_daily_safe_to_spend(
        self,
        user_id: int,
        cycle_id: int,
        *,
        today: Optional[date] = None,
    ) -> Optional[MoneyByCurrency]:
        summary = self.get_cycle_summary(user_id, cycle_id, today=today)
        return summary.daily_safe_to_spend

    def detect_invariant_violations(
        self, user_id: int
    ) -> list[InvariantViolation]:
        """Detect inconsistencies between ``FixedExpensePayment`` rows and
        their linked ``Expense`` mirrors.

        Only rows whose ``paid_at IS NOT NULL AND skipped = false`` are
        inspected (a skipped payment legitimately has no mirror; a
        pending payment has no mirror by design). The five checks
        (missing ``expense_id``, missing Expense row, soft-deleted
        Expense, wrong ``source_type``, wrong ``source_key``) are
        reported independently — they are mutually exclusive on a
        well-formed row, so the engine emits at most one violation per
        payment.

        Pure read. The engine **reports** violations, never repairs
        them.
        """
        out: list[InvariantViolation] = []
        payments = self.session.execute(
            select(FixedExpensePayment)
            .join(FixedExpense, FixedExpense.id == FixedExpensePayment.fixed_expense_id)
            .where(
                FixedExpense.telegram_user_id == user_id,
                FixedExpensePayment.paid_at.is_not(None),
                FixedExpensePayment.skipped.is_(False),
            )
        ).scalars().all()
        for p in payments:
            if p.expense_id is None:
                out.append(
                    InvariantViolation(
                        fixed_payment_id=p.id,
                        fixed_expense_id=p.fixed_expense_id,
                        month_year=p.month_year,
                        kind=InvariantViolationKind.EXPENSE_ID_NULL,
                        detail=(
                            f"Payment {p.id} ({p.fixed_expense_id} "
                            f"{p.month_year}) is paid but has no "
                            "expense_id."
                        ),
                    )
                )
                continue
            expense = self.session.get(Expense, p.expense_id)
            if expense is None:
                out.append(
                    InvariantViolation(
                        fixed_payment_id=p.id,
                        fixed_expense_id=p.fixed_expense_id,
                        month_year=p.month_year,
                        kind=InvariantViolationKind.EXPENSE_ROW_MISSING,
                        detail=(
                            f"Payment {p.id} points at missing expense "
                            f"{p.expense_id}."
                        ),
                    )
                )
                continue
            if expense.deleted_at is not None:
                out.append(
                    InvariantViolation(
                        fixed_payment_id=p.id,
                        fixed_expense_id=p.fixed_expense_id,
                        month_year=p.month_year,
                        kind=InvariantViolationKind.EXPENSE_SOFT_DELETED,
                        detail=(
                            f"Payment {p.id} links to soft-deleted "
                            f"expense {p.expense_id}."
                        ),
                    )
                )
                continue
            if expense.source_type != "fixed_payment":
                out.append(
                    InvariantViolation(
                        fixed_payment_id=p.id,
                        fixed_expense_id=p.fixed_expense_id,
                        month_year=p.month_year,
                        kind=(
                            InvariantViolationKind.EXPENSE_WRONG_SOURCE_TYPE
                        ),
                        detail=(
                            f"Payment {p.id} expense {p.expense_id} has "
                            f"source_type={expense.source_type!r}, "
                            "expected 'fixed_payment'."
                        ),
                    )
                )
                continue
            expected_key = fixed_payment_source_key(
                p.fixed_expense_id, p.month_year
            )
            if expense.source_key != expected_key:
                out.append(
                    InvariantViolation(
                        fixed_payment_id=p.id,
                        fixed_expense_id=p.fixed_expense_id,
                        month_year=p.month_year,
                        kind=(
                            InvariantViolationKind.EXPENSE_SOURCE_KEY_MISMATCH
                        ),
                        detail=(
                            f"Payment {p.id} expense {p.expense_id} "
                            f"source_key={expense.source_key!r}, "
                            f"expected {expected_key!r}."
                        ),
                    )
                )
        return out

    # ==================================================================
    # Internals — read-only summary construction
    # ==================================================================

    def _compute_open_summary(
        self,
        user_id: int,
        cycle: FinancialCycle,
        today: Optional[date] = None,
    ) -> CycleSummary:
        base = self._sum_incomes(user_id, cycle.id, kind="income")
        extra = self._sum_incomes(user_id, cycle.id, kind="extra")
        gross = base.union(extra)

        target = self._savings_target(user_id, cycle.id, gross)
        reserved_row = self._savings_reserved(user_id, cycle.id)
        realized = self._sum_realizations(user_id, cycle.id)
        committed = _max_by_currency(reserved_row, realized)

        realized_expenses = self._sum_realized_expenses(user_id, cycle)

        obligations = resolve_current_obligations(self.session, user_id, cycle)
        pending_fixed = MoneyByCurrency(obligations.pending_by_currency)
        pending_recurring = MoneyByCurrency()
        pending_reserved = pending_fixed.union(pending_recurring)

        available = _available_to_spend(
            gross=gross,
            committed=committed,
            realized_expenses=realized_expenses,
            pending_reserved=pending_reserved,
        )

        daily: Optional[MoneyByCurrency]
        remaining_days: Optional[int]
        if today is None:
            today = _business_today()
        if cycle.start_date <= today <= cycle.end_date:
            remaining_days = (cycle.end_date - today).days + 1
            if remaining_days <= 0:
                daily = None
            else:
                daily = _daily(available, remaining_days)
        else:
            remaining_days = None
            daily = None

        warnings: list[str] = []
        if realized.total() is not None and reserved_row.total() is not None:
            if realized.total() > reserved_row.total():
                warnings.append("savings_realized_exceeds_reserved")
        if available.total() is not None and available.total() < 0:
            warnings.append("available_negative")
        if gross.is_empty():
            warnings.append("no_income_recorded")

        return CycleSummary(
            cycle_id=cycle.id,
            cycle_index=cycle.cycle_index,
            status="open",
            start_date=cycle.start_date,
            end_date=cycle.end_date,
            payday_day=cycle.payday_day,
            snapshot_version=CURRENT_SNAPSHOT_VERSION,
            base_income=base,
            extra_income=extra,
            gross_income=gross,
            savings_target=target,
            savings_reserved=reserved_row,
            savings_realized=realized,
            savings_committed=committed,
            realized_expenses=realized_expenses,
            pending_fixed_obligations=pending_fixed,
            pending_recurring_obligations=pending_recurring,
            pending_reserved_obligations=pending_reserved,
            available_to_spend=available,
            daily_safe_to_spend=daily,
            remaining_days=remaining_days,
            warnings=tuple(warnings),
            carried_over_fixed=tuple(obligations.carried_over),
        )

    def _summary_from_snapshot(self, cycle: FinancialCycle) -> CycleSummary:
        payload = cycle.closing_snapshot or {}
        version = ensure_snapshot_version(payload)
        money_block = deserialize_money_block(payload.get("money", {}))
        carried = deserialize_carried_over(payload.get("carried_over_fixed", []))
        # ``warnings`` was serialized as a plain list of strings; restore
        # that shape explicitly because ``MoneyByCurrency`` only knows
        # about numbers.
        warnings = tuple(payload.get("money", {}).get("warnings", []) or [])
        return CycleSummary(
            cycle_id=cycle.id,
            cycle_index=cycle.cycle_index,
            status="closed",
            start_date=cycle.start_date,
            end_date=cycle.end_date,
            payday_day=cycle.payday_day,
            snapshot_version=version,
            base_income=money_block["income"],
            extra_income=money_block["extra"],
            gross_income=money_block["gross_income"],
            savings_target=money_block["savings_target"],
            savings_reserved=money_block["savings_reserved"],
            savings_realized=money_block["savings_realized"],
            savings_committed=money_block["savings_committed"],
            realized_expenses=money_block["realized_expenses"],
            pending_fixed_obligations=money_block[
                "pending_fixed_obligations"
            ],
            pending_recurring_obligations=money_block[
                "pending_recurring_obligations"
            ],
            pending_reserved_obligations=money_block[
                "pending_reserved_obligations"
            ],
            available_to_spend=money_block["available_to_spend"],
            daily_safe_to_spend=None,
            remaining_days=None,
            warnings=warnings,
            carried_over_fixed=carried,
        )

    # ---- SQL primitives (all read-only) ------------------------------

    def _require_cycle(
        self, user_id: int, cycle_id: int
    ) -> FinancialCycle:
        cycle = self.session.get(FinancialCycle, cycle_id)
        if cycle is None or cycle.telegram_user_id != user_id:
            raise LookupError(f"Cycle {cycle_id} not found for user {user_id}")
        return cycle

    def _sum_incomes(
        self, user_id: int, cycle_id: int, *, kind: str
    ) -> MoneyByCurrency:
        stmt = select(
            CycleIncome.currency,
            func.coalesce(func.sum(CycleIncome.amount), 0),
        ).where(
            CycleIncome.cycle_id == cycle_id,
            CycleIncome.kind == kind,
            CycleIncome.deleted_at.is_(None),
        ).group_by(CycleIncome.currency)
        rows = self.session.execute(stmt).all()
        return MoneyByCurrency(
            {currency: Decimal(total or 0) for currency, total in rows}
        )

    def _savings_target(
        self,
        user_id: int,
        cycle_id: int,
        gross: MoneyByCurrency,
    ) -> MoneyByCurrency:
        rows = self.session.execute(
            select(SavingsTarget).where(SavingsTarget.cycle_id == cycle_id)
        ).scalars().all()
        if not rows:
            return _default_target(gross)
        out: dict[str, Decimal] = {}
        for row in rows:
            if row.target_amount is not None:
                out[row.currency.upper()] = Decimal(row.target_amount)
            elif row.target_pct is not None and gross.get(row.currency) > 0:
                out[row.currency.upper()] = (
                    Decimal(row.target_pct) * gross.get(row.currency)
                ).quantize(Decimal("0.01"))
        return MoneyByCurrency(out)

    def _savings_reserved(
        self, user_id: int, cycle_id: int
    ) -> MoneyByCurrency:
        rows = self.session.execute(
            select(SavingsReserved).where(SavingsReserved.cycle_id == cycle_id)
        ).scalars().all()
        return MoneyByCurrency(
            {row.currency.upper(): Decimal(row.amount) for row in rows}
        )

    def _sum_realizations(
        self, user_id: int, cycle_id: int
    ) -> MoneyByCurrency:
        stmt = select(
            SavingsRealization.currency,
            func.coalesce(func.sum(SavingsRealization.amount), 0),
        ).where(
            SavingsRealization.cycle_id == cycle_id,
            SavingsRealization.deleted_at.is_(None),
        ).group_by(SavingsRealization.currency)
        rows = self.session.execute(stmt).all()
        return MoneyByCurrency(
            {currency: Decimal(total or 0) for currency, total in rows}
        )

    def _sum_realized_expenses(
        self, user_id: int, cycle: FinancialCycle
    ) -> MoneyByCurrency:
        """Sum expenses by currency in ``[start_date, end_date]``.

        Includes the three ``source_type`` flavours R2 cares about:
        ``telegram``, ``fixed_payment`` and ``recurring``. The
        invariant "paying a pending obligation does not change
        ``available``" depends on the fixed_payment mirror being
        counted: pending drops by ``expected_amount`` AND realized
        rises by the actual amount of the same obligation. Soft-deleted
        rows are excluded as usual.
        """
        stmt = select(
            Expense.currency,
            func.coalesce(func.sum(Expense.amount), 0),
        ).where(
            Expense.telegram_user_id == user_id,
            Expense.deleted_at.is_(None),
            Expense.expense_date >= cycle.start_date,
            Expense.expense_date <= cycle.end_date,
        ).group_by(Expense.currency)
        rows = self.session.execute(stmt).all()
        return MoneyByCurrency(
            {currency: Decimal(total or 0) for currency, total in rows}
        )


# ---------------------------------------------------------------------------
# helpers (module-level so tests can poke them directly)
# ---------------------------------------------------------------------------


def _max_by_currency(a: MoneyByCurrency, b: MoneyByCurrency) -> MoneyByCurrency:
    currencies = set(a.currencies()) | set(b.currencies())
    out: dict[str, Decimal] = {}
    for cur in currencies:
        out[cur] = max(a.get(cur), b.get(cur))
    return MoneyByCurrency(out)


def _default_target(gross: MoneyByCurrency) -> MoneyByCurrency:
    out: dict[str, Decimal] = {}
    for currency in gross.currencies():
        out[currency] = (gross.get(currency) * DEFAULT_SAVINGS_PCT).quantize(
            Decimal("0.01")
        )
    return MoneyByCurrency(out)


def _available_to_spend(
    *,
    gross: MoneyByCurrency,
    committed: MoneyByCurrency,
    realized_expenses: MoneyByCurrency,
    pending_reserved: MoneyByCurrency,
) -> MoneyByCurrency:
    currencies = (
        set(gross.currencies())
        | set(committed.currencies())
        | set(realized_expenses.currencies())
        | set(pending_reserved.currencies())
    )
    out: dict[str, Decimal] = {}
    for cur in currencies:
        out[cur] = (
            gross.get(cur)
            - committed.get(cur)
            - realized_expenses.get(cur)
            - pending_reserved.get(cur)
        )
    return MoneyByCurrency(out)


def _daily(available: MoneyByCurrency, remaining_days: int) -> MoneyByCurrency:
    """Per-currency daily allowance. Negative allowed; zero allowed.

    ``remaining_days`` is at least 1 by the caller's contract. If the
    caller ever wants ``None`` it must short-circuit before calling
    ``_daily``.
    """
    out: dict[str, Decimal] = {}
    divisor = Decimal(remaining_days)
    for currency, amount in available.items():
        daily = (amount / divisor).quantize(Decimal("0.01"))
        if daily != Decimal("0"):
            out[currency] = daily
    return MoneyByCurrency(out)


def _business_today() -> date:
    try:
        from app.utils.now import business_today as _today

        return _today()
    except Exception:
        return date.today()


__all__ = [
    "CycleCatchUpLimitExceeded",
    "CycleHasPendingError",
    "DEFAULT_SAVINGS_PCT",
    "FixedExpenseHasOutstandingDebtError",
    "MoneyEngine",
    "SnapshotVersionMismatchError",
    "SystemOwnedExpenseError",
]
