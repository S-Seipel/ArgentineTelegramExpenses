from dataclasses import asdict

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database.database import session_scope
from app.money import UserSettingsService
from app.money.models import FinancialCycle, UserSettings
from app.users import AppUser, OwnerConfigurationError, UserAccessDenied, UserPreferenceError, UserService


OWNER = 123456
MEMBER = 987654321012


def test_reading_owner_defaults_does_not_enroll_anyone(in_memory_db):
    with session_scope() as session:
        users = UserService(session)
        assert users.is_authorized(OWNER)
        assert not users.is_authorized(MEMBER)
        assert not users.is_authorized(None)
        assert not users.is_authorized(True)
        assert not users.is_authorized(9223372036854775808)
        assert users.get_preferences(OWNER).language == "es"
        assert users.get_preferences(OWNER).voice_enabled is False
        assert session.query(AppUser).count() == 0
        with pytest.raises(UserAccessDenied):
            users.get_preferences(MEMBER)
        assert session.query(AppUser).count() == 0


def test_seed_owner_is_idempotent_and_preserves_preferences(in_memory_db):
    with session_scope() as session:
        users = UserService(session)
        first = users.ensure_owner()
        assert first.telegram_user_id == OWNER and first.role == "owner"
        assert users.ensure_owner() == first
        updated = users.set_preferences(OWNER, language="en-US", voice_enabled=True, timezone="UTC")
        assert updated.language == "en" and updated.voice_enabled and updated.timezone == "UTC"
    with session_scope() as session:
        preferences = UserService(session).ensure_owner()
        assert preferences == updated
        assert session.query(AppUser).count() == 1


def test_owner_authorizes_pauses_and_reauthorizes_without_resetting_preferences(in_memory_db):
    with session_scope() as session:
        users = UserService(session)
        member = users.authorize_actor(OWNER, MEMBER)
        assert member.role == "user" and member.is_active
        assert users.is_authorized(MEMBER)
        users.set_preferences(MEMBER, language="pt_BR", voice_enabled=True, timezone="America/Sao_Paulo")
        assert users.deactivate_actor(OWNER, MEMBER).is_active is False
        assert not users.is_authorized(MEMBER)
        with pytest.raises(UserAccessDenied):
            users.get_preferences(MEMBER)
        restored = users.authorize_actor(OWNER, MEMBER)
        assert restored.language == "pt" and restored.voice_enabled
        assert restored.timezone == "America/Sao_Paulo"
        assert users.get_preferences(OWNER).language == "es"
        assert session.get(AppUser, MEMBER).created_by == OWNER
        assert users.deactivate_actor(OWNER, MEMBER + 1) is None
        assert [p.telegram_user_id for p in users.list_active()] == [OWNER, MEMBER]


def test_members_cannot_enroll_others_pause_users_or_disable_owner(in_memory_db):
    with session_scope() as session:
        users = UserService(session)
        users.authorize_actor(OWNER, MEMBER)
        for operation in (
            lambda: users.authorize_actor(MEMBER, MEMBER + 1),
            lambda: users.deactivate_actor(MEMBER, OWNER),
            lambda: users.deactivate_actor(MEMBER, MEMBER),
            lambda: users.deactivate_actor(OWNER, OWNER),
        ):
            with pytest.raises(UserAccessDenied):
                operation()
        assert users.is_owner(OWNER)
        assert not users.is_owner(MEMBER)
        assert session.query(AppUser).count() == 2


@pytest.mark.parametrize("value", [0, -1, True, "42", 1.5, 9223372036854775808])
def test_bad_telegram_id_rejected_without_bootstrapping_owner(in_memory_db, value):
    with session_scope() as session:
        users = UserService(session)
        with pytest.raises(UserPreferenceError):
            users.authorize_actor(OWNER, value)
        assert session.query(AppUser).count() == 0


@pytest.mark.parametrize("values", [
    {"language": "fr"}, {"language": 1}, {"voice_enabled": "true"},
    {"voice_enabled": 1}, {"timezone": "not/a/zone"}, {"timezone": " ../UTC"},
])
def test_bad_preferences_rejected_before_any_write(in_memory_db, values):
    with session_scope() as session:
        with pytest.raises(UserPreferenceError):
            UserService(session).set_preferences(OWNER, **values)
        assert session.query(AppUser).count() == 0


def test_unknown_cannot_create_profile_by_saving_preferences(in_memory_db):
    with session_scope() as session:
        with pytest.raises(UserAccessDenied):
            UserService(session).set_preferences(MEMBER, language="en")
        assert session.query(AppUser).count() == 0


def test_profile_preferences_do_not_change_another_user_or_financial_calendar(in_memory_db):
    with session_scope() as session:
        UserSettingsService(session).set_first_weekday(OWNER, 4)
        users = UserService(session)
        users.authorize_actor(OWNER, MEMBER)
        users.set_preferences(MEMBER, language="pt", voice_enabled=True, timezone="Europe/Lisbon")
        owner = users.get_preferences(OWNER)
        assert owner.language == "es" and not owner.voice_enabled
        assert owner.timezone == "America/Argentina/Buenos_Aires"
        calendar = session.get(UserSettings, OWNER)
        assert calendar.pay_rule == "first_weekday" and calendar.payday_weekday == 4
        assert session.query(FinancialCycle).count() == 0


def test_configured_owner_change_does_not_silently_transfer_access(in_memory_db, settings):
    with session_scope() as session:
        UserService(session).ensure_owner()
    with session_scope() as session:
        changed_settings = settings.model_copy(update={"telegram_allowed_user_id": MEMBER})
        changed = UserService(session, settings=changed_settings)
        assert not changed.is_authorized(MEMBER)
        assert not changed.is_owner(MEMBER)
        with pytest.raises(OwnerConfigurationError):
            changed.ensure_owner()
        original = session.execute(select(AppUser)).scalar_one()
        assert original.telegram_user_id == OWNER and original.role == "owner"


@pytest.mark.parametrize("changes", [
    {"telegram_user_id": 0}, {"language": "fr"}, {"role": "admin"},
    {"role": "owner", "is_active": False}, {"timezone": ""},
])
def test_schema_rejects_invalid_profiles(in_memory_db, changes):
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            values = {"telegram_user_id": MEMBER, "role": "user", "language": "es", "timezone": "UTC", "is_active": True}
            session.add(AppUser(**{**values, **changes}))


def test_database_permits_only_one_owner(in_memory_db):
    with session_scope() as session:
        UserService(session).ensure_owner()
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(AppUser(telegram_user_id=MEMBER, role="owner"))


def test_preferences_are_plain_detached_values(in_memory_db):
    with session_scope() as session:
        profile = UserService(session).ensure_owner()
    assert asdict(profile)["language"] == "es"
    assert profile.active and profile.user_id == OWNER
