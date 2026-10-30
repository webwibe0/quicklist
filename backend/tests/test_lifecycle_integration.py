"""Real PostgreSQL enum, transactions, history visibility and card-action API tests."""
from datetime import time
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.main import app
from app.models.entities import Conversation, Event
from app.temporal.resolver import TemporalResolver
from tests.fakes import FakeLLMProvider
from tests.test_integration import client_for
from tests.test_manual_ux_regressions import NOW, TOMORROW

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clock_and_overrides(monkeypatch):
    monkeypatch.setattr(TemporalResolver, "local_now", lambda self, now=None: (now or NOW).astimezone(self.zone))
    yield
    app.dependency_overrides.clear()


class NoLLM:
    def parse_command(self, *args):
        pytest.fail("This API action must not call a live or fake parser")


def test_card_actions_survive_history_reload_and_cancel(db, integration_engine):
    event = Event(type="task", title="Проверить датчик", source="user")
    db.add(event)
    db.commit()
    with client_for(integration_engine, NoLLM()) as client:
        completed = client.post(f"/api/v1/events/{event.id}/actions", json={"action": "COMPLETE_EVENT"}).json()
        conversation_id = completed["conversation_id"]
        entries = client.get(f"/api/v1/conversations/{conversation_id}").json()["entries"]
        assert entries[-1]["response"]["events"][0]["status"] == "completed"
        deleting = client.post(f"/api/v1/events/{event.id}/actions", json={
            "action": "DELETE_EVENT", "conversation_id": conversation_id,
        }).json()
        entries = client.get(f"/api/v1/conversations/{conversation_id}").json()["entries"]
        assert entries[-1]["response"] == deleting
        client.post("/api/v1/assistant/messages", json={"message": "нет", "conversation_id": conversation_id})
        entries = client.get(f"/api/v1/conversations/{conversation_id}").json()["entries"]
        assert len(entries) == 6
        assert entries[1]["response"]["events"][0]["status"] == "completed"
    db.expire_all()
    assert db.get(Event, event.id).status == "completed"


@pytest.mark.parametrize("answer,status", [("да", "deleted"), ("нет", "pending"), ("отмена", "pending")])
def test_delete_confirmation_roundtrips_postgres_then_changes_list_search_visibility(db, integration_engine, answer, status):
    event = Event(type="meeting", title="Встреча с Димой", event_date=TOMORROW,
                  event_time=time(12), end_time=time(23), source="user")
    db.add(event)
    db.commit()
    text = "удали встречу с Димой завтра"
    with client_for(integration_engine, FakeLLMProvider({text: {
        "intent": "DELETE_EVENT", "event_type": "MEETING", "search_query": "Димой",
        "date_expression": "завтра",
    }})) as client:
        result = client.post("/api/v1/assistant/messages", json={"message": text})
        assert result.status_code == 200, result.text
        first = result.json()
        assert first["missing_fields"] == ["confirmation"]
    conversation_id = first["conversation_id"]
    with Session(integration_engine) as check:
        pending = check.get(Conversation, conversation_id).pending_action
        assert pending["confirmation"]["event_id"] == str(event.id)
        assert check.get(Event, event.id).status == "pending"
    with client_for(integration_engine, NoLLM()) as client:
        invalid = client.post("/api/v1/assistant/messages", json={
            "message": "без разницы", "conversation_id": conversation_id,
        })
        assert invalid.status_code == 200 and invalid.json()["missing_fields"] == ["confirmation"]
        result = client.post("/api/v1/assistant/messages", json={"message": answer, "conversation_id": conversation_id})
        assert result.status_code == 200 and not result.json()["requires_input"]
    with Session(integration_engine) as check:
        stored = check.get(Event, event.id)
        assert stored.status == status and stored.end_time == time(23)
        assert check.get(Conversation, conversation_id).pending_action is None
    commands = {
        "покажи завтра": {"intent": "LIST_EVENTS", "period_expression": "завтра"},
        "Найди Димой": {"intent": "SEARCH_EVENTS", "search_query": "Димой"},
    }
    with client_for(integration_engine, FakeLLMProvider(commands)) as client:
        for message in commands:
            response = client.post("/api/v1/assistant/messages", json={"message": message})
            assert response.status_code == 200, response.text
            assert [item["id"] for item in response.json()["events"]] == ([] if status == "deleted" else [str(event.id)])


