"""Stage 7: real PostgreSQL transactions and persisted dialogue, with scripted NLU."""
from copy import deepcopy
from datetime import date, time, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.main import app
from app.models.entities import Conversation, Event
from app.repositories.events import EventRepository
from app.schemas.commands import EventResponse
from app.temporal.resolver import TemporalResolver
from app.use_cases.actions import CreateEventUseCase
from tests.fakes import FakeLLMProvider
from tests.test_integration import client_for
from tests.test_manual_ux_regressions import NOW

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clock_and_overrides(monkeypatch):
    monkeypatch.setattr(TemporalResolver, "local_now", lambda self, now=None: (now or NOW).astimezone(self.zone))
    monkeypatch.setattr("app.ai.openai_provider.OpenAI", lambda *a, **kw: pytest.fail("No live OpenAI"))
    yield
    app.dependency_overrides.clear()


CREATE = "создай встречу с командой 20 октября в 10:00 каждый вторник 3 раза"
CREATE_PARSED = dict(intent="CREATE_EVENT", event_type="MEETING", title="Встреча с командой",
                     date_expression="20 октября", time_expression="в 10:00",
                     recurrence_expression="каждый вторник 3 раза")


def post(client, text, conversation_id=None):
    response = client.post("/api/v1/assistant/messages", json={"message": text, "conversation_id": conversation_id})
    assert response.status_code == 200, response.text
    return response.json()


def test_created_series_preserves_visible_order_for_ordinal_update(db, integration_engine):
    followup = 'Перенеси вторую из этих встреч на час позже, только эту встречу'
    provider = FakeLLMProvider({CREATE: CREATE_PARSED, followup: dict(
        intent='UPDATE_EVENT', event_reference='last_listed', event_index=2,
        recurrence_scope='occurrence', shift_expression='на час позже',
    )})
    with client_for(integration_engine, provider) as client:
        created = post(client, CREATE)
        identifiers = [event['id'] for event in created['events']]
        updated = post(client, followup, created['conversation_id'])
    assert not updated['requires_input']
    assert [event['id'] for event in updated['events']] == [identifiers[1]]
    assert [event.event_time for event in stored(integration_engine)] == [time(10), time(11), time(10)]


def test_future_occurrence_conflict_is_detected_before_any_series_row_is_written(db, integration_engine):
    db.add(Event(type="meeting", title="Внешняя встреча", event_date=date(2026, 10, 27),
                 event_time=time(10), source="user"))
    db.commit()
    with client_for(integration_engine, FakeLLMProvider({CREATE: CREATE_PARSED})) as client:
        result = post(client, CREATE)
    assert result["missing_fields"] == ["confirmation"]
    with Session(integration_engine) as check:
        assert len(check.scalars(select(Event)).all()) == 1
        pending = check.get(Conversation, UUID(result["conversation_id"])).pending_action
        assert pending["confirmation_kind"] == "conflict"


class NoLLM:
    def parse_command(self, *args):
        pytest.fail("Persisted confirmation/scope must not call the parser")


def stored(engine):
    with Session(engine) as session:
        return session.scalars(select(Event).order_by(Event.event_date, Event.event_time, Event.id)).all()


def pending_for(engine, conversation_id):
    with Session(engine) as session:
        return deepcopy(session.get(Conversation, UUID(conversation_id)).pending_action)


def seed_series(db, *, kind="meeting", end=time(11)):
    series_id = uuid4()
    dates = [date(2026, 10, 20), date(2026, 10, 27), date(2026, 11, 3)]
    events = [Event(type=kind, title="Встреча с командой", event_date=day,
                    event_time=time(10), end_time=end, source="user",
                    recurrence_rule="FREQ=WEEKLY;BYDAY=TU;COUNT=3", recurrence_series_id=series_id)
              for day in dates]
    db.add_all(events)
    db.flush()
    conversation = Conversation(last_event_id=events[0].id, context={
        "last_created_event": EventResponse.model_validate(events[0]).model_dump(mode="json"),
        "last_listed_events": [EventResponse.model_validate(event).model_dump(mode="json") for event in events],
        "last_event_scope": "last_created",
    })
    db.add(conversation)
    db.commit()
    return events, str(conversation.id)


