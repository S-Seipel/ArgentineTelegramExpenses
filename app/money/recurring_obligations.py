"""Read-only recurring commitments for a financial cycle.

Only explicitly opted-in templates reserve money. Their financial due date
is stable across reminder snoozes. One overdue occurrence suppresses new
occurrences until it is paid/skipped; missed months never accumulate into
invented historical debt. Upcoming occurrences inside the cycle retain
their natural monthly/yearly identities, including month-end clamping.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.expenses.models import Expense
from app.money.models import FinancialCycle
from app.recurring.models import RecurringExpense
from app.recurring.service import advance_due_date, recurring_occurrence_source_key


def obligation_dates(template: RecurringExpense, start: date, end: date) -> tuple[date, ...]:
    due = template.obligation_due_date or template.next_due_date
    if due > end:
        return ()
    if due < start:
        return (due,)
    dates = []
    while due <= end:
        dates.append(due)
        due = advance_due_date(
            due, template.frequency,
            day_of_month=template.day_of_month,
            month_of_year=template.month_of_year,
        )
    return tuple(dates)


def resolve_pending_recurring_obligations(
    session: Session, user_id: int, cycle: FinancialCycle
) -> dict[str, Decimal]:
    """Return pending totals without changing templates or delivery state."""
    templates = session.execute(
        select(RecurringExpense).where(
            RecurringExpense.telegram_user_id == user_id,
            RecurringExpense.is_active.is_(True),
            RecurringExpense.is_obligation.is_(True),
        )
    ).scalars().all()
    occurrences = {
        recurring_occurrence_source_key(template.id, due): template
        for template in templates
        for due in obligation_dates(template, cycle.start_date, cycle.end_date)
    }
    if not occurrences:
        return {}
    paid_keys = set(session.execute(
        select(Expense.source_key).where(
            Expense.telegram_user_id == user_id,
            Expense.deleted_at.is_(None),
            Expense.source_type == "recurring",
            Expense.source_key.in_(occurrences),
        )
    ).scalars())
    totals: dict[str, Decimal] = {}
    for source_key, template in occurrences.items():
        if source_key in paid_keys:
            continue
        currency = template.currency.upper()
        totals[currency] = totals.get(currency, Decimal("0")) + template.amount
    return totals
