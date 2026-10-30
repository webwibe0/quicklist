"""V3 multi-turn and destructive regression tests; PostgreSQL assistant_test only."""
from datetime import date, datetime, time
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.main import app
from app.models.entities import Conversation, Event
from app.temporal.resolver import TemporalResolver
from tests.fakes import FakeLLMProvider
from tests.test_integration import client_for

NOW=datetime.fromisoformat('2026-10-19T22:00:00+03:00')
pytestmark=pytest.mark.integration


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(TemporalResolver,'local_now',lambda self,now=None:(now or NOW).astimezone(self.zone))
    monkeypatch.setattr('app.ai.openai_provider.OpenAI',lambda *a,**kw:pytest.fail('No live API in pytest'))
    yield
    app.dependency_overrides.clear()


def post(client,message,cid=None):
    response=client.post('/api/v1/assistant/messages',json={'message':message,'conversation_id':cid})
    assert response.status_code==200,response.text
    return response.json()


def stored(engine):
    with Session(engine) as session:
        return session.scalars(select(Event).order_by(Event.event_date,Event.event_time,Event.id)).all()


def pending(engine,cid):
    with Session(engine) as session:
        return session.get(Conversation,UUID(cid)).pending_action


@pytest.mark.parametrize('answer',['Васе в 9 утра завтра','Васе завтра в 9 утра'])
def test_combined_recipient_date_time_fills_all_missing_slots(db,integration_engine,answer):
    initial='напомни мне позвонить ему'
    provider=FakeLLMProvider({initial:dict(intent='CREATE_EVENT',event_type='REMINDER',title='Позвонить ему'),
        answer:dict(intent='CREATE_EVENT',title='Васе',date_expression='завтра',time_expression='в 9 утра')})
    with client_for(integration_engine,provider) as client:
        first=post(client,initial)
        result=post(client,answer,first['conversation_id'])
    assert not result['requires_input']
    event=stored(integration_engine)[0]
    assert (event.title,event.event_date,event.event_time)==('Позвонить Васе',date(2026,10,20),time(9))
    assert pending(integration_engine,result['conversation_id']) is None


def test_independent_morphological_update_does_not_inherit_empty_list_date(db,integration_engine):
    event=Event(type='meeting',title='Обсудить бюджет по презентации',event_date=date(2026,10,20),event_time=time(16),end_time=time(17),source='user')
    db.add(event)
    db.commit()
    question='какие планы на 18 октября?'
    update='перенеси обсуждение бюджета на 2 часа вперед'
    with client_for(integration_engine,FakeLLMProvider({question:dict(intent='LIST_EVENTS',period_expression='18 октября'),
        update:dict(intent='UPDATE_EVENT',search_query='обсуждение бюджета',shift_expression='на 2 часа вперед')})) as client:
        previous=post(client,question)
        assert previous['events']==[]
        result=post(client,update,previous['conversation_id'])
    assert not result['requires_input']
    assert (stored(integration_engine)[0].event_time,stored(integration_engine)[0].end_time)==(time(18),time(19))


@pytest.mark.parametrize('answer,expected',[('все',5),('первые 3',3),('с 1 по 4',4),('1, 3 и 5',3)])
@pytest.mark.parametrize('consent',['да','нет'])
def test_visible_bulk_selection_is_frozen_confirmed_and_atomic(db,integration_engine,monkeypatch,answer,expected,consent):
    monkeypatch.setattr(get_settings(),'result_limit',5)
    db.add_all([Event(type='task',title='Чистить зубы',event_date=date(2026,10,20+i),source='user') for i in range(8)])
    db.commit()
    text='удали чистить зубы'
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='DELETE_EVENT',search_query='чистить зубы')})) as client:
        first=post(client,text)
        visible={e['id'] for e in first['events']}
        assert len(visible)==5
        second=post(client,answer,first['conversation_id'])
        assert second['missing_fields']==['confirmation']
        assert all(event.status=='pending' for event in stored(integration_engine))
        final=post(client,consent,first['conversation_id'])
    assert not final['requires_input']
    deleted={str(e.id) for e in stored(integration_engine) if e.status=='deleted'}
    assert len(deleted)==(expected if consent=='да' else 0)
    assert deleted<=visible
    assert pending(integration_engine,first['conversation_id']) is None


