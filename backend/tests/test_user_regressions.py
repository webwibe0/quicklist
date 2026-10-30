"""Acceptance regressions for reported dialogues; no live LLM or user DB.

Scripted structured outputs isolate deterministic execution, not language recognition.
"""
import json
from datetime import date, datetime, time, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.ai.provider import ProviderError
from app.models.entities import Conversation, Event
from app.repositories.events import EventRepository
from app.services.dialogue import DialogueService
from app.services.responses import ResponseBuilder
from app.temporal.resolver import TemporalResolver
from app.use_cases.actions import ActionRouter
from tests.fakes import FakeLLMProvider

NOW = datetime.fromisoformat("2026-10-15T11:59:00+03:00")
DIMA = "запиши встречу с Димой через два дня"
REMIND = "напомни мне через 4 часа"
LIST = "что на завтра?"
MOVE = "с командой перенеси на час вперед"
ORDER = "мне через час нужно сделать заказ на озоне"
CORRECT = "хотя нет, через два часа"


@pytest.fixture
def harness():
    # Only Event's table is needed. SQLite does not emulate PostgreSQL search/JSONB.
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Event.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        provider = FakeLLMProvider({
            DIMA: {"intent": "CREATE_EVENT", "event_type": "MEETING",
                   "title": "Встреча с Димой", "date_expression": "через два дня"},
            "в 5 утра": {"intent": "CREATE_EVENT", "time_expression": "в 5 утра"},
            REMIND: {"intent": "CREATE_EVENT", "event_type": "REMINDER",
                     "date_expression": "через 4 часа"},
            LIST: {"intent": "LIST_EVENTS", "period_expression": "завтра"},
            MOVE: {"intent": "UPDATE_EVENT", "search_query": "командой",
                   "time_expression": "на час вперед"},
            ORDER: {"intent": "CREATE_EVENT", "event_type": "TASK",
                    "title": "Сделать заказ на озоне", "date_expression": "через час"},
            CORRECT: {"intent": "UPDATE_EVENT", "date_expression": "через два часа"},
        })
        service = DialogueService(
            provider, TemporalResolver("Europe/Moscow"),
            ActionRouter(EventRepository(session, 50)), ResponseBuilder(50),
        )
        conversation = Conversation(id=uuid4(), pending_action=None, last_event_id=None)
        yield service, conversation, session, provider
    engine.dispose()


def create_dima(service, conversation):
    reply = service.handle(conversation, DIMA, NOW)
    assert reply.requires_input and reply.missing_fields == ["time"]
    created = service.handle(conversation, "в 5 утра", NOW).events[0]
    assert (created.event_date, created.event_time) == (date(2026, 10, 17), time(5))
    assert conversation.last_event_id == created.id
    assert conversation.pending_action is None
    return created


def seed_meetings(session):
    team = Event(type="meeting", title="Встреча с командой",
                 event_date=date(2026, 10, 16), event_time=time(15))
    other = Event(type="meeting", title="Встреча с Иваном",
                  event_date=date(2026, 10, 16), event_time=time(15, 30))
    outside = Event(type="meeting", title="Встреча с другой командой",
                    event_date=date(2026, 10, 18), event_time=time(10))
    session.add_all([team, other, outside])
    session.flush()
    return team, other, outside


def handle_update(service, conversation, message, now=NOW):
    try:
        return service.handle(conversation, message, now)
    except ProviderError as exc:
        pytest.fail(f"UPDATE_EVENT blocked at structured-output boundary: {exc}")


def test_01_reminder_inherits_previous_meeting_subject(harness):
    service, conversation, session, _ = harness
    meeting = create_dima(service, conversation)
    reply = service.handle(conversation, REMIND, NOW)
    assert not reply.requires_input, (
        f"Previous meeting subject was lost: missing={reply.missing_fields}; {reply.message}"
    )
    reminder, = reply.events
    assert reminder.type == "reminder" and "Дим" in reminder.title
    assert (reminder.event_date, reminder.event_time) == (NOW.date(), time(15, 59))
    assert reminder.id != meeting.id
    session.expire_all()
    original = session.get(Event, meeting.id)
    assert (original.event_date, original.event_time) == (meeting.event_date, meeting.event_time)
    assert len(session.scalars(select(Event)).all()) == 2


