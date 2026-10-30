"""PostgreSQL search bounds, constraint migration and actions with no live LLM."""
from datetime import date, time, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.main import app
from app.models.entities import Conversation, Event
from app.temporal.resolver import TemporalResolver
from tests.fakes import FakeLLMProvider
from tests.test_dialogue_reliability import FRIDAY
from tests.test_integration import client_for

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def fixed_moscow_clock(monkeypatch):
    monkeypatch.setattr(TemporalResolver, "local_now", lambda self, now=None: (now or FRIDAY).astimezone(self.zone))
    yield
    app.dependency_overrides.clear()


def events_for_search(db):
    specs = [
        ("yesterday", date(2026, 10, 15), time(19), time(20)),
        ("ended", FRIDAY.date(), time(9), time(10)),
        ("ends_now", FRIDAY.date(), time(11), time(12)),
        ("ongoing", FRIDAY.date(), time(11), time(13)),
        ("starts_now", FRIDAY.date(), time(12), time(13)),
        ("later", FRIDAY.date(), time(15), time(16)),
        ("tomorrow", date(2026, 10, 17), time(9), time(10)),
        ("uncertain_today", FRIDAY.date(), None, None),
        ("uncertain_yesterday", date(2026, 10, 15), None, None),
        ("undated_task", None, None, None),
    ]
    rows = {name: Event(type="task" if name == "undated_task" else "meeting",
                        title="Встреча с Иваном", event_date=day, event_time=start, end_time=end)
            for name, day, start, end in specs}
    rows["deleted"] = Event(type="meeting", title="Встреча с Иваном", event_date=date(2026, 10, 15),
                             event_time=time(9), status="deleted")
    db.add_all(rows.values())
    db.commit()
    return rows


@pytest.mark.parametrize("text,expected", [
    ("Когда у меня была встреча с Иваном?", {"yesterday", "ended", "ends_now", "uncertain_yesterday"}),
    ("Когда будет встреча с Иваном?", {"later", "tomorrow"}),
    ("Найди встречу с Иваном", {"yesterday", "ended", "ends_now", "ongoing", "starts_now",
                                 "later", "tomorrow", "uncertain_today", "uncertain_yesterday"}),
])
def test_search_tense_uses_event_boundaries_before_limiting_rows(db, integration_engine, text, expected):
    rows = events_for_search(db)
    with client_for(integration_engine, FakeLLMProvider({text: {
        "intent": "SEARCH_EVENTS", "event_type": "MEETING", "search_query": "Иваном",
    }})) as client:
        response = client.post("/api/v1/assistant/messages", json={"message": text})
        assert response.status_code == 200, response.text
        result = response.json()
        assert not result["requires_input"]
        assert {x["id"] for x in result["events"]} == {str(rows[key].id) for key in expected}


@pytest.mark.parametrize("past", [True, False])
def test_tense_search_returns_empty_without_falling_back_to_other_period(db, integration_engine, past):
    day = FRIDAY.date() + timedelta(days=1 if past else -1)
    db.add(Event(type="meeting", title="Встреча с Иваном", event_date=day, event_time=time(10)))
    db.commit()
    text = "Когда была встреча с Иваном?" if past else "Когда будет встреча с Иваном?"
    with client_for(integration_engine, FakeLLMProvider({text: {
        "intent": "SEARCH_EVENTS", "event_type": "MEETING", "search_query": "Иваном",
    }})) as client:
        result = client.post("/api/v1/assistant/messages", json={"message": text}).json()
        assert not result["events"] and "не найдено" in result["message"]


def test_tense_respects_cancelled_and_completed_without_changing_neutral_history(db, integration_engine):
    db.add_all([
        Event(type="meeting", title="Встреча с Иваном", event_date=date(2026, 10, 15),
              event_time=time(10), status="cancelled"),
        Event(type="meeting", title="Встреча с Иваном", event_date=date(2026, 10, 17),
              event_time=time(10), status="completed"),
    ])
    db.commit()
    texts = ["Когда была встреча с Иваном?", "Когда будет встреча с Иваном?", "Найди встречи с Иваном"]
    provider = FakeLLMProvider({value: {
        "intent": "SEARCH_EVENTS", "event_type": "MEETING", "search_query": "Иваном",
    } for value in texts})
    with client_for(integration_engine, provider) as client:
        results = [client.post("/api/v1/assistant/messages", json={"message": value}).json() for value in texts]
        assert not results[0]["events"] and not results[1]["events"]
        assert len(results[2]["events"]) == 2


