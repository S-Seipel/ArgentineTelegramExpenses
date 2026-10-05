from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.bot.app import BotDependencies, _SilentStop
from app.bot.service import (
    _format_query,
    _help_text,
    _process_message_async,
)
from app.bot.specs import command_to_spec
from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.queries.service import QueryService
from app.recurring.repository import RecurringRepository
from app.recurring.service import (
    RecurringDraft,
    RecurringService,
    RecurringValidationError,
)
from app.recurring.schemas import RecurringOut
from app.budgets.repository import BudgetRepository
from app.budgets.service import BudgetDraft, BudgetService, BudgetValidationError
from app.fixed_expenses.repository import FixedExpenseRepository
from app.fixed_expenses.service import (
    FixedExpenseDraft,
    FixedExpenseHasHistoricalCarryOverError,
    FixedExpenseService,
    FixedExpenseValidationError,
    current_month_year,
)
from app.money import MoneyEngine, UserSettingsService
from app.utils.amounts import _normalize_decimal, find_thousand_amounts
from app.utils.dates import today_in_tz
from app.utils.formatting import format_currency_amount, format_date_short
from app.ai.schemas import AIError, AIUnavailable
from app.insights import format_spending_analysis, spending_context
from app.web.queries import resolve_period

logger = logging.getLogger(__name__)


async def handle_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: BotDependencies,
) -> None:
    try:
        await _ensure_authorized(update, deps)
    except _SilentStop:
        return

    if update.effective_message is None or update.effective_user is None:
        return

    raw = (update.effective_message.text or "").strip()
    parts = raw.split()
    command = parts[0].lower()
    args = parts[1:]
    user_id = update.effective_user.id

    if command in ("/analisis", "/analisis_mes", "/consejos"):
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        today = today_in_tz(deps.runtime.settings.timezone)
        with session_scope() as session:
            context_data = spending_context(session, user_id, today)
        if context_data is None:
            await update.effective_message.reply_text(
                "Todavía no tengo gastos del mes para analizar."
            )
            return
        try:
            analysis = await deps.runtime.ai.analyze_spending(context_data)
        except AIUnavailable:
            logger.exception("AI provider unreachable during spending analysis")
            await update.effective_message.reply_text(
                "🤖 No pude consultar la IA para analizar tus gastos. "
                "Probá de nuevo en un momento."
            )
            return
        except AIError:
            logger.exception("AI spending analysis failed")
            await update.effective_message.reply_text(
                "🤖 No pude generar un análisis válido. Probá de nuevo."
            )
            return
        await update.effective_message.reply_text(
            format_spending_analysis(
                analysis, resolve_period("month", today).label
            )
        )
        return

    spec = command_to_spec(command)
    if spec is not None:
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        with session_scope() as session:
            qservice = QueryService(ExpenseRepository(session))
            result = qservice.run(user_id=user_id, spec=spec)
        await update.effective_message.reply_text(_format_query(result))
        return

    if command in ("/borrar", "/borrar_ultimo"):
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        with session_scope() as session:
            repo = ExpenseRepository(session)
            reply = await _handle_delete_command(
                update, repo, user_id, command, args
            )
        await update.effective_message.reply_text(reply, parse_mode=None)
        return

    if command in ("/editar", "/editar_ultimo"):
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        with session_scope() as session:
            repo = ExpenseRepository(session)
            reply = await _handle_edit_command(
                update, repo, user_id, command, args
            )
        await update.effective_message.reply_text(reply)
        return

    if command in ("/exportar", "/csv"):
        await _handle_export_command(update, user_id)
        return

    if command in (
        "/recurrente_add",
        "/recurrente_add_anual",
        "/recurrentes",
        "/recurrente_del",
        "/recurrente_off",
        "/recurrente_on",
    ):
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        with session_scope() as session:
            repo = RecurringRepository(session)
            service = RecurringService(repo)
            reply = _handle_recurring_command(
                command, args, service, user_id, today_in_tz(deps.runtime.settings.timezone)
            )
        await update.effective_message.reply_text(reply)
        return

    if command in (
        "/presupuesto",
        "/presupuestos",
        "/presupuesto_del",
        "/presupuesto_off",
        "/presupuesto_on",
    ):
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        with session_scope() as session:
            repo = BudgetRepository(session)
            service = BudgetService(repo)
            reply = _handle_budget_command(
                command, args, service, user_id
            )
        await update.effective_message.reply_text(reply)
        return

    if command == "/buscar":
        if not args:
            await update.effective_message.reply_text(
                "❓ Decime qué buscar. Ej: `/buscar starbucks`"
            )
            return
        query = " ".join(args).strip()
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        with session_scope() as session:
            repo = ExpenseRepository(session)
            results = repo.search_by_name(user_id, query, limit=20)
        await update.effective_message.reply_text(
            _format_search_results(query, results)
        )
        return

    if command in (
        "/gastosfijos", "/gastofijo_add", "/gastofijo_del",
        "/gastofijo_off", "/gastofijo_on",
        "/pague", "/salte", "/liberado",
    ):
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        with session_scope() as session:
            repo = FixedExpenseRepository(session)
            service = FixedExpenseService(repo)
            reply = _handle_fixed_expense_command(
                command, args, service, user_id,
                today=today_in_tz(deps.runtime.settings.timezone),
            )
        await update.effective_message.reply_text(reply)
        return

    if command == "/config_payday":
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        reply = _handle_config_payday_command(
            args,
            user_id=user_id,
            timezone=deps.runtime.settings.timezone,
        )
        await update.effective_message.reply_text(reply)
        return

    if command in ("/cycle", "/disponible", "/gastar_diario"):
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        reply = _handle_cycle_read_command(
            command,
            user_id=user_id,
            today=today_in_tz(deps.runtime.settings.timezone),
        )
        await update.effective_message.reply_text(reply)
        return

    if command == "/ingreso":
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        reply = _handle_ingreso_command(
            args,
            user_id=user_id,
            today=today_in_tz(deps.runtime.settings.timezone),
            text=raw,
        )
        await update.effective_message.reply_text(reply)
        return

    if command == "/extra":
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        reply = _handle_extra_command(
            args,
            user_id=user_id,
            chat_id=update.effective_message.chat_id,
            message_id=update.effective_message.message_id,
            today=today_in_tz(deps.runtime.settings.timezone),
            text=raw,
        )
        await update.effective_message.reply_text(reply)
        return

    if command == "/extra_editar":
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        reply = _handle_extra_editar_command(
            args,
            user_id=user_id,
            today=today_in_tz(deps.runtime.settings.timezone),
        )
        await update.effective_message.reply_text(reply)
        return

    if command == "/extra_borrar":
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        reply = _handle_extra_borrar_command(
            args,
            user_id=user_id,
            today=today_in_tz(deps.runtime.settings.timezone),
        )
        await update.effective_message.reply_text(reply)
        return

    if command == "/target":
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        reply = _handle_target_command(
            args,
            user_id=user_id,
            today=today_in_tz(deps.runtime.settings.timezone),
            text=raw,
        )
        await update.effective_message.reply_text(reply)
        return

    if command == "/reservar":
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        reply = _handle_reservar_command(
            args,
            user_id=user_id,
            today=today_in_tz(deps.runtime.settings.timezone),
            text=raw,
        )
        await update.effective_message.reply_text(reply)
        return

    if command == "/liberar":
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        reply = _handle_liberar_command(
            args,
            user_id=user_id,
            today=today_in_tz(deps.runtime.settings.timezone),
            text=raw,
        )
        await update.effective_message.reply_text(reply)
        return

    if command == "/ahorro_realizado":
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        reply = _handle_ahorro_realizado_command(
            args,
            user_id=user_id,
            chat_id=update.effective_message.chat_id,
            message_id=update.effective_message.message_id,
            today=today_in_tz(deps.runtime.settings.timezone),
            text=raw,
        )
        await update.effective_message.reply_text(reply)
        return

    if command == "/ahorro_editar":
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        reply = _handle_ahorro_editar_command(
            args,
            user_id=user_id,
            today=today_in_tz(deps.runtime.settings.timezone),
        )
        await update.effective_message.reply_text(reply)
        return

    if command == "/ahorro_borrar":
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        reply = _handle_ahorro_borrar_command(
            args,
            user_id=user_id,
            today=today_in_tz(deps.runtime.settings.timezone),
        )
        await update.effective_message.reply_text(reply)
        return

    if command in ("/start", "/help", "/ayuda"):
        await update.effective_message.reply_text(_help_text())
        return

    await update.effective_message.reply_text("Comando no reconocido.")


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: BotDependencies,
) -> None:
    try:
        await _ensure_authorized(update, deps)
    except _SilentStop:
        return

    if update.effective_message is None or update.effective_user is None:
        return

    text = update.effective_message.text or ""
    user_id = update.effective_user.id
    today = today_in_tz(deps.runtime.settings.timezone)

    reply_to = getattr(update.effective_message, "reply_to_message", None)
    reply_to_message_id = getattr(reply_to, "message_id", None)
    if not isinstance(reply_to_message_id, int):
        reply_to_message_id = None
    handled = await _maybe_handle_reminder_reply(
        deps, user_id, text, today, reply_to_message_id=reply_to_message_id
    )
    if handled is not None:
        await update.effective_message.reply_text(handled)
        return

    await update.effective_message.chat.send_action(ChatAction.TYPING)
    try:
        reply = await _process_message_async(
            deps.runtime, text, user_id=user_id, today=today
        )
    except Exception:
        logger.exception("Unhandled error processing message")
        reply = "⚠️ Ocurrió un error procesando tu mensaje. Probá de nuevo."

    await update.effective_message.reply_text(reply)