def test_multi_action_daypart_clarification_preserves_other_action(db,integration_engine):
    text='завтра в 8 напомни мне проснуться и вечером в 7 напомни почистить зубы'
    actions=[dict(intent='CREATE_EVENT',event_type='REMINDER',title='Проснуться',date_expression='завтра',time_expression='в 8'),
             dict(intent='CREATE_EVENT',event_type='REMINDER',title='Почистить зубы',time_expression='вечером в 7')]
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='CREATE_EVENT',actions=actions),
            'утра':dict(intent='CREATE_EVENT',time_expression='утра')})) as client:
        first=post(client,text)
        assert first['requires_input'] and first['missing_fields']==['time']
        assert not stored(integration_engine)
        second=post(client,'утра',first['conversation_id'])
        assert second['missing_fields']==['confirmation']
        assert not stored(integration_engine)
        result=post(client,'да',first['conversation_id'])
    assert not result['requires_input']
    assert [(e.title,e.event_date,e.event_time) for e in stored(integration_engine)]==[
        ('Проснуться',date(2026,10,20),time(8)),('Почистить зубы',date(2026,10,20),time(19))]


@pytest.mark.parametrize('sequential,last_clock',[(True,time(2)),(False,time(1))])
def test_relative_reminders_have_correct_anchor_across_midnight(db,integration_engine,sequential,last_clock):
    text='напомни пойти спать через час и '+('потом еще ' if sequential else '')+'через 3 часа'
    actions=[dict(intent='CREATE_EVENT',event_type='REMINDER',title='Пойти спать',date_expression='через час'),
             dict(intent='CREATE_EVENT',event_type='REMINDER',date_expression='через 3 часа',relative_anchor_expression='потом еще' if sequential else None)]
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='CREATE_EVENT',actions=actions)})) as client:
        first=post(client,text)
        assert first['missing_fields']==['confirmation']
        result=post(client,'да',first['conversation_id'])
    assert not result['requires_input']
    assert [(e.event_date,e.event_time) for e in stored(integration_engine)]==[
        (date(2026,10,19),time(23)),(date(2026,10,20),last_clock)]


@pytest.mark.parametrize('selector',['все','с 1 по 50'])
def test_two_hundred_matches_only_delete_fifty_visible(db,integration_engine,monkeypatch,selector):
    monkeypatch.setattr(get_settings(),'result_limit',50)
    db.add_all([Event(type='task',title='Чистить зубы',event_date=date(2026,10,20),source='user') for _ in range(200)])
    db.commit()
    text='удали чистить зубы'
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='DELETE_EVENT',search_query='чистить зубы')})) as client:
        first=post(client,text)
        visible={e['id'] for e in first['events']}
        assert len(visible)==50
        selected=post(client,selector,first['conversation_id'])
        assert selected['missing_fields']==['confirmation']
        assert len(selected['events'])==50
        result=post(client,'да',first['conversation_id'])
        assert '50' in result['message']
        assert not post(client,'да',first['conversation_id'])['events']
    assert {str(e.id) for e in stored(integration_engine) if e.status=='deleted'}==visible


@pytest.mark.parametrize('stage',['selection','confirmation'])
@pytest.mark.parametrize('change',['title','status'])
def test_changed_candidate_fails_closed(db,integration_engine,stage,change):
    db.add_all([Event(type='task',title='Чистить зубы',source='user') for _ in range(3)])
    db.commit()
    text='удали чистить зубы'
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='DELETE_EVENT',search_query='чистить зубы')})) as client:
        first=post(client,text)
        cid=first['conversation_id']
        if stage=='confirmation':
            assert post(client,'все',cid)['missing_fields']==['confirmation']
        with Session(integration_engine) as session:
            event=session.get(Event,UUID(first['events'][0]['id']))
            setattr(event,change,'Другое содержание' if change=='title' else 'completed')
            session.commit()
        result=post(client,'все' if stage=='selection' else 'да',cid)
    assert not any(e.status=='deleted' for e in stored(integration_engine))
    assert not result['events'] and pending(integration_engine,cid) is None


