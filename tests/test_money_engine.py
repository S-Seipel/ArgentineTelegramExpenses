"""R2 MoneyEngine read model + formula tests.

Covers the core I2-I10 invariants from design v5 FINAL §L in their
SQLite-backed form. Postgres-only behaviour (partial unique indexes,
catch-up concurrency) lives in
:mod:`tests.test_money_engine_pg`.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.database.database import session_scope
from app.expenses.models import Expense
from app.expenses.repository import ExpenseRepository
from app.expenses.service import ExpenseDraft, ExpenseService
from app.expenses.schemas import ExpenseCreate
from app.fixed_expenses.models import (
    FixedExpense,
    FixedExpensePayment,
)
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.service import (
    FixedExpenseDraft,
    FixedExpenseService,
)
from app.money import CycleService, MoneyEngine, UserSettingsService
from app.money.dto import (
    InvariantViolationKind,
    MoneyByCurrency,
)
from app.money.models import (
    CycleIncome,
    SavingsRealization,
    SavingsReserved,
    SavingsTarget,
)
from app.fixed_expenses.service import current_month_year


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _open_cycle(user_id: int, *, payday_day: int = 1, today: date):
    with session_scope() as s:
        UserSettingsService(s).set_payday_day(user_id=user_id, payday_day=payday_day)
        view = CycleService(s).open_initial_cycle(user_id=user_id, today=today)
    return view


def _add_income(
    cycle_id: int,
    *,
    currency: str,
    amount: Decimal,
    kind: str = "income",
    source_key: str | None = None,
):
    with session_scope() as s:
        s.add(
            CycleIncome(
                cycle_id=cycle_id,
                currency=currency,
                kind=kind,
                amount=amount,
                source_key=source_key,
            )
        )


def _add_fixed(user_id: int, *, name: str, amount: Decimal, due_day: int = 5, currency: str = "ARS"):
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        svc = FixedExpenseService(repo)
        bill = svc.add(
            user_id,
            FixedExpenseDraft(
                name=name,
                expected_amount=amount,
                due_day_of_month=due_day,
                currency=currency,
            ),
        )
    return bill


def _add_expense(
    user_id: int,
    *,
    amount: Decimal,
    currency: str = "ARS",
    source_type: str = "telegram",
    expense_date: date,
    source_key: str | None = None,
) -> int:
    """Insert a live expense and return its id."""
    from app.expenses.repository import ExpenseRepository

    with session_scope() as s:
        repo = ExpenseRepository(s)
        row = repo.create(
            ExpenseCreate(
                telegram_user_id=user_id,
                name="x",
                amount=amount,
                currency=currency,
                category_id=1,
                expense_date=expense_date,
                original_message="",
                ai_confidence=Decimal("0"),
                source_type=source_type,
                source_key=source_key,
                revision=1,
            )
        )
        return row.id


def _add_target(
    cycle_id: int,
    *,
    currency: str,
    pct: Decimal | None = None,
    amount: Decimal | None = None,
):
    with session_scope() as s:
        s.add(
            SavingsTarget(
                cycle_id=cycle_id,
                currency=currency,
                target_pct=pct,
                target_amount=amount,
            )
        )


def _add_reserved(cycle_id: int, *, currency: str, amount: Decimal):
    with session_scope() as s:
        s.add(
            SavingsReserved(
                cycle_id=cycle_id,
                currency=currency,
                amount=amount,
            )
        )


def _add_realization(
    cycle_id: int,
    *,
    currency: str,
    amount: Decimal,
    source_key: str,
):
    with session_scope() as s:
        s.add(
            SavingsRealization(
                cycle_id=cycle_id,
                currency=currency,
                amount=amount,
                source_key=source_key,
            )
        )


# ---------------------------------------------------------------------------
# Formula
# ---------------------------------------------------------------------------


def test_basic_available_to_spend_320k(in_memory_db):
    """Available = gross − committed − realized − pending. The classic."""
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1100000.00"))
    _add_target(view.cycle_id, currency="ARS", pct=Decimal("0.20"))
    # R2 spec §4: ``savings_reserved`` is an explicit row that defaults
    # to 0; ``SavingsTarget`` defines the goal but does not auto-reserve.
    # The classic 1.1M ARS income / 20% reserve / 400k spent / 160k
    # pending example needs the explicit reservation row.
    _add_reserved(view.cycle_id, currency="ARS", amount=Decimal("220000"))
    _add_expense(
        1, amount=Decimal("400000"), expense_date=date(2026, 10, 5)
    )
    bill_netflix = _add_fixed(
        1, name="NETFLIX", amount=Decimal("50000"), due_day=15
    )
    bill_edesur = _add_fixed(
        1, name="EDESUR", amount=Decimal("80000"), due_day=20
    )
    bill_gym = _add_fixed(
        1, name="GIMNASIO", amount=Decimal("30000"), due_day=25
    )
    # No mark_paid, so all three are pending.
    with session_scope() as s:
        engine = MoneyEngine(s)
        available = engine.get_available_to_spend(1, view.cycle_id, today=today)
    assert available.get("ARS") == Decimal("320000.00")


def test_negative_available_when_expenses_exceed_income(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=1, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("100"))
    _add_expense(1, amount=Decimal("200"), expense_date=date(2026, 10, 5))
    with session_scope() as s:
        engine = MoneyEngine(s)
        available = engine.get_available_to_spend(1, view.cycle_id, today=today)
    assert available.get("ARS") == Decimal("-100")


def test_all_expense_source_types_count(in_memory_db):
    """normal/fixed_payment/recurring expenses all flow into realized."""
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=1, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1000000"))
    # Use a date inside the cycle so realized_expenses picks them up.
    _add_expense(
        1,
        amount=Decimal("100000"),
        source_type="telegram",
        expense_date=date(2026, 10, 5),
    )
    _add_expense(
        1,
        amount=Decimal("50000"),
        source_type="fixed_payment",
        source_key="fixed-payment:1:2026-10",
        expense_date=date(2026, 10, 10),
    )
    _add_expense(
        1,
        amount=Decimal("25000"),
        source_type="recurring",
        source_key="recurring:1:2026-10-15",
        expense_date=date(2026, 10, 15),
    )
    with session_scope() as s:
        engine = MoneyEngine(s)
        realized = engine.get_cycle_summary(
            1, view.cycle_id, today=today
        ).realized_expenses
    # R2 spec §4 includes all three source_type flavours in
    # realized_expenses so the pending→realized transition balances.
    assert realized.get("ARS") == Decimal("175000")


def test_soft_deleted_expense_excluded_then_restored(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=1, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1000000"))
    eid = _add_expense(
        1, amount=Decimal("100000"), expense_date=date(2026, 10, 5)
    )
    with session_scope() as s:
        engine = MoneyEngine(s)
        realized_before = engine.get_cycle_summary(
            1, view.cycle_id, today=today
        ).realized_expenses
    assert realized_before.get("ARS") == Decimal("100000")

    # Soft-delete the expense and re-query.
    with session_scope() as s:
        ExpenseRepository(s).soft_delete(1, eid, commit=True)
    with session_scope() as s:
        engine = MoneyEngine(s)
        realized_after_delete = engine.get_cycle_summary(
            1, view.cycle_id, today=today
        ).realized_expenses
    assert realized_after_delete.get("ARS") == Decimal("0")

    # Restore: counts again.
    with session_scope() as s:
        ExpenseRepository(s).restore(1, eid)
    with session_scope() as s:
        engine = MoneyEngine(s)
        realized_after_restore = engine.get_cycle_summary(
            1, view.cycle_id, today=today
        ).realized_expenses
    assert realized_after_restore.get("ARS") == Decimal("100000")


# ---------------------------------------------------------------------------
# Savings
# ---------------------------------------------------------------------------


def test_savings_committed_takes_max_reserved_and_realized(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=1, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1100000"))
    _add_target(view.cycle_id, currency="ARS", pct=Decimal("0.20"))
    _add_reserved(view.cycle_id, currency="ARS", amount=Decimal("220000"))
    _add_realization(
        view.cycle_id, currency="ARS", amount=Decimal("150000"), source_key="k1"
    )
    with session_scope() as s:
        engine = MoneyEngine(s)
        summary = engine.get_cycle_summary(1, view.cycle_id, today=today)
    # committed = max(reserved=220000, realized=150000) = 220000
    assert summary.savings_committed.get("ARS") == Decimal("220000")
    assert summary.savings_reserved.get("ARS") == Decimal("220000")
    assert summary.savings_realized.get("ARS") == Decimal("150000")


def test_savings_committed_takes_realized_when_higher(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=1, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1100000"))
    _add_target(view.cycle_id, currency="ARS", pct=Decimal("0.20"))
    _add_reserved(view.cycle_id, currency="ARS", amount=Decimal("220000"))
    _add_realization(
        view.cycle_id, currency="ARS", amount=Decimal("300000"), source_key="k1"
    )
    with session_scope() as s:
        engine = MoneyEngine(s)
        summary = engine.get_cycle_summary(1, view.cycle_id, today=today)
    assert summary.savings_committed.get("ARS") == Decimal("300000")


def test_realized_savings_does_not_change_available(in_memory_db):
    """Spec §18 invariant: available doesn't double-count savings realized."""
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=1, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1100000"))
    _add_target(view.cycle_id, currency="ARS", pct=Decimal("0.20"))
    _add_reserved(view.cycle_id, currency="ARS", amount=Decimal("220000"))
    with session_scope() as s:
        engine = MoneyEngine(s)
        available_pre = engine.get_available_to_spend(1, view.cycle_id, today=today)
    _add_realization(
        view.cycle_id, currency="ARS", amount=Decimal("150000"), source_key="k1"
    )
    with session_scope() as s:
        engine = MoneyEngine(s)
        available_post = engine.get_available_to_spend(1, view.cycle_id, today=today)
    assert available_pre.get("ARS") == available_post.get("ARS")