async def handle_voice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: BotDependencies,
) -> None:
    try:
        await _ensure_authorized(update, deps)
    except _SilentStop:
        return

    if update.effective_message is None or update.effective_user is None:
        return

    voice = update.effective_message.voice
    audio = update.effective_message.audio
    file_id = None
    mime_hint = "voice"
    if voice is not None:
        file_id = voice.file_id
        mime_hint = voice.mime_type or "audio/ogg"
    elif audio is not None:
        file_id = audio.file_id
        mime_hint = audio.mime_type or "audio/mpeg"
    if file_id is None:
        return

    user_id = update.effective_user.id
    today = today_in_tz(deps.runtime.settings.timezone)
    await update.effective_message.chat.send_action(ChatAction.TYPING)

    try:
        tg_file = await context.bot.get_file(file_id)
        audio_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception:
        logger.exception("Failed to download voice file")
        await update.effective_message.reply_text(
            "⚠️ No pude descargar el audio."
        )
        return

    try:
        transcript = await deps.runtime.ai.transcribe(audio_bytes)
    except AIUnavailable:
        logger.exception("Whisper unreachable")
        await update.effective_message.reply_text(
            "🤖 No pude transcribir el audio. Verificá que ffmpeg esté"
            " disponible y que el modelo de Whisper se haya descargado"
            f" ({deps.runtime.settings.whisper_model_size})."
        )
        return
    except AIError as exc:
        await update.effective_message.reply_text(f"🤖 {exc}")
        return
    except Exception:
        logger.exception("Unexpected error transcribing audio")
        await update.effective_message.reply_text(
            "⚠️ Error inesperado al transcribir."
        )
        return

    transcript = (transcript or "").strip()
    if not transcript:
        await update.effective_message.reply_text(
            "🤔 No pude entender el audio. ¿Podés escribirlo?"
        )
        return

    try:
        reply = await _process_message_async(
            deps.runtime, transcript, user_id=user_id, today=today
        )
    except Exception:
        logger.exception("Error processing transcript")
        reply = "⚠️ Ocurrió un error procesando la transcripción."

    formatted = (
        f"🎤 _{transcript}_\n\n"
        f"{reply}"
    )
    await update.effective_message.reply_text(formatted, parse_mode=None)


async def _maybe_handle_reminder_reply(
    deps: BotDependencies,
    user_id: int,
    text: str,
    today,
    *,
    reply_to_message_id: int | None = None,
) -> str | None:
    """Process sí/no/posponer/eliminar in response to a pending reminder.

    Returns the reply string if the message was a control word, else None
    so the regular flow can take over.
    """
    normalized = text.strip().lower()
    control_words = {
        "sí", "si", "dale", "ok", "listo", "confirmo",
        "no", "nope", "skip", "saltar",
        "posponer", "mañana", "manana",
        "eliminar", "borrar", "nunca más", "nunca mas", "quitar",
    }
    if normalized not in control_words:
        return None

    with session_scope() as session:
        repo = RecurringRepository(session)
        service = RecurringService(repo)
        pending_rows = repo.pending_reminders(
            user_id, message_id=reply_to_message_id
        )
        reminder = None
        if reply_to_message_id is not None:
            if not pending_rows:
                return "🤷 Ese recordatorio ya fue respondido o no existe."
            reminder = pending_rows[0]
        elif len(pending_rows) == 1:
            reminder = pending_rows[0]
        elif len(pending_rows) > 1:
            return (
                "🤔 Tenés varios recordatorios pendientes. Respondé usando "
                "Responder sobre el mensaje correspondiente."
            )
        else:
            # Compatibility for reminders created by older runtimes; new
            # scheduler deliveries are always persisted in recurring_reminders.
            legacy = deps.runtime.pending_reminders.get(user_id)
            if not isinstance(legacy, dict):
                return None
            recurring_id = legacy["recurring_id"]
            pending = legacy
        if reminder is not None:
            recurring_id = reminder.recurring_id
            pending = reminder

        template_obj = repo.get_by_id_for_update(user_id, recurring_id)
        if template_obj is None or not template_obj.is_active:
            return "🤷 Ese recurrente ya no existe."

        name = template_obj.name
        amount = template_obj.amount
        currency = template_obj.currency

        if normalized in ("sí", "si", "dale", "ok", "listo", "confirmo"):
            template_after, saved = service.register_occurrence(
                user_id=user_id,
                recurring_id=recurring_id,
                today=today,
                occurrence_date=(
                    pending.occurrence_date
                    if reminder is not None
                    else None
                ),
                commit=False,
            )
            if saved is None:
                return "🤷 No pude registrar el gasto."
            from app.utils.formatting import format_currency_amount as fca

            if reminder is not None:
                repo.resolve_reminder(reminder)
            else:
                deps.runtime.pending_reminders.pop(user_id, None)
            return (
                f"✅ Listo: *{name}* — {fca(amount, currency)} "
                f"registrado para hoy. "
                f"Próximo aviso: {format_date_short(template_after.next_due_date)}"
            )

        if normalized in ("posponer", "mañana", "manana"):
            service.postpone_occurrence(
                user_id=user_id, recurring_id=recurring_id, commit=False
            )
            if reminder is not None:
                repo.resolve_reminder(reminder)
            else:
                deps.runtime.pending_reminders.pop(user_id, None)
            template_after = repo.get_by_id_for_update(user_id, recurring_id)
            return (
                f"⏰ Dale, te aviso mañana. "
                f"Próximo intento: {format_date_short(template_after.next_due_date)}"
            )

        if normalized in ("eliminar", "borrar", "nunca más", "nunca mas", "quitar"):
            service.set_active(
                user_id, recurring_id, is_active=False, commit=False
            )
            if reminder is not None:
                repo.resolve_reminder(reminder)
            else:
                deps.runtime.pending_reminders.pop(user_id, None)
            return f"🗑️ Recurrente *{name}* eliminado."

        if normalized in ("no", "nope", "skip", "saltar"):
            template_after = service.skip_occurrence(
                user_id=user_id,
                recurring_id=recurring_id,
                today=today,
                commit=False,
            )
            if template_after is None:
                return "🤷 No pude saltar este recordatorio."
            if reminder is not None:
                repo.resolve_reminder(reminder)
            else:
                deps.runtime.pending_reminders.pop(user_id, None)
            return (
                f"👍 Saltado. "
                f"Próximo aviso: {format_date_short(template_after.next_due_date)}"
            )

    return None