@pytest.mark.parametrize("answer,expected", [("да", 4), ("нет", 1), ("отмена", 1)])
def test_create_conflict_confirmation_freezes_all_occurrences_and_single_series_id(
    db, integration_engine, monkeypatch, answer, expected,
):
    db.add(Event(type="meeting", title="Внешняя встреча", event_date=date(2026, 10, 27),
                 event_time=time(10), source="user"))
    db.commit()
    with client_for(integration_engine, FakeLLMProvider({CREATE: CREATE_PARSED})) as client:
        result = post(client, CREATE)
    pending = pending_for(integration_engine, result["conversation_id"])
    frozen = pending["confirmation"]["occurrences"]
    assert len(frozen) == 3
    series_id = frozen[0]["recurrence_series_id"]
    assert all(item["recurrence_series_id"] == series_id for item in frozen)
    monkeypatch.setattr(CreateEventUseCase, "expand", lambda *a, **kw: pytest.fail("No second expansion"))
    with client_for(integration_engine, NoLLM()) as client:
        response = post(client, answer, result["conversation_id"])
    assert not response["requires_input"]
    events = stored(integration_engine)
    assert len(events) == expected
    if answer == "да":
        series = [event for event in events if event.recurrence_series_id]
        assert {str(event.recurrence_series_id) for event in series} == {series_id}
        assert [event.event_date.isoformat() for event in series] == [item["event_date"] for item in frozen]
    assert pending_for(integration_engine, result["conversation_id"]) is None


def test_future_create_adjacency_is_allowed(db, integration_engine):
    db.add(Event(type="meeting", title="Внешняя встреча", event_date=date(2026, 10, 27),
                 event_time=time(11), end_time=time(12), source="user"))
    db.commit()
    message = CREATE.replace("в 10:00", "с 10:00 до 11:00")
    parsed = CREATE_PARSED | {"time_expression": "с 10:00 до 11:00"}
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        response = post(client, message)
    assert not response["requires_input"] and len(response["events"]) == 3
    assert len(stored(integration_engine)) == 4


@pytest.mark.parametrize("duplicates", [1, 2])
def test_future_duplicate_precedes_conflict_and_never_prompts_twice_for_exact_duplicates(
    db, integration_engine, duplicates,
):
    db.add_all([Event(type="meeting", title="Встреча с командой", event_date=date(2026, 10, 27),
                      event_time=time(10), source="user") for _ in range(duplicates)])
    db.commit()
    with client_for(integration_engine, FakeLLMProvider({CREATE: CREATE_PARSED})) as client:
        response = post(client, CREATE)
    assert pending_for(integration_engine, response["conversation_id"])["confirmation_kind"] == "duplicate"
    assert len(stored(integration_engine)) == duplicates
    with client_for(integration_engine, NoLLM()) as client:
        result = post(client, "да", response["conversation_id"])
    assert not result["requires_input"]
    assert len(stored(integration_engine)) == duplicates + 3


@pytest.mark.parametrize("kind", ["TASK", "REMINDER", "MEETING"])
def test_recurring_types_default_horizon_and_true_total_above_response_limit(db, integration_engine, monkeypatch, kind):
    monkeypatch.setattr(get_settings(), "result_limit", 5)
    message = "создай событие 20 октября в 10:00 каждый день"
    parsed = dict(intent="CREATE_EVENT", event_type=kind, title="Пить воду", date_expression="20 октября",
                  time_expression="в 10:00", recurrence_expression="каждый день")
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        result = post(client, message)
    assert not result["requires_input"] and "Событий: 90." in result["message"]
    assert "Создана серия" in result["message"]
    assert len(result["events"]) == 5
    events = stored(integration_engine)
    assert len(events) == 90
    assert events[-1].event_date == date(2026, 10, 20) + timedelta(days=89)
    assert len({event.recurrence_series_id for event in events}) == 1
    assert all(item["recurrence_rule"] == "FREQ=DAILY" and item["recurrence_series_id"] for item in result["events"])


