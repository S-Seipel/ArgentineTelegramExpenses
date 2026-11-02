"""Access control and preferences, with no implicit enrollment.

Writes flush within the caller's transaction. The startup owner is the
existing TELEGRAM_ALLOWED_USER_ID; changing that setting does not silently
transfer a database owner or grant a second administrator.
"""
from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config.settings import Settings, get_settings
from app.users.models import AppUser


class UserAccessDenied(PermissionError):
    pass


class UserPreferenceError(ValueError):
    pass


class OwnerConfigurationError(RuntimeError):
    pass


def _user_id(value: int) -> int:
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise UserPreferenceError("El ID de Telegram debe ser un entero positivo válido.")
    return value


def _timezone(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or value != value.strip():
        raise UserPreferenceError("Usá una zona horaria IANA válida.")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise UserPreferenceError("Usá una zona horaria IANA válida.") from exc
    return value


def _language(value: str) -> str:
    if not isinstance(value, str):
        raise UserPreferenceError("Elegí es, en o pt.")
    normalized = value.strip().lower().replace("_", "-").split("-", 1)[0]
    if normalized not in {"es", "en", "pt"}:
        raise UserPreferenceError("Elegí es, en o pt.")
    return normalized


@dataclass(frozen=True)
class UserPreferences:
    telegram_user_id: int
    role: str
    is_active: bool
    language: str
    voice_enabled: bool
    timezone: str

    @property
    def user_id(self) -> int:
        return self.telegram_user_id

    @property
    def active(self) -> bool:
        return self.is_active


def _preferences(row: AppUser) -> UserPreferences:
    return UserPreferences(row.telegram_user_id, row.role, row.is_active,
                           row.language, row.voice_enabled, row.timezone)


class UserService:
    def __init__(self, session: Session, *, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.owner_id = _user_id(self.settings.telegram_allowed_user_id)
        self.default_timezone = _timezone(self.settings.timezone)

    def _get(self, user_id: int, *, lock: bool = False) -> AppUser | None:
        stmt = select(AppUser).where(AppUser.telegram_user_id == user_id).execution_options(populate_existing=True)
        if lock:
            stmt = stmt.with_for_update(of=AppUser)
        return self.session.execute(stmt).scalar_one_or_none()

    def ensure_owner(self) -> UserPreferences:
        """Idempotently seed the configured owner, without resetting preferences."""
        other = self.session.execute(select(AppUser.telegram_user_id).where(
            AppUser.role == "owner", AppUser.telegram_user_id != self.owner_id,
        )).first()
        if other is not None:
            raise OwnerConfigurationError("El propietario configurado no coincide con el de la base de datos.")
        row = self._get(self.owner_id, lock=True)
        if row is None:
            row = AppUser(telegram_user_id=self.owner_id, role="owner", is_active=True,
                          language="es", voice_enabled=False, timezone=self.default_timezone)
            try:
                with self.session.begin_nested():
                    self.session.add(row)
                    self.session.flush()
            except IntegrityError as exc:
                row = self._get(self.owner_id, lock=True)
                if row is None or row.role != "owner":
                    raise OwnerConfigurationError("No se pudo confirmar el propietario configurado.") from exc
        elif row.role != "owner":
            row.role = "owner"
            row.is_active = True
            self.session.flush()
        return _preferences(row)

    def is_owner(self, user_id: int) -> bool:
        if type(user_id) is not int or user_id != self.owner_id:
            return False
        row = self._get(user_id)
        if row is None:
            return self.session.execute(select(AppUser.telegram_user_id).where(AppUser.role == "owner")).first() is None
        return row.role == "owner" and row.is_active

    def is_authorized(self, user_id: int) -> bool:
        if type(user_id) is not int or not 0 < user_id <= 9223372036854775807:
            return False
        if user_id == self.owner_id:
            return self.is_owner(user_id)
        row = self._get(user_id)
        return bool(row is not None and row.is_active and row.role == "user")

    def _require_owner(self, actor_id: int) -> None:
        if type(actor_id) is not int or actor_id != self.owner_id:
            raise UserAccessDenied("Sólo el propietario puede administrar usuarios.")
        self.ensure_owner()
        row = self._get(actor_id, lock=True)
        if row is None or row.role != "owner" or not row.is_active:
            raise UserAccessDenied("Sólo el propietario puede administrar usuarios.")

    def authorize_actor(self, actor_id: int, target_id: int) -> UserPreferences:
        target_id = _user_id(target_id)
        self._require_owner(actor_id)
        row = self._get(target_id, lock=True)
        if row is None:
            row = AppUser(telegram_user_id=target_id, role="user", is_active=True,
                          language="es", voice_enabled=False, timezone=self.default_timezone,
                          created_by=actor_id)
            self.session.add(row)
        else:
            if row.role == "owner" and target_id != self.owner_id:
                raise UserAccessDenied("El propietario no se puede reemplazar desde el bot.")
            row.is_active = True
        self.session.flush()
        return _preferences(row)

    def deactivate_actor(self, actor_id: int, target_id: int) -> UserPreferences | None:
        target_id = _user_id(target_id)
        self._require_owner(actor_id)
        if target_id == self.owner_id:
            raise UserAccessDenied("No podés pausar al propietario.")
        row = self._get(target_id, lock=True)
        if row is None:
            return None
        if row.role == "owner":
            raise UserAccessDenied("No podés pausar al propietario.")
        row.is_active = False
        self.session.flush()
        return _preferences(row)

    def get_preferences(self, user_id: int) -> UserPreferences:
        if not self.is_authorized(user_id):
            raise UserAccessDenied("Usuario no autorizado.")
        row = self._get(user_id)
        if row is None:
            return UserPreferences(self.owner_id, "owner", True, "es", False, self.default_timezone)
        return _preferences(row)

    def set_preferences(self, user_id: int, *, language: str | None = None,
                        voice_enabled: bool | None = None, timezone: str | None = None) -> UserPreferences:
        # Validate everything before any lazy owner insert or mutation.
        language = _language(language) if language is not None else None
        timezone = _timezone(timezone) if timezone is not None else None
        if voice_enabled is not None and type(voice_enabled) is not bool:
            raise UserPreferenceError("La preferencia de voz debe ser true o false.")
        if not self.is_authorized(user_id):
            raise UserAccessDenied("Usuario no autorizado.")
        if user_id == self.owner_id:
            self.ensure_owner()
        row = self._get(user_id, lock=True)
        # Deactivation may have committed while the lock was being acquired.
        if row is None or not row.is_active:
            raise UserAccessDenied("Usuario no autorizado.")
        if language is not None:
            row.language = language
        if voice_enabled is not None:
            row.voice_enabled = voice_enabled
        if timezone is not None:
            row.timezone = timezone
        self.session.flush()
        return _preferences(row)

    def list_active(self) -> list[UserPreferences]:
        rows = self.session.execute(select(AppUser).where(AppUser.is_active.is_(True))
                                    .order_by(AppUser.telegram_user_id)
                                    .execution_options(populate_existing=True)).scalars().all()
        return [_preferences(row) for row in rows]