async def _ensure_authorized(
    update: Update, deps: BotDependencies
) -> None:
    if update.effective_user is None:
        return
    allowed = deps.runtime.settings.telegram_allowed_user_id
    if update.effective_user.id != allowed:
        logger.warning("Rejected unauthorized user %s", update.effective_user.id)
        if update.effective_message is not None:
            await update.effective_message.reply_text(
                "⛔ Este bot es personal. Acceso denegado."
            )
        raise _SilentStop()


def _format_expense_brief(exp) -> str:
    return (
        f"*{exp.name}* — {format_currency_amount(exp.amount, exp.currency)} "
        f"({format_date_short(exp.expense_date)})"
    )


def _format_search_results(query: str, results) -> str:
    if not results:
        return f"🔍 No encontré gastos con *{query}*."
    lines = [f"🔍 Resultados para *{query}* ({len(results)}):", ""]
    for r in results:
        lines.append(
            f"`#{r.id}` {r.name} — "
            f"{format_currency_amount(r.amount, r.currency)}\n"
            f"      📂 {r.category_name} · 📅 {format_date_short(r.expense_date)}"
        )
    lines.append("")
    lines.append("Usá `/borrar <id>` o `/editar <id> <monto>` para modificar.")
    return "\n".join(lines)


async def _handle_delete_command(
    update: Update,
    repo: ExpenseRepository,
    user_id: int,
    command: str,
    args: list[str],
) -> str:
    target_id: int | None = None
    if command == "/borrar_ultimo":
        latest = repo.get_latest(user_id)
        if latest is None:
            return "🤷 No tenés gastos para borrar."
        target_id = latest.id
    else:
        if not args:
            return (
                "❓ Decime el ID del gasto. Ej: `/borrar 42` "
                "(usá `/gastos` para ver los IDs)."
            )
        try:
            target_id = int(args[0])
        except ValueError:
            return "❓ El ID tiene que ser un número entero. Ej: `/borrar 42`."

    target = repo.get_by_id(user_id, target_id)
    if target is None:
        return (
            f"🤷 No encontré el gasto #{target_id}. "
            "Usá `/gastos` para ver los IDs válidos."
        )
    deleted = repo.delete(user_id, target_id)
    if deleted is None:
        return f"🤷 No pude borrar el gasto #{target_id}."
    return (
        f"🗑️ Gasto borrado:\n"
        f"{_format_expense_brief(deleted)}"
    )


async def _handle_edit_command(
    update: Update,
    repo: ExpenseRepository,
    user_id: int,
    command: str,
    args: list[str],
) -> str:
    if command == "/editar_ultimo":
        if not args:
            return (
                "❓ Decime el nuevo monto. Ej: `/editar_ultimo 15000` "
                "o `/editar_ultimo nombre: Café`."
            )
        latest = repo.get_latest(user_id)
        if latest is None:
            return "🤷 No tenés gastos para editar."
        return await _apply_edit(repo, user_id, latest.id, args)

    if not args:
        return (
            "❓ Uso: `/editar <id> <monto>` o "
            "`/editar <id> nombre: <texto>`."
        )
    try:
        target_id = int(args[0])
    except ValueError:
        return "❓ El ID tiene que ser un número entero. Ej: `/editar 42 15000`."
    return await _apply_edit(repo, user_id, target_id, args[1:])


async def _apply_edit(
    repo: ExpenseRepository,
    user_id: int,
    expense_id: int,
    args: list[str],
) -> str:
    if not args:
        return "❓ Falta el nuevo valor. Ej: `/editar 42 15000` o `/editar 42 nombre: Café`."

    target = repo.get_by_id(user_id, expense_id)
    if target is None:
        return (
            f"🤷 No encontré el gasto #{expense_id}. "
            "Usá `/gastos` para ver los IDs válidos."
        )

    rest = " ".join(args).strip()
    if rest.lower().startswith("nombre:"):
        new_name = rest.split(":", 1)[1].strip()
        if not new_name:
            return "❓ El nombre no puede estar vacío."
        try:
            updated = repo.update_name(user_id, expense_id, new_name)
        except ValueError as exc:
            return f"⚠️ {exc}"
        if updated is None:
            return f"🤷 No pude editar el gasto #{expense_id}."
        return (
            f"✏️ Gasto #{expense_id} actualizado:\n"
            f"{_format_expense_brief(updated)}"
        )

    new_amount = _parse_user_amount(rest)
    if new_amount is None:
        return (
            f"❓ No pude interpretar el monto `{rest}`. "
            "Ej: `15000`, `15k`, `15 lucas`, `1.500,50`."
        )
    if new_amount <= 0:
        return "❓ El monto tiene que ser mayor a cero."
    try:
        updated = repo.update_amount(user_id, expense_id, new_amount)
    except ValueError as exc:
        return f"⚠️ {exc}"
    if updated is None:
        return f"🤷 No pude editar el gasto #{expense_id}."
    return (
        f"✏️ Gasto #{expense_id} actualizado:\n"
        f"{_format_expense_brief(updated)}"
    )


def _parse_user_amount(text: str) -> Decimal | None:
    """Parse a user-typed amount (e.g. `15000`, `15k`, `15 lucas`, `1.500,50`)."""
    cleaned = text.strip()
    if not cleaned:
        return None
    direct = _normalize_decimal(cleaned)
    if direct is not None and direct > 0:
        return direct
    parsed = find_thousand_amounts(cleaned)
    if parsed:
        return parsed[0].value
    return None