@pytest.mark.parametrize("expression", [
    "каждый день 99999999 раз", "каждый день до 19.10.2026", "каждый день до 20.10.9999",
    "каждый понедельник до 20.10.2026", "через день",
])
def test_invalid_series_returns_domain_clarification_with_zero_rows(db, integration_engine, expression):
    message = f"создай задачу 20 октября проверить почту {expression}"
    parsed = dict(intent="CREATE_EVENT", event_type="TASK", title="Проверить почту",
                  date_expression="20 октября", recurrence_expression=expression)
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        result = post(client, message)
    assert result["missing_fields"] == ["recurrence"]
    assert not stored(integration_engine)


def test_recurrence_clarification_can_be_repaired_on_next_turn(db, integration_engine):
    message = "создай задачу 20 октября проверить почту через день"
    provider = FakeLLMProvider({message: dict(intent="CREATE_EVENT", event_type="TASK", title="Проверить почту",
        date_expression="20 октября", recurrence_expression="через день"),
        "каждый день 3 раза": dict(intent="CREATE_EVENT", recurrence_expression="каждый день 3 раза")})
    with client_for(integration_engine, provider) as client:
        first = post(client, message)
        result = post(client, "каждый день 3 раза", first["conversation_id"])
    assert not result["requires_input"] and len(stored(integration_engine)) == 3


@pytest.mark.parametrize("scope,answer,deleted", [
    (None, "да", 3), ("series", "да", 3), ("series", "нет", 0), ("series", "отмена", 0),
    ("occurrence", "да", 1), ("occurrence", "нет", 0),
])
def test_delete_scope_confirmation_and_context_roundtrip(db, integration_engine, scope, answer, deleted):
    events, cid = seed_series(db)
    suffix = {None: "", "series": " всю серию", "occurrence": " только эту встречу"}[scope]
    message = "удали" + suffix
    provider = FakeLLMProvider({message: dict(intent="DELETE_EVENT", event_reference="last_created",
                                             recurrence_scope=scope)})
    with client_for(integration_engine, provider) as client:
        result = post(client, message, cid)
    if scope is None:
        assert result["missing_fields"] == ["recurrence_scope"]
        assert pending_for(integration_engine, cid)["target_event_id"] == str(events[0].id)
        with client_for(integration_engine, NoLLM()) as client:
            still = post(client, "да", cid)
            assert still["missing_fields"] == ["recurrence_scope"]
            result = post(client, "всю серию", cid)
    assert result["missing_fields"] == ["confirmation"]
    assert all(event.status == "pending" for event in stored(integration_engine))
    with client_for(integration_engine, NoLLM()) as client:
        result = post(client, answer, cid)
    assert not result["requires_input"]
    saved = stored(integration_engine)
    assert sum(event.status == "deleted" for event in saved) == deleted
    if scope == "occurrence" and answer == "да":
        assert next(event for event in saved if event.id == events[0].id).status == "deleted"
        assert all(event.status == "pending" for event in saved if event.id != events[0].id)
    with Session(integration_engine) as check:
        conversation = check.get(Conversation, UUID(cid))
        assert conversation.pending_action is None
        if deleted == 3:
            assert conversation.last_event_id is None and not conversation.context["last_listed_events"]


@pytest.mark.parametrize("scope,changed", [(None, 3), ("series", 3), ("occurrence", 1)])
def test_update_scope_survives_new_client_and_changes_only_requested_members(db, integration_engine, scope, changed):
    events, cid = seed_series(db)
    suffix = {None: "", "series": " всю серию", "occurrence": " только эту встречу"}[scope]
    message = "перенеси на 15:00" + suffix
    parsed = dict(intent="UPDATE_EVENT", event_reference="last_created", time_expression="на 15:00",
                  recurrence_scope=scope)
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        result = post(client, message, cid)
    if scope is None:
        assert result["missing_fields"] == ["recurrence_scope"]
        assert all(event.event_time == time(10) for event in stored(integration_engine))
        with client_for(integration_engine, NoLLM()) as client:
            result = post(client, "все повторения", cid)
    assert not result["requires_input"]
    saved = stored(integration_engine)
    assert sum(event.event_time == time(15) for event in saved) == changed
    assert all(event.end_time == (time(16) if event.event_time == time(15) else time(11)) for event in saved)
    assert {event.recurrence_series_id for event in saved} == {events[0].recurrence_series_id}
    with Session(integration_engine) as check:
        snapshots = check.get(Conversation, UUID(cid)).context["last_listed_events"]
        assert sum(item["event_time"] == "15:00:00" for item in snapshots) == changed