def test_01_completed_meeting_is_available_to_next_nlu_turn(harness):
    service, conversation, _, provider = harness
    meeting = create_dima(service, conversation)
    service.handle(conversation, REMIND, NOW)
    context = json.dumps(provider.contexts[-1], ensure_ascii=False)
    assert str(meeting.id) in context and meeting.title in context, (
        f"NLU receives no completed-event context: {context}"
    )


def test_02_zoom_explicit_range_preserves_start_and_end(harness):
    service, conversation, session, provider = harness
    message = "завтра зум с коллегами с 12:00 до 23:00"
    provider.commands[message] = {
        "intent": "CREATE_EVENT", "event_type": "MEETING", "title": "Зум с коллегами",
        "date_expression": "завтра", "time_expression": "с 12:00 до 23:00",
    }
    reply = service.handle(conversation, message, NOW)
    assert not reply.requires_input, (
        f"Explicit two-time range cannot reach storage: {reply.message}"
    )
    event, = reply.events
    session.expire_all()
    stored = session.get(Event, event.id)
    assert (stored.event_date, stored.event_time, stored.end_time) == (
        date(2026, 10, 16), time(12), time(23),
    )


def test_02_zoom_alternative_times_are_distinct_in_clarification(harness):
    service, conversation, session, provider = harness
    message = "завтра зум с коллегами в 12:00 или в 23:00"
    provider.commands[message] = {
        "intent": "CREATE_EVENT", "event_type": "MEETING", "title": "Зум с коллегами",
        "date_expression": "завтра", "time_expression": "в 12:00 или в 23:00",
    }
    reply = service.handle(conversation, message, NOW)
    assert reply.requires_input and reply.missing_fields == ["time"]
    assert not session.scalars(select(Event)).all()
    assert "12:00" in reply.message and "23:00" in reply.message, reply.message
    provider.commands["в 23:00"] = {"intent": "CREATE_EVENT", "time_expression": "в 23:00"}
    event, = service.handle(conversation, "в 23:00", NOW).events
    assert (event.event_date, event.event_time) == (date(2026, 10, 16), time(23))


@pytest.mark.parametrize("day", [12, 15, 18], ids=["monday", "thursday", "sunday"])
def test_03_colloquial_week_means_seven_dates_from_today(harness, day):
    service, conversation, session, provider = harness
    message = "че у меня на неделю?"
    provider.commands[message] = {"intent": "LIST_EVENTS", "period_expression": "на неделю"}
    events = [Event(type="task", title=f"Task {d}", event_date=date(2026, 10, d))
              for d in (11, 12, 18, 19)]
    session.add_all(events)
    session.flush()
    reply = service.handle(conversation, message, NOW.replace(day=day))
    assert not reply.requires_input, f"Current calendar week was not resolved: {reply.message}"
    assert reply.action == "LIST_EVENTS"
    # Targeted V2 explicitly changes bare "на неделю" to a seven-date window.
    assert [event.id for event in reply.events] == [event.id for event in events
        if day <= event.event_date.day <= day + 6]


def test_04_move_only_matching_previously_listed_meeting(harness):
    service, conversation, session, _ = harness
    team, other, outside = seed_meetings(session)
    listed = service.handle(conversation, LIST, NOW)
    assert {event.id for event in listed.events} == {team.id, other.id}
    reply = handle_update(service, conversation, MOVE)
    assert not reply.requires_input and reply.action == "UPDATE_EVENT"
    assert [event.id for event in reply.events] == [team.id]
    session.expire_all()
    assert session.get(Event, team.id).event_time == time(16)
    assert session.get(Event, team.id).event_date == date(2026, 10, 16)
    assert session.get(Event, other.id).event_time == time(15, 30)
    assert session.get(Event, outside.id).event_time == time(10)
    assert len(session.scalars(select(Event)).all()) == 3


def test_04_listed_events_are_available_to_next_nlu_turn(harness):
    service, conversation, session, provider = harness
    team, other, outside = seed_meetings(session)
    service.handle(conversation, LIST, NOW)
    # UNKNOWN keeps this context diagnostic independent of UPDATE_EVENT schema rejection.
    provider.commands[MOVE] = {"intent": "UNKNOWN"}
    service.handle(conversation, MOVE, NOW)
    context = json.dumps(provider.contexts[-1], ensure_ascii=False)
    assert all(str(event.id) in context and event.title in context for event in (team, other)), (
        f"NLU receives no listed-event candidates: {context}"
    )
    assert str(outside.id) not in context


