from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, joinedload

from app.recurring.models import RecurringExpense, RecurringReminder
from app.recurring.schemas import RecurringCreate, RecurringOut


REMINDER_CLAIM_TIMEOUT = timedelta(minutes=10)


class RecurringRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, payload: RecurringCreate) -> RecurringExpense:
        obj = RecurringExpense(
            telegram_user_id=payload.telegram_user_id,
            name=payload.name,
            amount=payload.amount,
            currency=payload.currency,
            category_id=payload.category_id,
            frequency=payload.frequency,
            day_of_month=payload.day_of_month,
            month_of_year=payload.month_of_year,
            next_due_date=payload.next_due_date,
        )
        self.session.add(obj)
        self.session.commit()
        self.session.refresh(obj)
        return obj

    def list_for_user(
        self, user_id: int, include_inactive: bool = False
    ) -> list[RecurringOut]:
        stmt: Select = (
            select(RecurringExpense)
            .options(joinedload(RecurringExpense.category))
            .where(RecurringExpense.telegram_user_id == user_id)
            .order_by(RecurringExpense.next_due_date.asc(), RecurringExpense.id.asc())
        )
        if not include_inactive:
            stmt = stmt.where(RecurringExpense.is_active.is_(True))
        rows = self.session.execute(stmt).scalars().all()
        return [self._to_out(r) for r in rows]

    def get_by_id(
        self, user_id: int, recurring_id: int
    ) -> RecurringOut | None:
        stmt = (
            select(RecurringExpense)
            .options(joinedload(RecurringExpense.category))
            .where(
                RecurringExpense.telegram_user_id == user_id,
                RecurringExpense.id == recurring_id,
            )
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return self._to_out(row)

    def get_by_id_for_update(
        self, user_id: int, recurring_id: int
    ) -> RecurringExpense | None:
        stmt = (
            select(RecurringExpense)
            .where(
                RecurringExpense.telegram_user_id == user_id,
                RecurringExpense.id == recurring_id,
            )
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def due_on(self, user_id: int, day: date) -> Sequence[RecurringExpense]:
        stmt = (
            select(RecurringExpense)
            .where(
                RecurringExpense.telegram_user_id == user_id,
                RecurringExpense.is_active.is_(True),
                RecurringExpense.next_due_date <= day,
            )
            .order_by(RecurringExpense.id.asc())
        )
        return self.session.execute(stmt).scalars().all()

    def update_next_due(
        self,
        obj: RecurringExpense,
        next_due: date,
        *,
        notified: bool = True,
        commit: bool = True,
    ) -> RecurringExpense:
        obj.next_due_date = next_due
        if notified:
            obj.last_notified_at = datetime.now(timezone.utc)
        if commit:
            self.session.commit()
            self.session.refresh(obj)
        return obj

    def set_active(
        self, obj: RecurringExpense, is_active: bool, *, commit: bool = True
    ) -> RecurringExpense:
        obj.is_active = is_active
        if commit:
            self.session.commit()
            self.session.refresh(obj)
        return obj

    def pending_reminders(
        self, user_id: int, *, message_id: int | None = None
    ) -> list[RecurringReminder]:
        stmt = select(RecurringReminder).where(
            RecurringReminder.telegram_user_id == user_id,
            RecurringReminder.status == "pending",
        )
        if message_id is not None:
            stmt = stmt.where(
                RecurringReminder.telegram_message_id == message_id
            )
        return list(
            self.session.execute(
                stmt.order_by(RecurringReminder.id.asc())
            ).scalars().all()
        )

    def get_or_create_reminder(
        self,
        user_id: int,
        recurring_id: int,
        occurrence_date: date,
    ) -> RecurringReminder:
        row = self.get_reminder(user_id, recurring_id, occurrence_date)
        if row is not None:
            return row
        row = RecurringReminder(
            telegram_user_id=user_id,
            recurring_id=recurring_id,
            occurrence_date=occurrence_date,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_reminder(
        self, user_id: int, recurring_id: int, occurrence_date: date
    ) -> RecurringReminder | None:
        return self.session.execute(
            select(RecurringReminder).where(
                RecurringReminder.telegram_user_id == user_id,
                RecurringReminder.recurring_id == recurring_id,
                RecurringReminder.occurrence_date == occurrence_date,
            )
        ).scalar_one_or_none()

    def resolve_reminder(self, row: RecurringReminder) -> None:
        row.status = "resolved"
        row.resolved_at = datetime.now(timezone.utc)

    def claim_reminder(
        self,
        row: RecurringReminder,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Claim a delivery, reclaiming only an expired in-flight lease."""
        if row.delivery_status == "sent":
            return False
        # Normalize ``now`` to a tz-aware UTC value. ``datetime.utcnow()``
        # is naive and PostgreSQL stores it according to the session
        # timezone, which corrupts subsequent lease comparisons.
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        # PostgreSQL returns ``DateTime(timezone=True)`` columns tagged
        # with the *session* timezone, not UTC. Convert back to UTC so
        # the lease arithmetic below stays timezone-of-session
        # independent.
        last_attempt_at = row.last_attempt_at
        if last_attempt_at is not None:
            if last_attempt_at.tzinfo is None:
                last_attempt_at = last_attempt_at.replace(tzinfo=timezone.utc)
            else:
                last_attempt_at = last_attempt_at.astimezone(timezone.utc)
        if (
            row.delivery_status == "sending"
            and last_attempt_at is not None
            and now - last_attempt_at < REMINDER_CLAIM_TIMEOUT
        ):
            return False
        row.delivery_status = "sending"
        row.last_attempt_at = now
        row.attempt_count = (row.attempt_count or 0) + 1
        self.session.flush()
        return True

    def mark_reminder_sent(
        self,
        row: RecurringReminder,
        message_id: int,
        *,
        sent_at: datetime | None = None,
    ) -> None:
        row.delivery_status = "sent"
        row.telegram_message_id = message_id
        if sent_at is None:
            row.sent_at = datetime.now(timezone.utc)
        elif sent_at.tzinfo is None:
            row.sent_at = sent_at.replace(tzinfo=timezone.utc)
        else:
            row.sent_at = sent_at

    def mark_reminder_delivery_failed(self, row: RecurringReminder) -> None:
        row.delivery_status = "pending"
        row.telegram_message_id = None
        row.sent_at = None

    @staticmethod
    def _to_out(row: RecurringExpense) -> RecurringOut:
        cat = getattr(row, "category", None)
        return RecurringOut(
            id=row.id,
            name=row.name,
            amount=row.amount,
            currency=row.currency,
            category_id=row.category_id,
            category_name=cat.name if cat is not None else "",
            frequency=row.frequency,
            day_of_month=row.day_of_month,
            month_of_year=row.month_of_year,
            next_due_date=row.next_due_date,
            last_notified_at=row.last_notified_at,
            is_active=row.is_active,
        )