def test_series_date_shift_preserves_intervals_and_excludes_all_own_occurrences(db, integration_engine):
    events, cid = seed_series(db)
    message = "перенеси всю серию на 27 октября"
    parsed = dict(intent="UPDATE_EVENT", event_reference="last_created", date_expression="на 27 октября",
                  recurrence_scope="series")
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        result = post(client, message, cid)
    assert not result["requires_input"], result
    assert [event.event_date for event in stored(integration_engine)] == [event.event_date + timedelta(days=7) for event in events]


@pytest.mark.parametrize("answer,expected_time", [("да", time(15)), ("нет", time(10)), ("отмена", time(10))])
def test_series_external_conflict_checks_later_occurrences_and_freezes_update(db, integration_engine, answer, expected_time):
    events, cid = seed_series(db)
    external = Event(type="meeting", title="Внешняя встреча", event_date=date(2026, 10, 27),
                     event_time=time(15, 30), end_time=time(16, 30), source="user")
    db.add(external)
    db.commit()
    message = "перенеси всю серию на 15:00"
    parsed = dict(intent="UPDATE_EVENT", event_reference="last_created", time_expression="на 15:00", recurrence_scope="series")
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        result = post(client, message, cid)
    assert result["missing_fields"] == ["confirmation"]
    pending = pending_for(integration_engine, cid)
    assert pending["confirmation_kind"] == "conflict"
    assert pending["confirmation_event"]["id"] == str(external.id)
    assert len(pending["confirmation"]["updates"]) == 3
    assert all(event.event_time == time(10) for event in stored(integration_engine) if event.recurrence_series_id)
    with client_for(integration_engine, NoLLM()) as client:
        result = post(client, answer, cid)
    assert not result["requires_input"]
    assert all(event.event_time == expected_time for event in stored(integration_engine) if event.recurrence_series_id)
    assert next(event for event in stored(integration_engine) if event.id == external.id).event_time == time(15, 30)


def test_update_series_adjacency_is_allowed(db, integration_engine):
    seed_series(db)
    # Start a new context pinned to the seed through a fresh helper query.
    with Session(integration_engine) as check:
        cid = str(check.scalar(select(Conversation.id)))
    db.add(Event(type="meeting", title="Смежная встреча", event_date=date(2026, 10, 27),
                 event_time=time(16), end_time=time(17), source="user"))
    db.commit()
    message = "перенеси всю серию на 15:00"
    parsed = dict(intent="UPDATE_EVENT", event_reference="last_created", time_expression="на 15:00", recurrence_scope="series")
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        result = post(client, message, cid)
    assert not result["requires_input"]
    assert sum(event.event_time == time(15) for event in stored(integration_engine)) == 3


def test_series_title_and_end_time_update(db, integration_engine):
    _, cid = seed_series(db)
    commands = {
        "переименуй всю серию в Ревью": dict(intent="UPDATE_EVENT", event_reference="last_created", new_title="Ревью", recurrence_scope="series"),
        "измени всю серию до 12:00": dict(intent="UPDATE_EVENT", event_reference="last_created", end_time_expression="до 12:00", recurrence_scope="series"),
    }
    with client_for(integration_engine, FakeLLMProvider(commands)) as client:
        for message in commands:
            result = post(client, message, cid)
            assert not result["requires_input"], result
    assert all(event.title == "Ревью" and event.event_time == time(10) and event.end_time == time(12) for event in stored(integration_engine))


def test_update_pattern_is_explicitly_unsupported_and_never_changes_dates(db, integration_engine):
    events, cid = seed_series(db)
    message = "измени всю серию: теперь не каждый вторник, а каждый понедельник"
    parsed = dict(intent="UPDATE_EVENT", event_reference="last_created", recurrence_scope="series",
                  recurrence_expression="каждый понедельник")
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        result = post(client, message, cid)
    assert result["missing_fields"] == ["recurrence"] and "не поддерживается" in result["message"]
    assert [event.event_date for event in stored(integration_engine)] == [event.event_date for event in events]


