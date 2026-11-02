"""Daily scheduler that fires recurring-expense reminders.

Runs as an asyncio task started from the FastAPI lifespan. Each active
user has a separate 09:00–09:30 window in their own timezone. Access and
presentation preferences are refreshed immediately before delivery.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time
from typing import TYPE_CHECKING

from app.database.database import session_scope
from app.metrics import metrics
from app.recurring.repository import RecurringRepository
from app.users import UserAccessDenied, UserService
from app.utils.formatting import format_currency_amount

if TYPE_CHECKING:
    from telegram.ext import Application

    from app.bot.service import BotRuntime

logger = logging.getLogger(__name__)

TICK_SECONDS = 60 * 5
NOTIFY_HOUR = 9
NOTIFY_MINUTE_WINDOW = 30


async def recurring_reminder_loop(
    bot_app: "Application", runtime: "BotRuntime"
) -> None:
    """Periodically send reminders to users with due recurring expenses."""
    last_fired_for: dict[int, str] = {}

    while True:
        try:
            await _fire_due_reminders(bot_app, runtime, last_fired_for)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Recurring reminder loop error")

        await asyncio.sleep(TICK_SECONDS)


def _now_in_tz(tz_name: str) -> datetime:
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return _dt.now(tz)


async def _fire_due_reminders(
    bot_app: "Application",
    runtime: "BotRuntime",
    last_fired_for: dict[int, str],
) -> None:
    with session_scope() as session:
        profiles = UserService(session, settings=runtime.settings).list_active()

    active_ids = {profile.telegram_user_id for profile in profiles}
    for user_id in list(last_fired_for):
        if user_id not in active_ids:
            last_fired_for.pop(user_id, None)
    for profile in profiles:
        user_id = profile.telegram_user_id
        now = _now_in_tz(profile.timezone)
        if not _in_notify_window(now):
            # Another user's clock must not clear this user's daily cache.
            last_fired_for.pop(user_id, None)
            continue
        day_key = now.date().isoformat()
        if last_fired_for.get(user_id) == day_key:
            continue
        await _try_send_for_user(bot_app, runtime, user_id, now.date(), day_key,
                                 last_fired_for, enforce_window=True)


def _in_notify_window(now: datetime) -> bool:
    return time(NOTIFY_HOUR, 0) <= now.time() <= time(NOTIFY_HOUR, NOTIFY_MINUTE_WINDOW)


def _authorized_preferences(runtime, user_id: int):
    with session_scope() as session:
        users = UserService(session, settings=runtime.settings)
        if not users.is_authorized(user_id):
            return None
        try:
            return users.get_preferences(user_id)
        except UserAccessDenied:
            return None


def _release_delivery_claim(user_id, template_id, occurrence_date, delivery_attempt):
    """Release only this attempt, preserving any newer payment or snooze."""
    with session_scope() as session:
        repo = RecurringRepository(session)
        repo.get_by_id_for_update(user_id, template_id)
        reminder = repo.get_reminder(user_id, template_id, occurrence_date)
        if (reminder is not None and reminder.status == "pending"
                and reminder.delivery_status == "sending"
                and reminder.attempt_count == delivery_attempt):
            repo.mark_reminder_delivery_failed(reminder)


async def _try_send_for_user(
    bot_app: "Application",
    runtime: "BotRuntime",
    user_id: int,
    today,
    day_key: str,
    last_fired_for: dict[int, str],
    *,
    enforce_window: bool = False,
) -> None:
    if _authorized_preferences(runtime, user_id) is None:
        last_fired_for.pop(user_id, None)
        return
    with session_scope() as session:
        repo = RecurringRepository(session)
        due = repo.due_on(user_id, today)

    if not due:
        return

    all_delivered = True
    for template in due:
        occurrence_date = (
            template.obligation_due_date or template.next_due_date
            if template.is_obligation else template.next_due_date
        )
        delivery_attempt = None
        try:
            with session_scope() as session:
                repo = RecurringRepository(session)
                if not UserService(session, settings=runtime.settings).is_authorized(user_id):
                    last_fired_for.pop(user_id, None)
                    return
                # Refresh under the same template lock used by replies. A
                # payment after the initial due query may already have moved
                # this template to its next occurrence.
                current = repo.get_by_id_for_update(user_id, template.id)
                if current is None or not current.is_active or current.next_due_date > today:
                    continue
                template = current
                occurrence_date = (
                    template.obligation_due_date or template.next_due_date
                    if template.is_obligation else template.next_due_date
                )
                reminder = repo.get_reminder(
                    user_id, template.id, occurrence_date
                )
                if reminder is None:
                    reminder = repo.get_or_create_reminder(
                        user_id, template.id, occurrence_date
                    )
                if reminder.status != "pending":
                    continue
                if not repo.claim_reminder(reminder):
                    if reminder.delivery_status != "sent":
                        all_delivered = False
                    continue
                delivery_attempt = reminder.attempt_count
            preferences = _authorized_preferences(runtime, user_id)
            if preferences is None:
                _release_delivery_claim(user_id, template.id, occurrence_date, delivery_attempt)
                last_fired_for.pop(user_id, None)
                return
            if enforce_window:
                now = _now_in_tz(preferences.timezone)
                if not _in_notify_window(now) or now.date() != today:
                    _release_delivery_claim(user_id, template.id, occurrence_date, delivery_attempt)
                    last_fired_for.pop(user_id, None)
                    return
            text = _format_reminder(template, language=preferences.language)
            with metrics.measure("reminder"):
                message = await bot_app.bot.send_message(chat_id=user_id, text=text)
            with session_scope() as session:
                repo = RecurringRepository(session)
                repo.get_by_id_for_update(user_id, template.id)
                reminder = repo.get_reminder(
                    user_id, template.id, occurrence_date
                )
                # Snooze/payment may have changed this occurrence while
                # send_message was in flight. Never overwrite that newer
                # response or suppress the explicitly requested re-delivery.
                if (
                    reminder is not None
                    and reminder.status == "pending"
                    and reminder.delivery_status == "sending"
                    and reminder.attempt_count == delivery_attempt
                ):
                    repo.mark_reminder_sent(reminder, message.message_id)
        except Exception:
            all_delivered = False
            logger.exception(
                "Failed to send reminder for user %s, template %s",
                user_id,
                template.id,
            )
            _release_delivery_claim(user_id, template.id, occurrence_date, delivery_attempt)

    if all_delivered:
        last_fired_for[user_id] = day_key
    else:
        # A delivery failure or unexpired claim remains retryable during
        # this morning's window; the persistent lease prevents duplicates.
        last_fired_for.pop(user_id, None)


def _format_reminder(template, language: str = "es") -> str:
    language = str(language).lower().replace("_", "-").split("-", 1)[0]
    if language not in {"es", "en", "pt"}:
        language = "es"
    month_names = {
        "es": ("", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"),
        "en": ("", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"),
        "pt": ("", "janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"),
    }[language]
    if template.frequency == "yearly" and template.month_of_year is not None:
        month = month_names[template.month_of_year]
        when = {"es": f"el {template.day_of_month} de {month}",
                "en": f"{month} {template.day_of_month}",
                "pt": f"dia {template.day_of_month} de {month}"}[language]
    else:
        when = {"es": f"el día {template.day_of_month} de cada mes",
                "en": f"day {template.day_of_month} of each month",
                "pt": f"dia {template.day_of_month} de cada mês"}[language]
    title, instructions = {
        "es": ("Recordatorio", "¿Lo registro? Respondé a este mensaje:\n• *sí* — registrar gasto hoy\n• *no* — saltar esta ocurrencia\n• *mañana* — avisame mañana\n• *en 3 días* — avisame en tres días\n• *el viernes que viene* — avisame el próximo viernes\n• *eliminar* — borrar este recurrente"),
        "en": ("Reminder", "Record it? Reply to this message:\n• *yes* — record today's expense\n• *no* — skip this occurrence\n• *tomorrow* — remind me tomorrow\n• *in 3 days* — remind me in three days\n• *next friday* — remind me next Friday\n• *eliminar* — delete this recurring expense"),
        "pt": ("Lembrete", "Registrar? Responda a esta mensagem:\n• *sim* — registrar a despesa de hoje\n• *no* — pular esta ocorrência\n• *amanhã* — lembrar amanhã\n• *em 3 dias* — lembrar em três dias\n• *next friday* — lembrar na próxima sexta-feira\n• *eliminar* — excluir esta despesa recorrente"),
    }[language]
    return f"💡 *{title}*: {template.name} — {format_currency_amount(template.amount, template.currency)} ({when})\n\n{instructions}"
