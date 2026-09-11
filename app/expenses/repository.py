from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session, joinedload

from app.expenses.models import Expense
from app.expenses.schemas import ExpenseCreate, ExpenseSummary
from app.categories.models import Category


_NOT_DELETED = Expense.deleted_at.is_(None)


class ExpenseRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # creation
    # ------------------------------------------------------------------

    def create(self, payload: ExpenseCreate) -> Expense:
        obj = Expense(
            telegram_user_id=payload.telegram_user_id,
            name=payload.name,
            amount=payload.amount,
            currency=payload.currency,
            category_id=payload.category_id,
            expense_date=payload.expense_date,
            original_message=payload.original_message,
            ai_confidence=payload.ai_confidence,
            source_type=payload.source_type,
            source_key=payload.source_key,
            telegram_chat_id=payload.telegram_chat_id,
            telegram_message_id=payload.telegram_message_id,
            revision=payload.revision,
        )
        self.session.add(obj)
        self.session.commit()
        self.session.refresh(obj)
        return obj

    def create_many(
        self,
        payloads: Sequence[ExpenseCreate],
        *,
        commit: bool = True,
    ) -> list[Expense]:
        """Persist a batch of expenses.

        ``commit=False`` lets the caller stay in a single transaction
        across more operations (used by ``FixedExpenseService.mark_paid``
        so the partial unique index protects both the new mirror and
        the payment row update under the same commit boundary).
        """
        objs = [
            Expense(
                telegram_user_id=p.telegram_user_id,
                name=p.name,
                amount=p.amount,
                currency=p.currency,
                category_id=p.category_id,
                expense_date=p.expense_date,
                original_message=p.original_message,
                ai_confidence=p.ai_confidence,
                source_type=p.source_type,
                source_key=p.source_key,
                telegram_chat_id=p.telegram_chat_id,
                telegram_message_id=p.telegram_message_id,
                revision=p.revision,
            )
            for p in payloads
        ]
        self.session.add_all(objs)
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        for obj in objs:
            self.session.refresh(obj)
        return objs

    # ------------------------------------------------------------------
    # lookups — live rows (deleted_at IS NULL)
    # ------------------------------------------------------------------

    def list_recent(
        self, user_id: int, limit: int = 10
    ) -> list[ExpenseSummary]:
        stmt: Select = (
            select(Expense)
            .options(joinedload(Expense.category))
            .where(
                Expense.telegram_user_id == user_id,
                _NOT_DELETED,
            )
            .order_by(Expense.expense_date.desc(), Expense.id.desc())
            .limit(limit)
        )
        rows = self.session.execute(stmt).scalars().all()
        return [self._to_summary(r) for r in rows]

    def search_by_name(
        self,
        user_id: int,
        query: str,
        limit: int = 20,
    ) -> list[ExpenseSummary]:
        """Return expenses whose name matches ``query`` (case-insensitive).

        Uses ILIKE for Postgres and LIKE for SQLite. The query is
        surrounded by wildcards so 'star' matches 'Starbucks'.
        """
        if not query or not query.strip():
            return []
        pattern = f"%{query.strip()}%"
        stmt: Select = (
            select(Expense)
            .options(joinedload(Expense.category))
            .where(
                Expense.telegram_user_id == user_id,
                Expense.name.ilike(pattern),
                _NOT_DELETED,
            )
            .order_by(Expense.expense_date.desc(), Expense.id.desc())
            .limit(limit)
        )
        rows = self.session.execute(stmt).scalars().all()
        return [self._to_summary(r) for r in rows]

    def list_in_range(
        self,
        user_id: int,
        start: date | None = None,
        end: date | None = None,
    ) -> list[ExpenseSummary]:
        stmt: Select = (
            select(Expense)
            .options(joinedload(Expense.category))
            .where(
                Expense.telegram_user_id == user_id,
                _NOT_DELETED,
            )
            .order_by(Expense.expense_date.desc(), Expense.id.desc())
        )
        if start is not None:
            stmt = stmt.where(Expense.expense_date >= start)
        if end is not None:
            stmt = stmt.where(Expense.expense_date <= end)
        rows = self.session.execute(stmt).scalars().all()
        return [self._to_summary(r) for r in rows]

    def sum_total(
        self,
        user_id: int,
        start: date | None = None,
        end: date | None = None,
        category_id: int | None = None,
        category_name: str | None = None,
    ) -> Decimal:
        stmt = select(func.coalesce(func.sum(Expense.amount), 0)).where(
            Expense.telegram_user_id == user_id,
            _NOT_DELETED,
        )
        if start is not None:
            stmt = stmt.where(Expense.expense_date >= start)
        if end is not None:
            stmt = stmt.where(Expense.expense_date <= end)
        if category_id is not None:
            stmt = stmt.where(Expense.category_id == category_id)
        elif category_name is not None:
            stmt = stmt.join(Category).where(Category.name == category_name)
        result = self.session.execute(stmt).scalar_one()
        return Decimal(result or 0)

    def sum_by_period(
        self,
        user_id: int,
        start: date | None,
        end: date | None,
    ) -> dict[str, Decimal]:
        """Sum amounts grouped by currency in a (possibly open-ended) window.

        Both bounds are optional: pass ``None`` for "no lower/upper bound".
        Soft-deleted rows are excluded.
        """
        stmt = select(
            Expense.currency, func.coalesce(func.sum(Expense.amount), 0)
        ).where(
            Expense.telegram_user_id == user_id,
            _NOT_DELETED,
        )
        if start is not None:
            stmt = stmt.where(Expense.expense_date >= start)
        if end is not None:
            stmt = stmt.where(Expense.expense_date <= end)
        stmt = stmt.group_by(Expense.currency)
        rows = self.session.execute(stmt).all()
        return {currency: Decimal(total or 0) for currency, total in rows}

    def sum_by_period_and_category(
        self,
        user_id: int,
        *,
        start: date | None,
        end: date | None,
        category_id: int,
        currency: str | None = None,
    ) -> dict[str, Decimal]:
        """Sum a single category grouped by currency.

        Both the period AND the category filter are applied. The
        pre-Phase-0 implementation forgot to apply the category filter
        whenever a period was supplied — that's the bug fixed here.
        """
        stmt = select(
            Expense.currency, func.coalesce(func.sum(Expense.amount), 0)
        ).where(
            Expense.telegram_user_id == user_id,
            Expense.category_id == category_id,
            _NOT_DELETED,
        )
        if start is not None:
            stmt = stmt.where(Expense.expense_date >= start)
        if end is not None:
            stmt = stmt.where(Expense.expense_date <= end)
        if currency is not None:
            stmt = stmt.where(Expense.currency == currency.upper())
        stmt = stmt.group_by(Expense.currency)
        rows = self.session.execute(stmt).all()
        return {currency: Decimal(total or 0) for currency, total in rows}

    def sum_by_period_for_category(
        self,
        user_id: int,
        category_id: int,
    ) -> dict[str, Decimal]:
        """Sum a single category across all dates, grouped by currency.

        Used when the spec has a category but no explicit period.
        """
        stmt = select(
            Expense.currency, func.coalesce(func.sum(Expense.amount), 0)
        ).where(
            Expense.telegram_user_id == user_id,
            Expense.category_id == category_id,
            _NOT_DELETED,
        ).group_by(Expense.currency)
        rows = self.session.execute(stmt).all()
        return {currency: Decimal(total or 0) for currency, total in rows}

    def sum_by_category(
        self,
        user_id: int,
        start: date | None = None,
        end: date | None = None,
        currency: str | None = None,
    ) -> dict[str, Decimal]:
        stmt = (
            select(Category.name, func.coalesce(func.sum(Expense.amount), 0))
            .join(Category, Category.id == Expense.category_id)
            .where(
                Expense.telegram_user_id == user_id,
                _NOT_DELETED,
            )
            .group_by(Category.name)
            .order_by(func.sum(Expense.amount).desc())
        )
        if start is not None:
            stmt = stmt.where(Expense.expense_date >= start)
        if end is not None:
            stmt = stmt.where(Expense.expense_date <= end)
        if currency is not None:
            stmt = stmt.where(Expense.currency == currency.upper())
        rows = self.session.execute(stmt).all()
        return {name: Decimal(total or 0) for name, total in rows}

    def sum_by_category_per_currency(
        self,
        user_id: int,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, dict[str, Decimal]]:
        """Currency-safe category totals: ``{category: {currency: total}}``.

        Use this for any user-facing breakdown. The non-per-currency
        ``sum_by_category`` helper silently sums across currencies when
        the optional ``currency`` filter is omitted; this one doesn't.
        """
        stmt = (
            select(
                Category.name,
                Expense.currency,
                func.coalesce(func.sum(Expense.amount), 0),
            )
            .join(Category, Category.id == Expense.category_id)
            .where(
                Expense.telegram_user_id == user_id,
                _NOT_DELETED,
            )
            .group_by(Category.name, Expense.currency)
        )
        if start is not None:
            stmt = stmt.where(Expense.expense_date >= start)
        if end is not None:
            stmt = stmt.where(Expense.expense_date <= end)
        rows = self.session.execute(stmt).all()
        out: dict[str, dict[str, Decimal]] = {}
        for name, currency, amount in rows:
            out.setdefault(name, {})
            out[name][currency or "ARS"] = out[name].get(
                currency or "ARS", Decimal("0")
            ) + Decimal(amount or 0)
        return out

    def sum_for_category(
        self,
        user_id: int,
        category_name: str,
        start: date | None = None,
        end: date | None = None,
        currency: str | None = None,
    ) -> Decimal:
        """Return the total spent in a single category within the window."""
        stmt = (
            select(func.coalesce(func.sum(Expense.amount), 0))
            .join(Category, Category.id == Expense.category_id)
            .where(
                Expense.telegram_user_id == user_id,
                Category.name == category_name,
                _NOT_DELETED,
            )
        )
        if start is not None:
            stmt = stmt.where(Expense.expense_date >= start)
        if end is not None:
            stmt = stmt.where(Expense.expense_date <= end)
        if currency is not None:
            stmt = stmt.where(Expense.currency == currency.upper())
        total = self.session.execute(stmt).scalar_one()
        return Decimal(total or 0)

    def largest(
        self,
        user_id: int,
        start: date | None = None,
        end: date | None = None,
    ) -> ExpenseSummary | None:
        stmt = (
            select(Expense)
            .options(joinedload(Expense.category))
            .where(
                Expense.telegram_user_id == user_id,
                _NOT_DELETED,
            )
        )
        if start is not None:
            stmt = stmt.where(Expense.expense_date >= start)
        if end is not None:
            stmt = stmt.where(Expense.expense_date <= end)
        stmt = stmt.order_by(Expense.amount.desc(), Expense.id.desc()).limit(1)
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return self._to_summary(row)

    def get_by_id(
        self, user_id: int, expense_id: int
    ) -> ExpenseSummary | None:
        stmt = (
            select(Expense)
            .options(joinedload(Expense.category))
            .where(
                Expense.telegram_user_id == user_id,
                Expense.id == expense_id,
                _NOT_DELETED,
            )
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return self._to_summary(row)

    def get_latest(self, user_id: int) -> ExpenseSummary | None:
        stmt = (
            select(Expense)
            .options(joinedload(Expense.category))
            .where(
                Expense.telegram_user_id == user_id,
                _NOT_DELETED,
            )
            .order_by(Expense.id.desc())
            .limit(1)
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return self._to_summary(row)

    # ------------------------------------------------------------------
    # lookups that intentionally bypass the soft-delete filter
    # ------------------------------------------------------------------

    def get_entity(
        self, user_id: int, expense_id: int, *, include_deleted: bool = False
    ) -> Expense | None:
        """Return the ORM row (not the summary) for administrative flows.

        Soft-deleted rows are returned only when ``include_deleted=True`` so
        the caller must explicitly opt in. Used by integrity audits and
        restore flows; never by user-facing reads.
        """
        stmt = select(Expense).where(
            Expense.telegram_user_id == user_id,
            Expense.id == expense_id,
        )
        if not include_deleted:
            stmt = stmt.where(_NOT_DELETED)
        return self.session.execute(stmt).scalar_one_or_none()

    def get_by_source_key(
        self,
        user_id: int,
        source_key: str,
        *,
        include_deleted: bool = True,
    ) -> Expense | None:
        """Lookup by stable source key. Used for fixed-payment identity.

        By default looks at *every* row for that key (including
        soft-deleted), so a re-pay can find and restore its previous
        mirror instead of creating a second physical row.
        """
        stmt = select(Expense).where(
            Expense.telegram_user_id == user_id,
            Expense.source_key == source_key,
        )
        if not include_deleted:
            stmt = stmt.where(_NOT_DELETED)
        return self.session.execute(stmt).scalar_one_or_none()

    # ------------------------------------------------------------------
    # mutations
    # ------------------------------------------------------------------

    def delete(self, user_id: int, expense_id: int) -> ExpenseSummary | None:
        """Hard-delete a live expense.

        Raises ``LinkedFixedPaymentError`` when the row is referenced by a
        ``fixed_expense_payments.expense_id`` — fixed payments must be
        reversed through the domain operation, not via the generic CRUD.
        """
        from app.expenses.service import LinkedFixedPaymentError

        row = self.get_entity(user_id, expense_id)
        if row is None:
            return None
        if self._is_linked_to_fixed_payment(row.id):
            raise LinkedFixedPaymentError(
                "Este gasto pertenece a un pago fijo. "
                "Deshacelo desde /despague o el dashboard de gastos fijos."
            )
        summary = self._to_summary(row)
        self.session.delete(row)
        self.session.commit()
        return summary

    def soft_delete(
        self,
        user_id: int,
        expense_id: int,
        *,
        when: datetime | None = None,
    ) -> Expense | None:
        row = self.get_entity(user_id, expense_id)
        if row is None:
            return None
        row.deleted_at = when or datetime.utcnow()
        row.revision = (row.revision or 1) + 1
        self.session.commit()
        self.session.refresh(row)
        return row

    def restore(self, user_id: int, expense_id: int) -> Expense | None:
        row = self.get_entity(user_id, expense_id, include_deleted=True)
        if row is None or row.deleted_at is None:
            return None
        row.deleted_at = None
        row.revision = (row.revision or 1) + 1
        self.session.commit()
        self.session.refresh(row)
        return row

    def update_amount(
        self, user_id: int, expense_id: int, new_amount: Decimal
    ) -> ExpenseSummary | None:
        row = self.get_entity(user_id, expense_id)
        if row is None:
            return None
        if new_amount <= 0:
            raise ValueError("amount must be positive")
        row.amount = new_amount
        row.revision = (row.revision or 1) + 1
        self.session.commit()
        self.session.refresh(row)
        return self._to_summary(row)

    def update_name(
        self, user_id: int, expense_id: int, new_name: str
    ) -> ExpenseSummary | None:
        row = self.get_entity(user_id, expense_id)
        if row is None:
            return None
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("name cannot be empty")
        if len(new_name) > 200:
            raise ValueError("name too long")
        row.name = new_name
        row.revision = (row.revision or 1) + 1
        self.session.commit()
        self.session.refresh(row)
        return self._to_summary(row)

    def touch_revision(self, row: Expense) -> None:
        """Bump revision in-place for callers that already mutated a row."""
        row.revision = (row.revision or 1) + 1

    # ------------------------------------------------------------------
    # integrity helpers
    # ------------------------------------------------------------------

    def _is_linked_to_fixed_payment(self, expense_id: int) -> bool:
        from app.fixed_expenses.models import FixedExpensePayment

        stmt = select(FixedExpensePayment.id).where(
            FixedExpensePayment.expense_id == expense_id
        )
        return self.session.execute(stmt).first() is not None

    @staticmethod
    def _to_summary(row: Expense) -> ExpenseSummary:
        cat = getattr(row, "category", None)
        cat_name = cat.name if cat is not None else ""
        return ExpenseSummary(
            id=row.id,
            name=row.name,
            amount=row.amount,
            currency=row.currency,
            category_id=row.category_id,
            category_name=cat_name,
            expense_date=row.expense_date,
            ai_confidence=row.ai_confidence,
            created_at=row.created_at,
            original_message=row.original_message or "",
        )
