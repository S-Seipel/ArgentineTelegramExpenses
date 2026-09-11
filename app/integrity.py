"""Read-only integrity checks between ``expenses`` and the fixed-expense
domain.

Phase 0 only AUTHORIZES detection (and tests around it). It does NOT
auto-repair anything, because the audit findings can be ambiguous — e.g.
a soft-deleted mirror whose payment row was lost could be either a
genuine double-payment we want to keep, or stale data we should fold.

The functions return dataclasses so tests can assert on them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.expenses.models import Expense
from app.fixed_expenses.models import (
    FixedExpense,
    FixedExpensePayment,
)


@dataclass
class IntegrityFinding:
    code: str
    detail: str
    expense_id: int | None = None
    payment_id: int | None = None
    fixed_expense_id: int | None = None


@dataclass
class IntegrityReport:
    findings: list[IntegrityFinding] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.findings

    def add(self, code: str, **kw) -> None:
        self.findings.append(IntegrityFinding(code=code, **kw))


def paid_payment_without_expense(session: Session) -> list[IntegrityFinding]:
    """``FixedExpensePayment`` marked paid (paid_at set, skipped=False) but
    has no live expense attached (either ``expense_id`` NULL or pointing
    at a soft-deleted/missing row)."""
    out: list[IntegrityFinding] = []
    rows = session.execute(
        select(FixedExpensePayment).where(
            FixedExpensePayment.paid_at.is_not(None),
            FixedExpensePayment.skipped.is_(False),
        )
    ).scalars().all()
    for p in rows:
        if p.expense_id is None:
            out.append(
                IntegrityFinding(
                    code="paid_payment_missing_expense",
                    detail=(
                        f"Payment {p.id} (fixed {p.fixed_expense_id}, "
                        f"month {p.month_year}) is paid but has no expense."
                    ),
                    payment_id=p.id,
                    fixed_expense_id=p.fixed_expense_id,
                )
            )
            continue
        expense = session.execute(
            select(Expense).where(Expense.id == p.expense_id)
        ).scalar_one_or_none()
        if expense is None or expense.deleted_at is not None:
            out.append(
                IntegrityFinding(
                    code="paid_payment_orphaned_expense",
                    detail=(
                        f"Payment {p.id} (fixed {p.fixed_expense_id}, "
                        f"month {p.month_year}) references a missing or "
                        f"soft-deleted expense."
                    ),
                    payment_id=p.id,
                    expense_id=p.expense_id,
                    fixed_expense_id=p.fixed_expense_id,
                )
            )
    return out


def orphan_fixed_payment_expense(session: Session) -> list[IntegrityFinding]:
    """``expenses`` with ``source_type='fixed_payment'`` but no matching
    ``FixedExpensePayment`` row."""
    out: list[IntegrityFinding] = []
    rows = session.execute(
        select(Expense).where(
            Expense.source_type == "fixed_payment",
            Expense.source_key.is_not(None),
        )
    ).scalars().all()
    for e in rows:
        payment = session.execute(
            select(FixedExpensePayment).where(
                FixedExpensePayment.expense_id == e.id
            )
        ).scalar_one_or_none()
        if payment is None:
            out.append(
                IntegrityFinding(
                    code="fixed_payment_expense_without_payment",
                    detail=(
                        f"Expense {e.id} has source_type=fixed_payment "
                        f"(source_key={e.source_key!r}) but no matching "
                        f"FixedExpensePayment row."
                    ),
                    expense_id=e.id,
                )
            )
    return out


def duplicate_fixed_payment_mirrors(
    session: Session,
) -> list[IntegrityFinding]:
    """Multiple LIVE ``expenses`` rows that share the same
    ``(source_key)`` for a fixed payment. Should be impossible with the
    partial unique index but is still checked defensively."""
    out: list[IntegrityFinding] = []
    rows = session.execute(
        select(
            Expense.source_key,
            func.count(Expense.id),
        )
        .where(
            Expense.source_type == "fixed_payment",
            Expense.source_key.is_not(None),
            Expense.deleted_at.is_(None),
        )
        .group_by(Expense.source_key)
        .having(func.count(Expense.id) > 1)
    ).all()
    for key, count in rows:
        out.append(
            IntegrityFinding(
                code="duplicate_fixed_payment_mirrors",
                detail=(
                    f"source_key {key!r} has {count} live mirrors; "
                    f"unique index is supposed to prevent this."
                ),
            )
        )
    return out


def dangling_payment_expense_fk(
    session: Session,
) -> list[IntegrityFinding]:
    """``FixedExpensePayment.expense_id`` pointing at a non-existent
    ``expenses.id`` (a FK violation that can happen if the DB layer
    accepted an invalid value before the FK was enforced)."""
    out: list[IntegrityFinding] = []
    rows = session.execute(
        select(FixedExpensePayment).where(
            FixedExpensePayment.expense_id.is_not(None)
        )
    ).scalars().all()
    for p in rows:
        exists = session.execute(
            select(Expense.id).where(Expense.id == p.expense_id)
        ).scalar_one_or_none()
        if exists is None:
            out.append(
                IntegrityFinding(
                    code="payment_expense_fk_dangling",
                    detail=(
                        f"Payment {p.id} points at expense_id "
                        f"{p.expense_id} which does not exist."
                    ),
                    payment_id=p.id,
                    expense_id=p.expense_id,
                )
            )
    return out


def run_full_audit(session: Session) -> IntegrityReport:
    """Aggregate every detection into one report."""
    report = IntegrityReport()
    for fn in (
        paid_payment_without_expense,
        orphan_fixed_payment_expense,
        duplicate_fixed_payment_mirrors,
        dangling_payment_expense_fk,
    ):
        for finding in fn(session):
            report.findings.append(finding)
    return report


def fixed_payment_totals_reconcile(
    session: Session,
    user_id: int,
    month_year: str,
) -> dict[str, Decimal]:
    """Return ``{currency: total}`` summed from LIVE mirror expenses for
    the given user/month. Useful for tests verifying the dashboard total
    matches the underlying mirror rows (i.e. nothing was double-counted
    or soft-deleted by mistake).
    """
    fixed_ids = session.execute(
        select(FixedExpense.id).where(
            FixedExpense.telegram_user_id == user_id
        )
    ).scalars().all()
    if not fixed_ids:
        return {}
    source_keys = [
        f"fixed-payment:{fid}:{month_year}" for fid in fixed_ids
    ]
    rows = session.execute(
        select(Expense.currency, func.coalesce(func.sum(Expense.amount), 0))
        .where(
            Expense.telegram_user_id == user_id,
            Expense.source_key.in_(source_keys),
            Expense.deleted_at.is_(None),
        )
        .group_by(Expense.currency)
    ).all()
    return {cur or "ARS": Decimal(total or 0) for cur, total in rows}