async def _handle_export_command(
    update: Update, user_id: int
) -> None:
    """Build a CSV in-memory and send it as a Telegram document."""
    import csv
    import io
    from datetime import date as _date, timedelta

    from telegram import InputFile

    if update.effective_message is None:
        return

    today = today_in_tz(
        update.application.bot_data["settings"].timezone
    )
    start: _date | None = None
    period_label = "mes en curso"
    raw_text = (update.effective_message.text or "").split()
    if len(raw_text) > 1:
        arg = raw_text[1].lower()
        if arg in ("hoy", "today"):
            start = today
            period_label = "hoy"
        elif arg in ("semana", "week"):
            start = today - timedelta(days=today.weekday())
            period_label = "semana"
        elif arg in ("mes", "month", ""):
            start = today.replace(day=1)
            period_label = "mes"
        elif arg in ("todo", "all", "historico", "histórico"):
            start = None
            period_label = "histórico"

    with session_scope() as session:
        repo = ExpenseRepository(session)
        rows = repo.list_in_range(user_id, start=start, end=today)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["id", "fecha", "nombre", "monto", "moneda", "categoria", "mensaje_original"]
    )
    for r in rows:
        writer.writerow(
            [
                r.id,
                r.expense_date.isoformat(),
                r.name,
                str(r.amount),
                r.currency,
                r.category_name,
                (r.original_message or "")[:200],
            ]
        )

    filename = f"gastos_{today.isoformat()}.csv"
    caption = (
        f"📎 Exportación CSV ({period_label}): {len(rows)} gastos."
    )
    await update.effective_message.reply_document(
        document=InputFile(
            buf.getvalue().encode("utf-8"), filename=filename
        ),
        caption=caption,
    )


def _handle_recurring_command(
    command: str,
    args: list[str],
    service: RecurringService,
    user_id: int,
    today,
) -> str:
    if command == "/recurrentes":
        return _recurrentes_list(user_id, service)

    if command == "/recurrente_add":
        if len(args) < 3:
            return (
                "❓ Uso: `/recurrente_add <nombre> <monto> <día>`\n"
                "Ej: `/recurrente_add Netflix 30000 15`"
            )
        name = args[0]
        amount = _parse_user_amount(args[1])
        if amount is None:
            return f"❓ No pude interpretar el monto `{args[1]}`."
        try:
            day = int(args[2])
        except ValueError:
            return "❓ El día tiene que ser un número (1-31)."
        draft = RecurringDraft(
            name=name,
            amount=amount,
            currency=None,
            category=None,
            frequency="monthly",
            day_of_month=day,
            month_of_year=None,
            confidence=1.0,
        )
        try:
            template = service.create_from_draft(
                user_id=user_id, draft=draft, today=today
            )
        except RecurringValidationError as exc:
            return f"🤔 {exc}"
        return _recurring_confirmation_message(template)

    if command == "/recurrente_add_anual":
        if len(args) < 4:
            return (
                "❓ Uso: `/recurrente_add_anual <nombre> <monto> <mes> <día>`\n"
                "Ej: `/recurrente_add_anual Dominio 15000 3 15`"
            )
        name = args[0]
        amount = _parse_user_amount(args[1])
        if amount is None:
            return f"❓ No pude interpretar el monto `{args[1]}`."
        try:
            month = int(args[2])
            day = int(args[3])
        except ValueError:
            return "❓ Mes y día tienen que ser números."
        draft = RecurringDraft(
            name=name,
            amount=amount,
            currency=None,
            category=None,
            frequency="yearly",
            day_of_month=day,
            month_of_year=month,
            confidence=1.0,
        )
        try:
            template = service.create_from_draft(
                user_id=user_id, draft=draft, today=today
            )
        except RecurringValidationError as exc:
            return f"🤔 {exc}"
        return _recurring_confirmation_message(template)

    if command in ("/recurrente_del", "/recurrente_off", "/recurrente_on"):
        if not args:
            return (
                "❓ Decime el ID. Ej: `/recurrente_del 3`. "
                "Usá `/recurrentes` para ver los IDs."
            )
        try:
            rid = int(args[0])
        except ValueError:
            return "❓ El ID tiene que ser un número entero."
        if command == "/recurrente_del":
            obj = service.set_active(user_id, rid, is_active=False)
        elif command == "/recurrente_off":
            obj = service.set_active(user_id, rid, is_active=False)
        else:
            obj = service.set_active(user_id, rid, is_active=True)
        if obj is None:
            return f"🤷 No encontré el recurrente #{rid}."
        action = {
            "/recurrente_del": "eliminado",
            "/recurrente_off": "pausado",
            "/recurrente_on": "reanudado",
        }[command]
        return f"🔁 Recurrente #{rid} {action}: *{obj.name}*."

    return "Comando no reconocido."


def _recurring_confirmation_message(template) -> str:
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
        f"🔁 Recurrente registrado\n\n"
        f"*{template.name}* — {format_currency_amount(template.amount, template.currency)}\n"
        f"📅 Te aviso {when}.\n"
        f"Próximo vencimiento: {format_date_short(template.next_due_date)}\n\n"
        f"Lo gestionás con `/recurrentes` o lo borrás con `/recurrente_del {template.id}`."
    )


def _recurrentes_list(user_id: int, service: RecurringService) -> str:
    items = service.list(user_id)
    if not items:
        return (
            "🤷 No tenés gastos recurrentes. Creá uno con "
            "`/recurrente_add Netflix 30000 15`."
        )
    lines = ["🔁 *Tus gastos recurrentes*", ""]
    for r in items:
        status = "" if r.is_active else " _(pausado)_"
        if r.frequency == "yearly" and r.month_of_year is not None:
            month_names = [
                "", "ene", "feb", "mar", "abr", "may", "jun",
                "jul", "ago", "sep", "oct", "nov", "dic",
            ]
            when = (
                f"el {r.day_of_month} de "
                f"{month_names[r.month_of_year]}"
            )
        else:
            when = f"día {r.day_of_month}"
        lines.append(
            f"`#{r.id}` {r.name} — "
            f"{format_currency_amount(r.amount, r.currency)} — "
            f"{when} — próx: {format_date_short(r.next_due_date)}{status}"
        )
    lines.append("")
    lines.append("`/recurrente_del <id>` elimina · `/recurrente_off <id>` pausa · `/recurrente_on <id>` reactiva")
    return "\n".join(lines)


def _handle_budget_command(
    command: str,
    args: list[str],
    service: BudgetService,
    user_id: int,
) -> str:
    if command == "/presupuestos":
        items = service.list(user_id)
        if not items:
            return (
                "🤷 No tenés presupuestos. Creá uno con "
                "`/presupuesto comida 50000`."
            )
        lines = ["💰 *Tus presupuestos mensuales*", ""]
        for b in items:
            status = "" if b.is_active else " _(pausado)_"
            lines.append(
                f"`#{b.id}` *{b.category_name}* — "
                f"{format_currency_amount(b.monthly_limit, b.currency)}{status}"
            )
        lines.append("")
        lines.append(
            "`/presupuesto_del <id>` elimina · "
            "`/presupuesto_off <id>` pausa · "
            "`/presupuesto_on <id>` reactiva"
        )
        return "\n".join(lines)

    if command == "/presupuesto":
        if len(args) < 2:
            return (
                "❓ Uso: `/presupuesto <categoría> <monto> [moneda]`\n"
                "Ej: `/presupuesto comida 50000` o "
                "`/presupuesto salidas 50 USD`"
            )
        category = args[0]
        amount = _parse_user_amount(args[1])
        if amount is None:
            return f"❓ No pude interpretar el monto `{args[1]}`."
        currency = args[2] if len(args) >= 3 else None
        draft = BudgetDraft(
            category=category, monthly_limit=amount, currency=currency
        )
        try:
            obj, created = service.upsert_from_draft(user_id, draft)
        except BudgetValidationError as exc:
            return f"🤔 {exc}"
        action = "creado" if created else "actualizado"
        return (
            f"💰 Presupuesto {action}:\n"
            f"*{obj.category.name if obj.category else category}* — "
            f"{format_currency_amount(obj.monthly_limit, obj.currency)} "
            f"por mes."
        )

    if command in ("/presupuesto_del", "/presupuesto_off", "/presupuesto_on"):
        if not args:
            return (
                "❓ Decime el ID. Ej: `/presupuesto_del 3`. "
                "Usá `/presupuestos` para ver los IDs."
            )
        try:
            budget_id = int(args[0])
        except ValueError:
            return "❓ El ID tiene que ser un número entero."
        if command == "/presupuesto_del":
            ok = service.delete(user_id, budget_id)
        elif command == "/presupuesto_off":
            ok = service.deactivate(user_id, budget_id) is not None
        else:
            ok = service.reactivate(user_id, budget_id) is not None
        if not ok:
            return f"🤷 No encontré el presupuesto #{budget_id}."
        action = {
            "/presupuesto_del": "eliminado",
            "/presupuesto_off": "pausado",
            "/presupuesto_on": "reanudado",
        }[command]
        return f"💰 Presupuesto #{budget_id} {action}."

    return "Comando no reconocido."