def test_05_correction_reschedules_same_task_relative_to_followup_now(harness):
    service, conversation, session, _ = harness
    unrelated = Event(type="task", title="Другая задача", event_date=NOW.date(), event_time=time(18))
    session.add(unrelated)
    session.flush()
    created, = service.handle(conversation, ORDER, NOW).events
    assert (created.event_date, created.event_time) == (NOW.date(), time(12, 59))
    followup_now = NOW + timedelta(minutes=2)
    reply = handle_update(service, conversation, CORRECT, followup_now)
    assert not reply.requires_input and reply.action == "UPDATE_EVENT"
    updated, = reply.events
    assert updated.id == created.id and updated.title == created.title
    assert (updated.event_date, updated.event_time) == (NOW.date(), time(14, 1))
    session.expire_all()
    assert session.get(Event, created.id).event_time == time(14, 1)
    assert session.get(Event, unrelated.id).event_time == time(18)
    assert len(session.scalars(select(Event)).all()) == 2


@pytest.mark.parametrize("message", [
    "че у меня на неделю?", "что у меня на неделю?", "покажи планы на неделю",
])
@pytest.mark.parametrize("field", ["date_expression", "period_expression"])
def test_week_wording_normalizes_both_nlu_shapes(harness, message, field):
    service, conversation, session, provider = harness
    provider.commands[message] = {"intent": "LIST_EVENTS", field: "на неделю"}
    seed_meetings(session)
    reply = service.handle(conversation, message, NOW)
    assert not reply.requires_input and len(reply.events) == 3


@pytest.mark.parametrize("expression,start,end", [
    ("на неделю", "2026-10-19", "2026-10-25"),
    ("на следующую неделю", "2026-10-26", "2026-11-01"),
    ("на прошлую неделю", "2026-10-12", "2026-10-18"),
])
def test_week_uses_moscow_monday_at_utc_sunday(expression, start, end):
    now = datetime.fromisoformat("2026-10-18T21:30:00+00:00")
    assert TemporalResolver("Europe/Moscow").resolve_period(expression, now) == (
        date.fromisoformat(start), date.fromisoformat(end),
    )


@pytest.mark.parametrize("expression", ["12:00–23:00", "12:00—23:00", "12:00-23:00"])
def test_zoom_short_range_reaches_storage_and_response(harness, expression):
    service, conversation, session, provider = harness
    message = f"зум завтра {expression}"
    provider.commands[message] = {
        "intent": "CREATE_EVENT", "event_type": "MEETING", "title": "Зум",
        "date_expression": "завтра", "time_expression": expression,
    }
    reply = service.handle(conversation, message, NOW)
    assert not reply.requires_input and "12:00" in reply.message and "23:00" in reply.message
    event, = reply.events
    session.commit()
    session.expire_all()
    stored = session.get(Event, event.id)
    assert (stored.event_time, stored.end_time) == (time(12), time(23))
    assert reply.model_dump(mode="json")["events"][0]["end_time"] == "23:00:00"
    assert conversation.context["last_created_event"]["end_time"] == "23:00:00"


@pytest.mark.parametrize("expression,missing,answer", [
    ("с 12:00", "end_time", "в 23:00"),
    ("до 23:00", "time", "в 12:00"),
    ("с 12:00 до 25:00", "end_time", "в 23:00"),
    ("с 12:00 до 11:00", "end_time", "в 23:00"),
    ("с 12:00 до 12:00", "end_time", "в 23:00"),
    ("с 25:00 до 23:00", "time", "в 12:00"),
])
def test_interval_asks_only_for_missing_endpoint_and_restores_pending(harness, expression, missing, answer):
    service, conversation, session, provider = harness
    message = f"завтра зум {expression}"
    provider.commands[message] = {
        "intent": "CREATE_EVENT", "event_type": "MEETING", "title": "Зум",
        "date_expression": "завтра", "time_expression": expression,
    }
    reply = service.handle(conversation, message, NOW)
    assert reply.requires_input and reply.missing_fields == [missing]
    assert not session.scalars(select(Event)).all()
    restored = Conversation(id=conversation.id, context=conversation.context,
                            pending_action=json.loads(json.dumps(conversation.pending_action)))
    provider.commands[answer] = {"intent": "CREATE_EVENT", "time_expression": answer}
    # A delayed clarification must keep the date resolved by the original request.
    reply = service.handle(restored, answer, NOW + timedelta(days=1))
    assert not reply.requires_input and restored.pending_action is None
    event, = reply.events
    assert (event.event_date, event.event_time, event.end_time) == (
        date(2026, 10, 16), time(12), time(23),
    )


