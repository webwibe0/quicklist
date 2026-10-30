"""STT -> existing message API -> PostgreSQL, with both AI providers mocked."""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai.openai_provider import get_provider
from app.ai.speech_provider import get_speech_provider
from app.db.session import get_session
from app.main import app
from app.models.entities import Conversation, Event
from tests.fakes import FakeLLMProvider
from tests.test_voice import recording

pytestmark = pytest.mark.integration


@pytest.fixture
def voice_api(db, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Paid calls forbidden")
    monkeypatch.setattr("app.ai.openai_provider.OpenAI", forbidden)
    monkeypatch.setattr("app.ai.speech_provider.OpenAI", forbidden)
    nlu = FakeLLMProvider({})
    speech = MagicMock()
    def session():
        # Use an independent request session, matching production ownership.
        from sqlalchemy.orm import Session
        with Session(db.get_bind(), expire_on_commit=False) as request_session:
            yield request_session
    app.dependency_overrides.update({get_session: session, get_provider: lambda: nlu,
                                    get_speech_provider: lambda: speech})
    try:
        with TestClient(app) as client:
            yield client, nlu, speech
    finally:
        for dependency in (get_session, get_provider, get_speech_provider):
            app.dependency_overrides.pop(dependency, None)


def spoken(api, text, command=None, conversation_id=None):
    client, nlu, speech = api
    speech.transcribe.return_value = text
    if command is not None:
        nlu.commands[text] = command
    transcription = client.post("/api/v1/voice/transcriptions", content=recording(), headers={"Content-Type": "audio/wav"})
    assert transcription.status_code == 200
    reply = client.post("/api/v1/assistant/messages", json={
        "message": transcription.json()["text"], "conversation_id": conversation_id,
    })
    assert reply.status_code == 200, reply.text
    return reply.json()


def test_voice_clarification_reuses_persisted_conversation_and_task_conversion(voice_api, db):
    first = spoken(voice_api, "Напомни завтра позвонить Диме", {
        "intent": "CREATE_EVENT", "event_type": "REMINDER", "title": "Позвонить Диме", "date_expression": "завтра",
    })
    assert first["missing_fields"] == ["time"]
    conversation_id = first["conversation_id"]
    db.expire_all()  # Follow-up must work from persisted JSON, not a request-local draft.
    second = spoken(voice_api, "Без разницы", conversation_id=conversation_id)
    assert second["missing_fields"] == ["confirmation"]
    final = spoken(voice_api, "Да", conversation_id=conversation_id)
    assert final["conversation_id"] == conversation_id and not final["requires_input"]
    assert final["events"][0]["type"] == "task" and final["events"][0]["event_time"] is None
    assert final["events"][0]["title"] == "Позвонить Диме"
    assert len(db.scalars(select(Event)).all()) == 1
    assert db.scalar(select(Conversation)).pending_action is None


@pytest.mark.parametrize("answer,status", [("Да", "deleted"), ("Нет", "pending"), ("Отмена", "pending")])
def test_spoken_delete_consent_keeps_existing_safety(voice_api, db, answer, status):
    event = Event(type="task", title="Отправить отчёт")
    db.add(event)
    db.commit()
    first = spoken(voice_api, "Удали отправить отчёт", {"intent": "DELETE_EVENT", "search_query": "отправить отчёт"})
    assert first["missing_fields"] == ["confirmation"]
    db.refresh(event)
    assert event.status == "pending"
    spoken(voice_api, answer, conversation_id=first["conversation_id"])
    db.refresh(event)
    assert event.status == status


def test_stt_alone_never_changes_events_or_conversations(voice_api, db):
    client, _, provider = voice_api
    provider.transcribe.return_value = "Удали всё"
    assert client.post("/api/v1/voice/transcriptions", content=recording(), headers={"Content-Type": "audio/wav"}).status_code == 200
    assert db.scalar(select(Event)) is None and db.scalar(select(Conversation)) is None
