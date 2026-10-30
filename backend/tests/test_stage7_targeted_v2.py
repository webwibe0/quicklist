"""Targeted V2 regressions: fixed Moscow clock, no live API in pytest."""
from datetime import date, datetime, time
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.provider import IntentParser, ProviderError
from app.main import app
from app.models.entities import Event
from app.schemas.commands import ParsedCommand
from app.temporal.resolver import TemporalAmbiguity, TemporalResolver
from tests.fakes import FakeLLMProvider
from tests.test_integration import client_for

NOW=datetime.fromisoformat('2026-10-20T12:00:00+03:00')
resolver=TemporalResolver('Europe/Moscow')

@pytest.mark.parametrize('expression',['25 число','25-е','25 числа','на двадцать пятое'])
def test_monthless_day_is_nearest_upcoming_date(expression):
    assert resolver.resolve_date(expression,NOW)[0]==date(2026,10,25)

@pytest.mark.parametrize('now,expression,direction,expected',[
    ('2026-10-26T12:00:00+03:00','25 число',None,'2026-11-25'),
    ('2026-10-20T12:00:00+03:00','25 число','past','2026-09-25'),
    ('2026-10-26T12:00:00+03:00','25 число','past','2026-10-25'),
    ('2026-12-31T12:00:00+03:00','1-е',None,'2027-01-01'),
    ('2026-02-20T12:00:00+03:00','31 число',None,'2026-03-31'),
    ('2026-03-01T12:00:00+03:00','31 числа','past','2026-01-31'),
    ('2026-02-20T12:00:00+03:00','28-е',None,'2026-02-28'),
    ('2026-10-20T22:30:00+00:00','20 число',None,'2026-11-20'),
    ('2026-10-20T12:00:00+03:00','25 числа следующего месяца',None,'2026-11-25'),
])
def test_monthless_day_direction_month_edges_and_timezone(now,expression,direction,expected):
    assert resolver.resolve_date(expression,datetime.fromisoformat(now),direction=direction)[0]==date.fromisoformat(expected)

@pytest.mark.parametrize('expression',['0 число','32-е','31 числа этого месяца'])
def test_invalid_day_is_clarified_instead_of_clamped(expression):
    with pytest.raises(TemporalAmbiguity):
        resolver.resolve_date(expression,NOW.replace(month=2))

@pytest.mark.parametrize('expression,start,end',[
    ('на неделю','2026-10-20','2026-10-26'),
    ('на ближайшую неделю','2026-10-20','2026-10-26'),
    ('на 7 дней','2026-10-20','2026-10-26'),
    ('на ближайшие 7 дней','2026-10-20','2026-10-26'),
    ('на этой неделе','2026-10-19','2026-10-25'),
    ('на следующей неделе','2026-10-26','2026-11-01'),
    ('на прошлой неделе','2026-10-12','2026-10-18'),
    ('за последнюю неделю','2026-10-13','2026-10-20'),
    ('за неделю','2026-10-13','2026-10-20'),
    ('на выходные','2026-10-24','2026-10-25'),
])
def test_rolling_calendar_and_history_periods_are_distinct(expression,start,end):
    assert resolver.resolve_period(expression,NOW)==(date.fromisoformat(start),date.fromisoformat(end))

@pytest.mark.parametrize('now,expected',[
    ('2026-10-25T12:00:00+03:00',('2026-10-25','2026-10-31')),
    ('2026-12-29T12:00:00+03:00',('2026-12-29','2027-01-04')),
    ('2026-10-19T23:30:00+00:00',('2026-10-20','2026-10-26')),
])
def test_rolling_week_edges_use_moscow_calendar(now,expected):
    assert resolver.resolve_period('на неделю',datetime.fromisoformat(now))==tuple(date.fromisoformat(d) for d in expected)

