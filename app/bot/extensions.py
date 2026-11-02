"""User preferences, media previews and reversible expense tools."""
from datetime import date
from decimal import Decimal
from io import BytesIO
import logging
from time import monotonic
from types import SimpleNamespace

from app.database.database import session_scope
from app.metrics import metrics, instrument
from app.utils.dates import today_in_tz

logger = logging.getLogger(__name__)


def preferences_for(deps, user_id):
    from app.users import UserService
    try:
        with session_scope() as session:
            return UserService(session, settings=deps.runtime.settings).get_preferences(user_id)
    except Exception:
        return SimpleNamespace(language='es', voice_enabled=False, timezone=deps.runtime.settings.timezone)


def timezone_for(deps, user_id):
    return preferences_for(deps, user_id).timezone


async def reply(update, deps, text, **kwargs):
    from app.users.i18n import localize_response, translate
    prefs = preferences_for(deps, update.effective_user.id)
    localized = localize_response(str(text), prefs.language)
    await update.effective_message.reply_text(localized, **kwargs)
    if prefs.voice_enabled:
        from app.media.voice import LocalVoiceService
        try:
            with metrics.measure('voice'):
                audio = await LocalVoiceService().synthesize(localized, language=prefs.language)
            await update.effective_message.reply_voice(voice=BytesIO(audio.audio), filename=audio.filename)
        except Exception:
            logger.warning('Local voice reply unavailable')
            await update.effective_message.reply_text(translate('voice_unavailable', prefs.language))


async def handle_extension_command(update, context, deps):
    from app.users import UserService
    from app.users.i18n import translate
    from app.expenses.service import SystemOwnedExpenseError, LinkedFixedPaymentError
    parts = (update.effective_message.text or '').split()
    if not parts:
        return False
    command, args = parts[0].lower().split('@', 1)[0], parts[1:]
    known = {'/undo', '/deshacer', '/idioma', '/voz', '/zona', '/usuarios', '/usuario_add',
             '/usuario_off', '/ubicacion', '/ticket_confirmar', '/ticket_cancelar', '/rango', '/clasificador'}
    if command not in known:
        return False
    uid, chat = update.effective_user.id, update.effective_message.chat_id
    prefs = preferences_for(deps, uid)
    try:
        if command in {'/idioma', '/voz', '/zona'}:
            if len(args) != 1:
                raise ValueError({'/idioma': '/idioma es|en|pt', '/voz': '/voz on|off', '/zona': '/zona America/Argentina/Buenos_Aires'}[command])
            if command == '/voz' and args[0].lower() not in {'on', 'off'}:
                raise ValueError('/voz on|off')
            values = ({'language': args[0].lower()} if command == '/idioma' else
                      {'voice_enabled': args[0].lower() == 'on'} if command == '/voz' else {'timezone': args[0]})
            with session_scope() as session:
                updated = UserService(session, settings=deps.runtime.settings).set_preferences(uid, **values)
            key = 'language_changed' if command == '/idioma' else ('voice_on' if updated.voice_enabled else 'voice_off') if command == '/voz' else 'timezone_saved'
            answer = translate(key, updated.language)
        elif command in {'/usuarios', '/usuario_add', '/usuario_off'}:
            with session_scope() as session:
                service = UserService(session, settings=deps.runtime.settings)
                if not service.is_owner(uid):
                    raise PermissionError(translate('owner_only', prefs.language))
                if command == '/usuarios':
                    answer = translate('users', prefs.language) + '\n' + '\n'.join(
                        f'{user.telegram_user_id} · {user.role} · {user.language}' for user in service.list_active())
                else:
                    if len(args) != 1 or not args[0].isdigit():
                        raise ValueError(command + ' <ID de Telegram>')
                    if command == '/usuario_add':
                        service.authorize_actor(uid, int(args[0]))
                        answer = '✅ Usuario autorizado.'
                    else:
                        value = service.deactivate_actor(uid, int(args[0]))
                        answer = '✅ Usuario pausado.' if value else 'Usuario no encontrado.'
        elif command in {'/undo', '/deshacer'}:
            from app.expenses.repository import ExpenseRepository
            from app.expenses.service import ExpenseService, UndoConflictError
            try:
                with session_scope() as session:
                    result = ExpenseService(ExpenseRepository(session)).undo_last(uid)
                answer = translate('undo_done' if result else 'undo_empty', prefs.language)
                if result:
                    answer += f' #{result.expense.id}'
            except UndoConflictError:
                answer = translate('undo_conflict', prefs.language)
        elif command == '/ubicacion':
            from app.expenses.repository import ExpenseRepository
            from app.expenses.service import ExpenseService
            if not 1 <= len(args) <= 2 or not args[0].isdigit():
                raise ValueError('/ubicacion <ID de gasto> [quitar]')
            with session_scope() as session:
                repo = ExpenseRepository(session)
                value = repo.get_by_id(uid, int(args[0]))
                if value is None:
                    raise ValueError('Gasto no encontrado.')
                if len(args) == 2:
                    if args[1].lower() != 'quitar':
                        raise ValueError('/ubicacion <ID> quitar')
                    ExpenseService(repo).set_location(uid, value.id, None, None)
                    answer = '📍 Ubicación quitada.'
                else:
                    deps.runtime.pending_locations[(uid, chat)] = (value.id, monotonic())
                    answer = translate('location_prompt', prefs.language)
        elif command == '/ticket_confirmar':
            from app.bot.pending import confirm_receipt
            from app.utils.amounts import _normalize_decimal
            if not 1 <= len(args) <= 3:
                raise ValueError('/ticket_confirmar <borrador> [total] [moneda]')
            amount = _normalize_decimal(args[1]) if len(args) > 1 else None
            if len(args) > 1 and amount is None:
                raise ValueError('Indicá un total válido.')
            saved_id = confirm_receipt(args[0], uid, chat, amount=amount,
                currency=args[2] if len(args) > 2 else None, today=today_in_tz(prefs.timezone))
            answer = f'✅ Ticket registrado. Gasto #{saved_id}.'
        elif command == '/ticket_cancelar':
            from app.bot.pending import receipt_previews
            if len(args) != 1:
                raise ValueError('/ticket_cancelar <borrador>')
            receipt_previews.remove(args[0], uid, chat)
            answer = '👍 Borrador descartado.'
        elif command == '/rango':
            from app.queries.intents import QuerySpec
            from app.queries.service import QueryService
            from app.expenses.repository import ExpenseRepository
            from app.bot.service import _format_query
            if not 2 <= len(args) <= 3:
                raise ValueError('/rango AAAA-MM-DD AAAA-MM-DD [moneda]')
            start, end = date.fromisoformat(args[0]), date.fromisoformat(args[1])
            if start > end:
                raise ValueError('La fecha inicial debe ser anterior a la final.')
            currency = args[2].upper() if len(args) > 2 else None
            if currency and (len(currency) != 3 or not currency.isalpha() or not currency.isascii()):
                raise ValueError('Moneda inválida.')
            with session_scope() as session:
                result = QueryService(ExpenseRepository(session)).run(uid, QuerySpec(
                    kind='total', period='range', range_start=start, range_end=end, currency=currency))
            answer = _format_query(result)
        else:
            from app.expenses.classifier import classifier_status
            with session_scope() as session:
                info = classifier_status(session, uid)
            answer = f'Clasificador local: {info["examples"]} ejemplos · {info["categories"]} categorías. Los casos dudosos siguen usando IA.'
    except (ValueError, LookupError, PermissionError) as exc:
        answer = str(exc)
    except (SystemOwnedExpenseError, LinkedFixedPaymentError):
        answer = 'Este gasto pertenece a un pago fijo o recurrente. Revertí el pago desde su flujo correspondiente.'
    await reply(update, deps, answer)
    return True