def _handle_fixed_expense_command(
    command: str,
    args: list[str],
    service: FixedExpenseService,
    user_id: int,
    today,
) -> str:
    month_year = current_month_year(today)

    if command == "/gastosfijos":
        return _format_fixed_expenses_month(service, user_id, month_year)

    if command == "/gastofijo_add":
        if len(args) < 2:
            return (
                "❓ Uso: `/gastofijo_add <nombre> <monto> [día] [método]`\n"
                "Ej: `/gastofijo_add CASA 90000 10 transferencia`"
            )
        name = args[0]
        amount = _parse_user_amount(args[1])
        if amount is None:
            return f"❓ No pude interpretar el monto `{args[1]}`."
        due_day = 1
        method = None
        for extra in args[2:]:
            try:
                due_day = int(extra)
                continue
            except ValueError:
                method = extra.upper()
        if method and method not in {
            "TRANSFERENCIA", "EFECTIVO", "DEBITO", "TARJETA",
            "CREDITO", "MERCADO PAGO", "APP", "OTRO",
        }:
            method = "OTRO"
        draft = FixedExpenseDraft(
            name=name,
            expected_amount=amount,
            currency=None,
            payment_method=method,
            due_day_of_month=due_day,
        )
        try:
            obj = service.add(user_id, draft)
        except FixedExpenseValidationError as exc:
            return f"🤔 {exc}"
        return (
            f"💸 Gasto fijo agregado:\n"
            f"*{obj.name}* — ${obj.expected_amount} {obj.currency}\n"
            f"📅 Día {obj.due_day_of_month} · 💳 {obj.payment_method or '—'}\n\n"
            f"Lo gestionás con `/gastosfijos` o lo borrás con "
            f"`/gastofijo_del {obj.id}`."
        )

    if command in ("/gastofijo_del", "/gastofijo_off", "/gastofijo_on"):
        if not args:
            return (
                "❓ Decime el ID. Ej: `/gastofijo_del 3`. "
                "Usá `/gastosfijos` para ver los IDs."
            )
        try:
            fid = int(args[0])
        except ValueError:
            return "❓ El ID tiene que ser un número entero."
        if command == "/gastofijo_del":
            from app.money.engine import FixedExpenseHasOutstandingDebtError

            try:
                obj = service.delete(user_id, fid)
            except (
                FixedExpenseHasHistoricalCarryOverError,
                FixedExpenseHasOutstandingDebtError,
            ) as exc:
                return f"⚠️ {exc}"
            if obj is None:
                return f"🤷 No encontré el gasto fijo #{fid}."
            action = "eliminado"
        elif command == "/gastofijo_off":
            obj = service.set_active(user_id, fid, is_active=False)
            if obj is None:
                return f"🤷 No encontré el gasto fijo #{fid}."
            action = "pausado"
        else:
            obj = service.set_active(user_id, fid, is_active=True)
            if obj is None:
                return f"🤷 No encontré el gasto fijo #{fid}."
            action = "reanudado"
        return f"💸 Gasto fijo #{fid} {action}: *{obj.name}*."

    if command in ("/pague", "/salte"):
        if not args:
            name = None
        else:
            name = args[0]
            # Optionally: second arg can be the actual amount.
        amount_paid: object = None
        if command == "/pague" and len(args) >= 2:
            amount_paid = _parse_user_amount(args[1])
            if amount_paid is None:
                return f"❓ No pude interpretar el monto `{args[1]}`."
        if name is None:
            return (
                "❓ Decime cuál pagaste. Ej: `/pague CASA` o "
                "`/pague CASA 92000` si pagaste un poco más."
            )
        bill = service.find_by_name(user_id, name)
        if bill is None:
            matches = [
                b for b in service.list_active(user_id)
                if name.lower() in b.name.lower()
            ]
            if len(matches) == 1:
                bill = matches[0]
            elif matches:
                names = ", ".join(f"`{m.name}`" for m in matches[:5])
                return f"🤔 Encontré varios: {names}. Decime el exacto."
            else:
                return (
                    f"🤷 No encontré un gasto fijo llamado *{name}*. "
                    f"Usá `/gastosfijos` para verlos."
                )
        if command == "/pague":
            obj, _ = service.mark_paid(
                user_id, bill.id, month_year, actual_amount=amount_paid,
            )
            if obj is None:
                return f"🤷 Error pagando *{bill.name}*."
            paid_amt = amount_paid if amount_paid else bill.expected_amount
            if amount_paid and amount_paid != bill.expected_amount:
                diff = amount_paid - bill.expected_amount
                sign = "+" if diff > 0 else ""
                return (
                    f"✅ *{bill.name}* pagado: ${paid_amt} "
                    f"(esperado ${bill.expected_amount}, {sign}{diff})"
                )
            return f"✅ *{bill.name}* pagado: ${paid_amt}"
        else:
            obj, _ = service.mark_skipped(user_id, bill.id, month_year)
            if obj is None:
                return f"🤷 Error salteando *{bill.name}*."
            return f"⏭️ *{bill.name}* salteado este mes."

    if command == "/liberado":
        return _format_month_summary(service, user_id, month_year)

    return "Comando no reconocido."


def _format_fixed_expenses_month(
    service: FixedExpenseService, user_id: int, month_year: str
) -> str:
    bills = service.with_status_for_month(user_id, month_year)
    if not bills:
        return (
            "🤷 No tenés gastos fijos. Creá uno con "
            "`/gastofijo_add CASA 90000 10 transferencia`."
        )
    paid = [b for b in bills if b.paid]
    pending = [b for b in bills if not b.paid and not b.skipped]
    skipped = [b for b in bills if b.skipped]
    lines = [
        f"💸 *Gastos fijos — {month_year}*",
        f"📊 Progreso: {len(paid)}/{len(bills)} pagados",
        "",
    ]
    for b in bills:
        icon = (
            "✅" if b.paid
            else ("⏭️" if b.skipped else "⏳")
        )
        method = b.payment_method or "—"
        amount_str = format_currency_amount(b.expected_amount, b.currency)
        actual_str = ""
        if b.actual_amount is not None and b.actual_amount != b.expected_amount:
            diff = b.actual_amount - b.expected_amount
            sign = "+" if diff > 0 else ""
            actual_str = f" → ${b.actual_amount} ({sign}{diff})"
        day_str = f"día {b.due_day_of_month}"
        lines.append(
            f"{icon} `{b.id}` *{b.name}* — {amount_str}{actual_str} "
            f"({method}, {day_str})"
        )
    if skipped:
        lines.append(f"\n⏭️ {len(skipped)} salteado(s) este mes")
    if pending:
        names = ", ".join(f"`{b.name}`" for b in pending[:5])
        lines.append(
            f"\nPendientes: {names}. Marcá con `/pague <nombre>` "
            f"(/pague <nombre> <monto_real> si pagaste distinto)."
        )
    lines.append(
        f"\n💰 Ingreso + extra → `/ingreso 1100000`, `/extra 75000`"
    )
    lines.append(f"📊 Resumen → `/liberado`")
    return "\n".join(lines)


