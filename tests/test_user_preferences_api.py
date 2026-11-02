from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.bot.app import BotDependencies
from app.bot.handlers import handle_command
from app.categories.models import Category
from app.database.database import session_scope
from app.expenses.models import Expense
from app.expenses.repository import ExpenseRepository
from app.expenses.schemas import ExpenseCreate
from app.main import app
from app.money import CycleService, MoneyEngine
from app.money.models import CycleIncome, FinancialCycle, SavingsReserved
from app.users import AppUser, UserService
from app.web.routes import _current_user_id
from tests.test_money_state_telegram import _run, runtime  # noqa: F401


OWNER = 123456
MEMBER = 888888888888


@pytest.fixture
def client(in_memory_db):
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def test_preference_get_and_save_create_no_financial_state(client):
    profile = _ok(client.get('/api/preferences'))
    assert profile['telegram_user_id'] == OWNER
    assert profile['language'] == 'es' and profile['voice_enabled'] is False
    profile = _ok(client.patch('/api/preferences', json={
        'language': 'pt', 'voice_enabled': True, 'timezone': 'America/Sao_Paulo',
    }))
    assert profile['language'] == 'pt' and profile['voice_enabled'] is True
    assert profile['timezone'] == 'America/Sao_Paulo'
    assert _ok(client.get('/api/preferences')) == profile
    with session_scope() as session:
        assert session.query(AppUser).count() == 1
        assert session.query(FinancialCycle).count() == 0
        assert session.query(CycleIncome).count() == 0
        assert session.query(SavingsReserved).count() == 0


@pytest.mark.parametrize('payload', [
    {}, {'language': 'fr'}, {'voice_enabled': 1}, {'timezone': 'Mars/Here'},
    {'language': 'en', 'telegram_user_id': MEMBER}, {'role': 'owner'},
])
def test_invalid_preferences_never_create_a_profile_or_cycle(client, payload):
    response = client.patch('/api/preferences', json=payload)
    assert response.status_code == 400
    with session_scope() as session:
        assert session.query(AppUser).count() == session.query(FinancialCycle).count() == 0


def test_owner_adds_and_pauses_member_without_affecting_owner(client):
    member = _ok(client.post('/api/users', json={'telegram_user_id': MEMBER}))
    assert member['role'] == 'user' and member['is_active']
    assert {p['telegram_user_id'] for p in _ok(client.get('/api/users'))['items']} == {OWNER, MEMBER}
    assert client.delete(f'/api/users/{OWNER}').status_code == 403
    assert _ok(client.delete(f'/api/users/{MEMBER}'))['ok']
    assert [p['telegram_user_id'] for p in _ok(client.get('/api/users'))['items']] == [OWNER]
    with session_scope() as session:
        assert not UserService(session).is_authorized(MEMBER)
        assert UserService(session).is_authorized(OWNER)
        assert session.query(FinancialCycle).count() == 0


def test_member_cannot_administer_access_or_change_someone_elses_profile(client):
    _ok(client.post('/api/users', json={'telegram_user_id': MEMBER}))
    app.dependency_overrides[_current_user_id] = lambda: MEMBER
    assert client.get('/api/users').status_code == 403
    assert client.post('/api/users', json={'telegram_user_id': MEMBER + 1}).status_code == 403
    assert client.delete(f'/api/users/{OWNER}').status_code == 403
    assert client.patch('/api/preferences', json={'telegram_user_id': OWNER, 'language': 'en'}).status_code == 400
    own = _ok(client.patch('/api/preferences', json={'language': 'en'}))
    assert own['telegram_user_id'] == MEMBER and own['role'] == 'user'
    app.dependency_overrides.clear()
    assert _ok(client.get('/api/preferences'))['language'] == 'es'
    with session_scope() as session:
        assert session.query(AppUser).count() == 2


def test_deactivated_member_cannot_read_or_save_preferences(client):
    _ok(client.post('/api/users', json={'telegram_user_id': MEMBER}))
    _ok(client.delete(f'/api/users/{MEMBER}'))
    app.dependency_overrides[_current_user_id] = lambda: MEMBER
    assert client.get('/api/preferences').status_code == 403
    assert client.patch('/api/preferences', json={'language': 'en'}).status_code == 403
    with session_scope() as session:
        member = session.get(AppUser, MEMBER)
        assert member.language == 'es' and not member.is_active