def test_savings_delta_80000_increases_then_decreases(in_memory_db):
    """Spec §18: −80000 between 150k→300k, +80000 between 300k→180k.

    The absolute change is +80000 / -80000 (not the negative signed
    delta) because available moves opposite to committed.
    """
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=1, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1100000"))
    _add_target(view.cycle_id, currency="ARS", pct=Decimal("0.20"))
    _add_reserved(view.cycle_id, currency="ARS", amount=Decimal("220000"))

    _add_realization(
        view.cycle_id, currency="ARS", amount=Decimal("150000"), source_key="k1"
    )
    with session_scope() as s:
        engine = MoneyEngine(s)
        available_a = engine.get_available_to_spend(1, view.cycle_id, today=today)

    _add_realization(
        view.cycle_id, currency="ARS", amount=Decimal("150000"), source_key="k2"
    )
    with session_scope() as s:
        engine = MoneyEngine(s)
        available_b = engine.get_available_to_spend(1, view.cycle_id, today=today)
    # committed rose from 220k → 300k → available falls by 80k
    assert available_b.get("ARS") - available_a.get("ARS") == Decimal("-80000")

    # Now move 300k → 180k by *reducing* realized (e.g. release
    # operation lands at 180k). committed falls from 300k → 220k → +
    # 80000 to available.
    with session_scope() as s:
        rows = (
            s.query(SavingsRealization)
            .filter_by(cycle_id=view.cycle_id)
            .order_by(SavingsRealization.id)
            .all()
        )
        # Two realizations of 150000; delete one to land at 150k total,
        # then update the remaining to 180k so the spec's
        # "300000 → 180000" deltas land exactly.
        rows[0].deleted_at = datetime.now(timezone.utc)
        rows[1].amount = Decimal("180000")
        s.commit()
    with session_scope() as s:
        engine = MoneyEngine(s)
        available_c = engine.get_available_to_spend(1, view.cycle_id, today=today)
    # committed fell from 300k → 220k → available rises by 80k
    assert available_c.get("ARS") - available_b.get("ARS") == Decimal("80000")