class NoParser:
    def parse_command(self, *args):
        pytest.fail("Card actions and consent must not use any LLM")


@pytest.mark.parametrize("source", ["user", "dataset"])
@pytest.mark.parametrize("event_type", ["task", "reminder", "meeting"])
def test_historical_card_actions_complete_idempotently_then_confirm_delete(db, integration_engine, source, event_type):
    event = Event(type=event_type, title="Историческая запись", event_date=date(2025, 1, 1),
                  event_time=time(10), source=source)
    db.add(event)
    db.commit()
    path = f"/api/v1/events/{event.id}/actions"
    with client_for(integration_engine, NoParser()) as client:
        first = client.post(path, json={"action": "COMPLETE_EVENT"}).json()
        assert first["events"][0]["status"] == "completed"
        if event_type == "meeting":
            assert "состоялась" in first["message"]
        with Session(integration_engine) as check:
            timestamp = check.get(Event, event.id).updated_at
        again = client.post(path, json={"action": "COMPLETE_EVENT"}).json()
        assert again["events"][0]["status"] == "completed"
        with Session(integration_engine) as check:
            assert check.get(Event, event.id).updated_at == timestamp
        question = client.post(path, json={"action": "DELETE_EVENT"}).json()
        assert question["missing_fields"] == ["confirmation"]
        with Session(integration_engine) as check:
            assert check.get(Event, event.id).status == "completed"
        last = client.post("/api/v1/assistant/messages", json={
            "conversation_id": question["conversation_id"], "message": "да",
        }).json()
        assert last["events"][0]["status"] == "deleted"
        # Stage 10 hides unavailable/deleted/foreign IDs with the same 404 contract.
        assert client.post(path, json={"action": "COMPLETE_EVENT"}).status_code == 404
        with Session(integration_engine) as check:
            assert check.get(Event, event.id).status == 'deleted'


def test_meeting_without_clock_roundtrips_json_then_persists_on_confirmation(db, integration_engine):
    text = "Поставь встречу с Иваном завтра"
    with client_for(integration_engine, FakeLLMProvider({text: {
        "intent": "CREATE_EVENT", "event_type": "MEETING", "title": "Встреча с Иваном",
        "date_expression": "завтра",
    }})) as client:
        first = client.post("/api/v1/assistant/messages", json={"message": text}).json()
    cid = first["conversation_id"]
    with client_for(integration_engine, NoParser()) as client:
        second = client.post("/api/v1/assistant/messages", json={"message": "без разницы", "conversation_id": cid}).json()
        assert second["missing_fields"] == ["confirmation"]
        with Session(integration_engine) as check:
            assert check.get(Conversation, UUID(cid)).pending_action is not None
            assert check.scalar(select(Event)) is None
        final = client.post("/api/v1/assistant/messages", json={"message": "да", "conversation_id": cid}).json()
        assert final["events"][0]["type"] == "meeting"
        assert final["events"][0]["event_time"] is None
    with Session(integration_engine) as check:
        saved = check.scalar(select(Event))
        assert saved.event_date == date(2026, 10, 17) and saved.event_time is None


@pytest.mark.parametrize("event_type,day", [("reminder", FRIDAY.date()), ("meeting", None)])
def test_migration_does_not_allow_missing_reminder_time_or_undated_meeting(db, event_type, day):
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(Event(type=event_type, title="Недопустимая запись", event_date=day))
        db.flush()


def test_downgrade_refuses_to_invent_time_or_drop_meetings(db, integration_engine):
    event = Event(type="meeting", title="Встреча без времени", event_date=FRIDAY.date())
    db.add(event)
    db.commit()
    original_revision = db.connection().scalar(text("SELECT version_num FROM alembic_version"))
    db.rollback()
    # Exercise the original 0004 guard directly: newer 0007/0008 correctly block a
    # destructive whole-schema downgrade before it can reach this historical guard.
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parents[1] / 'alembic/versions/0004_meeting_unspecified_time.py'
    spec = importlib.util.spec_from_file_location('meeting_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with Operations.context(MigrationContext.configure(db.connection())):
        with pytest.raises(RuntimeError, match="Schedule meetings"):
            migration.downgrade()
    with Session(integration_engine) as check:
        assert check.connection().scalar(text("SELECT version_num FROM alembic_version")) == original_revision
        assert check.get(Event, event.id).event_time is None
