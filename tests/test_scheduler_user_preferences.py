import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.categories.models import Category
from app.config.settings import Settings
from app.database.database import session_scope
from app.metrics import Metrics
from app.recurring import scheduler
from app.recurring.models import RecurringExpense, RecurringReminder
from app.recurring.repository import RecurringRepository
from app.recurring.schemas import RecurringCreate
from app.users import UserService


OWNER, MEMBER = 42, 81


@pytest.fixture
def runtime(in_memory_db, monkeypatch):
    settings = Settings(_env_file=None, TELEGRAM_BOT_TOKEN='test-token',
                        TELEGRAM_ALLOWED_USER_ID=OWNER, TIMEZONE='UTC')
    runtime = SimpleNamespace(settings=settings)
    with session_scope() as session:
        UserService(session, settings=settings).ensure_owner()
    monkeypatch.setattr(scheduler, 'metrics', Metrics())
    return runtime


def _authorize(runtime, *, language='es', tz='UTC'):
    with session_scope() as session:
        users = UserService(session, settings=runtime.settings)
        users.authorize_actor(OWNER, MEMBER)
        users.set_preferences(MEMBER, language=language, timezone=tz)


def _recurring(user_id, due=date(2026, 11, 2), *, name='Disponible Café *exact*', currency='ARS'):
    with session_scope() as session:
        category_id = session.execute(select(Category.id).limit(1)).scalar_one()
        row = RecurringRepository(session).create(RecurringCreate(
            telegram_user_id=user_id, name=name, amount=Decimal('1234.56'), currency=currency,
            category_id=category_id, frequency='monthly', day_of_month=due.day,
            month_of_year=None, next_due_date=due,
        ))
        return row.id


def _bot():
    return SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=101))))


def _clock(monkeypatch, instant):
    clock = [instant]
    monkeypatch.setattr(scheduler, '_now_in_tz', lambda tz: clock[0].astimezone(ZoneInfo(tz)))
    return clock


