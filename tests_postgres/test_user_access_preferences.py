"""User bootstrap, owner access and preferences on an isolated PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from subprocess import CalledProcessError
from threading import Barrier

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config.settings import Settings
from app.users import AppUser, UserAccessDenied, UserService
from tests_postgres.conftest import run_alembic
from tests_postgres.test_fixed_payment_concurrency import fresh_db  # noqa: F401


def _settings():
    return Settings(_env_file=None, TELEGRAM_BOT_TOKEN="test-token", TELEGRAM_ALLOWED_USER_ID=42, TIMEZONE="UTC")


def test_owner_bootstrap_and_grant_are_idempotent_under_concurrency(fresh_db):
    engine = create_engine(fresh_db, pool_size=12, max_overflow=4, future=True)
    workers = 12
    barrier = Barrier(workers)
    settings = _settings()

    def seed(_):
        with Session(engine) as session:
            barrier.wait(timeout=30)
            result = UserService(session, settings=settings).ensure_owner()
            session.commit()
            return result

    def grant(_):
        with Session(engine) as session:
            barrier.wait(timeout=30)
            result = UserService(session, settings=settings).authorize_actor(42, 9000000000001)
            session.commit()
            return result

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            assert {profile.telegram_user_id for profile in executor.map(seed, range(workers))} == {42}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            assert {profile.telegram_user_id for profile in executor.map(grant, range(workers))} == {9000000000001}
        with Session(engine) as session:
            assert session.query(AppUser).count() == 2
            assert session.query(AppUser).filter_by(role="owner").count() == 1
            member = session.get(AppUser, 9000000000001)
            assert member.role == "user" and member.created_by == 42
    finally:
        engine.dispose()


def test_cached_authorization_refreshes_after_owner_deactivates_user(fresh_db):
    engine = create_engine(fresh_db, future=True)
    settings = _settings()
    try:
        with Session(engine) as session:
            UserService(session, settings=settings).authorize_actor(42, 81)
            session.commit()
        with Session(engine) as stale:
            assert stale.get(AppUser, 81).is_active
            with Session(engine) as writer:
                UserService(writer, settings=settings).deactivate_actor(42, 81)
                writer.commit()
            users = UserService(stale, settings=settings)
            assert not users.is_authorized(81)
            with pytest.raises(UserAccessDenied):
                users.set_preferences(81, language="en", voice_enabled=True)
            stale.rollback()
        with Session(engine) as session:
            user = session.get(AppUser, 81)
            assert user.language == "es" and not user.voice_enabled and not user.is_active
    finally:
        engine.dispose()


def test_profile_migration_and_preferences_never_reassign_financial_data(fresh_db, pg_database_url):
    engine = create_engine(fresh_db, future=True)
    try:
        # Empty users migration round-trips without enrolling anyone.
        run_alembic("downgrade", "0018_expense_history_location", database_url=pg_database_url)
        with engine.begin() as connection:
            category = connection.execute(text("SELECT min(id) FROM categories")).scalar_one()
            connection.execute(text(
                "INSERT INTO expenses (telegram_user_id,name,amount,currency,category_id,expense_date,"
                "original_message,ai_confidence,source_type,revision) "
                "VALUES (42,'Original expense',12.25,'ARS',:category,'2026-11-01','fixture',1,'telegram',1)"
            ), {"category": category})
            before = connection.execute(text("SELECT row_to_json(e)::text FROM expenses e")).scalar_one()
        run_alembic("upgrade", "head", database_url=pg_database_url)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM app_users")).scalar_one() == 0
        with Session(engine) as session:
            users = UserService(session, settings=_settings())
            users.ensure_owner()
            users.authorize_actor(42, 81)
            users.set_preferences(81, language="pt", voice_enabled=True, timezone="America/Sao_Paulo")
            session.commit()
        with engine.connect() as connection:
            assert connection.execute(text("SELECT row_to_json(e)::text FROM expenses e")).scalar_one() == before
            assert connection.execute(text("SELECT count(*) FROM financial_cycles")).scalar_one() == 0
        with pytest.raises(CalledProcessError):
            run_alembic("downgrade", "0018_expense_history_location", database_url=pg_database_url)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019_user_preferences"
            assert connection.execute(text("SELECT count(*) FROM app_users")).scalar_one() == 2
            assert connection.execute(text("SELECT row_to_json(e)::text FROM expenses e")).scalar_one() == before
    finally:
        engine.dispose()