@pytest.mark.parametrize('expression,clock',[('через неделю','в 08:00'),('через два дня','в 8 утра'),('через час','в 13:00')])
def test_multi_action_normalization_preserves_explicit_clock(expression,clock):
    text=f'Напомни послезавтра в 16:00 поесть и {expression} {clock} позвонить'
    command=ParsedCommand(intent='CREATE_EVENT',actions=[
        ParsedCommand(intent='CREATE_EVENT',event_type='REMINDER',title='Поесть',date_expression='послезавтра',time_expression='в 16:00'),
        ParsedCommand(intent='CREATE_EVENT',event_type='REMINDER',title='Позвонить',date_expression=expression,time_expression=clock)])
    result=IntentParser(FakeLLMProvider({text:command.model_dump()})).parse(text,{})
    assert result.actions[1].time_expression==clock

@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(TemporalResolver,'local_now',lambda self,now=None:(now or NOW).astimezone(self.zone))
    monkeypatch.setattr('app.ai.openai_provider.OpenAI',lambda *a,**kw:pytest.fail('No live API in pytest'))
    yield
    app.dependency_overrides.clear()


def post(client,message,cid=None):
    response=client.post('/api/v1/assistant/messages',json=dict(message=message,conversation_id=cid))
    assert response.status_code==200,response.text
    return response.json()


def events(engine):
    with Session(engine) as session:
        return session.scalars(select(Event).order_by(Event.event_date,Event.event_time,Event.id)).all()


@pytest.mark.integration
@pytest.mark.parametrize('names',[['Тест А','Тест Б'],['Тест А','Тест Б','Тест В'],['Поесть','Поесть']])
def test_batch_confirmation_lists_each_title_once_per_object(db,integration_engine,fixed_clock,names):
    text='Напомни завтра '+ ' и '.join(f'в {8+i}:00 {name}' for i,name in enumerate(names))
    actions=[dict(intent='CREATE_EVENT',event_type='REMINDER',title=name,date_expression='завтра',time_expression=f'в {8+i}:00') for i,name in enumerate(names)]
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='CREATE_EVENT',actions=actions)})) as client:
        first=post(client,text)
        assert first['missing_fields']==['confirmation']
        for name in set(names):
            assert first['message'].count('«'+name+'»')==names.count(name)
        assert not events(integration_engine)
        result=post(client,'да',first['conversation_id'])
        assert len(result['events'])==len(names)
        post(client,'да',first['conversation_id'])
    assert len(events(integration_engine))==len(names)


@pytest.mark.integration
@pytest.mark.parametrize('same_title',[True,False])
@pytest.mark.parametrize('first_date',['послезавтра','22.10.2026'])
@pytest.mark.parametrize('reply',['да','нет','отмена'])
def test_mixed_relative_dates_keep_each_clock_and_atomic_confirmation(db,integration_engine,fixed_clock,same_title,first_date,reply):
    titles=['Поесть','Поесть' if same_title else 'Позвонить']
    text=f'Напомни {first_date} в 16:00 {titles[0]} и через неделю в 08:00 {titles[1]}'
    actions=[dict(intent='CREATE_EVENT',event_type='REMINDER',title=titles[0],date_expression=first_date,time_expression='в 16:00'),
        dict(intent='CREATE_EVENT',event_type='REMINDER',title=titles[1],date_expression='через неделю',time_expression='в 08:00')]
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='CREATE_EVENT',actions=actions)})) as client:
        first=post(client,text)
        assert first['missing_fields']==['confirmation']
        assert not events(integration_engine)
        result=post(client,reply,first['conversation_id'])
    assert not result['requires_input']
    assert [(e.title,e.event_date,e.event_time) for e in events(integration_engine)]==([
        (titles[0],date(2026,10,22),time(16)),(titles[1],date(2026,10,27),time(8))] if reply=='да' else [])


@pytest.mark.integration
@pytest.mark.parametrize('message,period,indices',[
    ('Покажи планы на неделю','на неделю',{1,2,3}),
    ('Покажи планы на этой неделе','на этой неделе',{0,1,2}),
    ('Покажи задачи на 25-е','на 25-е',{2}),
])
def test_list_ranges_include_exact_ids_and_preserve_statuses(db,integration_engine,fixed_clock,message,period,indices):
    seeded=[Event(type='task',title=f'Событие {day}',event_date=date(2026,10,day),status='pending',source='user') for day in [19,20,25,26,27]]
    db.add_all(seeded)
    db.commit()
    with client_for(integration_engine,FakeLLMProvider({message:dict(intent='LIST_EVENTS',period_expression=period)})) as client:
        result=post(client,message)
    assert not result['requires_input']
    assert {UUID(e['id']) for e in result['events']}=={seeded[i].id for i in indices}