def test_split_endpoints_and_single_time_meeting(harness):
    service, conversation, _, provider = harness
    for start, ending in ((12, None), (13, "23:00")):
        message = f"завтра зум в {start:02d}:00" + (" до 23:00" if ending else "")
        provider.commands[message] = {
            "intent": "CREATE_EVENT", "event_type": "MEETING", "title": "Зум",
            "date_expression": "завтра", "time_expression": f"в {start:02d}:00",
            "end_time_expression": ending,
        }
        reply = service.handle(conversation, message, NOW)
        assert not reply.requires_input
        assert reply.events[0].end_time == (time(23) if ending else None)


@pytest.mark.parametrize("change,field,expected", [
    ("на час вперед", "shift_expression", (time(13), time(15))),
    ("в 15:00", "time_expression", (time(15), time(17))),
    ("12:00–23:00", "time_expression", (time(12), time(23))),
    ("до 16:00", "end_time_expression", (time(12), time(16))),
    ("на четыре часа раньше", "shift_expression", (time(8), time(10))),
])
def test_update_interval_and_frozen_past_confirmation(harness, change, field, expected):
    service, conversation, session, provider = harness
    target = Event(type="meeting", title="Зум", event_date=NOW.date(),
                   event_time=time(12), end_time=time(14))
    session.add(target)
    session.flush()
    conversation.last_event_id = target.id
    provider.commands[change] = {"intent": "UPDATE_EVENT", field: change}
    reply = service.handle(conversation, change, NOW)
    if expected[0] < NOW.time().replace(tzinfo=None):
        assert reply.missing_fields == ["confirmation"]
        session.expire_all()
        assert (target.event_time, target.end_time) == (time(12), time(14))
        frozen = json.loads(json.dumps(conversation.pending_action))
        assert frozen["confirmation"]["end_time"] == "10:00:00"
        conversation.pending_action = frozen
        reply = service.handle(conversation, "да", NOW + timedelta(hours=1))
    assert not reply.requires_input
    assert (reply.events[0].event_time, reply.events[0].end_time) == expected
    session.expire_all()
    assert (target.event_time, target.end_time) == expected


def test_end_time_guard_rejects_hallucinated_endpoint(harness):
    service, conversation, session, provider = harness
    message = "завтра зум в 12:00"
    provider.commands[message] = {
        "intent": "CREATE_EVENT", "event_type": "MEETING", "title": "Зум",
        "date_expression": "завтра", "time_expression": "12:00",
        "end_time_expression": "23:00",
    }
    with pytest.raises(ProviderError):
        service.handle(conversation, message, NOW)
    assert not session.scalars(select(Event)).all()


def test_declining_past_interval_move_preserves_both_times(harness):
    service, conversation, session, provider = harness
    target = Event(type="meeting", title="Зум", event_date=NOW.date(),
                   event_time=time(12), end_time=time(14))
    session.add(target)
    session.flush()
    conversation.last_event_id = target.id
    message = "на четыре часа раньше"
    provider.commands[message] = {"intent": "UPDATE_EVENT", "shift_expression": message}
    assert service.handle(conversation, message, NOW).missing_fields == ["confirmation"]
    assert not service.handle(conversation, "нет", NOW).requires_input
    session.expire_all()
    assert (target.event_time, target.end_time) == (time(12), time(14))
    assert conversation.pending_action is None


def test_invalid_interval_update_never_writes_until_corrected(harness):
    service, conversation, session, provider = harness
    target = Event(type="meeting", title="Зум", event_date=NOW.date(),
                   event_time=time(12), end_time=time(14))
    session.add(target)
    session.flush()
    conversation.last_event_id = target.id
    message = "перенеси на 15:00–14:00"
    provider.commands[message] = {"intent": "UPDATE_EVENT", "time_expression": "15:00–14:00"}
    reply = service.handle(conversation, message, NOW)
    assert reply.missing_fields == ["end_time"]
    session.expire_all()
    assert (target.event_time, target.end_time) == (time(12), time(14))
    provider.commands["до 17:00"] = {"intent": "UPDATE_EVENT", "end_time_expression": "до 17:00"}
    reply = service.handle(conversation, "до 17:00", NOW)
    assert not reply.requires_input
    assert (reply.events[0].event_time, reply.events[0].end_time) == (time(15), time(17))
