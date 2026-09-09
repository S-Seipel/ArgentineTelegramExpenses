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
from app.utils.amounts import _normalize_decimal, find_thousand_amounts
from app.utils.dates import today_in_tz
from app.utils.formatting import format_currency_amount, format_date_short
from app.ai.schemas import AIError, AIUnavailable

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

    pending = deps.runtime.pending_reminders.get(user_id)
    if pending is not None:
        handled = await _maybe_handle_reminder_reply(
            deps, user_id, text, pending, today
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


async def handle_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: BotDependencies,
) -> None:
    """Download the highest-resolution photo, ask the vision model to
    extract a receipt/ticket, and show a confirmation prompt with
    inline keyboard buttons.
    """
    try:
        await _ensure_authorized(update, deps)
    except _SilentStop:
        return

    if update.effective_message is None or update.effective_user is None:
        return
    photos = update.effective_message.photo
    if not photos:
        return
    user_id = update.effective_user.id
    await update.effective_message.chat.send_action(ChatAction.TYPING)

    best = photos[-1]
    try:
        tg_file = await context.bot.get_file(best.file_id)
        image_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception:
        logger.exception("Failed to download photo")
        await update.effective_message.reply_text(
            "⚠️ No pude descargar la imagen."
        )
        return

    try:
        extracted = await deps.runtime.ai.describe_image(image_bytes)
    except AIUnavailable:
        logger.exception("Vision unreachable")
        await update.effective_message.reply_text(
            "🤖 El servicio de visión no responde. Verificá que Ollama "
            f"esté corriendo y que el modelo `{deps.runtime.settings.vision_model}` "
            "esté descargado."
        )
        return
    except AIError as exc:
        await update.effective_message.reply_text(f"🤖 {exc}")
        return
    except Exception:
        logger.exception("Vision error")
        await update.effective_message.reply_text(
            "⚠️ Error inesperado procesando la imagen."
        )
        return

    confidence = float(extracted.get("confidence") or 0)
    if confidence < deps.runtime.settings.vision_min_confidence:
        await update.effective_message.reply_text(
            f"🤔 No pude leer bien el ticket (confianza {confidence:.0%}). "
            "¿Lo cargás a mano con el monto?"
        )
        return
    if not extracted.get("amount"):
        await update.effective_message.reply_text(
            "🤔 No pude identificar el monto del ticket. "
            "¿Lo cargás a mano?"
        )
        return

    # Stash the extraction pending user confirmation.
    deps.runtime.pending_visions[user_id] = {
        "name": extracted.get("name") or "Ticket",
        "amount": extracted.get("amount"),
        "currency": (extracted.get("currency") or "ARS").upper(),
        "category": extracted.get("category") or "Otros",
        "date": extracted.get("date"),
        "confidence": confidence,
    }

    markup = _vision_keyboard()
    await update.effective_message.reply_text(
        _format_vision_preview(deps.runtime.pending_visions[user_id]),
        reply_markup=markup,
    )


async def handle_vision_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: BotDependencies,
) -> None:
    """Handle inline keyboard responses for vision previews."""
    query = update.callback_query
    if query is None:
        return
    user_id = query.from_user.id if query.from_user else 0
    pending = deps.runtime.pending_visions.pop(user_id, None)
    await query.answer()

    if pending is None:
        await query.edit_message_text(
            "🤔 Esta confirmación ya expiró. Mandame la foto de nuevo."
        )
        return

    action = (query.data or "").split(":", 1)[1] if query.data else ""
    if action == "confirm":
        await _register_pending_vision_expense(
            update, deps, user_id, pending
        )
    elif action == "cancel":
        await query.edit_message_text("🗑️ Ticket descartado.")
    elif action == "edit":
        deps.runtime.pending_visions[user_id] = pending
        await query.edit_message_text(
            "✏️ Respondé con el dato corregido. Ejemplos:\n"
            "• `monto: 4500`\n"
            "• `nombre: Café Starbucks`\n"
            "• `categoría: Café`"
        )