@pytest.mark.integration
@pytest.mark.parametrize('answer',['да','нет','отмена'])
def test_named_delete_targets_require_one_frozen_confirmation(db,integration_engine,fixed_clock,answer):
    selected=[Event(type='reminder',title='проверить тест '+letter,event_date=date(2026,10,21),event_time=clock,source='user') for letter,clock in [('А',time(8)),('Б',time(19))]]
    outside=Event(type='reminder',title='проверить тест В',event_date=date(2026,10,21),event_time=time(20),source='user')
    db.add_all([*selected,outside])
    db.commit()
    text='Удали напоминания "проверить тест А" и "проверить тест Б"'
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='DELETE_EVENT',event_type='REMINDER',search_query='проверить тест А и проверить тест Б')})) as client:
        first=post(client,text)
        assert first['missing_fields']==['confirmation']
        assert {UUID(e['id']) for e in first['events']}=={e.id for e in selected}
        assert all(e.status=='pending' for e in events(integration_engine))
        post(client,answer,first['conversation_id'])
        post(client,'да',first['conversation_id'])
    assert {e.id for e in events(integration_engine) if e.status=='deleted'}==({e.id for e in selected} if answer=='да' else set())


@pytest.mark.integration
@pytest.mark.parametrize('utterance,period,day',[
    ('Покажи все события на этой неделе','на этой неделе',25),
    ('Покажи все события на следующей неделе','на следующей неделе',27),
    ('Покажи все прошедшие события за неделю','за неделю',19),
])
@pytest.mark.parametrize('status',['pending','completed','cancelled','deleted'])
def test_explicit_all_events_separates_status_from_temporal_direction(db,integration_engine,fixed_clock,utterance,period,day,status):
    event=Event(type='task',title='Событие',event_date=date(2026,10,day),event_time=time(8),status=status,source='user')
    db.add(event)
    db.commit()
    with client_for(integration_engine,FakeLLMProvider({utterance:dict(intent='LIST_EVENTS',period_expression=period)})) as client:
        result=post(client,utterance)
    assert not result['requires_input']
    assert [e['id'] for e in result['events']]==([str(event.id)] if status!='deleted' else [])


@pytest.mark.integration
def test_next_seven_calendar_dates_include_today_before_now(db,integration_engine,fixed_clock):
    event=Event(type='task',title='Утром сегодня',event_date=NOW.date(),event_time=time(8),source='user')
    db.add(event)
    db.commit()
    text='Покажи планы на следующие 7 дней'
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='LIST_EVENTS',period_expression='на следующие 7 дней')})) as client:
        result=post(client,text)
    assert [e['id'] for e in result['events']]==[str(event.id)]


def test_update_monthless_ordinal_is_a_date_not_candidate_number():
    text='Перенеси встречу на двадцать пятое'
    command=IntentParser(FakeLLMProvider({text:dict(intent='UPDATE_EVENT',search_query='встречу',date_expression='на двадцать пятое')})).parse(text,{})
    assert command.event_index is None


def test_named_delete_cannot_invent_a_second_target():
    text='Удали тест А и тест Б'
    with pytest.raises(ProviderError):
        IntentParser(FakeLLMProvider({text:dict(intent='DELETE_EVENT',delete_targets=['тест А','тест В'])})).parse(text,{})


