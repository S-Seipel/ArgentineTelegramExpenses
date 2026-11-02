"""F1.5 financial commitments: stable identities and exact money changes."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event, select, text

from app.database.database import session_scope
from app.expenses.models import Expense
from app.money import CycleHasPendingError, CycleService, MoneyEngine, MoneyStateService, UserSettingsService
from app.money.models import FinancialCycle
from app.recurring.models import RecurringExpense, RecurringReminder
from app.recurring.repository import RecurringRepository
from app.recurring.scheduler import _try_send_for_user
from app.recurring.schemas import RecurringCreate
from app.recurring.service import RecurringService, recurring_occurrence_source_key


USER = 123456


def _cycle(today=date(2026, 10, 15), payday=1, income="1000", currency="ARS"):
    with session_scope() as s:
        UserSettingsService(s).set_payday_day(USER, payday)
        cycle_id = CycleService(s).open_initial_cycle(USER, today).cycle_id
        MoneyStateService(s).set_income(USER, cycle_id, currency, Decimal(income))
    return cycle_id


def _recurring(due=date(2026, 10, 15), *, amount="100", currency="ARS",
               user=USER, obligation=True, frequency="monthly", day=None, month=None):
    with session_scope() as s:
        from app.categories.models import Category
        category = s.execute(select(Category).where(Category.name == "Otros")).scalar_one()
        repo = RecurringRepository(s)
        row = repo.create(RecurringCreate(
            telegram_user_id=user, name="Suscripción", amount=Decimal(amount),
            currency=currency, category_id=category.id, frequency=frequency,
            day_of_month=day or due.day, month_of_year=month,
            next_due_date=due,
        ))
        if obligation:
            RecurringService(repo).set_obligation(user, row.id, True)
        return row.id


def _summary(cycle_id, today=date(2026, 10, 15)):
    with session_scope() as s:
        return MoneyEngine(s).get_cycle_summary(USER, cycle_id, today=today)


def test_opt_in_default_user_scope_and_disable(in_memory_db):
    cycle_id = _cycle()
    rid = _recurring(obligation=False)
    assert _summary(cycle_id).available_to_spend.get("ARS") == 1000
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        assert service.get(USER, rid).is_obligation is False
        assert service.set_obligation(USER + 1, rid, True) is None
        row = service.set_obligation(USER, rid, True)
        assert row.obligation_due_date == date(2026, 10, 15)
    assert _summary(cycle_id).available_to_spend.get("ARS") == 900
    with session_scope() as s:
        row = RecurringService(RecurringRepository(s)).set_obligation(USER, rid, False)
        assert row.obligation_due_date is None
    assert _summary(cycle_id).available_to_spend.get("ARS") == 1000


def test_raw_sql_default_remains_false(in_memory_db):
    rid = _recurring(obligation=False)
    with session_scope() as s:
        s.execute(text("""
            INSERT INTO recurring_expenses (
                telegram_user_id, name, amount, currency, category_id,
                frequency, day_of_month, next_due_date, is_active
            ) SELECT telegram_user_id, 'Raw default', amount, currency, category_id,
                     frequency, day_of_month, next_due_date, is_active
              FROM recurring_expenses WHERE id = :id
        """), {"id": rid})
        row = s.execute(select(RecurringExpense).where(RecurringExpense.name == "Raw default")).scalar_one()
        assert row.is_obligation is False


def test_inactive_future_and_other_user_do_not_reserve(in_memory_db):
    cycle_id = _cycle()
    _recurring(date(2026, 11, 15))
    _recurring(user=USER + 1)
    inactive = _recurring()
    with session_scope() as s:
        RecurringService(RecurringRepository(s)).set_active(USER, inactive, False)
    assert _summary(cycle_id).pending_recurring_obligations.is_empty()


def test_payment_moves_reservation_to_realized_once(in_memory_db):
    cycle_id = _cycle()
    rid = _recurring()
    before = _summary(cycle_id)
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        _, expense = service.register_occurrence(USER, rid, date(2026, 10, 15), date(2026, 10, 15))
        _, retry = service.register_occurrence(USER, rid, date(2026, 10, 16), date(2026, 10, 15))
        assert retry.id == expense.id
        assert expense.source_key == recurring_occurrence_source_key(rid, date(2026, 10, 15))
        assert s.query(Expense).count() == 1
        assert service.get(USER, rid).obligation_due_date == date(2026, 11, 15)
    after = _summary(cycle_id)
    assert after.pending_recurring_obligations.is_empty()
    assert after.realized_expenses.get("ARS") == 100
    assert after.available_to_spend == before.available_to_spend


def test_default_retry_cannot_charge_next_future_occurrence(in_memory_db):
    _cycle()
    rid = _recurring()
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        service.register_occurrence(USER, rid, date(2026, 10, 15))
        service.register_occurrence(USER, rid, date(2026, 10, 15))
        assert s.query(Expense).count() == 1
        assert service.get(USER, rid).obligation_due_date == date(2026, 11, 15)


def test_skip_releases_only_one_reservation_and_old_reply_cannot_charge(in_memory_db):
    cycle_id = _cycle()
    rid = _recurring()
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        service.skip_occurrence(USER, rid, date(2026, 10, 15), occurrence_date=date(2026, 10, 15))
        service.skip_occurrence(USER, rid, date(2026, 10, 15), occurrence_date=date(2026, 10, 15))
        _, expense = service.register_occurrence(USER, rid, date(2026, 10, 15), date(2026, 10, 15))
        assert expense is None
        assert s.query(Expense).count() == 0
        assert service.get(USER, rid).obligation_due_date == date(2026, 11, 15)
    assert _summary(cycle_id).available_to_spend.get("ARS") == 1000


def test_snooze_crosses_cycle_without_releasing_money_or_identity(in_memory_db):
    cycle_id = _cycle(date(2026, 10, 31))
    due = date(2026, 10, 31)
    rid = _recurring(due)
    with session_scope() as s:
        repo = RecurringRepository(s)
        reminder = repo.get_or_create_reminder(USER, rid, due)
        repo.mark_reminder_sent(reminder, 10)
    before = _summary(cycle_id, due)
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        row = service.postpone_occurrence(USER, rid, occurrence_date=due)
        assert row.next_due_date == date(2026, 11, 1)
        assert row.obligation_due_date == due
        reminder = s.query(RecurringReminder).one()
        assert reminder.status == "pending"
        assert reminder.delivery_status == "pending"
        assert reminder.telegram_message_id is None
        assert reminder.sent_at is None
    assert _summary(cycle_id, due).available_to_spend == before.available_to_spend
    with session_scope() as s:
        new_cycle = CycleService(s).ensure_current_cycle(USER, date(2026, 11, 1))
        new_id = new_cycle.id
    assert _summary(new_id, date(2026, 11, 1)).pending_recurring_obligations.get("ARS") == 100


@pytest.mark.asyncio
async def test_scheduler_reuses_canonical_reminder_after_snooze(in_memory_db):
    due = date(2026, 10, 31)
    rid = _recurring(due)
    bot = MagicMock()
    bot.bot.send_message = AsyncMock(side_effect=[MagicMock(message_id=10), MagicMock(message_id=11)])
    await _try_send_for_user(bot, MagicMock(), USER, due, "2026-10-31", {})
    with session_scope() as s:
        RecurringService(RecurringRepository(s)).postpone_occurrence(USER, rid, occurrence_date=due)
    await _try_send_for_user(bot, MagicMock(), USER, date(2026, 11, 1), "2026-11-01", {})
    with session_scope() as s:
        reminders = s.query(RecurringReminder).all()
        assert len(reminders) == 1
        assert reminders[0].occurrence_date == due
        assert reminders[0].telegram_message_id == 11
        assert reminders[0].status == "pending"
    assert bot.bot.send_message.await_count == 2


def test_old_snooze_after_payment_cannot_move_next_reminder(in_memory_db):
    _cycle()
    due = date(2026, 10, 15)
    rid = _recurring(due)
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        service.register_occurrence(USER, rid, due, due)
        row = service.postpone_occurrence(USER, rid, occurrence_date=due)
        assert row.next_due_date == date(2026, 11, 15)
        assert row.obligation_due_date == date(2026, 11, 15)
        row = service.postpone_occurrence(USER, rid)
        assert row.next_due_date == date(2026, 11, 15)


def test_overdue_snooze_schedules_tomorrow_but_keeps_reserved_identity(in_memory_db):
    cycle_id = _cycle(date(2026, 11, 2))
    due = date(2026, 10, 5)
    rid = _recurring(due)
    before = _summary(cycle_id, date(2026, 11, 2))
    with session_scope() as s:
        row = RecurringService(RecurringRepository(s)).postpone_occurrence(
            USER, rid, occurrence_date=due, today=date(2026, 11, 2),
        )
        assert row.next_due_date == date(2026, 11, 3)
        assert row.obligation_due_date == due
    assert _summary(cycle_id, date(2026, 11, 2)).available_to_spend == before.available_to_spend


@pytest.mark.asyncio
async def test_snooze_while_delivery_is_in_flight_keeps_new_attempt_pending(in_memory_db):
    due = date(2026, 10, 31)
    rid = _recurring(due)
    bot = MagicMock()
    async def deliver_then_snooze(**kwargs):
        with session_scope() as s:
            RecurringService(RecurringRepository(s)).postpone_occurrence(
                USER, rid, occurrence_date=due, today=due,
            )
        return MagicMock(message_id=10)
    bot.bot.send_message = AsyncMock(side_effect=deliver_then_snooze)
    await _try_send_for_user(bot, MagicMock(), USER, due, due.isoformat(), {})
    with session_scope() as s:
        reminder = s.query(RecurringReminder).one()
        assert reminder.status == "pending"
        assert reminder.delivery_status == "pending"
        assert reminder.telegram_message_id is None
        assert s.get(RecurringExpense, rid).next_due_date == date(2026, 11, 1)


def test_month_end_window_can_reserve_two_distinct_occurrences(in_memory_db):
    start = date(2026, 2, 28)
    cycle_id = _cycle(start, payday=31)
    rid = _recurring(start, day=30)
    assert _summary(cycle_id, start).pending_recurring_obligations.get("ARS") == 200
    with session_scope() as s:
        RecurringService(RecurringRepository(s)).register_occurrence(USER, rid, start, start)
    summary = _summary(cycle_id, start)
    assert summary.pending_recurring_obligations.get("ARS") == 100
    assert summary.realized_expenses.get("ARS") == 100
    assert summary.available_to_spend.get("ARS") == 800


def test_late_payment_keeps_second_reserved_occurrence_in_same_cycle(in_memory_db):
    first = date(2026, 2, 28)
    second = date(2026, 3, 30)
    cycle_id = _cycle(first, payday=31)
    rid = _recurring(first, day=30)
    before = _summary(cycle_id, second)
    assert before.pending_recurring_obligations.get("ARS") == 200
    with session_scope() as s:
        service = RecurringService(RecurringRepository(s))
        row, _ = service.register_occurrence(USER, rid, second, first)
        assert row.obligation_due_date == second
    after = _summary(cycle_id, second)
    assert after.pending_recurring_obligations.get("ARS") == 100
    assert after.realized_expenses.get("ARS") == 100
    assert after.available_to_spend == before.available_to_spend
    with session_scope() as s:
        RecurringService(RecurringRepository(s)).register_occurrence(USER, rid, second, second)
    final = _summary(cycle_id, second)
    assert final.pending_recurring_obligations.is_empty()
    assert final.realized_expenses.get("ARS") == 200
    assert final.available_to_spend == before.available_to_spend


def test_late_skip_releases_only_first_of_two_reserved_occurrences(in_memory_db):
    first = date(2026, 2, 28)
    second = date(2026, 3, 30)
    cycle_id = _cycle(first, payday=31)
    rid = _recurring(first, day=30)
    with session_scope() as s:
        row = RecurringService(RecurringRepository(s)).skip_occurrence(
            USER, rid, second, occurrence_date=first,
        )
        assert row.obligation_due_date == second
    summary = _summary(cycle_id, second)
    assert summary.pending_recurring_obligations.get("ARS") == 100
    assert summary.realized_expenses.is_empty()
    assert summary.available_to_spend.get("ARS") == 900


def test_money_refresh_preserves_local_unflushed_changes(in_memory_db):
    from app.money.models import SavingsReserved
    cycle_id = _cycle()
    with session_scope() as s:
        service = MoneyStateService(s)
        service.set_savings_reserved(USER, cycle_id, "ARS", Decimal("100"))
        row = s.get(SavingsReserved, (cycle_id, "ARS"))
        row.amount = Decimal("80")
        result = service.release_savings(USER, cycle_id, "ARS", Decimal("30"))
        assert result.amount == Decimal("50")


def test_close_snapshot_includes_local_unflushed_money(in_memory_db):
    from app.money.models import CycleIncome
    cycle_id = _cycle()
    with session_scope() as s:
        income = s.execute(select(CycleIncome).where(CycleIncome.cycle_id == cycle_id)).scalar_one()
        income.amount = Decimal("2000")
        closed = CycleService(s).close_cycle(USER, cycle_id)
        assert closed.gross_income.get("ARS") == 2000
        assert closed.available_to_spend.get("ARS") == 2000


def test_yearly_leap_day_clamps_and_advances_canonically(in_memory_db):
    due = date(2028, 2, 29)
    cycle_id = _cycle(due)
    rid = _recurring(due, frequency="yearly", day=29, month=2)
    assert _summary(cycle_id, due).pending_recurring_obligations.get("ARS") == 100
    with session_scope() as s:
        row, _ = RecurringService(RecurringRepository(s)).register_occurrence(USER, rid, due, due)
        assert row.obligation_due_date == date(2029, 2, 28)


def test_overdue_is_one_reservation_not_accumulated_months(in_memory_db):
    cycle_id = _cycle(date(2026, 10, 25))
    due = date(2026, 6, 15)
    rid = _recurring(due)
    before = _summary(cycle_id, date(2026, 10, 25))
    assert before.pending_recurring_obligations.get("ARS") == 100
    with session_scope() as s:
        row, _ = RecurringService(RecurringRepository(s)).register_occurrence(USER, rid, date(2026, 10, 25), due)
        assert row.obligation_due_date == date(2026, 11, 15)
    assert _summary(cycle_id, date(2026, 10, 25)).available_to_spend == before.available_to_spend


def test_resolving_overdue_can_reveal_next_natural_obligation(in_memory_db):
    cycle_id = _cycle(date(2026, 10, 10))
    due = date(2026, 9, 15)
    rid = _recurring(due)
    assert _summary(cycle_id).pending_recurring_obligations.get("ARS") == 100
    with session_scope() as s:
        RecurringService(RecurringRepository(s)).register_occurrence(USER, rid, date(2026, 10, 10), due)
    summary = _summary(cycle_id)
    assert summary.pending_recurring_obligations.get("ARS") == 100
    assert summary.realized_expenses.get("ARS") == 100
    assert summary.available_to_spend.get("ARS") == 800


def test_passage_of_time_does_not_release_reserved_occurrences(in_memory_db):
    cycle_id = _cycle(date(2026, 10, 1), payday=2)
    _recurring(date(2026, 9, 5))
    before = _summary(cycle_id, date(2026, 10, 1))
    after = _summary(cycle_id, date(2026, 10, 6))
    assert before.pending_recurring_obligations == after.pending_recurring_obligations
    assert before.available_to_spend == after.available_to_spend


def test_currency_partition_and_negative_available(in_memory_db):
    cycle_id = _cycle()
    _recurring(amount="125.75")
    _recurring(amount="20.01", currency="USD")
    summary = _summary(cycle_id)
    assert summary.pending_recurring_obligations.to_dict() == {"ARS": "125.75", "USD": "20.01"}
    assert summary.available_to_spend.to_dict() == {"ARS": "874.25", "USD": "-20.01"}
    assert "available_negative" in summary.warnings


def test_multi_currency_savings_warning_compares_each_currency(in_memory_db):
    cycle_id = _cycle()
    with session_scope() as s:
        state = MoneyStateService(s)
        state.set_savings_reserved(USER, cycle_id, "ARS", Decimal("100"))
        state.set_savings_reserved(USER, cycle_id, "USD", Decimal("10"))
        state.record_savings_realized(USER, cycle_id, "ARS", Decimal("50"), "save-ars")
        state.record_savings_realized(USER, cycle_id, "USD", Decimal("20"), "save-usd")
    summary = _summary(cycle_id)
    assert "savings_realized_exceeds_reserved" in summary.warnings
    assert "available_negative" in summary.warnings
    assert summary.available_to_spend.to_dict() == {"ARS": "900.00", "USD": "-20.00"}


def test_close_requires_force_and_history_never_recomputes(in_memory_db):
    cycle_id = _cycle()
    rid = _recurring()
    with pytest.raises(CycleHasPendingError):
        with session_scope() as s:
            CycleService(s).close_cycle(USER, cycle_id)
    with session_scope() as s:
        CycleService(s).close_cycle(USER, cycle_id, force=True)
        frozen = deepcopy(s.get(FinancialCycle, cycle_id).closing_snapshot)
    with session_scope() as s:
        row = s.get(RecurringExpense, rid)
        row.amount = Decimal("900")
        RecurringService(RecurringRepository(s)).set_obligation(USER, rid, False)
    with session_scope() as s:
        assert s.get(FinancialCycle, cycle_id).closing_snapshot == frozen
        summary = MoneyEngine(s).get_cycle_summary(USER, cycle_id)
        assert summary.pending_recurring_obligations.get("ARS") == 100
        assert summary.available_to_spend.get("ARS") == 900


def test_engine_recurring_reads_issue_no_writes(in_memory_db):
    cycle_id = _cycle()
    rid = _recurring()
    with session_scope() as s:
        before = s.get(RecurringExpense, rid).obligation_due_date
        writes = []
        def inspect_statement(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().split(None, 1)[0].upper() in {"UPDATE", "DELETE", "INSERT"}:
                writes.append(statement)
        event.listen(s.get_bind(), "before_cursor_execute", inspect_statement)
        try:
            for _ in range(3):
                MoneyEngine(s).get_cycle_summary(USER, cycle_id, today=date(2026, 10, 15))
        finally:
            event.remove(s.get_bind(), "before_cursor_execute", inspect_statement)
        assert writes == []
        assert s.get(RecurringExpense, rid).obligation_due_date == before
