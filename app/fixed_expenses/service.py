"""Service layer for fixed monthly expenses.

Combines the template (``FixedExpense``) with the monthly payment record
(``FixedExpensePayment``) to produce a single view object per bill per
month, suitable for rendering in chat or the dashboard.

Phase 0 of the project (correctness foundation) tightens the persistence
model here:

- ``mark_paid`` runs as a single transaction guarded by ``SELECT ... FOR
  UPDATE`` on the template row, with a partial unique key
  ``(fixed_id, month_year)`` and a stable ``source_key`` on the linked
  ``expenses`` row as second defense against duplicates.
- ``unmark`` and ``mark_skipped`` soft-delete the linked expense
  (``deleted_at IS NOT NULL``) instead of removing it; the payment row
  keeps ``expense_id`` so the historical link survives.
- A re-``mark_paid`` after an unpay restores the previous mirror via the
  ``source_key`` lookup rather than creating a new row.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.categories.models import Category
from app.expenses.models import Expense
from app.expenses.repository import ExpenseRepository
from app.expenses.service import ExpenseDraft, ExpenseService
from app.fixed_expenses.models import FixedExpense, FixedExpensePayment, MonthlyBudget
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.schemas import (
    FixedExpenseCreate,
    FixedExpenseWithStatus,
    MonthlyBudgetOut,
)
from app.utils.now import business_now, business_today


class FixedExpenseValidationError(ValueError):
    pass


_ALLOWED_METHODS = {
    "TRANSFERENCIA", "EFECTIVO", "DEBITO", "DEBITO AUTOMATICO",
    "TARJETA", "CREDITO", "MERCADO PAGO", "APP", "OTRO",
}
_ALLOWED_CURRENCIES = {
    "ARS", "USD", "EUR", "BRL", "CLP", "MXN", "UYU", "PYG", "GBP", "JPY",
}


@dataclass
class FixedExpenseDraft:
    name: str
    expected_amount: Decimal
    currency: str | None = None
    payment_method: str | None = None
    due_day_of_month: int | None = None
    category: str | None = None
    confidence: float = 1.0


def current_month_year(today: _date | None = None) -> str:
    """Return 'YYYY-MM' for the given (or current) business date."""
    d = today or business_today()
    return f"{d.year:04d}-{d.month:02d}"


def fixed_payment_source_key(fixed_id: int, month_year: str) -> str:
    """Stable identity of the expense mirror for a (bill, month) occurrence."""
    return f"fixed-payment:{fixed_id}:{month_year}"


def _resolve_category_id(
    session: Session, name: str | None
) -> int | None:
    """Resolve a category NAME for a ``FixedExpense`` template.

    Categories are stored as a tree; identity is ``(parent_id, name)``.
    A naive ``WHERE name = ?`` returns multiple rows when the same name
    appears under different parents (the canonical ``Café`` lives
    under ``Comida`` and the seed tree maps ``Café`` to that parent).
    The hierarchy-aware picker below is the same one
    ``ExpenseService._pick_category_id`` uses — we keep a tiny local
    copy here so the fixed-expense flow stays independent of the
    expenses module.
    """
    if not name:
        return None
    from app.categories.categories import get_parent_of

    candidates = (
        session.query(Category)
        .filter(Category.name == name.strip())
        .all()
    )
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0].id
    parent_name = get_parent_of(name.strip())
    if parent_name is None:
        return None
    parent_row = (
        session.query(Category)
        .filter(Category.name == parent_name)
        .first()
    )
    if parent_row is None:
        return None
    for cand in candidates:
        if cand.parent_id == parent_row.id:
            return cand.id
    return None


class FixedExpenseService:
    def __init__(self, repo: FixedExpenseRepository) -> None:
        self.repo = repo

    # ------------------------------------------------------------------
    # template CRUD
    # ------------------------------------------------------------------

    def add(self, user_id: int, draft: FixedExpenseDraft) -> FixedExpense:
        if not draft.name or not draft.name.strip():
            raise FixedExpenseValidationError(
                "Decime el nombre del gasto fijo."
            )
        if draft.expected_amount is None or draft.expected_amount <= 0:
            raise FixedExpenseValidationError(
                "El monto esperado tiene que ser mayor a cero."
            )
        if draft.expected_amount > Decimal("100000000"):
            raise FixedExpenseValidationError(
                "El monto parece demasiado grande, ¿podés confirmarlo?"
            )
        currency = (draft.currency or "ARS").upper()
        if currency not in _ALLOWED_CURRENCIES:
            raise FixedExpenseValidationError(
                f"No reconozco la moneda {currency!r}."
            )
        method = (draft.payment_method or "").upper().strip() or None
        if method and method not in _ALLOWED_METHODS:
            method = "OTRO"
        due_day = draft.due_day_of_month or 1
        if not (1 <= due_day <= 31):
            raise FixedExpenseValidationError(
                "El día de vencimiento tiene que estar entre 1 y 31."
            )

        category_id = _resolve_category_id(self.repo.session, draft.category)
        payload = FixedExpenseCreate(
            telegram_user_id=user_id,
            name=draft.name.strip()[:255],
            expected_amount=draft.expected_amount,
            currency=currency,
            category_id=category_id,
            payment_method=method,
            due_day_of_month=due_day,
        )
        return self.repo.create(payload)

    def list_active(self, user_id: int) -> list[FixedExpense]:
        return self.repo.list_active(user_id)

    def set_active(
        self, user_id: int, fixed_id: int, is_active: bool
    ) -> FixedExpense | None:
        obj = self.repo.get_by_id(user_id, fixed_id)
        if obj is None:
            return None
        return self.repo.set_active(obj, is_active)

    # ------------------------------------------------------------------
    # payments (atomic, source-keyed)
    # ------------------------------------------------------------------

    def mark_paid(
        self,
        user_id: int,
        fixed_id: int,
        month_year: str,
        actual_amount: Decimal | None = None,
        note: str | None = None,
        paid_on: Optional[_date] = None,
    ) -> tuple[Optional[FixedExpense], Optional[FixedExpensePayment]]:
        """Mark a fixed expense as paid for the given month, atomically.

        Single transaction:

        1. ``SELECT ... FOR UPDATE`` on the ``fixed_expenses`` row so a
           concurrent caller waits on the same template.
        2. Lock / create the matching ``fixed_expense_payments`` row. The
           ``uq_payment_per_month`` unique constraint is the ultimate DB
           defense if two sessions bypass the lock (e.g. direct SQL).
        3. Resolve the linked expense by ``source_key`` (this is the
           *second* defense — even if the payment row is somehow
           recreated, the source key keeps the linked expense unique).
           Restore the row if it had been soft-deleted.
        4. Persist payment fields and return.

        No intermediate commits: the caller (``session_scope`` or the
        FastAPI handler) decides when to commit.
        """
        obj = self.repo.get_by_id_for_update(user_id, fixed_id)
        if obj is None:
            return None, None

        amount = (
            actual_amount if actual_amount is not None else obj.expected_amount
        )
        paid_on_date = paid_on or business_today()
        source_key = fixed_payment_source_key(obj.id, month_year)

        payment = self.repo.get_payment_for_update(fixed_id, month_year)
        if payment is None:
            payment = FixedExpensePayment(
                fixed_expense_id=fixed_id,
                month_year=month_year,
                skipped=False,
            )
            self.repo.session.add(payment)
            try:
                self.repo.session.flush()
            except IntegrityError:
                # Lost the race against another session that committed
                # a payment for the same ``(fixed_expense_id, month_year)``
                # before us. The DB still has an open transaction in a
                # failed state — ``session.rollback()`` clears it so we
                # can re-query without ``InvalidRequestError``.
                self.repo.session.rollback()
                # Refetch the row that won, with the lock still held
                # by the original transaction (which is now empty).
                # The outer ``session_scope`` (or test commit) will
                # commit our mutations at the end.
                payment = self.repo.get_payment_for_update(
                    fixed_id, month_year
                )
                if payment is None:
                    # Defensive: another session rolled back between
                    # the IntegrityError and our refetch. Surface the
                    # original error rather than guessing.
                    raise

        expense = self._resolve_or_create_expense(
            user_id=user_id,
            amount=amount,
            currency=obj.currency,
            name=obj.name,
            category_id=obj.category_id,
            paid_on=paid_on_date,
            source_key=source_key,
            original_message=(
                f"[gastofijo #{obj.id}] {obj.name} "
                f"({obj.payment_method or 's/método'})"
            ),
        )

        payment.paid_at = business_now()
        payment.actual_amount = actual_amount
        payment.note = note
        payment.skipped = False
        payment.expense_id = expense.id

        return obj, payment

    def mark_skipped(
        self,
        user_id: int,
        fixed_id: int,
        month_year: str,
    ) -> tuple[Optional[FixedExpense], Optional[FixedExpensePayment]]:
        """Mark the occurrence as skipped: status flag only.

        If a previous ``mark_paid`` had produced an expense mirror, it is
        soft-deleted so it stops counting. The ``expense_id`` link is kept
        so a later re-``mark_paid`` can find and restore the original row
        by ``source_key`` (and via the FK).
        """
        obj = self.repo.get_by_id_for_update(user_id, fixed_id)
        if obj is None:
            return None, None

        payment = self.repo.get_payment_for_update(fixed_id, month_year)
        if payment is None:
            payment = FixedExpensePayment(
                fixed_expense_id=fixed_id,
                month_year=month_year,
                skipped=True,
            )
            self.repo.session.add(payment)
            self.repo.session.flush()

        # Soft-delete any mirror that an earlier pay produced.
        if payment.expense_id:
            self._soft_delete_expense(user_id, payment.expense_id)

        payment.paid_at = None
        payment.actual_amount = None
        payment.note = None
        payment.skipped = True
        # Keep payment.expense_id so the historical link survives.

        return obj, payment

    def unmark(
        self,
        user_id: int,
        fixed_id: int,
        month_year: str,
    ) -> bool:
        """Revert a previous ``mark_paid`` while keeping the audit trail.

        The payment row is updated to the pending state (paid_at=None,
        actual_amount=None, skipped=False) and its mirror expense is
        soft-deleted. ``expense_id`` is preserved on the payment row so a
        future re-``mark_paid`` can recover the mirror via the
        ``source_key`` second-defense lookup.
        """
        obj = self.repo.get_by_id_for_update(user_id, fixed_id)
        if obj is None:
            return False

        payment = self.repo.get_payment_for_update(fixed_id, month_year)
        if payment is None:
            return False

        if payment.expense_id:
            self._soft_delete_expense(user_id, payment.expense_id)

        payment.paid_at = None
        payment.actual_amount = None
        payment.note = None
        payment.skipped = False
        # payment.expense_id is preserved on purpose: see docstring.

        return True

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def find_by_name(
        self, user_id: int, name: str
    ) -> FixedExpense | None:
        return self.repo.get_by_name(user_id, name)

    def _resolve_or_create_expense(
        self,
        *,
        user_id: int,
        amount: Decimal,
        currency: str,
        name: str,
        category_id: Optional[int],
        paid_on: _date,
        source_key: str,
        original_message: str,
    ) -> Expense:
        """Find the mirror expense by source_key (including soft-deleted)
        or create a new one. Idempotent under concurrency: the unique
        partial index on ``(telegram_user_id, source_key)`` keeps a single
        row per occurrence."""
        expense_repo = ExpenseRepository(self.repo.session)
        existing = expense_repo.get_by_source_key(
            user_id, source_key, include_deleted=True
        )
        if existing is not None:
            existing.amount = amount
            existing.currency = currency
            existing.expense_date = paid_on
            if category_id is not None:
                existing.category_id = category_id
            existing.deleted_at = None
            existing.source_type = "fixed_payment"
            existing.revision = (existing.revision or 1) + 1
            self.repo.session.flush()
            return existing

        category_name = _resolve_category_name(self.repo.session, category_id)
        draft = ExpenseDraft(
            name=name[:255],
            amount=amount,
            currency=currency,
            category=category_name,
            expense_date=paid_on,
            confidence=Decimal("1"),
        )
        service = ExpenseService(expense_repo)
        # ``commit=False``: the mark_paid flow is a single transaction.
        # Letting the inner ``register_many`` commit would let the new
        # payment row leak out even if a later step raises, defeating
        # the atomicity guarantee.
        outcome = service.register_many(
            user_id=user_id,
            drafts=[draft],
            original_message=original_message,
            source_type="fixed_payment",
            source_key=source_key,
            commit=False,
        )
        if not outcome.saved:
            raise RuntimeError(
                "Failed to create linked expense for fixed payment"
            )
        # Stamp source_type / source_key because ExpenseService writes
        # the row before knowing the caller's intent.
        row = outcome.saved[0]
        row.source_type = "fixed_payment"
        row.source_key = source_key
        row.revision = 1
        self.repo.session.flush()
        return row

    def _soft_delete_expense(
        self, user_id: int, expense_id: int
    ) -> None:
        expense_repo = ExpenseRepository(self.repo.session)
        expense_repo.soft_delete(user_id, expense_id)

    def with_status_for_month(
        self,
        user_id: int,
        month_year: str,
    ) -> list[FixedExpenseWithStatus]:
        """Combine each active fixed expense with this month's payment."""
        bills = self.repo.list_active(user_id)
        payments_by_id = {
            p.fixed_expense_id: p
            for p in self.repo.payments_for_month(user_id, month_year)
        }
        result: list[FixedExpenseWithStatus] = []
        for b in bills:
            p = payments_by_id.get(b.id)
            cat_name = ""
            if getattr(b, "category", None) is not None:
                cat_name = b.category.name or ""
            result.append(
                FixedExpenseWithStatus(
                    id=b.id,
                    name=b.name,
                    expected_amount=b.expected_amount,
                    currency=b.currency,
                    payment_method=b.payment_method,
                    due_day_of_month=b.due_day_of_month,
                    category_name=cat_name,
                    is_active=b.is_active,
                    paid=(p is not None and p.paid_at is not None),
                    skipped=(p.skipped if p else False),
                    actual_amount=(p.actual_amount if p else None),
                    paid_at=(p.paid_at if p else None),
                    note=(p.note if p else None),
                )
            )
        return result

    def month_summary(
        self,
        user_id: int,
        month_year: str,
    ) -> MonthlyBudgetOut:
        budget = self.repo.get_budget(user_id, month_year)
        income = budget.income if budget else Decimal("0")
        extra = budget.extra if budget else Decimal("0")
        bills = self.with_status_for_month(user_id, month_year)
        total_expected = sum(
            (b.expected_amount for b in bills if b.is_active),
            Decimal("0"),
        )
        total_paid = Decimal("0")
        for b in bills:
            if not b.is_active or b.skipped or not b.paid:
                continue
            total_paid += (
                b.actual_amount
                if b.actual_amount is not None
                else b.expected_amount
            )
        liberado = income + extra - total_paid
        return MonthlyBudgetOut(
            income=income,
            extra=extra,
            total_fixed_expected=total_expected,
            total_fixed_paid=total_paid,
            liberado=liberado,
        )

    def set_budget(
        self,
        user_id: int,
        month_year: str,
        *,
        income: Decimal | None = None,
        extra: Decimal | None = None,
        note: str | None = None,
    ):
        return self.repo.upsert_budget(
            user_id, month_year, income=income, extra=extra, note=note
        )


# ---------------------------------------------------------------------------
# private helpers
# ---------------------------------------------------------------------------


def _resolve_category_name(session: Session, category_id: int | None) -> str:
    """Reverse-resolve a category_id back to a name for ExpenseDraft."""
    if category_id is None:
        return "Otros"
    row = (
        session.query(Category)
        .filter(Category.id == category_id)
        .one_or_none()
    )
    if row is None:
        return "Otros"
    return row.name or "Otros"