def _format_month_summary(
    service: FixedExpenseService, user_id: int, month_year: str
) -> str:
    summary = service.month_summary(user_id, month_year)
    bills = service.with_status_for_month(user_id, month_year)
    paid_count = sum(1 for b in bills if b.paid)
    total_count = len(bills)
    lines = [
        f"📊 *Resumen del mes {month_year}*",
        "",
        f"💵 Ingreso:    ${fmt_money(summary.income)}",
        f"➕ Extra:       ${fmt_money(summary.extra)}",
        f"📉 Gastos fijos: ${fmt_money(summary.total_fixed_paid)}",
        f"   _(esperado ${fmt_money(summary.total_fixed_expected)})_",
        "",
        f"💰 *Liberado:   ${fmt_money(summary.liberado)}*",
        "",
        f"Pagados: {paid_count}/{total_count}",
    ]
    return "\n".join(lines)


def fmt_money(n) -> str:
    from app.utils.formatting import format_amount
    return format_amount(n)


def _handle_config_payday_command(
    args: list[str],
    *,
    timezone: str,
    user_id: int = 0,
) -> str:
    """Handle ``/config_payday <day>`` (R1).

    Lazily creates the user_settings row, persisting ``payday_day`` (and
    the global timezone as a fallback) on the first call.
    """
    if not args:
        return (
            "❓ Decime el día de cobro. Ej: `/config_payday 5`.\n"
            "Valores válidos: 1-31."
        )
    try:
        day = int(args[0])
    except ValueError:
        return "❓ El día tiene que ser un número entero entre 1 y 31."
    try:
        with session_scope() as session:
            UserSettingsService(session).set_payday_day(
                user_id=user_id, payday_day=day, timezone=timezone
            )
    except ValueError:
        return "❓ El día tiene que estar entre 1 y 31."
    return f"📅 Día de cobro configurado: día *{day}* del mes."


def _handle_cycle_read_command(
    command: str,
    *,
    user_id: int,
    today,
) -> str:
    """R2 read commands: ``/cycle``, ``/disponible``, ``/gastar_diario``.

    ``CycleService.ensure_current_cycle`` walks the rollover chain if
    the open cycle is expired; ``MoneyEngine.get_cycle_summary`` is
    the read-only source of truth inside the same session so the
     numbers stay in lockstep with the API layer.
    """
    from app.money import CycleService

    with session_scope() as session:
        cycle_service = CycleService(session)
        engine = MoneyEngine(session)
        try:
            cycle = cycle_service.ensure_current_cycle(user_id, today)
        except Exception as exc:  # noqa: BLE001
            return (
                "⚠️ No pude resolver tu ciclo actual. "
                f"Detalle: {exc}"
            )
        summary = engine.get_cycle_summary(
            user_id, cycle.id, today=today
        )

    if command == "/cycle":
        return _format_cycle_summary(summary)
    if command == "/disponible":
        return _format_available(summary)
    if command == "/gastar_diario":
        return _format_daily(summary)
    return "Comando no reconocido."


def _format_cycle_summary(summary) -> str:
    from app.utils.formatting import format_currency_amount

    label = (
        f"📆 Ciclo #{summary.cycle_index} "
        f"({summary.start_date.isoformat()} → "
        f"{summary.end_date.isoformat()}) — {summary.status}"
    )
    lines: list[str] = [label, ""]

    def line_for(title: str, mc) -> str:
        if mc.is_empty():
            return f"• {title}: —"
        bits = ", ".join(
            f"{format_currency_amount(amount)} {cur}"
            for cur, amount in mc.items()
        )
        return f"• {title}: {bits}"

    lines.append(line_for("Ingreso base", summary.base_income))
    lines.append(line_for("Extras", summary.extra_income))
    lines.append(line_for("Ingreso bruto", summary.gross_income))
    lines.append(line_for("Ahorro target", summary.savings_target))
    lines.append(line_for("Ahorro reservado", summary.savings_reserved))
    lines.append(line_for("Ahorro realizado", summary.savings_realized))
    lines.append(line_for("Comprometido", summary.savings_committed))
    lines.append(line_for("Gastos realizados", summary.realized_expenses))
    lines.append(
        line_for("Pendientes fijos", summary.pending_fixed_obligations)
    )
    lines.append(line_for("Disponible", summary.available_to_spend))
    if summary.carried_over_fixed:
        bits = ", ".join(
            f"{occ.name} {format_currency_amount(occ.expected_amount)}"
            f" {occ.currency} ({occ.original_month_year})"
            for occ in summary.carried_over_fixed
        )
        lines.append("")
        lines.append(f"🔁 Llevado de ciclos previos: {bits}")
    if summary.warnings:
        lines.append("")
        lines.append("⚠️ " + " · ".join(summary.warnings))
    return "\n".join(lines)


def _format_available(summary) -> str:
    from app.utils.formatting import format_currency_amount

    if summary.status == "closed":
        return (
            "🔒 El ciclo está cerrado. "
            "Los números son los del snapshot; no cambian."
        )
    if summary.available_to_spend.is_empty():
        return (
            "💸 Aún no tengo datos suficientes para calcular tu "
            "disponible. Cargá un ingreso o un gasto para empezar."
        )
    bits = ", ".join(
        f"{format_currency_amount(amount)} {currency}"
        for currency, amount in summary.available_to_spend.items()
    )
    return f"💰 Disponible en este ciclo: {bits}"


def _format_daily(summary) -> str:
    from app.utils.formatting import format_currency_amount

    if summary.status == "closed":
        return "🔒 Ciclo cerrado: no hay diario para mostrar."
    if summary.daily_safe_to_spend is None:
        return "📅 El ciclo ya cerró; no hay diario para hoy."
    if summary.daily_safe_to_spend.is_empty():
        return "💸 Sin ingresos registrados este ciclo."
    bits = ", ".join(
        f"{format_currency_amount(amount)} {currency}"
        for currency, amount in summary.daily_safe_to_spend.items()
    )
    days = summary.remaining_days or 0
    return (
        f"📅 Podés gastar {bits} por día "
        f"(quedan {days} día(s) en el ciclo)."
    )


# ---------------------------------------------------------------------------
# R3 — money mutations over Telegram
# ---------------------------------------------------------------------------
#
# Source keys are deterministic per (chat, message) so retried / replayed
# messages never create duplicate rows. Currency comes from the existing
# detect_currency_hint helper; ARS is the default.


def _tg_source_key(prefix: str, chat_id: int, message_id: int) -> str:
    return f"telegram:{prefix}:{chat_id}:{message_id}"


def _parse_tg_amount(args: list[str]) -> Decimal | None:
    if not args:
        return None
    return _normalize_decimal(args[0])


def _parse_tg_currency(text: str, args: list[str]) -> str:
    # Reuse the existing detector so the surface stays consistent.
    from app.utils.amounts import detect_currency_hint

    hint = detect_currency_hint(" ".join(args) or text or "")
    return (hint or "ARS").upper()


