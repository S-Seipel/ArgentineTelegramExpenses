"""Short-lived receipt previews, bounded and scoped to user and chat."""
from collections import OrderedDict
from dataclasses import dataclass
import secrets
from threading import RLock
from time import monotonic


@dataclass(frozen=True)
class PendingReceipt:
    user_id: int
    chat_id: int | None
    draft: object
    source_key: str
    created_at: float


class ReceiptPreviews:
    def __init__(self, *, ttl=900, capacity=64, clock=monotonic):
        self.ttl, self.capacity, self.clock = ttl, capacity, clock
        self._items = OrderedDict()
        self._lock = RLock()

    def _prune(self):
        for key, item in list(self._items.items()):
            if self.clock() - item.created_at >= self.ttl:
                del self._items[key]

    def put(self, user_id, chat_id, draft, source_key):
        with self._lock:
            self._prune()
            for key, item in self._items.items():
                if (item.user_id, item.chat_id, item.source_key) == (user_id, chat_id, source_key):
                    return key
            # A single user cannot evict everybody else's pending receipts.
            own = [key for key, item in self._items.items() if item.user_id == user_id]
            for key in own[:-3]:
                del self._items[key]
            while len(self._items) >= self.capacity:
                self._items.popitem(last=False)
            key = secrets.token_urlsafe(12)
            self._items[key] = PendingReceipt(user_id, chat_id, draft, source_key, self.clock())
            return key

    def get(self, key, user_id, chat_id):
        with self._lock:
            self._prune()
            item = self._items.get(key)
            return item if item and (item.user_id, item.chat_id) == (user_id, chat_id) else None

    def remove(self, key, user_id, chat_id):
        with self._lock:
            if self.get(key, user_id, chat_id):
                del self._items[key]


receipt_previews = ReceiptPreviews()


def confirm_receipt(key, user_id, chat_id, *, amount=None, currency=None, name=None, expense_date=None, today=None):
    """Persist only an explicitly confirmed preview, with a stable DB identity."""
    from datetime import date
    from decimal import Decimal, InvalidOperation
    import re
    from sqlalchemy.exc import IntegrityError
    from app.database.database import session_scope
    from app.expenses.repository import ExpenseRepository
    from app.expenses.service import ExpenseDraft, ExpenseService

    preview = receipt_previews.get(key, user_id, chat_id)
    if preview is None:
        raise LookupError("El borrador venció o no pertenece a este chat. Volvé a enviar la foto.")
    draft = preview.draft
    value = amount if amount is not None else draft.amount
    if isinstance(value, bool):
        raise ValueError("Indicá el total correcto del ticket.")
    try:
        value = Decimal(str(value))
        if not value.is_finite() or not 0 < value < Decimal('10000000000000000') or value != value.quantize(Decimal('0.01')):
            raise ValueError("El total debe ser positivo y tener hasta dos decimales.")
    except (InvalidOperation, TypeError):
        raise ValueError("Indicá el total correcto del ticket.") from None
    cur = str(currency or draft.currency or 'ARS').upper().strip()
    if not re.fullmatch(r'[A-Z]{3}', cur):
        raise ValueError("Usá una moneda de tres letras, como ARS o USD.")
    title = str(name or draft.name or 'Ticket').strip()
    if not title or len(title) > 255:
        raise ValueError("El nombre debe tener entre 1 y 255 caracteres.")
    try:
        when = date.fromisoformat(expense_date) if expense_date is not None else draft.expense_date or today or date.today()
    except (ValueError, TypeError):
        raise ValueError('Indicá la fecha del ticket como AAAA-MM-DD.') from None
    try:
        with session_scope() as session:
            repo = ExpenseRepository(session)
            existing = repo.get_by_source_key(user_id, preview.source_key, include_deleted=True)
            if existing:
                if existing.deleted_at is not None:
                    raise ValueError('Este ticket ya fue registrado y eliminado. Usá /undo para recuperar el gasto.')
                saved_id = existing.id
            else:
                outcome = ExpenseService(repo).register_many(user_id, [ExpenseDraft(
                    name=title, amount=value, currency=cur, category='Otros',
                    expense_date=when, confidence=Decimal('1'),
                )], original_message='Ticket confirmado por el usuario',
                   source_type='receipt', source_key=preview.source_key,
                   telegram_chat_id=chat_id, commit=False)
                if not outcome.saved:
                    raise ValueError('No se pudo validar el ticket: ' + '; '.join(outcome.questions))
                session.flush()
                saved_id = outcome.saved[0].id
    except IntegrityError:
        with session_scope() as session:
            existing = ExpenseRepository(session).get_by_source_key(user_id, preview.source_key, include_deleted=True)
            if existing is None:
                raise
            if existing.deleted_at is not None:
                raise ValueError('Este ticket ya fue registrado y eliminado. Usá /undo para recuperar el gasto.')
            saved_id = existing.id
    # Keep the preview until TTL so a lost reply can be retried safely.
    return saved_id
