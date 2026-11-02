from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.ai.service import StubAIService
from app.bot.service import BotRuntime, _process_message_async, _try_local_expense
from app.config.settings import Settings
from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.expenses.service import ExpenseDraft, ExpenseService
from app.queries.intents import QuerySpec
from app.queries.service import QueryService
from app.utils.dates import parse_date_range


def _seed(user, name, amount, when, currency='ARS'):
    with session_scope() as session:
        ExpenseService(ExpenseRepository(session)).register_many(user, [ExpenseDraft(
            name=name, amount=Decimal(amount), currency=currency, category='Café',
            expense_date=when, confidence=Decimal('1'),
        )], original_message='synthetic learning example')


@pytest.mark.parametrize('text,expected', [
    ('cuánto gasté del 5 al 15 de marzo', (date(2026,3,5),date(2026,3,15))),
    ('del 5 al 15 de marzo de 2025', (date(2025,3,5),date(2025,3,15))),
    ('desde 2025-12-29 hasta 2026-01-04', (date(2025,12,29),date(2026,1,4))),
    ('del 28 de enero al 5 de febrero de 2026', (date(2026,1,28),date(2026,2,5))),
    ('cuánto gasté hoy', None),
])
def test_explicit_inclusive_date_ranges(text, expected):
    assert parse_date_range(text, today=date(2026,11,2)) == expected


@pytest.mark.parametrize('text', ['del 15 al 5 de marzo', 'del 30 al 31 de febrero',
                                  'del 2026-04-05 al 2026-04-01'])
def test_invalid_ranges_rejected(text):
    with pytest.raises(ValueError):
        parse_date_range(text, today=date(2026,11,2))


def test_range_queries_keep_bounds_and_currencies(in_memory_db):
    for day, amount, currency in [(4,'999','ARS'),(5,'10','ARS'),(15,'20','ARS'),(16,'999','ARS'),(10,'5','USD')]:
        _seed(123456,'range example',amount,date(2026,3,day),currency)
    _seed(777,'other user','777',date(2026,3,10))
    with session_scope() as session:
        service = QueryService(ExpenseRepository(session), today=date(2026,11,2))
        spec = QuerySpec(kind='total', period='range', range_start=date(2026,3,5), range_end=date(2026,3,15))
        assert service.run(123456,spec).total_by_currency == {'ARS':Decimal('30'), 'USD':Decimal('5')}
        spec.currency='USD'
        assert service.run(123456,spec).total_by_currency == {'USD':Decimal('5')}


@pytest.mark.asyncio
async def test_natural_date_range_uses_deterministic_bounds(in_memory_db):
    text='cuánto gasté del 5 al 15 de marzo'
    _seed(123456,'in range','12',date(2026,3,15))
    _seed(123456,'outside','9999',date(2026,3,16))
    ai=StubAIService({text:{'type':'query','query':{'period':'month'},'expenses':[], 'confidence':1}})
    runtime=BotRuntime(Settings(telegram_bot_token='test-token',telegram_allowed_user_id=123456),ai)
    answer=await _process_message_async(runtime,text,user_id=123456,today=date(2026,11,2))
    assert '2026-03-05' in answer and '2026-03-15' in answer
    assert '12' in answer and '9.999' not in answer


@pytest.mark.asyncio
async def test_learned_simple_expense_skips_ai_and_keeps_exact_amount(in_memory_db):
    for _ in range(3):
        _seed(123456,'Cafe conocido','100',date(2026,11,1))
    ai=StubAIService()
    runtime=BotRuntime(Settings(telegram_bot_token='test-token',telegram_allowed_user_id=123456),ai)
    answer=await _process_message_async(runtime,'ayer gasté 3k en Cafe conocido',user_id=123456,today=date(2026,11,2))
    assert ai.calls == []
    assert '3.000' in answer
    with session_scope() as session:
        rows=ExpenseRepository(session).list_recent(123456,limit=10)
        assert len(rows)==4
        assert rows[0].amount==Decimal('3000') and rows[0].expense_date==date(2026,11,1)
    assert _try_local_expense(runtime,'gasté 5 en Desconocido',user_id=123456,today=date(2026,11,2)) is None
    assert _try_local_expense(runtime,'gasté 5 en Cafe conocido',user_id=777,today=date(2026,11,2)) is None


def test_snooze_options_preserve_obligation_due_date(in_memory_db):
    from tests.test_recurring_obligations import _recurring, USER
    from app.recurring.repository import RecurringRepository
    from app.recurring.service import RecurringService
    due=date(2026,11,2)
    recurring_id=_recurring(due)
    with session_scope() as session:
        service=RecurringService(RecurringRepository(session))
        row=service.postpone_occurrence(USER,recurring_id,occurrence_date=due,today=due,until=due+timedelta(days=3))
        assert row.next_due_date==date(2026,11,5)
        assert row.obligation_due_date==due
        with pytest.raises(ValueError):
            service.postpone_occurrence(USER,recurring_id,occurrence_date=due,today=due,until=due)


def test_metrics_histogram_counts_failures_and_success_without_data():
    from app.metrics import Metrics
    metrics=Metrics()
    metrics.operation('interpret',0.2)
    with pytest.raises(RuntimeError):
        with metrics.measure('receipt'):
            raise RuntimeError('private detail never becomes a label')
    rendered=metrics.render()
    assert 'private detail' not in rendered
    assert 'operation="receipt",status="error"' in rendered
    assert 'tge_operation_duration_seconds_bucket{operation="interpret",le="+Inf"} 1' in rendered
    assert rendered.endswith('\n')