def test_creation_sql_failure_rolls_back_all_rows_and_preserves_frozen_confirmation(db, integration_engine, monkeypatch):
    db.add(Event(type="meeting", title="Конфликт", event_date=date(2026, 10, 27), event_time=time(10), source="user"))
    db.commit()
    with client_for(integration_engine, FakeLLMProvider({CREATE: CREATE_PARSED})) as client:
        first = post(client, CREATE)
    cid = first["conversation_id"]
    before = pending_for(integration_engine, cid)
    original, calls = EventRepository.create, []
    def fail_second(self, command):
        calls.append(command)
        if len(calls) == 2:
            command = command.model_copy(update={"title": ""})
        return original(self, command)
    monkeypatch.setattr(EventRepository, "create", fail_second)
    with client_for(integration_engine, NoLLM()) as client:
        result = client.post("/api/v1/assistant/messages", json={"message": "да", "conversation_id": cid})
    assert result.status_code == 503
    assert len(stored(integration_engine)) == 1 and pending_for(integration_engine, cid) == before


def test_series_update_sql_failure_rolls_back_every_member(db, integration_engine, monkeypatch):
    _, cid = seed_series(db)
    original, calls = EventRepository.update, []
    def fail_second(self, command):
        calls.append(command)
        if len(calls) == 2:
            command = command.model_copy(update={"title": ""})
        return original(self, command)
    monkeypatch.setattr(EventRepository, "update", fail_second)
    message = "перенеси всю серию на 15:00"
    parsed = dict(intent="UPDATE_EVENT", event_reference="last_created", time_expression="на 15:00", recurrence_scope="series")
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        result = client.post("/api/v1/assistant/messages", json={"message": message, "conversation_id": cid})
    assert result.status_code == 503 and len(calls) == 2
    assert all(event.event_time == time(10) for event in stored(integration_engine))


def test_series_delete_db_failure_preserves_every_member_and_confirmation(db, integration_engine):
    events, cid = seed_series(db)
    message = "удали всю серию"
    with client_for(integration_engine, FakeLLMProvider({message: dict(intent="DELETE_EVENT", recurrence_scope="series")})) as client:
        first = post(client, message, cid)
    assert first["missing_fields"] == ["confirmation"]
    before = pending_for(integration_engine, cid)
    constraint = "stage7_test_reject_delete"
    with integration_engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE events ADD CONSTRAINT {constraint} CHECK (NOT (id = '{events[1].id}' AND status = 'deleted'))"))
    try:
        with client_for(integration_engine, NoLLM()) as client:
            result = client.post("/api/v1/assistant/messages", json={"message": "да", "conversation_id": cid})
        assert result.status_code == 503
        assert all(event.status == "pending" for event in stored(integration_engine))
        assert pending_for(integration_engine, cid) == before
    finally:
        with integration_engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE events DROP CONSTRAINT {constraint}"))


@pytest.mark.parametrize("intent", ["UPDATE_EVENT", "DELETE_EVENT"])
def test_explicit_series_from_list_resolves_one_series_without_an_occurrence_selection(db, integration_engine, intent):
    _, cid = seed_series(db)
    message = "перенеси всю серию на 15:00" if intent == "UPDATE_EVENT" else "удали всю серию"
    parsed = dict(intent=intent, event_reference="last_listed", recurrence_scope="series")
    if intent == "UPDATE_EVENT":
        parsed["time_expression"] = "на 15:00"
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        response = post(client, message, cid)
    if intent == "DELETE_EVENT":
        assert response["missing_fields"] == ["confirmation"]
    else:
        assert not response["requires_input"] and len(response["events"]) == 3


