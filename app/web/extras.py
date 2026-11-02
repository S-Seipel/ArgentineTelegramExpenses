"""Optional tools for the private local dashboard."""
from datetime import date
from decimal import Decimal
from pathlib import Path
import hashlib

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import select

from app.config.settings import get_settings
from app.database.database import session_scope
from app.web.routes import _current_user_id, _today

router = APIRouter()


@router.get('/assets/extras.js', include_in_schema=False)
async def extras_script():
    return Response(Path(__file__).with_name('extras.js').read_text(), media_type='application/javascript')


def _preferences(value):
    return {'telegram_user_id': value.telegram_user_id, 'role': value.role,
            'is_active': value.is_active, 'language': value.language,
            'voice_enabled': value.voice_enabled, 'timezone': value.timezone}


@router.get('/metrics')
async def operational_metrics(user_id: int = Depends(_current_user_id)):
    from app.metrics import metrics
    return Response(metrics.render(), headers={'Content-Type': 'text/plain; version=0.0.4; charset=utf-8'})


@router.get('/api/preferences')
async def preferences(user_id: int = Depends(_current_user_id)):
    from app.users import UserService
    try:
        with session_scope() as session:
            return _preferences(UserService(session, settings=get_settings()).get_preferences(user_id))
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@router.patch('/api/preferences')
async def save_preferences(payload: dict, user_id: int = Depends(_current_user_id)):
    from app.users import UserService
    if not payload or set(payload) - {'language', 'voice_enabled', 'timezone'}:
        raise HTTPException(400, 'Preferencias inválidas.')
    try:
        with session_scope() as session:
            return _preferences(UserService(session, settings=get_settings()).set_preferences(user_id, **payload))
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/api/users')
async def users(user_id: int = Depends(_current_user_id)):
    from app.users import UserService
    with session_scope() as session:
        service = UserService(session, settings=get_settings())
        if not service.is_owner(user_id):
            raise HTTPException(403, 'Sólo el propietario administra usuarios.')
        return {'items': [_preferences(user) for user in service.list_active()]}


@router.post('/api/users')
async def add_user(payload: dict, user_id: int = Depends(_current_user_id)):
    from app.users import UserService
    target = payload.get('telegram_user_id')
    if set(payload) != {'telegram_user_id'} or type(target) is not int or target <= 0:
        raise HTTPException(400, 'Indicá el ID numérico de Telegram.')
    try:
        with session_scope() as session:
            return _preferences(UserService(session, settings=get_settings()).authorize_actor(user_id, target))
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete('/api/users/{target_id}')
async def pause_user(target_id: int, user_id: int = Depends(_current_user_id)):
    from app.users import UserService
    try:
        with session_scope() as session:
            value = UserService(session, settings=get_settings()).deactivate_actor(user_id, target_id)
            if value is None:
                raise HTTPException(404, 'Usuario no encontrado.')
            return {'ok': True}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/api/undo')
async def undo(user_id: int = Depends(_current_user_id)):
    from app.expenses.service import ExpenseService, UndoConflictError, SystemOwnedExpenseError, LinkedFixedPaymentError
    from app.expenses.repository import ExpenseRepository
    try:
        with session_scope() as session:
            result = ExpenseService(ExpenseRepository(session)).undo_last(user_id)
            return {'undone': result is not None, 'result': {
                'expense_id': result.expense.id, 'action': result.action,
                'name': result.expense.name, 'amount': str(result.expense.amount),
                'currency': result.expense.currency,
            } if result else None}
    except (UndoConflictError, SystemOwnedExpenseError, LinkedFixedPaymentError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.patch('/api/expenses/{expense_id}/location')
async def set_location(expense_id: int, payload: dict, user_id: int = Depends(_current_user_id)):
    from app.expenses.service import ExpenseService, ExpenseValidationError, SystemOwnedExpenseError, LinkedFixedPaymentError
    from app.expenses.repository import ExpenseRepository
    if set(payload) != {'latitude', 'longitude'}:
        raise HTTPException(400, 'Indicá latitud y longitud, o ambas null para quitar la ubicación.')
    try:
        with session_scope() as session:
            value = ExpenseService(ExpenseRepository(session)).set_location(user_id, expense_id, payload['latitude'], payload['longitude'])
            if value is None:
                raise HTTPException(404, 'Gasto no encontrado.')
            return {'id': value.id, 'latitude': str(value.latitude) if value.latitude is not None else None,
                    'longitude': str(value.longitude) if value.longitude is not None else None}
    except (ExpenseValidationError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except (SystemOwnedExpenseError, LinkedFixedPaymentError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get('/api/locations')
async def locations(limit: int = Query(200, ge=1, le=500), user_id: int = Depends(_current_user_id)):
    from app.expenses.models import Expense
    with session_scope() as session:
        rows = session.execute(select(Expense).where(Expense.telegram_user_id == user_id,
            Expense.deleted_at.is_(None), Expense.latitude.is_not(None), Expense.longitude.is_not(None))
            .order_by(Expense.expense_date.desc(), Expense.id.desc()).limit(limit)).scalars().all()
        return {'items': [{'id': row.id, 'name': row.name, 'date': row.expense_date.isoformat(),
                           'latitude': str(row.latitude), 'longitude': str(row.longitude)} for row in rows]}


@router.post('/api/receipts/preview')
async def receipt_preview(request: Request, user_id: int = Depends(_current_user_id), today: date = Depends(_today)):
    from app.media.receipts import ReceiptService
    from app.bot.pending import receipt_previews
    from app.metrics import metrics
    if request.headers.get('content-type', '').split(';')[0] not in {'image/jpeg', 'image/png'}:
        raise HTTPException(400, 'Subí una imagen JPEG o PNG.')
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 8 * 1024 * 1024:
            raise HTTPException(413, 'La imagen debe pesar menos de 8 MB.')
    try:
        with metrics.measure('receipt'):
            draft = await ReceiptService().analyze_receipt(bytes(data), today=today)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    key = receipt_previews.put(user_id, None, draft, 'receipt:web:' + hashlib.sha256(data).hexdigest())
    return {'preview_id': key, 'name': draft.name, 'amount': str(draft.amount) if draft.amount is not None else None,
            'currency': draft.currency, 'expense_date': (draft.expense_date or today).isoformat(),
            'warnings': list(draft.warnings), 'requires_confirmation': True}


@router.post('/api/receipts/confirm')
async def receipt_confirm(payload: dict, user_id: int = Depends(_current_user_id), today: date = Depends(_today)):
    from app.bot.pending import confirm_receipt
    if not isinstance(payload.get('preview_id'), str) or set(payload) - {'preview_id', 'amount', 'currency', 'name', 'expense_date'}:
        raise HTTPException(400, 'Borrador inválido.')
    try:
        saved_id = confirm_receipt(payload['preview_id'], user_id, None, amount=payload.get('amount'),
                                   currency=payload.get('currency'), name=payload.get('name'),
                                   expense_date=payload.get('expense_date'), today=today)
        return {'id': saved_id, 'saved': True}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