@pytest.mark.parametrize('answer',['нет','отмена'])
def test_multi_different_actions_decline_and_cancel_are_atomic(db,integration_engine,answer):
    text='напомни завтра в 8 утра проснуться и в 7 вечера почистить зубы'
    actions=[dict(intent='CREATE_EVENT',event_type='REMINDER',title='Проснуться',date_expression='завтра',time_expression='в 8 утра'),
        dict(intent='CREATE_EVENT',event_type='REMINDER',title='Почистить зубы',time_expression='в 7 вечера')]
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='CREATE_EVENT',actions=actions)})) as client:
        first=post(client,text)
        assert 'Проснуться' in first['message'] and 'Почистить зубы' in first['message']
        assert post(client,answer,first['conversation_id'])['events']==[]
        assert post(client,'да',first['conversation_id'])['events']==[]
    assert not stored(integration_engine)


@pytest.mark.parametrize('rule,interval,days',[
    ('каждый понедельник и пятницу',1,{0,4}),
    ('раз в две недели по четвергам',2,{3}),
    ('каждые 2 недели по вторникам и пятницам',2,{1,4})])
def test_recurrence_time_followup_preserves_rule_and_one_series(db,integration_engine,rule,interval,days):
    text='напоминай '+rule+' есть пиццу'
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='CREATE_EVENT',event_type='REMINDER',title='Есть пиццу',recurrence_expression=rule),
        'в 12:00':dict(intent='CREATE_EVENT',time_expression='в 12:00')})) as client:
        first=post(client,text)
        assert first['missing_fields']==['time']
        result=post(client,'в 12:00',first['conversation_id'])
    events=stored(integration_engine)
    assert not result['requires_input'] and len(events)>1
    assert len({e.recurrence_series_id for e in events})==1 and events[0].recurrence_series_id
    assert all(e.event_date.weekday() in days and e.event_time==time(12) for e in events)
    assert ('INTERVAL=2' in events[0].recurrence_rule)==(interval==2)



def test_multi_action_second_insert_failure_rolls_back_all_and_retry_succeeds(db,integration_engine,monkeypatch):
    from app.repositories.events import EventRepository
    text='напомни завтра в 8 утра проснуться и в 7 вечера поесть'
    actions=[dict(intent='CREATE_EVENT',event_type='REMINDER',title='Проснуться',date_expression='завтра',time_expression='в 8 утра'),
        dict(intent='CREATE_EVENT',event_type='REMINDER',title='Поесть',time_expression='в 7 вечера')]
    original=EventRepository.create
    calls=[]
    def fail_second(self,command):
        calls.append(command)
        if len(calls)==2:
            command=command.model_copy(update={'title':''})
        return original(self,command)
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='CREATE_EVENT',actions=actions)})) as client:
        first=post(client,text)
        monkeypatch.setattr(EventRepository,'create',fail_second)
        result=client.post('/api/v1/assistant/messages',json={'message':'да','conversation_id':first['conversation_id']})
        assert result.status_code==503 and not stored(integration_engine)
        assert pending(integration_engine,first['conversation_id'])['confirmation']
        monkeypatch.setattr(EventRepository,'create',original)
        assert len(post(client,'да',first['conversation_id'])['events'])==2


def test_bulk_delete_storage_failure_rolls_back_every_row(db,integration_engine,monkeypatch):
    from sqlalchemy import event as sqla_event
    from sqlalchemy.exc import IntegrityError
    db.add_all([Event(type='task',title='Чистить зубы',source='user') for _ in range(3)])
    db.commit()
    text='удали чистить зубы'
    count=[]
    def fail_second(mapper,connection,target):
        if target.status=='deleted':
            count.append(target.id)
            if len(count)==2:
                raise IntegrityError('test-only failure',{},Exception('injected'))
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='DELETE_EVENT',search_query='чистить зубы')})) as client:
        first=post(client,text)
        post(client,'все',first['conversation_id'])
        sqla_event.listen(Event,'before_update',fail_second)
        try:
            response=client.post('/api/v1/assistant/messages',json={'message':'да','conversation_id':first['conversation_id']})
            assert response.status_code==503
        finally:
            sqla_event.remove(Event,'before_update',fail_second)
        assert all(e.status=='pending' for e in stored(integration_engine))
        assert len(post(client,'да',first['conversation_id'])['events'])==3