@pytest.mark.integration
@pytest.mark.parametrize('change',['none','after-confirmation'])
def test_ambiguous_named_target_is_selected_without_losing_other_target(db,integration_engine,fixed_clock,change):
    a=Event(type='reminder',title='Тест А',event_date=date(2026,10,21),event_time=time(8),source='user')
    b1=Event(type='reminder',title='Тест Б',event_date=date(2026,10,21),event_time=time(19),source='user')
    b2=Event(type='reminder',title='Тест Б',event_date=date(2026,10,22),event_time=time(19),source='user')
    db.add_all([a,b1,b2])
    db.commit()
    text='Удали "Тест А" и "Тест Б"'
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='DELETE_EVENT',delete_targets=['Тест А','Тест Б'])})) as client:
        first=post(client,text)
        assert first['missing_fields']==['event_reference']
        assert {e['id'] for e in first['events']}=={str(b1.id),str(b2.id)}
        second=post(client,'1',first['conversation_id'])
        assert second['missing_fields']==['confirmation']
        assert {e['id'] for e in second['events']}=={str(a.id),str(b1.id)}
        if change!='none':
            with Session(integration_engine) as session:
                session.get(Event,a.id).title='Изменённый тест'
                session.commit()
        post(client,'да',first['conversation_id'])
    assert {e.id for e in events(integration_engine) if e.status=='deleted'}==({a.id,b1.id} if change=='none' else set())


@pytest.mark.integration
def test_missing_second_named_target_never_deletes_first(db,integration_engine,fixed_clock):
    a=Event(type='task',title='Тест А',source='user')
    db.add(a)
    db.commit()
    text='Удали "Тест А" и "Тест Б"'
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='DELETE_EVENT',delete_targets=['Тест А','Тест Б'])})) as client:
        result=post(client,text)
    assert not result['events'] and events(integration_engine)[0].status=='pending'


@pytest.mark.parametrize('selector',['1–5','1-5','с 1 по 5'])
def test_numeric_delete_ranges_keep_visible_boundaries(selector):
    from app.services.candidate_selection import candidate_indices
    assert candidate_indices(selector,5)==list(range(5))


@pytest.mark.integration
@pytest.mark.parametrize('now,dates',[
    ('2026-10-29T12:00:00+03:00',['2026-10-31','2026-11-05']),
    ('2026-12-29T12:00:00+03:00',['2026-12-31','2027-01-05']),
])
def test_multi_relative_dates_cross_month_and_year_without_clock_loss(db,integration_engine,fixed_clock,monkeypatch,now,dates):
    clock_now=datetime.fromisoformat(now)
    monkeypatch.setattr(TemporalResolver,'local_now',lambda self,now=None:(now or clock_now).astimezone(self.zone))
    text='Напомни послезавтра в 16:00 поесть и через неделю в 08:00 позвонить'
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='CREATE_EVENT',actions=[
        dict(intent='CREATE_EVENT',event_type='REMINDER',title='Поесть',date_expression='послезавтра',time_expression='в 16:00'),
        dict(intent='CREATE_EVENT',event_type='REMINDER',title='Позвонить',date_expression='через неделю',time_expression='в 08:00')])})) as client:
        first=post(client,text)
        assert first['missing_fields']==['confirmation']
        post(client,'да',first['conversation_id'])
    assert [(e.event_date.isoformat(),e.event_time) for e in events(integration_engine)]==[(dates[0],time(16)),(dates[1],time(8))]


@pytest.mark.integration
def test_named_delete_storage_error_is_atomic_and_confirmation_retryable(db,integration_engine,fixed_clock):
    from sqlalchemy import event as sqla_event
    from sqlalchemy.exc import IntegrityError
    db.add_all([Event(type='task',title='Тест '+letter,source='user') for letter in ['А','Б']])
    db.commit()
    text='Удали "Тест А" и "Тест Б"'
    changes=[]
    def fail_second(mapper,connection,target):
        if target.status=='deleted':
            changes.append(target.id)
            if len(changes)==2:
                raise IntegrityError('test-only failure',{},Exception('injected'))
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='DELETE_EVENT',delete_targets=['Тест А','Тест Б'])})) as client:
        first=post(client,text)
        sqla_event.listen(Event,'before_update',fail_second)
        try:
            failed=client.post('/api/v1/assistant/messages',json=dict(message='да',conversation_id=first['conversation_id']))
            assert failed.status_code==503
        finally:
            sqla_event.remove(Event,'before_update',fail_second)
        assert all(e.status=='pending' for e in events(integration_engine))
        assert len(post(client,'да',first['conversation_id'])['events'])==2