def _vision_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Registrar", callback_data="vision:confirm"),
                InlineKeyboardButton("✏️ Corregir", callback_data="vision:edit"),
            ],
            [
                InlineKeyboardButton("❌ Descartar", callback_data="vision:cancel"),
            ],
        ]
    )


def _format_vision_preview(d: dict) -> str:
    amt = d.get("amount")
    currency = d.get("currency") or "ARS"
    conf = float(d.get("confidence") or 0) * 100
    date_part = f"\n📅 {d['date']}" if d.get("date") else ""
    return (
        f"📸 *Extraído del ticket:*\n\n"
        f"🏪 *{d.get('name') or 'Ticket'}*\n"
        f"💰 $ {amt} {currency}\n"
        f"📂 {d.get('category') or 'Otros'}"
        f"{date_part}\n"
        f"\nConfianza: {conf:.0f}%\n\n"
        f"¿Lo registro?"
    )


async def _register_pending_vision_expense(
    update: Update, deps: BotDependencies, user_id: int, pending: dict
) -> None:
    """Insert the pending vision expense via the existing register_many path."""
    from datetime import date as _date
    from decimal import Decimal

    from app.ai.schemas import AIError, AIUnavailable
    from app.bot.service import _process_message_async

    amount = pending.get("amount")
    if amount is None:
        await update.effective_message.reply_text(
            "🤔 Falta el monto. Reintentá con `/exportar` no, mejor mandá el ticket de nuevo."
        )
        return
    try:
        amount_dec = Decimal(str(amount))
    except Exception:
        await update.effective_message.reply_text("🤔 Monto inválido.")
        return

    name = pending.get("name") or "Ticket"
    category = pending.get("category") or "Otros"
    currency = (pending.get("currency") or "ARS").upper()
    date_str = pending.get("date")

    text = (
        f"gasté {amount_dec} {currency} en {name}"
        + (f" ({category})" if category else "")
    )
    today = today_in_tz(deps.runtime.settings.timezone)
    reply = await _process_message_async(
        deps.runtime,
        text,
        user_id=user_id,
        today=today,
    )
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(reply)
        except Exception:
            await update.callback_query.message.reply_text(reply)
    else:
        await update.effective_message.reply_text(reply)


async def _maybe_handle_reminder_reply(
    deps: BotDependencies,
    user_id: int,
    text: str,
    pending: dict,
    today,
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

    recurring_id = pending["recurring_id"]
    deps.runtime.pending_reminders.pop(user_id, None)

    with session_scope() as session:
        repo = RecurringRepository(session)
        service = RecurringService(repo)
        template_obj = repo.get_by_id_for_update(user_id, recurring_id)
        if template_obj is None or not template_obj.is_active:
            return "🤷 Ese recurrente ya no existe."

        name = template_obj.name
        amount = template_obj.amount
        currency = template_obj.currency

        if normalized in ("sí", "si", "dale", "ok", "listo", "confirmo"):
            template_after, saved = service.register_occurrence(
                user_id=user_id, recurring_id=recurring_id, today=today
            )
            if saved is None:
                return "🤷 No pude registrar el gasto."
            from app.utils.formatting import format_currency_amount as fca

            return (
                f"✅ Listo: *{name}* — {fca(amount, currency)} "
                f"registrado para hoy. "
                f"Próximo aviso: {format_date_short(template_after.next_due_date)}"
            )

        if normalized in ("posponer", "mañana", "manana"):
            service.postpone_occurrence(user_id=user_id, recurring_id=recurring_id)
            template_after = repo.get_by_id_for_update(user_id, recurring_id)
            return (
                f"⏰ Dale, te aviso mañana. "
                f"Próximo intento: {format_date_short(template_after.next_due_date)}"
            )

        if normalized in ("eliminar", "borrar", "nunca más", "nunca mas", "quitar"):
            service.set_active(user_id, recurring_id, is_active=False)
            return f"🗑️ Recurrente *{name}* eliminado."

        if normalized in ("no", "nope", "skip", "saltar"):
            template_after = service.skip_occurrence(
                user_id=user_id, recurring_id=recurring_id, today=today
            )
            if template_after is None:
                return "🤷 No pude saltar este recordatorio."
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
