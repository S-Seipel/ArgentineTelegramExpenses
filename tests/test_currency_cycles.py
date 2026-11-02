"""Independent money calendars, immutable legacy history, and replay safety."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database.database import get_session_factory, session_scope
from app.expenses.repository import ExpenseRepository
from app.expenses.schemas import ExpenseCreate
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.service import FixedExpenseDraft, FixedExpenseService
from app.money import (
    CycleCurrencyMismatchError, CycleService, MoneyEngine,
    MoneySourceCurrencyConflictError, MoneyStateService, UserSettingsService,
)
from app.money.dto import CarriedOverFixedOccurrence, MoneyByCurrency
from app.money.models import (
    CurrencyPaydaySettings, CycleIncome, FinancialCycle, SavingsRealization,
)
from app.money.snapshot import serialize_snapshot


USER = 123456
TODAY = date(2026, 10, 15)


def _open(currency="ARS", *, today=TODAY, user=USER):
    with session_scope() as s:
        return CycleService(s).open_initial_cycle(user, today, currency=currency).cycle_id


def _summary(cycle, *, today=TODAY):
    with session_scope() as s:
        return MoneyEngine(s).get_cycle_summary(USER, cycle, today=today)


def _fixed(currency, amount):
    with session_scope() as s:
        return FixedExpenseService(FixedExpenseRepository(s)).add(USER, FixedExpenseDraft(
            name="Bill " + currency, expected_amount=Decimal(amount), currency=currency,
            due_day_of_month=10,
        )).id


def test_same_user_can_have_one_open_cycle_per_currency(in_memory_db):
    ars = _open()
    usd = _open("USD")
    assert ars != usd
    with session_scope() as s:
        service = CycleService(s)
        assert service.get_current_cycle(USER, TODAY).cycle_id == ars
        assert service.get_current_cycle(USER, TODAY, currency="USD").cycle_id == usd
        assert s.get(FinancialCycle, ars).cycle_index == s.get(FinancialCycle, usd).cycle_index == 1
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(FinancialCycle(
                telegram_user_id=USER, currency="USD", cycle_index=2,
                start_date=date(2026, 10, 1), end_date=date(2026, 10, 31), payday_day=1,
            ))


def test_inherited_currency_calendar_freezes_at_first_open(in_memory_db):
    with session_scope() as s:
        config = UserSettingsService(s)
        config.set_first_weekday(USER)
        inherited = config.get_settings(USER, currency="USD")
        assert inherited.inherited is True
        assert (inherited.pay_rule, inherited.payday_weekday) == ("first_weekday", 4)
        assert s.query(CurrencyPaydaySettings).count() == 0
    usd = _open("USD")
    with session_scope() as s:
        config = UserSettingsService(s)
        config.set_payday_day(USER, 20)
        frozen = config.get_settings(USER, currency="USD")
        assert frozen.inherited is False
        assert (frozen.pay_rule, frozen.payday_weekday) == ("first_weekday", 4)
        successor = CycleService(s).ensure_current_cycle(USER, date(2026, 11, 6), currency="USD")
        assert successor.currency == "USD"
        assert (successor.start_date, successor.end_date) == (date(2026, 11, 6), date(2026, 12, 3))
        assert s.get(FinancialCycle, usd).currency == "USD"


def test_currency_override_does_not_open_cycles_or_change_ars(in_memory_db):
    with session_scope() as s:
        config = UserSettingsService(s)
        config.set_first_weekday(USER)
        config.set_payday_day(USER, 20, currency="USD")
        assert config.get_settings(USER).pay_rule == "first_weekday"
        assert config.get_settings(USER, currency="USD").payday_day == 20
        assert s.query(FinancialCycle).count() == 0


@pytest.mark.parametrize("currency", ["", "US", "USDT", "USD OR 1=1", "12$", None])
def test_invalid_currency_never_opens_a_cycle(in_memory_db, currency):
    with session_scope() as s:
        with pytest.raises(ValueError):
            CycleService(s).ensure_current_cycle(USER, TODAY, currency=currency)
        assert s.query(FinancialCycle).count() == 0


@pytest.mark.parametrize("operation", ["income", "extra", "target", "reserved", "release", "realized"])
def test_money_mutations_reject_a_different_cycle_currency(in_memory_db, operation):
    ars = _open()
    with pytest.raises(CycleCurrencyMismatchError):
        with session_scope() as s:
            state = MoneyStateService(s)
            if operation == "income": state.set_income(USER, ars, "USD", Decimal("10"))
            elif operation == "extra": state.record_extra_income(USER, ars, "USD", Decimal("10"), "key")
            elif operation == "target": state.set_savings_target(USER, ars, "USD", target_amount=Decimal("10"))
            elif operation == "reserved": state.set_savings_reserved(USER, ars, "USD", Decimal("10"))
            elif operation == "release": state.release_savings(USER, ars, "USD", Decimal("10"))
            else: state.record_savings_realized(USER, ars, "USD", Decimal("10"), "key")
    assert _summary(ars).gross_income.is_empty()


@pytest.mark.parametrize("kind", ["extra", "savings_realized"])
def test_source_key_cannot_move_currency_between_cycles_or_after_delete(in_memory_db, kind):
    ars, usd = _open(), _open("USD")
    with session_scope() as s:
        state = MoneyStateService(s)
        if kind == "extra":
            row = state.record_extra_income(USER, ars, "ARS", Decimal("10"), "replay")
            state.soft_delete_extra_income(USER, row.id)
        else:
            row = state.record_savings_realized(USER, ars, "ARS", Decimal("10"), "replay")
            state.soft_delete_savings_realized(USER, row.id)
    with pytest.raises(MoneySourceCurrencyConflictError):
        with session_scope() as s:
            state = MoneyStateService(s)
            if kind == "extra": state.record_extra_income(USER, usd, "USD", Decimal("10"), "replay")
            else: state.record_savings_realized(USER, usd, "USD", Decimal("10"), "replay")
    with session_scope() as s:
        model = CycleIncome if kind == "extra" else SavingsRealization
        assert s.query(model).filter(model.source_key == "replay").count() == 1


def test_custom_source_key_can_repeat_in_later_cycle_in_same_currency(in_memory_db):
    first = _open()
    with session_scope() as s:
        MoneyStateService(s).record_extra_income(USER, first, "ARS", Decimal("10"), "salary-extra")
        second = CycleService(s).ensure_current_cycle(USER, date(2026, 11, 5))
        MoneyStateService(s).record_extra_income(USER, second.id, "ARS", Decimal("20"), "salary-extra")
        assert s.query(CycleIncome).filter(CycleIncome.source_key == "salary-extra").count() == 2


def test_separate_windows_scope_income_expenses_savings_and_obligations(in_memory_db):
    with session_scope() as s:
        config = UserSettingsService(s)
        config.set_first_weekday(USER)
        config.set_payday_day(USER, 20, currency="USD")
    ars, usd = _open(), _open("USD")
    _fixed("ARS", "50")
    _fixed("USD", "5")
    with session_scope() as s:
        from app.categories.models import Category
        category = s.execute(select(Category).where(Category.name == "Otros")).scalar_one()
        state = MoneyStateService(s)
        state.set_income(USER, ars, "ARS", Decimal("1000"))
        state.set_income(USER, usd, "USD", Decimal("100"))
        state.set_savings_reserved(USER, ars, "ARS", Decimal("200"))
        state.set_savings_reserved(USER, usd, "USD", Decimal("10"))
        for currency, amount, day in [
            ("ARS", "100", date(2026, 10, 25)), ("USD", "10", date(2026, 10, 25)),
            ("USD", "20", date(2026, 10, 15)), ("ARS", "999", date(2026, 9, 25)),
        ]:
            ExpenseRepository(s).create(ExpenseCreate(
                telegram_user_id=USER, name="Synthetic", amount=Decimal(amount), currency=currency,
                category_id=category.id, expense_date=day, original_message="synthetic",
                ai_confidence=Decimal("1"),
            ))
    a, u = _summary(ars), _summary(usd)
    assert a.currency == "ARS" and u.currency == "USD"
    assert a.gross_income.to_dict() == {"ARS": "1000.00"}
    assert u.gross_income.to_dict() == {"USD": "100.00"}
    assert a.realized_expenses.to_dict() == {"ARS": "100.00"}
    assert u.realized_expenses.to_dict() == {"USD": "20.00"}
    assert a.pending_fixed_obligations.to_dict() == {"ARS": "50.00"}
    assert u.pending_fixed_obligations.to_dict() == {"USD": "5.00"}
    assert a.available_to_spend.get("ARS") == 650
    assert u.available_to_spend.get("USD") == 65
    with session_scope() as s:
        CycleService(s).ensure_current_cycle(USER, date(2026, 10, 20), currency="USD")
        assert s.get(FinancialCycle, ars).status == "open"


def _legacy_history(*, carried=()):
    cycle_id = _open()
    with session_scope() as s:
        original = MoneyEngine(s).get_cycle_summary(USER, cycle_id, today=TODAY)
        mixed = replace(
            original, base_income=MoneyByCurrency({"ARS": Decimal("1000"), "USD": Decimal("50")}),
            gross_income=MoneyByCurrency({"ARS": Decimal("1000"), "USD": Decimal("50")}),
            available_to_spend=MoneyByCurrency({"ARS": Decimal("920"), "USD": Decimal("30")}),
            carried_over_fixed=tuple(carried),
        )
        snapshot = serialize_snapshot(mixed, closed_at=datetime.now(timezone.utc), closed_by="manual")
        row = s.get(FinancialCycle, cycle_id)
        row.status = "closed"
        row.legacy_mixed_currency = True
        row.closing_snapshot = snapshot
    return cycle_id, deepcopy(snapshot)


def test_legacy_closed_multi_currency_snapshot_stays_verbatim(in_memory_db):
    cycle_id, frozen = _legacy_history()
    summary = _summary(cycle_id)
    assert summary.legacy_mixed_currency is True
    assert summary.available_to_spend.to_dict() == {"ARS": "920", "USD": "30"}
    with session_scope() as s:
        CycleService(s).ensure_current_cycle(USER, date(2026, 11, 5), currency="USD")
        assert s.get(FinancialCycle, cycle_id).closing_snapshot == frozen


def test_shared_legacy_carry_over_is_partitioned_and_never_borrows_other_timeline(in_memory_db):
    ars_id = _fixed("ARS", "80")
    usd_id = _fixed("USD", "20")
    old, frozen = _legacy_history(carried=[
        CarriedOverFixedOccurrence(ars_id, "ARS debt", Decimal("80"), "ARS", date(2026, 10, 10), "2026-10"),
        CarriedOverFixedOccurrence(usd_id, "USD debt", Decimal("20"), "USD", date(2026, 10, 10), "2026-10"),
    ])
    with session_scope() as s:
        ars = CycleService(s).ensure_current_cycle(USER, date(2026, 11, 5))
        usd = CycleService(s).ensure_current_cycle(USER, date(2026, 11, 5), currency="USD")
        assert ars.cycle_index == usd.cycle_index == 2
        ars_summary = MoneyEngine(s).get_cycle_summary(USER, ars.id, today=date(2026, 11, 5))
        usd_summary = MoneyEngine(s).get_cycle_summary(USER, usd.id, today=date(2026, 11, 5))
        assert ars_summary.pending_fixed_obligations.to_dict() == {"ARS": "80"}
        assert usd_summary.pending_fixed_obligations.to_dict() == {"USD": "20"}
        assert [o.currency for o in ars_summary.carried_over_fixed] == ["ARS"]
        assert [o.currency for o in usd_summary.carried_over_fixed] == ["USD"]
        CycleService(s).ensure_current_cycle(USER, date(2026, 12, 5), currency="USD")
        assert s.get(FinancialCycle, ars.id).status == "open"
        assert s.get(FinancialCycle, old).closing_snapshot == frozen


def test_legacy_multi_currency_flag_cannot_be_used_for_new_open_cycles(in_memory_db):
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(FinancialCycle(
                telegram_user_id=USER, currency="ARS", legacy_mixed_currency=True,
                cycle_index=1, start_date=date(2026, 10, 1), end_date=date(2026, 10, 31), payday_day=1,
            ))


def test_cached_open_reader_uses_frozen_snapshot_after_concurrent_close(in_memory_db):
    cycle = _open()
    with session_scope() as s:
        MoneyStateService(s).set_income(USER, cycle, "ARS", Decimal("1000"))
    with get_session_factory()() as stale:
        cached = stale.get(FinancialCycle, cycle)
        assert cached.status == "open"
        with session_scope() as fresh:
            CycleService(fresh).close_cycle(USER, cycle)
            frozen = deepcopy(fresh.get(FinancialCycle, cycle).closing_snapshot)
        # A normal expense can be corrected historically, but cannot change
        # what a closed financial cycle reported at its closing instant.
        with session_scope() as fresh:
            from app.categories.models import Category
            category = fresh.execute(select(Category).where(Category.name == "Otros")).scalar_one()
            ExpenseRepository(fresh).create(ExpenseCreate(
                telegram_user_id=USER, name="Historical correction", amount=Decimal("200"),
                currency="ARS", category_id=category.id, expense_date=TODAY,
                original_message="synthetic", ai_confidence=Decimal("1"),
            ))
        summary = MoneyEngine(stale).get_cycle_summary(USER, cycle, today=TODAY)
        assert summary.status == "closed"
        assert summary.available_to_spend.get("ARS") == 1000
        assert stale.get(FinancialCycle, cycle).closing_snapshot == frozen
