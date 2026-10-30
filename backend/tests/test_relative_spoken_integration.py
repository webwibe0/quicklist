"""Fresh HTTP/DB connections retain drafts and create exactly one owned event."""
from datetime import time

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.models.entities import Conversation, Event
from app.temporal.resolver import TemporalResolver
from tests.fakes import FakeLLMProvider
from tests.test_integration import client_for
from tests.test_relative_spoken_pipeline import NOW

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('answer,clock', [
    ('Через полчаса', time(12, 33)), ('В шестнадцать сорок восемь', time(16, 48)),
    ('В шестнадцать пятьдесят сегодня', time(16, 50)),
])
def test_time_patch_survives_new_http_and_postgresql_session(db, integration_engine, monkeypatch, answer, clock):
    monkeypatch.setattr(TemporalResolver, 'local_now', lambda self, now=None: (now or NOW).astimezone(self.zone))
    first = 'Создай напоминание сегодня почистить зубы'
    try:
        with client_for(integration_engine, FakeLLMProvider({first: {
            'intent': 'CREATE_EVENT', 'event_type': 'REMINDER', 'title': 'почистить зубы', 'date_expression': 'сегодня',
        }})) as client:
            r = client.post('/api/v1/assistant/messages', json={'message': first})
            assert r.status_code == 200 and r.json()['missing_fields'] == ['time']
            conversation_id = r.json()['conversation_id']
        with Session(integration_engine) as session:
            assert session.get(Conversation, conversation_id).pending_action['command']['title'] == 'почистить зубы'
            assert not session.scalars(select(Event)).all()
        with client_for(integration_engine, FakeLLMProvider({})) as client:
            r = client.post('/api/v1/assistant/messages', json={'message': answer, 'conversation_id': conversation_id})
            assert r.status_code == 200 and not r.json()['requires_input'], r.text
        with Session(integration_engine) as session:
            event, = session.scalars(select(Event)).all()
            assert event.event_time == clock and event.event_date == NOW.date() and event.owner_id is not None
            assert event.title == 'почистить зубы'
            assert session.get(Conversation, conversation_id).pending_action is None
    finally:
        app.dependency_overrides.clear()