@instrument('telegram_photo')
async def handle_photo(update, context, deps):
    from app.bot.handlers import _ensure_authorized
    from app.bot.app import _SilentStop
    from app.media.receipts import ReceiptService, receipt_source_key
    from app.bot.pending import receipt_previews
    try:
        await _ensure_authorized(update, deps)
    except _SilentStop:
        return
    message = update.effective_message
    if message is None or update.effective_user is None or not message.photo:
        return
    uid, chat = update.effective_user.id, message.chat_id
    picture = message.photo[-1]
    if isinstance(picture.file_size, int) and picture.file_size > 8 * 1024 * 1024:
        await reply(update, deps, 'La foto debe pesar menos de 8 MB.')
        return
    try:
        file = await context.bot.get_file(picture.file_id)
        data = bytes(await file.download_as_bytearray())
        with metrics.measure('receipt'):
            draft = await ReceiptService().analyze_receipt(data, caption=message.caption,
                today=today_in_tz(timezone_for(deps, uid)))
        key = receipt_previews.put(uid, chat, draft, receipt_source_key(chat, message.message_id))
        amount = str(draft.amount) if draft.amount is not None else 'revisar total'
        answer = f'🧾 Borrador: {draft.name}\nTotal: {amount} {draft.currency}\nFecha: {draft.expense_date or today_in_tz(timezone_for(deps, uid))}\n'
        if draft.warnings:
            answer += '\n'.join(draft.warnings) + '\n'
        answer += f'\nRevisá el total. Para guardar: /ticket_confirmar {key} [total corregido] [moneda]\nPara descartar: /ticket_cancelar {key}'
        await reply(update, deps, answer, parse_mode=None)
    except Exception as exc:
        logger.warning('Receipt processing failed (%s)', type(exc).__name__)
        await reply(update, deps, 'No pude leer el ticket. Probá con una foto nítida donde se vea TOTAL, o escribí el gasto.')


@instrument('telegram_location')
async def handle_location(update, context, deps):
    from app.bot.handlers import _ensure_authorized
    from app.bot.app import _SilentStop
    from app.expenses.repository import ExpenseRepository
    from app.expenses.service import ExpenseService
    from app.users.i18n import translate
    try:
        await _ensure_authorized(update, deps)
    except _SilentStop:
        return
    message = update.effective_message
    if message is None or update.effective_user is None or message.location is None:
        return
    key = (update.effective_user.id, message.chat_id)
    pending = deps.runtime.pending_locations.get(key)
    if pending is None or monotonic() - pending[1] > 900:
        deps.runtime.pending_locations.pop(key, None)
        await reply(update, deps, 'Primero elegí el gasto con /ubicacion <ID> y después compartí una ubicación.')
        return
    try:
        with session_scope() as session:
            value = ExpenseService(ExpenseRepository(session)).set_location(key[0], pending[0],
                Decimal(str(message.location.latitude)), Decimal(str(message.location.longitude)))
        if value is None:
            raise ValueError('Gasto no encontrado.')
        deps.runtime.pending_locations.pop(key, None)
        answer = translate('location_saved', preferences_for(deps, key[0]).language)
    except (ValueError, RuntimeError) as exc:
        answer = str(exc)
    await reply(update, deps, answer)