@pytest.mark.parametrize('payload', [
    {'telegram_user_id': True}, {'telegram_user_id': -1},
    {'telegram_user_id': MEMBER, 'role': 'owner'}, {'telegram_user_id': '42'},
])
def test_bad_grant_payload_cannot_promote_or_create_users(client, payload):
    assert client.post('/api/users', json=payload).status_code == 400
    with session_scope() as session:
        assert session.query(AppUser).count() == 0


def _send_as(runtime, text, user_id):
    message = MagicMock()
    message.text = text
    message.chat_id = user_id
    message.message_id = 1
    message.chat.send_action = AsyncMock()
    message.reply_text = AsyncMock()
    message.reply_voice = AsyncMock()
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_message = message
    with patch('app.bot.handlers.today_in_tz', return_value=date(2026, 11, 20)):
        _run(handle_command(update, None, BotDependencies(runtime=runtime)))
    return message.reply_text.call_args.args[0] if message.reply_text.call_args else ''


def test_bot_members_keep_currency_cycles_and_expense_categories_separate(runtime, in_memory_db):
    owner = runtime.settings.telegram_allowed_user_id
    with session_scope() as session:
        UserService(session, settings=runtime.settings).authorize_actor(owner, MEMBER)
        ids = dict(session.execute(select(Category.name, Category.id)).all())
        for uid, name, amount, category in ((owner, 'Owner coffee', '100', 'Comida'),
                                             (MEMBER, 'Member bus', '200', 'Transporte')):
            ExpenseRepository(session).create(ExpenseCreate(
                telegram_user_id=uid, name=name, amount=Decimal(amount), currency='ARS',
                category_id=ids[category], expense_date=date(2026, 11, 20),
                original_message='isolated fixture', ai_confidence=Decimal('1'),
            ))
    assert 'Ingreso base' in _send_as(runtime, '/ingreso 1000 ARS', owner)
    assert 'Ingreso base' in _send_as(runtime, '/ingreso 50 USD', MEMBER)
    with session_scope() as session:
        owner_cycle = CycleService(session).get_current_cycle(owner, date(2026, 11, 20), currency='ARS')
        member_cycle = CycleService(session).get_current_cycle(MEMBER, date(2026, 11, 20), currency='USD')
        assert owner_cycle is not None and member_cycle is not None
        assert owner_cycle.cycle_id != member_cycle.cycle_id
        assert MoneyEngine(session).get_cycle_summary(owner, owner_cycle.cycle_id).base_income.to_dict() == {'ARS': '1000.00'}
        assert MoneyEngine(session).get_cycle_summary(MEMBER, member_cycle.cycle_id).base_income.to_dict() == {'USD': '50.00'}
        assert session.query(Expense).count() == 2
    owner_report = _send_as(runtime, '/desglose', owner)
    member_report = _send_as(runtime, '/desglose', MEMBER)
    assert 'Comida' in owner_report and 'Transporte' not in owner_report
    assert 'Transporte' in member_report and 'Comida' not in member_report


def test_bot_language_preferences_apply_only_to_the_sending_member(runtime, in_memory_db):
    owner = runtime.settings.telegram_allowed_user_id
    with session_scope() as session:
        UserService(session, settings=runtime.settings).authorize_actor(owner, MEMBER)
    assert 'Language updated' in _send_as(runtime, '/idioma en', MEMBER)
    assert 'Voice replies disabled' in _send_as(runtime, '/voz off', MEMBER)
    assert 'How can I help?' in _send_as(runtime, '/help', MEMBER)
    assert '¿Cómo te puedo ayudar?' in _send_as(runtime, '/help', owner)
    with session_scope() as session:
        assert UserService(session, settings=runtime.settings).get_preferences(owner).language == 'es'
        assert session.query(FinancialCycle).count() == 0


def test_unknown_telegram_user_cannot_enroll_by_preferences_command(runtime, in_memory_db):
    _send_as(runtime, '/idioma en', MEMBER)
    with session_scope() as session:
        assert session.query(AppUser).count() == 0
        assert session.query(FinancialCycle).count() == 0


def test_bot_money_reply_uses_selected_language_and_preserves_currency(runtime, in_memory_db):
    owner = runtime.settings.telegram_allowed_user_id
    assert 'Language updated' in _send_as(runtime, '/idioma en', owner)
    reply = _send_as(runtime, '/ingreso 1000 USD', owner)
    assert 'Base income updated' in reply
    assert 'Available:' in reply
    assert 'Committed savings:' in reply
    assert 'USD' in reply
    assert 'Ingreso base' not in reply