# ---------------------------------------------------------------------------
# Fixed obligations
# ---------------------------------------------------------------------------


def test_paid_occurrence_does_not_change_available(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1100000"))
    _add_target(view.cycle_id, currency="ARS", pct=Decimal("0.20"))
    _add_reserved(view.cycle_id, currency="ARS", amount=Decimal("220000"))
    _add_expense(1, amount=Decimal("400000"), expense_date=date(2026, 10, 5))
    my = current_month_year(today)
    bill = _add_fixed(1, name="NETFLIX", amount=Decimal("50000"), due_day=15)

    # Capture available BEFORE the payment: NETFLIX is pending (50000)
    # at this point so available = 1100 - 220 - 400 - 50 = 430k.
    with session_scope() as s:
        engine = MoneyEngine(s)
        available_before = engine.get_available_to_spend(
            1, view.cycle_id, today=today
        )
    assert available_before.get("ARS") == Decimal("430000")

    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        svc = FixedExpenseService(repo)
        svc.mark_paid(1, bill.id, my, actual_amount=Decimal("50000"), paid_on=today)

    with session_scope() as s:
        engine = MoneyEngine(s)
        available_after = engine.get_available_to_spend(
            1, view.cycle_id, today=today
        )
        summary = engine.get_cycle_summary(1, view.cycle_id, today=today)
    # Spec §I2: paying an obligation does not change available.
    assert available_after.get("ARS") == available_before.get("ARS")
    assert summary.pending_fixed_obligations.is_empty()


def test_paid_actual_above_expected_reduces_available(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1100000"))
    _add_target(view.cycle_id, currency="ARS", pct=Decimal("0.20"))
    _add_reserved(view.cycle_id, currency="ARS", amount=Decimal("220000"))

    my = current_month_year(today)
    bill = _add_fixed(1, name="LUZ", amount=Decimal("80"), due_day=15)
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        svc = FixedExpenseService(repo)
        svc.mark_paid(1, bill.id, my, actual_amount=Decimal("92"), paid_on=today)

    with session_scope() as s:
        engine = MoneyEngine(s)
        available = engine.get_available_to_spend(1, view.cycle_id, today=today)
    # gross 1100000 - reserved 220000 - realized 92 = 879908
    assert available.get("ARS") == Decimal("879908.00")


def test_paid_actual_below_expected_increases_available(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1100000"))
    _add_target(view.cycle_id, currency="ARS", pct=Decimal("0.20"))
    _add_reserved(view.cycle_id, currency="ARS", amount=Decimal("220000"))

    my = current_month_year(today)
    bill = _add_fixed(1, name="LUZ", amount=Decimal("80"), due_day=15)
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        svc = FixedExpenseService(repo)
        svc.mark_paid(1, bill.id, my, actual_amount=Decimal("70"), paid_on=today)

    with session_scope() as s:
        engine = MoneyEngine(s)
        available = engine.get_available_to_spend(1, view.cycle_id, today=today)
    # gross 1100000 - reserved 220000 - realized 70 = 879930
    assert available.get("ARS") == Decimal("879930.00")


def test_skipped_occurrence_excluded_from_pending(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1000000"))
    _add_target(view.cycle_id, currency="ARS", pct=Decimal("0.20"))

    my = current_month_year(today)
    bill = _add_fixed(1, name="X", amount=Decimal("50000"), due_day=15)
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        svc = FixedExpenseService(repo)
        svc.mark_skipped(1, bill.id, my)

    with session_scope() as s:
        engine = MoneyEngine(s)
        pending = engine.get_cycle_summary(
            1, view.cycle_id, today=today
        ).pending_fixed_obligations
    assert pending.is_empty()


def test_pending_occurrence_uses_expected_amount(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=2, today=today)
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1000000"))
    _add_target(view.cycle_id, currency="ARS", pct=Decimal("0.20"))

    bill = _add_fixed(1, name="A", amount=Decimal("30000"), due_day=15)
    with session_scope() as s:
        engine = MoneyEngine(s)
        pending = engine.get_cycle_summary(
            1, view.cycle_id, today=today
        ).pending_fixed_obligations
    assert pending.get("ARS") == Decimal("30000")


# ---------------------------------------------------------------------------
# Currency partition
# ---------------------------------------------------------------------------


def test_ars_and_usd_independent(in_memory_db):
    today = date(2026, 10, 15)
    view = _open_cycle(1, payday_day=1, today=today)
    with session_scope() as s:
        usd_view = CycleService(s).open_initial_cycle(1, today, currency="USD")
    _add_income(view.cycle_id, currency="ARS", amount=Decimal("1100000"))
    _add_income(usd_view.cycle_id, currency="USD", amount=Decimal("100"))
    _add_target(view.cycle_id, currency="ARS", pct=Decimal("0.20"))
    _add_target(usd_view.cycle_id, currency="USD", pct=Decimal("0.20"))
    _add_reserved(view.cycle_id, currency="ARS", amount=Decimal("220000"))
    _add_reserved(usd_view.cycle_id, currency="USD", amount=Decimal("20"))
    _add_expense(
        1, amount=Decimal("400000"), currency="ARS", expense_date=date(2026, 10, 5)
    )
    _add_expense(
        1, amount=Decimal("50"), currency="USD", expense_date=date(2026, 10, 6)
    )
    _add_fixed(1, name="F1", amount=Decimal("50000"), due_day=15)
    _add_fixed(
        1, name="F2", amount=Decimal("20"), currency="USD", due_day=15
    )
    with session_scope() as s:
        engine = MoneyEngine(s)
        summary = engine.get_cycle_summary(1, view.cycle_id, today=today)
        usd_summary = engine.get_cycle_summary(1, usd_view.cycle_id, today=today)
    # ARS: 1100 - 220 - 400 - 50 = 430 (F1=50 pending)
    # USD: 100 - 20 - 50 - 20 = 10
    assert summary.available_to_spend.get("ARS") == Decimal("430000")
    assert usd_summary.available_to_spend.get("USD") == Decimal("10")
    assert summary.available_to_spend.get("USD") == 0
    assert usd_summary.available_to_spend.get("ARS") == 0
    # And: total() refuses to add them.
    assert summary.available_to_spend.union(usd_summary.available_to_spend).total() is None


def test_money_by_currency_total_returns_none_for_multi_currency():
    mc = MoneyByCurrency({"ARS": Decimal("100"), "USD": Decimal("5")})
    assert mc.total() is None


def test_money_by_currency_total_returns_value_for_single_currency():
    mc = MoneyByCurrency({"ARS": Decimal("100")})
    assert mc.total() == Decimal("100")


def test_money_by_currency_zero_entries_are_pruned():
    """Zero amounts do not appear in the JSON contract."""
    mc = MoneyByCurrency({"ARS": Decimal("0"), "USD": Decimal("50")})
    assert mc.currencies() == ["USD"]
    assert mc.to_dict() == {"USD": "50"}


def test_money_by_currency_total_returns_zero_when_empty():
    assert MoneyByCurrency().total() == Decimal("0")
