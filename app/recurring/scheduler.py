"""Daily scheduler that fires recurring-expense reminders.

Runs as an asyncio task started from the FastAPI lifespan. On each tick it
checks whether it's 09:00 in the configured timezone, then walks the
``recurring_expenses`` table and sends a reminder per due row.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import distinct, select

from app.database.database import session_scope
from app.recurring.models import RecurringExpense
from app.recurring.repository import RecurringRepository
from app.utils.dates import today_in_tz
from app.utils.formatting import format_currency_amount, format_date_short

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
    tz_name = runtime.settings.timezone
    last_fired_for: dict[int, str] = {}

    while True:
        try:
            now_in_tz = _now_in_tz(tz_name)
            day_key = now_in_tz.date().isoformat()
            should_fire = (
                now_in_tz.time() >= time(NOTIFY_HOUR, 0)
                and now_in_tz.time()
                <= time(NOTIFY_HOUR, NOTIFY_MINUTE_WINDOW)
            )

            if should_fire:
                await _fire_due_reminders(
                    bot_app, runtime, now_in_tz.date(), day_key, last_fired_for
                )
            else:
                last_fired_for.clear()
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
    today,
    day_key: str,
    last_fired_for: dict[int, str],
) -> None:
    with session_scope() as session:
        repo = RecurringRepository(session)
        user_ids = [
            row[0]
            for row in session.execute(
                select(distinct(RecurringExpense.telegram_user_id))
            ).all()
        ]

    for user_id in user_ids:
        if last_fired_for.get(user_id) == day_key:
            continue
        await _try_send_for_user(bot_app, runtime, user_id, today, day_key, last_fired_for)


async def _try_send_for_user(
    bot_app: "Application",
    runtime: "BotRuntime",
    user_id: int,
    today,
    day_key: str,
    last_fired_for: dict[int, str],
) -> None:
    with session_scope() as session:
        repo = RecurringRepository(session)
        due = repo.due_on(user_id, today)

    if not due:
        return

    for template in due:
        try:
            text = _format_reminder(template)
            await bot_app.bot.send_message(chat_id=user_id, text=text)
            runtime.pending_reminders[user_id] = {
                "recurring_id": template.id,
                "name": template.name,
                "amount": template.amount,
                "currency": template.currency,
            }
        except Exception:
            logger.exception(
                "Failed to send reminder for user %s, template %s",
                user_id,
                template.id,
            )

    last_fired_for[user_id] = day_key


def _format_reminder(template) -> str:
    if template.frequency == "yearly" and template.month_of_year is not None:
        month_names = [
            "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
        ]
        when = (
            f"el {template.day_of_month} de "
            f"{month_names[template.month_of_year]}"
        )
    else:
        when = f"el día {template.day_of_month} de cada mes"
    return (
        f"💡 *Recordatorio*: {template.name} — "
        f"{format_currency_amount(template.amount, template.currency)} "
        f"({when})\n\n"
        f"¿Lo registro?\n"
        f"• *sí* — registrar gasto hoy\n"
        f"• *no* — saltar este mes\n"
        f"• *posponer* — avisame mañana\n"
        f"• *eliminar* — borrar este recurrente"
    )