@pytest.mark.parametrize("start,conflict", [(time(15), True), (time(15, 30), False)])
def test_series_update_preserves_no_end_time_conflict_rule(db, integration_engine, start, conflict):
    _, cid = seed_series(db)
    db.add(Event(type="meeting", title="Внешний звонок без окончания", event_date=date(2026, 10, 27),
                 event_time=start, source="user"))
    db.commit()
    message = "перенеси всю серию на 15:00"
    parsed = dict(intent="UPDATE_EVENT", event_reference="last_created", time_expression="на 15:00", recurrence_scope="series")
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        response = post(client, message, cid)
    assert response["requires_input"] is conflict
    assert all(event.event_time == (time(10) if conflict else time(15)) for event in stored(integration_engine) if event.recurrence_series_id)


@pytest.mark.parametrize("answer", ["только эту встречу", "только это событие", "только это напоминание", "«всю серию»"])
def test_scope_reply_preserves_target_even_after_context_changes(db, integration_engine, answer):
    events, cid = seed_series(db)
    message = "удали это событие"
    with client_for(integration_engine, FakeLLMProvider({message: dict(intent="DELETE_EVENT", event_reference="last_created")})) as client:
        assert post(client, message, cid)["missing_fields"] == ["recurrence_scope"]
    other = Event(type="task", title="Другая запись", source="user")
    db.add(other)
    db.flush()
    conversation = db.get(Conversation, UUID(cid))
    db.refresh(conversation)
    conversation.last_event_id = other.id
    conversation.context = {}
    db.commit()
    with client_for(integration_engine, NoLLM()) as client:
        confirmation = post(client, answer, cid)
        assert confirmation["missing_fields"] == ["confirmation"]
        assert pending_for(integration_engine, cid)["confirmation"]["event_id"] == str(events[0].id)
        post(client, "да", cid)
    saved = stored(integration_engine)
    assert next(event for event in saved if event.id == other.id).status == "pending"
    assert sum(event.status == "deleted" for event in saved) == (3 if "серию" in answer else 1)


def test_different_series_are_never_collapsed_into_one_target(db, integration_engine):
    events, cid = seed_series(db)
    events[1].recurrence_series_id = uuid4()
    db.commit()
    message = "удали всю серию"
    with client_for(integration_engine, FakeLLMProvider({message: dict(
        intent="DELETE_EVENT", event_reference="last_listed", recurrence_scope="series",
    )})) as client:
        result = post(client, message, cid)
    assert result["missing_fields"] == ["event_reference"]
    assert all(event.status == "pending" for event in stored(integration_engine))


@pytest.mark.parametrize("reply,deleted", [("всю серию", 3), ("только это событие", 1)])
def test_recurring_card_delete_uses_same_scope_flow_without_llm(db, integration_engine, reply, deleted):
    events, cid = seed_series(db)
    with client_for(integration_engine, NoLLM()) as client:
        response = client.post(f"/api/v1/events/{events[0].id}/actions", json={"action": "DELETE_EVENT", "conversation_id": cid})
        assert response.status_code == 200 and response.json()["missing_fields"] == ["recurrence_scope"]
        assert post(client, reply, cid)["missing_fields"] == ["confirmation"]
        post(client, "да", cid)
    assert sum(event.status == "deleted" for event in stored(integration_engine)) == deleted


def test_relative_series_shift_preserves_different_occurrence_clock_times(db, integration_engine):
    events, cid = seed_series(db, kind="reminder", end=None)
    events[1].event_time = time(19)
    db.commit()
    message = "перенеси всю серию на час позже"
    parsed = dict(intent="UPDATE_EVENT", event_reference="last_created", shift_expression="на час позже", recurrence_scope="series")
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        result = post(client, message, cid)
    assert not result["requires_input"]
    assert [event.event_time for event in stored(integration_engine)] == [time(11), time(20), time(11)]


def test_recurrence_until_is_inclusive_at_api_boundary(db, integration_engine):
    message = "создай задачу 20 октября пить воду каждый день до 22.10.2026"
    parsed = dict(intent="CREATE_EVENT", event_type="TASK", title="Пить воду", date_expression="20 октября",
                  recurrence_expression="каждый день до 22.10.2026")
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        result = post(client, message)
    assert not result["requires_input"]
    assert [event.event_date for event in stored(integration_engine)] == [date(2026, 10, day) for day in (20, 21, 22)]
    assert all(event["recurrence_until"] == "2026-10-22" for event in result["events"])