@pytest.mark.integration
def test_new_named_delete_does_not_inherit_partial_old_selection(db,integration_engine,fixed_clock):
    db.add_all([Event(type='task',title='Тест '+letter,source='user') for letter in ['А','Б','Б','В','Г']])
    db.commit()
    first_text='Удали "Тест А" и "Тест Б"'
    new_text='Удали "Тест В" и "Тест Г"'
    with client_for(integration_engine,FakeLLMProvider({first_text:dict(intent='DELETE_EVENT',delete_targets=['Тест А','Тест Б']),
        new_text:dict(intent='DELETE_EVENT',delete_targets=['Тест В','Тест Г'])})) as client:
        first=post(client,first_text)
        assert first['missing_fields']==['event_reference']
        second=post(client,new_text,first['conversation_id'])
        assert {e['title'] for e in second['events']}=={'Тест В','Тест Г'}
        post(client,'да',first['conversation_id'])
    assert {e.title for e in events(integration_engine) if e.status=='deleted'}=={'Тест В','Тест Г'}


@pytest.mark.integration
def test_two_named_occurrences_do_not_expand_to_whole_series(db,integration_engine,fixed_clock):
    from uuid import uuid4
    series=uuid4()
    db.add_all([Event(type='task',title='Тест '+letter,recurrence_series_id=series,recurrence_rule='FREQ=DAILY',source='user') for letter in ['А','Б','В']])
    db.commit()
    text='Удали "Тест А" и "Тест Б"'
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='DELETE_EVENT',delete_targets=['Тест А','Тест Б'])})) as client:
        first=post(client,text)
        assert first['missing_fields']==['confirmation']
        post(client,'да',first['conversation_id'])
    assert {e.title for e in events(integration_engine) if e.status=='deleted'}=={'Тест А','Тест Б'}


@pytest.mark.integration
def test_historical_monthless_day_resolves_month_before_query(db,integration_engine,fixed_clock):
    db.add_all([Event(type='task',title='Событие '+str(month),event_date=date(2026,month,25),source='user') for month in [9,10]])
    db.commit()
    text='Покажи прошедшие планы на 25 число'
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='LIST_EVENTS',period_expression='на 25 число')})) as client:
        result=post(client,text)
    assert [e['event_date'] for e in result['events']]==['2026-09-25']


@pytest.mark.parametrize('now,expression,direction,expected',[
    ('2026-10-20T12:00:00+03:00','последний день месяца',None,'2026-10-31'),
    ('2026-02-20T12:00:00+03:00','последнее число месяца',None,'2026-02-28'),
    ('2026-10-20T12:00:00+03:00','последний день месяца','past','2026-09-30'),
    ('2026-12-20T12:00:00+03:00','последний день следующего месяца',None,'2027-01-31'),
])
def test_last_calendar_day_is_computed_without_clamping_explicit_numbers(now,expression,direction,expected):
    assert resolver.resolve_date(expression,datetime.fromisoformat(now),direction=direction)[0]==date.fromisoformat(expected)



def test_last_day_of_month_is_not_a_historical_direction_marker():
    assert resolver.resolve_period('на последний день месяца',NOW)==(date(2026,10,31),date(2026,10,31))


@pytest.mark.parametrize('user_day,extracted',[('20 число','20 число'),('20-е','20-е'),('20 число','20-е')])
def test_monthless_day_guard_does_not_misread_day_as_clock(user_day,extracted):
    text='Какие планы на '+user_day+'?'
    command=IntentParser(FakeLLMProvider({text:dict(intent='LIST_EVENTS',period_expression=extracted)})).parse(text,{})
    assert resolver.resolve(command,NOW).period_start==date(2026,10,20)


def test_monthless_day_guard_still_rejects_invented_day():
    text='Какие планы на 20 число?'
    with pytest.raises(ProviderError):
        IntentParser(FakeLLMProvider({text:dict(intent='LIST_EVENTS',period_expression='21-е')})).parse(text,{})



def test_day_with_explicit_month_context_is_not_expanded_to_whole_month():
    assert resolver.resolve_period('на 25 числа следующего месяца',NOW)==(date(2026,11,25),date(2026,11,25))