@pytest.mark.parametrize('selection',['1','3','все'])
def test_bulk_selection_never_expands_mixed_series_implicitly(db,integration_engine,selection):
    from uuid import uuid4
    series=[uuid4(),uuid4()]
    for i in range(8):
        db.add(Event(type='task',title='Чистить зубы',source='user',event_date=date(2026,10,20+i),
            recurrence_series_id=series[i%2],recurrence_rule='FREQ=DAILY'))
    db.commit()
    text='удали чистить зубы'
    commands={text:dict(intent='DELETE_EVENT',search_query='чистить зубы')}
    with client_for(integration_engine,FakeLLMProvider(commands)) as client:
        first=post(client,text)
        cid=first['conversation_id']
        selected=post(client,selection,cid)
        if selection!='все':
            assert selected['missing_fields']==['recurrence_scope']
            selected=post(client,'только это событие',cid)
        assert selected['missing_fields']==['confirmation']
        result=post(client,'да',cid)
    assert len([e for e in stored(integration_engine) if e.status=='deleted'])==(8 if selection=='все' else 1)
    assert len(result['events'])==(8 if selection=='все' else 1)


@pytest.mark.parametrize('reply',['отмена','новая задача'])
def test_candidate_selection_cancel_or_independent_request_does_not_delete(db,integration_engine,reply):
    db.add_all([Event(type='task',title='Чистить зубы',source='user') for _ in range(3)])
    db.commit()
    text='удали чистить зубы'
    new='создай задачу купить хлеб'
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='DELETE_EVENT',search_query='чистить зубы'),
        new:dict(intent='CREATE_EVENT',event_type='TASK',title='Купить хлеб')})) as client:
        first=post(client,text)
        result=post(client,'отмена' if reply=='отмена' else new,first['conversation_id'])
    assert not any(e.status=='deleted' for e in stored(integration_engine))
    assert not result['requires_input']


def test_multi_pronoun_clarification_preserves_second_action(db,integration_engine):
    text='напомни завтра в 9 утра позвонить ему и в 7 вечера поесть'
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='CREATE_EVENT',actions=[
        dict(intent='CREATE_EVENT',event_type='REMINDER',title='Позвонить ему',date_expression='завтра',time_expression='в 9 утра'),
        dict(intent='CREATE_EVENT',event_type='REMINDER',title='Поесть',time_expression='в 7 вечера')]),
        'Васе':dict(intent='CREATE_EVENT',recipient_expression='Васе')})) as client:
        first=post(client,text)
        assert first['missing_fields']==['referent']
        second=post(client,'Васе',first['conversation_id'])
        assert second['missing_fields']==['confirmation']
        result=post(client,'да',first['conversation_id'])
    assert not result['requires_input']
    assert {e.title for e in stored(integration_engine)}=={'Позвонить Васе','Поесть'}



def test_relative_suffix_after_first_reminder_uses_first_datetime(db,integration_engine):
    text='напомни спать через час и через два часа после первого напоминания'
    with client_for(integration_engine,FakeLLMProvider({text:dict(intent='CREATE_EVENT',actions=[
        dict(intent='CREATE_EVENT',event_type='REMINDER',title='Спать',date_expression='через час'),
        dict(intent='CREATE_EVENT',event_type='REMINDER',date_expression='через два часа после первого напоминания')])})) as client:
        first=post(client,text)
        assert first['missing_fields']==['confirmation']
        post(client,'да',first['conversation_id'])
    assert [(e.event_date,e.event_time) for e in stored(integration_engine)]==[
        (date(2026,10,19),time(23)),(date(2026,10,20),time(1))]
