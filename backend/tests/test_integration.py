from datetime import date, time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from scripts.import_dataset import import_dataset
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.openai_provider import get_provider
from app.ai.provider import ProviderError
from app.db.session import get_session
from app.main import app
from app.models.entities import Conversation, Event
from app.repositories.events import EventRepository
from app.schemas.commands import ListEventsCommand, SearchEventsCommand
from tests.fakes import FakeLLMProvider

pytestmark = pytest.mark.integration

COMMANDS = {
    "Поставь завтра встречу с Иваном": {"intent": "CREATE_EVENT", "event_type": "MEETING",
                                      "title": "Встреча с Иваном", "date_expression": "завтра"},
    "В 15:00": {"intent": "CREATE_EVENT", "time_expression": "В 15:00"},
    "Что у меня завтра?": {"intent": "LIST_EVENTS", "period_expression": "завтра"},
    "Найди что-нибудь про Иванова": {"intent": "SEARCH_EVENTS", "search_query": "Иванова"},
}


def client_for(engine, provider):
    def session_dependency():
        with Session(engine, expire_on_commit=False) as session:
            yield session
    app.dependency_overrides[get_session] = session_dependency
    app.dependency_overrides[get_provider] = lambda: provider
    return TestClient(app)


def test_api_vertical_slice_survives_new_connection_and_search(db, integration_engine):
    provider = FakeLLMProvider(COMMANDS)
    with client_for(integration_engine, provider) as client:
        assert client.get("/health").status_code == 200
        result = client.post("/api/v1/assistant/messages", json={
            "message": "Поставь завтра встречу с Иваном",
        })
        assert result.status_code == 200, result.text
        first = result.json()
        assert first["requires_input"] and first["missing_fields"] == ["time"]
        conversation_id = first["conversation_id"]
        with Session(integration_engine) as check:
            assert check.scalar(select(func.count()).select_from(Event)) == 0
            assert check.get(Conversation, conversation_id).pending_action
    # New client and session simulate a backend restart: no in-process state is reused.
    with client_for(integration_engine, FakeLLMProvider(COMMANDS)) as client:
        result = client.post("/api/v1/assistant/messages", json={
            "conversation_id": conversation_id, "message": "В 15:00",
        })
        assert result.status_code == 200, result.text
        created = result.json()
        assert not created["requires_input"] and created["events"][0]["event_time"] == "15:00:00"
        event_id = created["events"][0]["id"]
        with Session(integration_engine) as check:
            assert check.scalar(select(Conversation.id).where(
                Conversation.id == conversation_id, Conversation.pending_action.is_(None),
            )) is not None
        listed = client.post("/api/v1/assistant/messages", json={
            "conversation_id": conversation_id, "message": "Что у меня завтра?",
        }).json()
        assert event_id in [event["id"] for event in listed["events"]]
        found = client.post("/api/v1/assistant/messages", json={
            "message": "Найди что-нибудь про Иванова",
        }).json()
        assert event_id in [event["id"] for event in found["events"]]
    app.dependency_overrides.clear()


def test_import_twice_no_duplicates_and_provenance(db):
    first = import_dataset(db)
    db.commit()
    second = import_dataset(db)
    db.commit()
    assert first["inserted"] == 1000 and second["inserted"] == 0
    assert db.scalar(select(func.count()).select_from(Event)) == 1000
    assert db.scalar(select(func.count()).select_from(Event).where(Event.type_inferred)) == 1000
    matches = EventRepository(db, 50).search(SearchEventsCommand(query="Иванова"))
    assert matches and all(event.source == "dataset" for event in matches)


def test_type_and_period_filters_inclusive_and_undated_tasks_excluded(db):
    db.add_all([
        Event(type="task", title="Задача без даты", source="user"),
        Event(type="meeting", title="Встреча с Иваном", event_date=date(2026, 10, 15), event_time=time(15)),
        Event(type="task", title="Договор Иванова", event_date=date(2026, 10, 15)),
        Event(type="meeting", title="Встреча с Иваном", event_date=date(2026, 10, 16), event_time=time(15)),
    ])
    db.commit()
    repo = EventRepository(db, 50)
    listed = repo.list_by_period(ListEventsCommand(start=date(2026, 10, 15), end=date(2026, 10, 15), event_type="MEETING"))
    assert len(listed) == 1
    found = repo.search(SearchEventsCommand(query="Иванова", start=date(2026, 10, 15), end=date(2026, 10, 15), event_type="TASK"))
    assert len(found) == 1 and found[0].title == "Договор Иванова"
    assert not repo.search(SearchEventsCommand(query="%' OR 1=1 --"))


def test_db_enforces_required_schedule(db):
    db.add(Event(type="meeting", title="Неполная встреча"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_period_range_search_shape_returns_filtered_list_without_query_clarification(db, integration_engine):
    meeting = Event(type="meeting", title="Планёрка", event_date=date(2026, 10, 18), event_time=time(15))
    db.add_all([
        meeting,
        Event(type="task", title="Отчёт", event_date=date(2026, 10, 18)),
        Event(type="meeting", title="Поздняя встреча", event_date=date(2026, 10, 21), event_time=time(15)),
    ])
    db.commit()
    message = "Покажи встречи с 16.10.2026 по 20.10.2026"
    provider = FakeLLMProvider({message: {
        "intent": "SEARCH_EVENTS", "event_type": "MEETING", "search_query": None,
        "period_expression": "с 16.10.2026 по 20.10.2026",
    }})
    try:
        with client_for(integration_engine, provider) as client:
            result = client.post("/api/v1/assistant/messages", json={"message": message})
            assert result.status_code == 200, result.text
            response = result.json()
            assert response["action"] == "LIST_EVENTS" and not response["requires_input"]
            assert response["missing_fields"] == []
            assert [event["id"] for event in response["events"]] == [str(meeting.id)]
        with Session(integration_engine) as check:
            assert check.scalar(select(func.count()).select_from(Event)) == 3
            assert check.get(Conversation, response["conversation_id"]).pending_action is None
    finally:
        app.dependency_overrides.clear()


def test_invalid_requests_and_provider_failure_do_not_write(db, integration_engine):
    class BrokenProvider:
        def parse_command(self, text, context):
            raise ProviderError("LLM временно недоступна.")
    with client_for(integration_engine, BrokenProvider()) as client:
        assert client.post("/api/v1/assistant/messages", json={"message": " "}).status_code == 422
        assert client.post("/api/v1/assistant/messages", json={"message": "x", "conversation_id": "bad"}).status_code == 422
        assert client.post("/api/v1/assistant/messages", json={"message": "x", "conversation_id": str(uuid4())}).status_code == 404
        assert client.post("/api/v1/assistant/messages", json={"message": "Встреча"}).status_code == 503
    app.dependency_overrides.clear()
    with Session(integration_engine) as check:
        assert check.scalar(select(func.count()).select_from(Event)) == 0
        assert check.scalar(select(func.count()).select_from(Conversation)) == 0