def _resolve_current_cycle_id_or_message(user_id: int, today) -> tuple[int | None, str | None]:
    """Return ``(cycle_id, error_message)``.

    ``CycleService.ensure_current_cycle`` walks the rollover chain so a
    Telegram user does not need to know about cycles; the handler is
    always implicitly about the open one. Failures map to user-friendly
    Spanish strings.
    """
    from app.money import CycleCatchUpLimitExceeded, CycleService

    with session_scope() as s:
        try:
            cycle = CycleService(s).ensure_current_cycle(user_id, today)
        except CycleCatchUpLimitExceeded as exc:
            return None, (
                "⚠️ Detecté muchos ciclos cerrados sin cerrar; "
                "avisale al admin para revisar el estado. "
                f"Detalle: {exc}"
            )
    return cycle.id, None


def _handle_ingreso_command(
    args: list[str],
    *,
    user_id: int,
    today,
    text: str = "",
) -> str:
    from app.money import MoneyStateService

    amount = _parse_tg_amount(args)
    if amount is None:
        return (
            "❓ Decime el monto. Ej: `/ingreso 1100000`.\n"
            "Valores válidos: 0 o más."
        )
    if amount < 0:
        return "❓ El monto tiene que ser 0 o más."
    currency = _parse_tg_currency(text, args)
    cycle_id, err = _resolve_current_cycle_id_or_message(user_id, today)
    if err is not None:
        return err
    try:
        with session_scope() as s:
            MoneyStateService(s).set_income(
                user_id=user_id, cycle_id=cycle_id,
                currency=currency, amount=amount,
            )
            summary = MoneyEngine(s).get_cycle_summary(
                user_id, cycle_id, today=today
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("ingreso failed")
        return f"⚠️ No pude guardar el ingreso: {exc}"
    return _format_mutation_ack(
        "💼 Ingreso base actualizado.",
        currency, summary.base_income.get(currency),
        summary=summary,
    )


def _handle_extra_command(
    args: list[str],
    *,
    user_id: int,
    chat_id: int,
    message_id: int,
    today,
    text: str = "",
) -> str:
    from app.money import MoneySourceCurrencyConflictError, MoneyStateService

    amount = _parse_tg_amount(args)
    if amount is None:
        return (
            "❓ Decime el monto. Ej: `/extra 50000 regalo cumpleaños`.\n"
            "Valores válidos: mayor a 0."
        )
    if amount <= 0:
        return "❓ El monto tiene que ser mayor a 0."
    currency = _parse_tg_currency(text, args)
    note = " ".join(args[1:]).strip() or None
    cycle_id, err = _resolve_current_cycle_id_or_message(user_id, today)
    if err is not None:
        return err
    source_key = _tg_source_key("extra", chat_id, message_id)
    try:
        with session_scope() as s:
            row = MoneyStateService(s).record_extra_income(
                user_id=user_id, cycle_id=cycle_id,
                currency=currency, amount=amount,
                source_key=source_key, note=note,
            )
            summary = MoneyEngine(s).get_cycle_summary(
                user_id, cycle_id, today=today
            )
    except MoneySourceCurrencyConflictError:
        return "⚠️ Ese extra ya fue registrado en otra moneda."
    except Exception as exc:  # noqa: BLE001
        logger.exception("extra failed")
        return f"⚠️ No pude guardar el extra: {exc}"
    return _format_mutation_ack(
        f"🎁 Extra registrado (id #{row.id}).",
        currency, summary.extra_income.get(currency),
        summary=summary,
    )


def _handle_extra_editar_command(
    args: list[str],
    *,
    user_id: int,
    today,
) -> str:
    from app.money import MoneyStateService

    if not args:
        return "❓ Uso: `/extra_editar <id> <monto> [nota]`."
    try:
        extra_id = int(args[0])
    except ValueError:
        return "❓ El id tiene que ser un número entero."
    if len(args) < 2:
        return "❓ Falta el monto. Uso: `/extra_editar <id> <monto> [nota]`."
    amount = _normalize_decimal(args[1])
    if amount is None:
        return "❓ El monto no es un número válido."
    note = " ".join(args[2:]).strip() or None
    try:
        with session_scope() as s:
            row = MoneyStateService(s).update_extra_income(
                user_id=user_id, extra_id=extra_id,
                amount=amount, note=note,
            )
            summary = MoneyEngine(s).get_cycle_summary(
                user_id, row.cycle_id, today=today
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("extra_editar failed")
        return f"⚠️ No pude editar el extra: {exc}"
    return _format_mutation_ack(
        f"✏️ Extra #{extra_id} actualizado.",
        row.currency, summary.extra_income.get(row.currency),
        summary=summary,
    )


def _handle_extra_borrar_command(
    args: list[str], *, user_id: int, today
) -> str:
    from app.money import MoneyStateService

    if not args:
        return "❓ Uso: `/extra_borrar <id>`."
    try:
        extra_id = int(args[0])
    except ValueError:
        return "❓ El id tiene que ser un número entero."
    try:
        with session_scope() as s:
            MoneyStateService(s).soft_delete_extra_income(
                user_id=user_id, extra_id=extra_id,
            )
            summary = MoneyEngine(s).get_cycle_summary(
                user_id, _resolve_cycle_id_for_extra(s, user_id, extra_id),
                today=today,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("extra_borrar failed")
        return f"⚠️ No pude borrar el extra: {exc}"
    return _format_mutation_ack(
        f"🗑️ Extra #{extra_id} borrado.",
        "ARS", None, summary=summary,
    )


def _resolve_cycle_id_for_extra(s, user_id: int, extra_id: int) -> int:
    """Look up the cycle id for a deleted extra so we can summarize."""
    from sqlalchemy import select
    from app.money.models import CycleIncome, FinancialCycle

    cycle_id = s.execute(
        select(CycleIncome.cycle_id).where(CycleIncome.id == extra_id)
    ).scalar_one_or_none()
    if cycle_id is None:
        return 0
    s.execute(
        select(FinancialCycle.id).where(
            FinancialCycle.id == cycle_id,
            FinancialCycle.telegram_user_id == user_id,
        )
    ).scalar_one_or_none()
    return cycle_id


def _parse_target_value(args: list[str]) -> tuple[Decimal | None, Decimal | None]:
    """Parse the /target argument as either ``pct`` or ``amount``.

    Spec §15 keeps the syntax explicit:

    - ``/target 20%``     → pct = 0.20
    - ``/target 220000``  → amount = 220000

    The percentage suffix (``%``) is the only thing that disambiguates
    "20%" (pct) from "20" (amount). Anything without the suffix is
    treated as an amount — we never silently interpret a bare number as
    a percentage.
    """
    if not args:
        return None, None
    raw = args[0]
    if raw.endswith("%"):
        pct = _normalize_decimal(raw[:-1].strip())
        if pct is None:
            return None, None
        return pct / Decimal("100"), None
    val = _normalize_decimal(raw)
    if val is None:
        return None, None
    return None, val


def _handle_target_command(
    args: list[str], *, user_id: int, today, text: str = ""
) -> str:
    from app.money import MoneyStateService

    if not args:
        return (
            "❓ Decime el target. Ej: `/target 20%` o `/target 220000`."
        )
    pct, amount = _parse_target_value(args)
    if pct is None and amount is None:
        return (
            "❓ No pude entender el target.\n"
            "Usá `/target 20%` para porcentaje o "
            "`/target 220000` para monto."
        )
    currency = _parse_tg_currency(text, args)
    cycle_id, err = _resolve_current_cycle_id_or_message(user_id, today)
    if err is not None:
        return err
    try:
        with session_scope() as s:
            MoneyStateService(s).set_savings_target(
                user_id=user_id, cycle_id=cycle_id,
                currency=currency,
                target_pct=pct, target_amount=amount,
            )
            summary = MoneyEngine(s).get_cycle_summary(
                user_id, cycle_id, today=today
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("target failed")
        return f"⚠️ No pude guardar el target: {exc}"
    return _format_mutation_ack(
        "🎯 Target de ahorro actualizado.",
        currency, summary.savings_target.get(currency),
        summary=summary,
    )


def _handle_reservar_command(
    args: list[str], *, user_id: int, today, text: str = ""
) -> str:
    from app.money import MoneyStateService

    amount = _parse_tg_amount(args)
    if amount is None:
        return (
            "❓ Decime el monto. Ej: `/reservar 220000`.\n"
            "Valores válidos: 0 o más."
        )
    if amount < 0:
        return "❓ El monto tiene que ser 0 o más."
    currency = _parse_tg_currency(text, args)
    cycle_id, err = _resolve_current_cycle_id_or_message(user_id, today)
    if err is not None:
        return err
    try:
        with session_scope() as s:
            MoneyStateService(s).set_savings_reserved(
                user_id=user_id, cycle_id=cycle_id,
                currency=currency, amount=amount,
            )
            summary = MoneyEngine(s).get_cycle_summary(
                user_id, cycle_id, today=today
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("reservar failed")
        return f"⚠️ No pude guardar la reserva: {exc}"
    return _format_mutation_ack(
        "🔒 Reserva actualizada.",
        currency, summary.savings_reserved.get(currency),
        summary=summary,
    )


def _handle_liberar_command(
    args: list[str], *, user_id: int, today, text: str = ""
) -> str:
    from app.money import MoneyStateService

    amount = _parse_tg_amount(args)
    if amount is None:
        return (
            "❓ Decime el monto a liberar. Ej: `/liberar 100000`.\n"
            "Valores válidos: mayor a 0."
        )
    if amount <= 0:
        return "❓ El monto tiene que ser mayor a 0."
    currency = _parse_tg_currency(text, args)
    cycle_id, err = _resolve_current_cycle_id_or_message(user_id, today)
    if err is not None:
        return err
    try:
        with session_scope() as s:
            MoneyStateService(s).release_savings(
                user_id=user_id, cycle_id=cycle_id,
                currency=currency, amount=amount,
            )
            summary = MoneyEngine(s).get_cycle_summary(
                user_id, cycle_id, today=today
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("liberar failed")
        return f"⚠️ No pude liberar la reserva: {exc}"
    return _format_mutation_ack(
        "🔓 Reserva liberada.",
        currency, summary.savings_reserved.get(currency),
        summary=summary,
    )


def _handle_ahorro_realizado_command(
    args: list[str],
    *,
    user_id: int,
    chat_id: int,
    message_id: int,
    today,
    text: str = "",
) -> str:
    from app.money import MoneySourceCurrencyConflictError, MoneyStateService

    amount = _parse_tg_amount(args)
    if amount is None:
        return (
            "❓ Decime el monto. Ej: `/ahorro_realizado 50000 transferencia`."
        )
    if amount <= 0:
        return "❓ El monto tiene que ser mayor a 0."
    currency = _parse_tg_currency(text, args)
    note = " ".join(args[1:]).strip() or None
    cycle_id, err = _resolve_current_cycle_id_or_message(user_id, today)
    if err is not None:
        return err
    source_key = _tg_source_key("savings", chat_id, message_id)
    try:
        with session_scope() as s:
            row = MoneyStateService(s).record_savings_realized(
                user_id=user_id, cycle_id=cycle_id,
                currency=currency, amount=amount,
                source_key=source_key, note=note,
            )
            summary = MoneyEngine(s).get_cycle_summary(
                user_id, cycle_id, today=today
            )
    except MoneySourceCurrencyConflictError:
        return "⚠️ Ese ahorro ya fue registrado en otra moneda."
    except Exception as exc:  # noqa: BLE001
        logger.exception("ahorro_realizado failed")
        return f"⚠️ No pude guardar el ahorro: {exc}"
    return _format_mutation_ack(
        f"🏦 Ahorro realizado registrado (id #{row.id}).",
        currency, summary.savings_realized.get(currency),
        summary=summary,
    )


def _handle_ahorro_editar_command(
    args: list[str], *, user_id: int, today
) -> str:
    from app.money import MoneyStateService

    if not args:
        return "❓ Uso: `/ahorro_editar <id> <monto> [nota]`."
    try:
        rid = int(args[0])
    except ValueError:
        return "❓ El id tiene que ser un número entero."
    if len(args) < 2:
        return "❓ Falta el monto. Uso: `/ahorro_editar <id> <monto> [nota]`."
    amount = _normalize_decimal(args[1])
    if amount is None:
        return "❓ El monto no es un número válido."
    note = " ".join(args[2:]).strip() or None
    try:
        with session_scope() as s:
            row = MoneyStateService(s).update_savings_realized(
                user_id=user_id, realization_id=rid,
                amount=amount, note=note,
            )
            summary = MoneyEngine(s).get_cycle_summary(
                user_id, row.cycle_id, today=today
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("ahorro_editar failed")
        return f"⚠️ No pude editar el ahorro: {exc}"
    return _format_mutation_ack(
        f"✏️ Ahorro #{rid} actualizado.",
        row.currency, summary.savings_realized.get(row.currency),
        summary=summary,
    )


def _handle_ahorro_borrar_command(
    args: list[str], *, user_id: int, today
) -> str:
    from app.money import MoneyStateService

    if not args:
        return "❓ Uso: `/ahorro_borrar <id>`."
    try:
        rid = int(args[0])
    except ValueError:
        return "❓ El id tiene que ser un número entero."
    try:
        with session_scope() as s:
            MoneyStateService(s).soft_delete_savings_realized(
                user_id=user_id, realization_id=rid,
            )
            summary = MoneyEngine(s).get_cycle_summary(
                user_id,
                _resolve_cycle_id_for_realization(s, rid),
                today=today,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("ahorro_borrar failed")
        return f"⚠️ No pude borrar el ahorro: {exc}"
    return _format_mutation_ack(
        f"🗑️ Ahorro #{rid} borrado.",
        "ARS", None, summary=summary,
    )


def _resolve_cycle_id_for_realization(s, realization_id: int) -> int:
    from sqlalchemy import select
    from app.money.models import SavingsRealization

    return s.execute(
        select(SavingsRealization.cycle_id).where(
            SavingsRealization.id == realization_id
        )
    ).scalar_one_or_none() or 0


def _format_mutation_ack(
    headline: str,
    currency: str,
    new_value: Decimal | None,
    *,
    summary,
) -> str:
    from app.utils.formatting import format_currency_amount

    parts: list[str] = [headline]
    if new_value is not None:
        parts.append(
            f"→ {currency}: {format_currency_amount(new_value, currency)}"
        )
    available = summary.available_to_spend.get(currency)
    if available is not None:
        parts.append(
            f"💰 Disponible: {format_currency_amount(available, currency)} {currency}"
        )
    committed = summary.savings_committed.get(currency)
    if committed is not None:
        parts.append(
            f"🔒 Comprometido: {format_currency_amount(committed, currency)} {currency}"
        )
    return "\n".join(parts)
    return "\n".join(parts)
