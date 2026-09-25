"""F1 R3 MoneyStateService tests.

Covers the spec §21 test matrix for base income, extra income,
savings target, savings reserved, savings release, savings realized,
committed semantics, closed cycle enforcement, cross-user rejection
and decimal precision.

Concurrency tests (concurrent same-key UPSERT → one physical row)
live in :mod:`tests.test_money_state_concurrency`. PostgreSQL-only
schema assertions (UNIQUE surviving soft-delete, CHECK constraints)
live in :mod:`tests.test_money_state_pg`.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.database.database import session_scope
from app.money import (
    CycleService,
    MoneyEngine,
    MoneyStateService,
    UserSettingsService,
)
from app.money.engine import CycleHasPendingError
from app.money.models import (
    CycleIncome,
    SavingsRealization,
    SavingsReserved,
    SavingsTarget,
)
from app.money.money_state_service import (
    CycleClosedError,
    CycleNotFoundError,
    CycleOwnershipMismatchError,
    ExtraIncomeNotFoundError,
    InvalidAmountError,
    InvalidSourceKeyError,
    InvalidTargetValueError,
    SavingsRealizationNotFoundError,
    TargetXorViolationError,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _open_cycle(user_id: int, *, payday_day: int = 1, today: date):
    with session_scope() as s:
        UserSettingsService(s).set_payday_day(
            user_id=user_id, payday_day=payday_day
        )
        view = CycleService(s).open_initial_cycle(
            user_id=user_id, today=today
        )
    return view


def _close_cycle(user_id: int, cycle_id: int) -> None:
    with session_scope() as s:
        CycleService(s).close_cycle(
            user_id=user_id, cycle_id=cycle_id, force=True, closed_by="test"
        )


def _read_summary(user_id: int, cycle_id: int):
    with session_scope() as s:
        return MoneyEngine(s).get_cycle_summary(user_id, cycle_id)


# ---------------------------------------------------------------------------
# BASE INCOME
# ---------------------------------------------------------------------------


def test_set_income_creates_row(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        row = MoneyStateService(s).set_income(
            user_id=1,
            cycle_id=view.cycle_id,
            currency="ARS",
            amount=Decimal("1100000.00"),
        )
    assert row.amount == Decimal("1100000.00")
    assert row.kind == "income"
    assert row.currency == "ARS"
    assert row.source_key is None


def test_set_income_same_physical_position(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("1100000"),
        )
    with session_scope() as s:
        MoneyStateService(s).set_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("1100000"),
        )
    with session_scope() as s:
        from sqlalchemy import func, select
        from app.money.models import CycleIncome
        count = s.execute(
            select(func.count(CycleIncome.id)).where(
                CycleIncome.cycle_id == view.cycle_id,
                CycleIncome.kind == "income",
            )
        ).scalar_one()
        assert count == 1


def test_set_income_update_same_row_updates_amount(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("1100000"),
        )
    with session_scope() as s:
        MoneyStateService(s).set_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("1200000"),
        )
    summary = _read_summary(1, view.cycle_id)
    assert summary.base_income.get("ARS") == Decimal("1200000.00")


def test_set_income_zero_valid(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("0"),
        )
    summary = _read_summary(1, view.cycle_id)
    assert summary.base_income.get("ARS") == Decimal("0.00")


def test_set_income_negative_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        with pytest.raises(InvalidAmountError):
            MoneyStateService(s).set_income(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                amount=Decimal("-1"),
            )


def test_set_income_cross_currency_isolation(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("1100000"),
        )
        MoneyStateService(s).set_income(
            user_id=1, cycle_id=view.cycle_id, currency="USD",
            amount=Decimal("500"),
        )
    # Update ARS; USD must stay intact.
    with session_scope() as s:
        MoneyStateService(s).set_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("1200000"),
        )
    summary = _read_summary(1, view.cycle_id)
    assert summary.base_income.get("ARS") == Decimal("1200000.00")
    assert summary.base_income.get("USD") == Decimal("500.00")


def test_set_income_closed_cycle_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    _close_cycle(1, view.cycle_id)
    with session_scope() as s:
        with pytest.raises(CycleClosedError):
            MoneyStateService(s).set_income(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                amount=Decimal("100"),
            )


def test_set_income_cycle_not_found(in_memory_db):
    with session_scope() as s:
        with pytest.raises(CycleNotFoundError):
            MoneyStateService(s).set_income(
                user_id=1, cycle_id=999, currency="ARS",
                amount=Decimal("100"),
            )


def test_set_income_ownership_mismatch(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        with pytest.raises(CycleOwnershipMismatchError):
            MoneyStateService(s).set_income(
                user_id=999, cycle_id=view.cycle_id, currency="ARS",
                amount=Decimal("100"),
            )


# ---------------------------------------------------------------------------
# EXTRA INCOME
# ---------------------------------------------------------------------------


def test_record_extra_income_creates_row(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        row = MoneyStateService(s).record_extra_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50000"), source_key="telegram:extra:1:42",
            note="regalo",
        )
    assert row.amount == Decimal("50000.00")
    assert row.source_key == "telegram:extra:1:42"
    assert row.note == "regalo"


def test_record_extra_income_same_source_same_row(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        first = MoneyStateService(s).record_extra_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50000"), source_key="X",
        )
    with session_scope() as s:
        second = MoneyStateService(s).record_extra_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50000"), source_key="X",
        )
    assert first.id == second.id


def test_record_extra_income_same_source_updates_amount(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).record_extra_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50000"), source_key="X",
        )
    with session_scope() as s:
        row = MoneyStateService(s).record_extra_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("60000"), source_key="X",
        )
    assert row.amount == Decimal("60000.00")
    assert row.revision >= 2


def test_record_extra_income_soft_delete_then_restore(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        first = MoneyStateService(s).record_extra_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50"), source_key="X",
        )
    with session_scope() as s:
        MoneyStateService(s).soft_delete_extra_income(
            user_id=1, extra_id=first.id,
        )
    with session_scope() as s:
        restored = MoneyStateService(s).record_extra_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("70"), source_key="X",
        )
    assert restored.id == first.id
    assert restored.amount == Decimal("70.00")
    assert restored.deleted_at is None


def test_record_extra_income_zero_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        with pytest.raises(InvalidAmountError):
            MoneyStateService(s).record_extra_income(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                amount=Decimal("0"), source_key="X",
            )


def test_record_extra_income_negative_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        with pytest.raises(InvalidAmountError):
            MoneyStateService(s).record_extra_income(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                amount=Decimal("-1"), source_key="X",
            )


def test_record_extra_income_missing_source_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        with pytest.raises(InvalidSourceKeyError):
            MoneyStateService(s).record_extra_income(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                amount=Decimal("50"), source_key="",
            )


def test_record_extra_income_empty_source_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        with pytest.raises(InvalidSourceKeyError):
            MoneyStateService(s).record_extra_income(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                amount=Decimal("50"), source_key="   ",
            )


def test_update_extra_income_revision_bump(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        row = MoneyStateService(s).record_extra_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50"), source_key="X",
        )
    with session_scope() as s:
        updated = MoneyStateService(s).update_extra_income(
            user_id=1, extra_id=row.id, amount=Decimal("75"),
        )
    assert updated.amount == Decimal("75.00")
    assert updated.revision >= 2


def test_update_extra_income_no_change_no_bump(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        row = MoneyStateService(s).record_extra_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50"), source_key="X",
        )
    initial_rev = row.revision
    with session_scope() as s:
        same = MoneyStateService(s).update_extra_income(
            user_id=1, extra_id=row.id, amount=Decimal("50"),
        )
    assert same.revision == initial_rev


def test_soft_delete_extra_income_idempotent(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        row = MoneyStateService(s).record_extra_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50"), source_key="X",
        )
    with session_scope() as s:
        first = MoneyStateService(s).soft_delete_extra_income(
            user_id=1, extra_id=row.id,
        )
    rev_first = first.revision
    with session_scope() as s:
        second = MoneyStateService(s).soft_delete_extra_income(
            user_id=1, extra_id=row.id,
        )
    # Idempotent: revision bumps by exactly 1 on the second call
    # but no other state changes.
    assert second.deleted_at == first.deleted_at
    assert second.revision == rev_first + 1


def test_update_extra_income_cross_user_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        row = MoneyStateService(s).record_extra_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50"), source_key="X",
        )
    with session_scope() as s:
        with pytest.raises(ExtraIncomeNotFoundError):
            MoneyStateService(s).update_extra_income(
                user_id=2, extra_id=row.id, amount=Decimal("75"),
            )


def test_soft_delete_extra_income_cross_user_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        row = MoneyStateService(s).record_extra_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50"), source_key="X",
        )
    with session_scope() as s:
        with pytest.raises(ExtraIncomeNotFoundError):
            MoneyStateService(s).soft_delete_extra_income(
                user_id=2, extra_id=row.id,
            )


# ---------------------------------------------------------------------------
# SAVINGS TARGET
# ---------------------------------------------------------------------------


def test_set_savings_target_pct(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        row = MoneyStateService(s).set_savings_target(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            target_pct=Decimal("0.20"),
        )
    assert row.target_pct == Decimal("0.2000")
    assert row.target_amount is None


def test_set_savings_target_amount(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        row = MoneyStateService(s).set_savings_target(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            target_amount=Decimal("220000"),
        )
    assert row.target_amount == Decimal("220000.00")
    assert row.target_pct is None


def test_set_savings_target_both_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        with pytest.raises(TargetXorViolationError):
            MoneyStateService(s).set_savings_target(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                target_pct=Decimal("0.2"), target_amount=Decimal("100"),
            )


def test_set_savings_target_neither_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        with pytest.raises(TargetXorViolationError):
            MoneyStateService(s).set_savings_target(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
            )


def test_set_savings_target_pct_negative_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        with pytest.raises(InvalidTargetValueError):
            MoneyStateService(s).set_savings_target(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                target_pct=Decimal("-0.1"),
            )


def test_set_savings_target_pct_above_one_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        with pytest.raises(InvalidTargetValueError):
            MoneyStateService(s).set_savings_target(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                target_pct=Decimal("1.5"),
            )


def test_set_savings_target_amount_negative_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        with pytest.raises(InvalidTargetValueError):
            MoneyStateService(s).set_savings_target(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                target_amount=Decimal("-1"),
            )


def test_set_savings_target_default_soft_unpersisted(in_memory_db):
    """Reading a fresh cycle must NOT auto-insert a 20% target row.

    The 20% default lives in ``MoneyEngine``; R3 only persists a row
    when the user explicitly sets one.
    """
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    summary = _read_summary(1, view.cycle_id)
    assert summary.savings_target.is_empty()
    with session_scope() as s:
        from sqlalchemy import func, select
        count = s.execute(
            select(func.count()).select_from(SavingsTarget).where(
                SavingsTarget.cycle_id == view.cycle_id
            )
        ).scalar_one()
        assert count == 0


def test_set_savings_target_change_does_not_touch_reserved(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_savings_reserved(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("150000"),
        )
    with session_scope() as s:
        MoneyStateService(s).set_savings_target(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            target_pct=Decimal("0.20"),
        )
    summary = _read_summary(1, view.cycle_id)
    assert summary.savings_reserved.get("ARS") == Decimal("150000.00")


# ---------------------------------------------------------------------------
# SAVINGS RESERVED + RELEASE
# ---------------------------------------------------------------------------


def test_set_savings_reserved_zero(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_savings_reserved(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("0"),
        )
    summary = _read_summary(1, view.cycle_id)
    assert summary.savings_reserved.get("ARS") == Decimal("0.00")


def test_set_savings_reserved_positive(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_savings_reserved(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("220000"),
        )
    summary = _read_summary(1, view.cycle_id)
    assert summary.savings_reserved.get("ARS") == Decimal("220000.00")


def test_set_savings_reserved_replaces_value(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_savings_reserved(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("220000"),
        )
        MoneyStateService(s).set_savings_reserved(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("250000"),
        )
    summary = _read_summary(1, view.cycle_id)
    assert summary.savings_reserved.get("ARS") == Decimal("250000.00")


def test_set_savings_reserved_can_go_below_realized(in_memory_db):
    """Reserved < realized is a warning, not an error."""
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_savings_reserved(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("220000"),
        )
        MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("150000"), source_key="R1",
        )
        # Drop reserved below realized.
        MoneyStateService(s).set_savings_reserved(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("100000"),
        )
    summary = _read_summary(1, view.cycle_id)
    # committed = max(reserved, realized) = 150000
    assert summary.savings_committed.get("ARS") == Decimal("150000.00")
    assert summary.savings_reserved.get("ARS") == Decimal("100000.00")
    assert "savings_realized_exceeds_reserved" in summary.warnings


def test_set_savings_reserved_negative_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        with pytest.raises(InvalidAmountError):
            MoneyStateService(s).set_savings_reserved(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                amount=Decimal("-1"),
            )


def test_release_savings_partial(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_savings_reserved(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("220000"),
        )
        MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("150000"), source_key="R1",
        )
    with session_scope() as s:
        MoneyStateService(s).release_savings(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("100000"),
        )
    summary = _read_summary(1, view.cycle_id)
    # reserved 220 - 100 = 120; realized stays 150; committed = 150
    assert summary.savings_reserved.get("ARS") == Decimal("120000.00")
    assert summary.savings_realized.get("ARS") == Decimal("150000.00")
    assert summary.savings_committed.get("ARS") == Decimal("150000.00")


def test_release_savings_clamps_to_zero(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_savings_reserved(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("100"),
        )
    with session_scope() as s:
        MoneyStateService(s).release_savings(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("500"),
        )
    summary = _read_summary(1, view.cycle_id)
    assert summary.savings_reserved.get("ARS") == Decimal("0.00")


def test_release_savings_does_not_mutate_realized(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_savings_reserved(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("220"),
        )
        MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("150"), source_key="R1",
        )
    with session_scope() as s:
        MoneyStateService(s).release_savings(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("100"),
        )
    summary = _read_summary(1, view.cycle_id)
    # Reserved drops 220→120; realized stays 150; committed = max = 150.
    assert summary.savings_reserved.get("ARS") == Decimal("120.00")
    assert summary.savings_realized.get("ARS") == Decimal("150.00")
    assert summary.savings_committed.get("ARS") == Decimal("150.00")


# ---------------------------------------------------------------------------
# SAVINGS REALIZED
# ---------------------------------------------------------------------------


def test_record_savings_realized_creates_row(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        row = MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50000"), source_key="R1",
            note="transferencia",
        )
    assert row.amount == Decimal("50000.00")
    assert row.source_key == "R1"
    assert row.note == "transferencia"


def test_record_savings_realized_same_source_updates(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        first = MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50000"), source_key="R1",
        )
    with session_scope() as s:
        second = MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("70000"), source_key="R1",
        )
    assert first.id == second.id
    assert second.amount == Decimal("70000.00")


def test_record_savings_realized_soft_delete_then_restore(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        first = MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50"), source_key="R1",
        )
    with session_scope() as s:
        MoneyStateService(s).soft_delete_savings_realized(
            user_id=1, realization_id=first.id,
        )
    with session_scope() as s:
        restored = MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("70"), source_key="R1",
        )
    assert restored.id == first.id
    assert restored.amount == Decimal("70.00")
    assert restored.deleted_at is None


def test_record_savings_realized_amount_zero_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        with pytest.raises(InvalidAmountError):
            MoneyStateService(s).record_savings_realized(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                amount=Decimal("0"), source_key="R1",
            )


def test_record_savings_realized_amount_negative_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        with pytest.raises(InvalidAmountError):
            MoneyStateService(s).record_savings_realized(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                amount=Decimal("-1"), source_key="R1",
            )


def test_record_savings_realized_source_required(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        with pytest.raises(InvalidSourceKeyError):
            MoneyStateService(s).record_savings_realized(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                amount=Decimal("50"), source_key="",
            )


def test_update_savings_realized_revision_bump(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        row = MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50"), source_key="R1",
        )
    with session_scope() as s:
        updated = MoneyStateService(s).update_savings_realized(
            user_id=1, realization_id=row.id, amount=Decimal("75"),
        )
    assert updated.amount == Decimal("75.00")
    assert updated.revision >= 2


def test_soft_delete_savings_realized_idempotent(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        row = MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50"), source_key="R1",
        )
    with session_scope() as s:
        MoneyStateService(s).soft_delete_savings_realized(
            user_id=1, realization_id=row.id,
        )
    with session_scope() as s:
        MoneyStateService(s).soft_delete_savings_realized(
            user_id=1, realization_id=row.id,
        )
    summary = _read_summary(1, view.cycle_id)
    # After soft-delete the realization no longer counts.
    assert summary.savings_realized.get("ARS") == Decimal("0")


def test_update_savings_realized_cross_user_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        row = MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50"), source_key="R1",
        )
    with session_scope() as s:
        with pytest.raises(SavingsRealizationNotFoundError):
            MoneyStateService(s).update_savings_realized(
                user_id=2, realization_id=row.id, amount=Decimal("75"),
            )


def test_soft_delete_savings_realized_cross_user_rejected(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        row = MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50"), source_key="R1",
        )
    with session_scope() as s:
        with pytest.raises(SavingsRealizationNotFoundError):
            MoneyStateService(s).soft_delete_savings_realized(
                user_id=2, realization_id=row.id,
            )


# ---------------------------------------------------------------------------
# COMMITTED semantics
# ---------------------------------------------------------------------------


def test_committed_delta_zero_reserved_above_realized(in_memory_db):
    """150→200 realized; reserved 220k; committed stays 220k.

    delta available = 0.
    """
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("1000000"),
        )
        MoneyStateService(s).set_savings_reserved(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("220000"),
        )
        MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("150000"), source_key="R1",
        )
    before = _read_summary(1, view.cycle_id)
    assert before.savings_committed.get("ARS") == Decimal("220000.00")

    with session_scope() as s:
        MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50000"), source_key="R2",
        )
    after = _read_summary(1, view.cycle_id)
    assert after.savings_committed.get("ARS") == Decimal("220000.00")
    # Available: gross - committed - realized_expenses - pending_reserved
    # = 1000000 - 220000 - 0 - 0 = 780000
    assert after.available_to_spend.get("ARS") == Decimal("780000.00")


def test_committed_delta_negative_reserved_below_realized(in_memory_db):
    """reserved=220k, realized 150k→300k. committed 220k→300k.

    delta available = -80000.
    """
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("1000000"),
        )
        MoneyStateService(s).set_savings_reserved(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("220000"),
        )
        MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("150000"), source_key="R1",
        )
    with session_scope() as s:
        MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("150000"), source_key="R2",
        )
    summary = _read_summary(1, view.cycle_id)
    # Realized now 300k, reserved 220k → committed = 300k
    assert summary.savings_committed.get("ARS") == Decimal("300000.00")
    # Available = 1000000 - 300000 - 0 - 0 = 700000
    assert summary.available_to_spend.get("ARS") == Decimal("700000.00")


def test_committed_delta_positive_realized_drop(in_memory_db):
    """Realized 300k→180k via soft-delete. Committed 300k→220k.

    delta available = +80000.
    """
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("1000000"),
        )
        MoneyStateService(s).set_savings_reserved(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("220000"),
        )
        MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("150000"), source_key="R1",
        )
        MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("150000"), source_key="R2",
        )
    with session_scope() as s:
        # Soft-delete R2: realized goes 300k → 150k → committed = max(220, 150) = 220k
        from sqlalchemy import select
        r2 = s.execute(
            select(SavingsRealization).where(
                SavingsRealization.source_key == "R2"
            )
        ).scalar_one()
        MoneyStateService(s).soft_delete_savings_realized(
            user_id=1, realization_id=r2.id,
        )
    summary = _read_summary(1, view.cycle_id)
    assert summary.savings_committed.get("ARS") == Decimal("220000.00")
    # Available = 1000000 - 220000 - 0 - 0 = 780000
    assert summary.available_to_spend.get("ARS") == Decimal("780000.00")


# ---------------------------------------------------------------------------
# CLOSED CYCLE — every mutation rejected
# ---------------------------------------------------------------------------


def test_closed_cycle_rejects_every_mutation(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    # Seed an extra + realization so update/delete paths exist.
    with session_scope() as s:
        MoneyStateService(s).set_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("100"),
        )
        extra = MoneyStateService(s).record_extra_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50"), source_key="X",
        )
        realized = MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("50"), source_key="R1",
        )
    _close_cycle(1, view.cycle_id)

    with session_scope() as s:
        with pytest.raises(CycleClosedError):
            MoneyStateService(s).set_income(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                amount=Decimal("100"),
            )
    with session_scope() as s:
        with pytest.raises(CycleClosedError):
            MoneyStateService(s).record_extra_income(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                amount=Decimal("50"), source_key="X2",
            )
    with session_scope() as s:
        with pytest.raises(CycleClosedError):
            MoneyStateService(s).update_extra_income(
                user_id=1, extra_id=extra.id, amount=Decimal("75"),
            )
    with session_scope() as s:
        with pytest.raises(CycleClosedError):
            MoneyStateService(s).soft_delete_extra_income(
                user_id=1, extra_id=extra.id,
            )
    with session_scope() as s:
        with pytest.raises(CycleClosedError):
            MoneyStateService(s).set_savings_target(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                target_pct=Decimal("0.2"),
            )
    with session_scope() as s:
        with pytest.raises(CycleClosedError):
            MoneyStateService(s).set_savings_reserved(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                amount=Decimal("100"),
            )
    with session_scope() as s:
        with pytest.raises(CycleClosedError):
            MoneyStateService(s).record_savings_realized(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                amount=Decimal("50"), source_key="R2",
            )
    with session_scope() as s:
        with pytest.raises(CycleClosedError):
            MoneyStateService(s).update_savings_realized(
                user_id=1, realization_id=realized.id,
                amount=Decimal("75"),
            )
    with session_scope() as s:
        with pytest.raises(CycleClosedError):
            MoneyStateService(s).soft_delete_savings_realized(
                user_id=1, realization_id=realized.id,
            )
    with session_scope() as s:
        with pytest.raises(CycleClosedError):
            MoneyStateService(s).release_savings(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                amount=Decimal("50"),
            )


# ---------------------------------------------------------------------------
# DECIMAL PRECISION
# ---------------------------------------------------------------------------


def test_decimal_roundtrip_one_cent(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("0.01"),
        )
    summary = _read_summary(1, view.cycle_id)
    assert summary.base_income.get("ARS") == Decimal("0.01")


def test_decimal_roundtrip_arbitrary(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("1234.56"),
        )
        MoneyStateService(s).record_extra_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("0.01"), source_key="cent",
        )
        MoneyStateService(s).set_savings_reserved(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("9999.99"),
        )
        MoneyStateService(s).record_savings_realized(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=Decimal("0.01"), source_key="cent-R",
        )
    summary = _read_summary(1, view.cycle_id)
    assert summary.base_income.get("ARS") == Decimal("1234.56")
    assert summary.extra_income.get("ARS") == Decimal("0.01")
    assert summary.savings_reserved.get("ARS") == Decimal("9999.99")
    assert summary.savings_realized.get("ARS") == Decimal("0.01")
    # committed = max(9999.99, 0.01) = 9999.99
    assert summary.savings_committed.get("ARS") == Decimal("9999.99")


def test_set_income_int_coerced_to_decimal(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount=1100000,
        )
    summary = _read_summary(1, view.cycle_id)
    assert summary.base_income.get("ARS") == Decimal("1100000.00")


def test_set_income_string_coerced_to_decimal(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        MoneyStateService(s).set_income(
            user_id=1, cycle_id=view.cycle_id, currency="ARS",
            amount="1234.56",
        )
    summary = _read_summary(1, view.cycle_id)
    assert summary.base_income.get("ARS") == Decimal("1234.56")


def test_set_income_string_rejected_if_garbage(in_memory_db):
    today = date(2026, 10, 5)
    view = _open_cycle(1, payday_day=2, today=today)
    with session_scope() as s:
        with pytest.raises(InvalidAmountError):
            MoneyStateService(s).set_income(
                user_id=1, cycle_id=view.cycle_id, currency="ARS",
                amount="not-a-number",
            )