def test_complete_card_api_is_idempotent_and_completed_stays_searchable(db, integration_engine):
    event = Event(type="task", title="Купить лекарства", event_date=TOMORROW, source="user")
    db.add(event)
    db.commit()
    path = f"/api/v1/events/{event.id}/actions"
    with client_for(integration_engine, NoLLM()) as client:
        first = client.post(path, json={"action": "COMPLETE_EVENT"})
        assert first.status_code == 200 and first.json()["events"][0]["status"] == "completed"
    with Session(integration_engine) as check:
        timestamp = check.get(Event, event.id).updated_at
    with client_for(integration_engine, NoLLM()) as client:
        second = client.post(path, json={"action": "COMPLETE_EVENT", "conversation_id": first.json()["conversation_id"]})
        assert second.status_code == 200 and second.json()["events"][0]["status"] == "completed"
    with Session(integration_engine) as check:
        assert check.get(Event, event.id).updated_at == timestamp
    with client_for(integration_engine, FakeLLMProvider({
        "покажи завтра": {"intent": "LIST_EVENTS", "period_expression": "завтра"},
        "найди лекарства": {"intent": "SEARCH_EVENTS", "search_query": "лекарства"},
    })) as client:
        assert client.post("/api/v1/assistant/messages", json={"message": "покажи завтра"}).json()["events"] == []
        history = client.post("/api/v1/assistant/messages", json={"message": "найди лекарства"}).json()
        assert history["events"][0]["status"] == "completed"


def test_delete_card_api_needs_confirmation_and_completed_user_event_can_be_deleted(db, integration_engine):
    event = Event(type="task", title="Готовая задача", status="completed", source="user")
    db.add(event)
    db.commit()
    with client_for(integration_engine, NoLLM()) as client:
        first = client.post(f"/api/v1/events/{event.id}/actions", json={"action": "DELETE_EVENT"})
        assert first.status_code == 200 and first.json()["requires_input"]
        with Session(integration_engine) as check:
            assert check.get(Event, event.id).status == "completed"
        last = client.post("/api/v1/assistant/messages", json={
            "message": "да", "conversation_id": first.json()["conversation_id"],
        })
        assert last.status_code == 200 and last.json()["events"][0]["status"] == "deleted"


def test_card_api_rejects_unknown_id_invalid_action_and_supports_meeting_dataset(db, integration_engine):
    meeting = Event(type="meeting", title="Зум", event_date=TOMORROW, event_time=time(12), source="user")
    imported = Event(type="task", title="Исходная задача", source="dataset", source_id="1")
    db.add_all([meeting, imported])
    db.commit()
    with client_for(integration_engine, NoLLM()) as client:
        assert client.post(f"/api/v1/events/{meeting.id}/actions", json={"action": "UPDATE_EVENT"}).status_code == 422
        assert client.post(f"/api/v1/events/{uuid4()}/actions", json={"action": "DELETE_EVENT"}).status_code == 404
        assert client.post(f"/api/v1/events/{meeting.id}/actions", json={"action": "COMPLETE_EVENT"}).json()["events"][0]["status"] == "completed"
        assert client.post(f"/api/v1/events/{imported.id}/actions", json={"action": "DELETE_EVENT"}).json()["requires_input"]
    with Session(integration_engine) as check:
        assert check.get(Event, meeting.id).status == "completed"
        assert check.get(Event, imported.id).status == "pending"