@pytest.mark.asyncio
async def test_loop_uses_each_active_users_morning_even_when_global_clock_is_afternoon(runtime, monkeypatch):
    _authorize(runtime, language='pt', tz='America/Argentina/Buenos_Aires')
    _recurring(OWNER)
    _recurring(MEMBER)
    _clock(monkeypatch, datetime(2026, 11, 2, 12, 10, tzinfo=timezone.utc))
    app = _bot()

    async def cancel_after_tick(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(scheduler.asyncio, 'sleep', cancel_after_tick)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.recurring_reminder_loop(app, runtime)
    app.bot.send_message.assert_awaited_once()
    message = app.bot.send_message.call_args.kwargs
    assert message['chat_id'] == MEMBER
    assert '*Lembrete*' in message['text']
    assert 'Disponible Café *exact*' in message['text']
    assert '1.234,56 ARS' in message['text']
    for keyword in ('*sim*', '*amanhã*', '*em 3 dias*', '*next friday*'):
        assert keyword in message['text']


@pytest.mark.asyncio
async def test_due_date_is_local_to_the_user_not_the_configured_owners_day(runtime, monkeypatch):
    _authorize(runtime, language='en', tz='Asia/Tokyo')
    due = date(2026, 11, 3)
    _recurring(MEMBER, due, currency='USD')
    _clock(monkeypatch, datetime(2026, 11, 3, 0, 10, tzinfo=timezone.utc))
    app = _bot()
    await scheduler._fire_due_reminders(app, runtime, {})
    message = app.bot.send_message.call_args.kwargs
    assert message['chat_id'] == MEMBER and '*Reminder*' in message['text']
    assert 'USD' in message['text'] and 'Disponible Café *exact*' in message['text']
    with session_scope() as session:
        assert session.query(RecurringReminder).one().occurrence_date == due


@pytest.mark.asyncio
@pytest.mark.parametrize('hour,minute,second,sends', [
    (8, 59, 59, 0), (9, 0, 0, 1), (9, 29, 59, 1), (9, 30, 0, 1), (9, 30, 1, 0),
])
async def test_each_notification_window_has_explicit_boundaries(runtime, monkeypatch, hour, minute, second, sends):
    _recurring(OWNER)
    _clock(monkeypatch, datetime(2026, 11, 2, hour, minute, second, tzinfo=timezone.utc))
    app = _bot()
    await scheduler._fire_due_reminders(app, runtime, {})
    assert app.bot.send_message.await_count == sends


@pytest.mark.asyncio
async def test_unknown_and_deactivated_users_never_get_reminders_or_delivery_rows(runtime, monkeypatch):
    _authorize(runtime)
    for user_id in (MEMBER, 99):
        _recurring(user_id)
    with session_scope() as session:
        UserService(session, settings=runtime.settings).deactivate_actor(OWNER, MEMBER)
    _clock(monkeypatch, datetime(2026, 11, 2, 9, 10, tzinfo=timezone.utc))
    app = _bot()
    cache = {MEMBER: '2026-11-02', 99: '2026-11-02'}
    await scheduler._fire_due_reminders(app, runtime, cache)
    for user_id in (MEMBER, 99):
        await scheduler._try_send_for_user(app, runtime, user_id, date(2026, 11, 2), '2026-11-02', cache)
    app.bot.send_message.assert_not_awaited()
    assert cache == {}
    with session_scope() as session:
        assert session.query(RecurringReminder).count() == 0


@pytest.mark.asyncio
async def test_access_is_checked_again_after_claim_and_before_sending(runtime, monkeypatch):
    _authorize(runtime)
    _recurring(MEMBER)
    original = scheduler._authorized_preferences
    calls = 0

    def revoke_before_delivery(runtime, user_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            with session_scope() as session:
                UserService(session, settings=runtime.settings).deactivate_actor(OWNER, MEMBER)
        return original(runtime, user_id)

    monkeypatch.setattr(scheduler, '_authorized_preferences', revoke_before_delivery)
    app = _bot()
    cache = {}
    await scheduler._try_send_for_user(app, runtime, MEMBER, date(2026, 11, 2), '2026-11-02', cache)
    app.bot.send_message.assert_not_awaited()
    assert cache == {}
    with session_scope() as session:
        reminder = session.query(RecurringReminder).one()
        assert reminder.delivery_status == 'pending' and reminder.attempt_count == 1


@pytest.mark.asyncio
async def test_language_change_after_claim_uses_fresh_preference_for_delivery(runtime, monkeypatch):
    _authorize(runtime)
    _recurring(MEMBER)
    original = scheduler._authorized_preferences
    calls = 0

    def update_before_delivery(runtime, user_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            with session_scope() as session:
                UserService(session, settings=runtime.settings).set_preferences(MEMBER, language='en')
        return original(runtime, user_id)

    monkeypatch.setattr(scheduler, '_authorized_preferences', update_before_delivery)
    app = _bot()
    await scheduler._try_send_for_user(app, runtime, MEMBER, date(2026, 11, 2), '2026-11-02', {})
    text = app.bot.send_message.call_args.kwargs['text']
    assert '*Reminder*' in text
    assert 'Disponible Café *exact*' in text
    for keyword in ('*yes*', '*tomorrow*', '*in 3 days*', '*next friday*'):
        assert keyword in text


@pytest.mark.asyncio
async def test_new_timezone_outside_morning_releases_claim_before_delivery(runtime, monkeypatch):
    _authorize(runtime, tz='America/Argentina/Buenos_Aires')
    _recurring(MEMBER)
    _clock(monkeypatch, datetime(2026, 11, 2, 12, 10, tzinfo=timezone.utc))
    original = scheduler._authorized_preferences
    calls = 0

    def change_zone_before_delivery(runtime, user_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            with session_scope() as session:
                UserService(session, settings=runtime.settings).set_preferences(MEMBER, timezone='UTC')
        return original(runtime, user_id)

    monkeypatch.setattr(scheduler, '_authorized_preferences', change_zone_before_delivery)
    app = _bot()
    await scheduler._fire_due_reminders(app, runtime, {})
    app.bot.send_message.assert_not_awaited()
    with session_scope() as session:
        assert session.query(RecurringReminder).one().delivery_status == 'pending'


@pytest.mark.asyncio
async def test_one_users_night_does_not_clear_another_users_fired_cache(runtime, monkeypatch):
    _authorize(runtime, tz='America/Argentina/Buenos_Aires')
    _recurring(MEMBER)
    _clock(monkeypatch, datetime(2026, 11, 2, 12, 10, tzinfo=timezone.utc))
    app = _bot()
    cache = {}
    await scheduler._fire_due_reminders(app, runtime, cache)
    assert cache == {MEMBER: '2026-11-02'}
    await scheduler._fire_due_reminders(app, runtime, cache)
    assert cache == {MEMBER: '2026-11-02'}
    app.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_send_remains_retryable_in_the_same_morning_and_records_bounded_metrics(runtime, monkeypatch):
    _recurring(OWNER)
    _clock(monkeypatch, datetime(2026, 11, 2, 9, 10, tzinfo=timezone.utc))
    app = _bot()
    app.bot.send_message.side_effect = [TimeoutError('test timeout'), SimpleNamespace(message_id=102)]
    cache = {}
    await scheduler._fire_due_reminders(app, runtime, cache)
    assert cache == {}
    await scheduler._fire_due_reminders(app, runtime, cache)
    assert cache == {OWNER: '2026-11-02'}
    assert app.bot.send_message.await_count == 2
    report = scheduler.metrics.render()
    assert 'operation="reminder",status="ok"} 1' in report
    assert 'operation="reminder",status="error"} 1' in report
    assert 'user_id=' not in report and 'chat_id=' not in report
    assert 'Disponible Café' not in report and '1234.56' not in report
    with session_scope() as session:
        reminder = session.query(RecurringReminder).one()
        assert reminder.attempt_count == 2 and reminder.delivery_status == 'sent'


@pytest.mark.asyncio
async def test_deactivation_during_first_delivery_prevents_second_template_send(runtime):
    _authorize(runtime)
    _recurring(MEMBER, name='First')
    second = _recurring(MEMBER, name='Second')
    app = _bot()

    async def deliver_and_deactivate(**kwargs):
        with session_scope() as session:
            UserService(session, settings=runtime.settings).deactivate_actor(OWNER, MEMBER)
        return SimpleNamespace(message_id=201)

    app.bot.send_message.side_effect = deliver_and_deactivate
    await scheduler._try_send_for_user(app, runtime, MEMBER, date(2026, 11, 2), '2026-11-02', {})
    app.bot.send_message.assert_awaited_once()
    with session_scope() as session:
        assert session.query(RecurringReminder).count() == 1
        assert session.query(RecurringReminder).filter_by(recurring_id=second).count() == 0


def test_yearly_reminder_localizes_date_and_retains_user_name_and_amount(runtime):
    template = SimpleNamespace(frequency='yearly', month_of_year=11, day_of_month=2,
                               name='Comida Disponible *literal*', amount=Decimal('5.25'), currency='USD')
    for language, when in (('es', '2 de noviembre'), ('en', 'November 2'), ('pt', '2 de novembro')):
        text = scheduler._format_reminder(template, language=language)
        assert when in text
        assert 'Comida Disponible *literal*' in text
        assert '5,25 USD' in text
