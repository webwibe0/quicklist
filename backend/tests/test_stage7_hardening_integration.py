"""Full user messages through HTTP, guarded structured NLU and disposable PostgreSQL."""
from datetime import date, datetime, time
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.models.entities import Conversation, Event
from app.repositories.events import EventRepository
from app.schemas.commands import CreateEventCommand, SearchEventsCommand
from app.temporal.resolver import TemporalResolver
from tests.fakes import FakeLLMProvider
from tests.test_integration import client_for

NOW = datetime.fromisoformat("2026-10-19T11:00:00+03:00")
pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(TemporalResolver, "local_now", lambda self, now=None: (now or NOW).astimezone(self.zone))
    monkeypatch.setattr("app.ai.openai_provider.OpenAI", lambda *a, **kw: pytest.fail("No live OpenAI"))
    yield
    app.dependency_overrides.clear()


def post(client, message, conversation_id=None):
    response = client.post("/api/v1/assistant/messages", json={"message": message, "conversation_id": conversation_id})
    assert response.status_code == 200, response.text
    return response.json()


def rows(engine):
    with Session(engine) as session:
        return session.scalars(select(Event).order_by(Event.event_date, Event.event_time)).all()


@pytest.mark.parametrize("intent", ["LIST_EVENTS", "SEARCH_EVENTS"])
@pytest.mark.parametrize("message,period,day", [
    ("покажи прошедшие планы за 2 недели", "за 2 недели", date(2026, 10, 5)),
    ("покажи прошедшие планы с 10.10 по 19.10", "с 10.10 по 19.10", date(2026, 10, 10)),
    ("покажи прошедшие планы с 10.10.2026 по 19.10.2026", "с 10.10.2026 по 19.10.2026", date(2026, 10, 10)),
    ("что было за последние 7 дней", "за последние 7 дней", date(2026, 10, 13)),
    ("что было 15 октября", "15 октября", date(2026, 10, 15)),
])
def test_historical_filter_reaches_sql_without_requesting_year_or_mixing_future(
    db, integration_engine, intent, message, period, day,
):
    db.add_all([
        Event(type="task", title="В прошлом", event_date=day, event_time=time(10), source="user"),
        Event(type="task", title="В будущем сегодня", event_date=NOW.date(), event_time=time(18), source="user"),
        Event(type="task", title="В будущем году", event_date=day.replace(year=2027), event_time=time(10), source="user"),
    ])
    db.commit()
    with client_for(integration_engine, FakeLLMProvider({message: dict(intent=intent, period_expression=period)})) as client:
        result = post(client, message)
    assert not result["requires_input"] and result["missing_fields"] == []
    assert [event["title"] for event in result["events"]] == ["В прошлом"]


@pytest.mark.parametrize("message,first,last", [
    ("что было с 28 декабря по 5 января", "2025-12-28", "2026-01-05"),
    ("что будет с 28 декабря по 5 января", "2026-12-28", "2027-01-05"),
    ("покажи встречи с 28 декабря по 5 января", "2026-12-28", "2027-01-05"),
])
def test_year_boundary_uses_request_direction(db, integration_engine, message, first, last):
    for year in (2025, 2026):
        db.add_all([Event(type="meeting", title="Декабрь", event_date=date(year, 12, 28), source="user"),
                    Event(type="meeting", title="Январь", event_date=date(year + 1, 1, 5), source="user")])
    db.commit()
    with client_for(integration_engine, FakeLLMProvider({message: dict(intent="LIST_EVENTS", event_type="MEETING",
            period_expression="с 28 декабря по 5 января")})) as client:
        result = post(client, message)
    assert not result["requires_input"]
    assert [event["event_date"] for event in result["events"]] == [first, last]


def test_pending_historical_range_retains_intent_type_and_direction(db, integration_engine):
    db.add_all([Event(type=kind, title=kind, event_date=date(2026, 9, 15), source="user")
                for kind in ("meeting", "task")])
    db.commit()
    initial, answer = "покажи прошедшие встречи", "с 10.09 по 20.09"
    provider = FakeLLMProvider({initial: dict(intent="LIST_EVENTS", event_type="MEETING", period_expression="прошедшие"),
                              answer: dict(intent="LIST_EVENTS", period_expression=answer)})
    with client_for(integration_engine, provider) as client:
        first = post(client, initial)
        assert first["missing_fields"] == ["period"]
        result = post(client, answer, first["conversation_id"])
    assert not result["requires_input"] and [event["type"] for event in result["events"]] == ["meeting"]
    assert result["events"][0]["event_date"] == "2026-09-15"
    with Session(integration_engine) as session:
        assert session.get(Conversation, UUID(result["conversation_id"])).pending_action is None


@pytest.mark.parametrize("split", [False, True])
def test_exact_evening_call_persists_normalized_interval(db, integration_engine, split):
    message = "назначь созвон на послезавтра с 7 до 9 вечера"
    parsed = dict(intent="CREATE_EVENT", event_type="MEETING", title="Созвон",
                  date_expression="послезавтра", time_expression="с 7 до 9 вечера")
    if split:
        parsed.update(time_expression="7", end_time_expression="9 вечера")
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        result = post(client, message)
    assert not result["requires_input"] and len(result["events"]) == 1
    event = rows(integration_engine)[0]
    assert (event.event_date, event.event_time, event.end_time) == (date(2026, 10, 21), time(19), time(21))


def test_daypart_followup_repairs_both_interval_endpoints(db, integration_engine):
    message = "назначь встречу на послезавтра с 7 до 9"
    provider = FakeLLMProvider({message: dict(intent="CREATE_EVENT", event_type="MEETING", title="Встреча",
        date_expression="послезавтра", time_expression="с 7 до 9"),
        "вечера": dict(intent="CREATE_EVENT", time_expression="вечера")})
    with client_for(integration_engine, provider) as client:
        first = post(client, message)
        assert first["requires_input"] and not rows(integration_engine)
        result = post(client, "вечера", first["conversation_id"])
    assert not result["requires_input"]
    assert (rows(integration_engine)[0].event_time, rows(integration_engine)[0].end_time) == (time(19), time(21))


@pytest.mark.parametrize("external_day,external_start,external_end,conflict", [
    (21, time(22), time(23), False), (21, time(23, 30), time(23, 59), True),
    (22, time(0, 30), time(2), True), (22, time(1), time(2), False),
])
def test_overnight_meeting_conflicts_use_absolute_end_date(
    db, integration_engine, external_day, external_start, external_end, conflict,
):
    db.add(Event(type="meeting", title="Внешняя", event_date=date(2026, 10, external_day),
                 event_time=external_start, end_time=external_end, source="user"))
    db.commit()
    message = "назначь созвон на послезавтра с 11 вечера до 1 ночи"
    with client_for(integration_engine, FakeLLMProvider({message: dict(intent="CREATE_EVENT", event_type="MEETING",
            title="Созвон", date_expression="послезавтра", time_expression="с 11 вечера до 1 ночи")})) as client:
        result = post(client, message)
    assert result["requires_input"] is conflict
    assert len(rows(integration_engine)) == (1 if conflict else 2)
    if conflict:
        with Session(integration_engine) as session:
            assert session.get(Conversation, UUID(result["conversation_id"])).pending_action["confirmation_kind"] == "conflict"
    else:
        assert "следующего дня" in result["message"]


@pytest.mark.parametrize("clock,finish,conflict", [
    (time(0, 30), time(2), True), (time(1), time(2), False),
    (time(23), None, False), (time(0, 30), None, False),
])
def test_existing_previous_night_is_checked_and_missing_end_keeps_same_start_policy(db, clock, finish, conflict):
    db.add(Event(type="meeting", title="Ночной созвон", event_date=date(2026, 10, 20),
                 event_time=time(23), end_time=time(1), source="user"))
    db.flush()
    command = CreateEventCommand(event_type="MEETING", title="Новая", event_date=date(2026, 10, 21),
                                 event_time=clock, end_time=finish, raw_user_text="test")
    assert bool(EventRepository(db, 100).find_conflicts(command)) is conflict


@pytest.mark.parametrize("clock,expected", [("2026-10-21T00:30:00+03:00", 0), ("2026-10-21T01:01:00+03:00", 1)])
def test_historical_search_does_not_end_overnight_meeting_at_midnight(db, clock, expected):
    db.add(Event(type="meeting", title="Ночной созвон", event_date=date(2026, 10, 20),
                 event_time=time(23), end_time=time(1), source="user"))
    db.flush()
    results = EventRepository(db, 100).search(SearchEventsCommand(
        query="созвон", temporal_direction="past", as_of=datetime.fromisoformat(clock),
    ))
    assert len(results) == expected


@pytest.mark.parametrize("expression,start,end", [("с 7 до 9 вечера", time(19), time(21)),
                                                   ("с 11 вечера до 1 ночи", time(23), time(1))])
def test_recurrence_normalizes_every_occurrence_before_persistence(db, integration_engine, expression, start, end):
    message = f"каждый понедельник {expression} встреча команды"
    with client_for(integration_engine, FakeLLMProvider({message: dict(intent="CREATE_EVENT", event_type="MEETING",
        title="Встреча команды", time_expression=expression, recurrence_expression="каждый понедельник")})) as client:
        result = post(client, message)
    assert not result["requires_input"] and "Событий: 13." in result["message"]
    events = rows(integration_engine)
    assert len(events) == 13 and len({event.recurrence_series_id for event in events}) == 1
    assert all(event.event_date.weekday() == 0 and event.event_time == start and event.end_time == end for event in events)


def test_night_series_shift_preserves_duration_and_excludes_all_own_occurrences(db, integration_engine):
    message = "каждый понедельник 3 раза с 11 вечера до 1 ночи встреча команды"
    update = "перенеси всю серию на час вперед"
    provider = FakeLLMProvider({message: dict(intent="CREATE_EVENT", event_type="MEETING", title="Встреча команды",
        recurrence_expression="каждый понедельник 3 раза", time_expression="с 11 вечера до 1 ночи"),
        update: dict(intent="UPDATE_EVENT", recurrence_scope="series", event_reference="last_created", shift_expression="на час вперед")})
    with client_for(integration_engine, provider) as client:
        first = post(client, message)
        assert not first["requires_input"]
        result = post(client, update, first["conversation_id"])
    assert not result["requires_input"] and len(rows(integration_engine)) == 3
    assert all(event.event_date.weekday() == 1 and event.event_time == time(0) and event.end_time == time(2)
               for event in rows(integration_engine))


@pytest.mark.parametrize("status,expected", [("pending", 1), ("completed", 0), ("cancelled", 0), ("deleted", 0)])
def test_historical_listing_keeps_existing_status_policy_and_never_marks_time_as_completed(db, integration_engine, status, expected):
    db.add(Event(type="meeting", title="Архивная", event_date=date(2026, 10, 15), status=status, source="user"))
    db.commit()
    message = "покажи прошедшие встречи с 10.10 по 19.10"
    with client_for(integration_engine, FakeLLMProvider({message: dict(intent="LIST_EVENTS", event_type="MEETING",
            period_expression="с 10.10 по 19.10")})) as client:
        result = post(client, message)
    assert not result["requires_input"] and len(result["events"]) == expected
    assert rows(integration_engine)[0].status == status


@pytest.mark.parametrize("old_request", ["покажи прошедшие встречи", "напомни сегодня в 9:00 позвонить"])
def test_complete_appoint_request_replaces_old_clarification_or_confirmation(db, integration_engine, old_request):
    old = (dict(intent="LIST_EVENTS", event_type="MEETING", period_expression="прошедшие") if old_request.startswith("покажи")
           else dict(intent="CREATE_EVENT", event_type="REMINDER", title="Позвонить", date_expression="сегодня", time_expression="в 9:00"))
    message = "назначь созвон на послезавтра с 7 до 9 вечера"
    with client_for(integration_engine, FakeLLMProvider({old_request: old, message: dict(intent="CREATE_EVENT", event_type="MEETING",
            title="Созвон", date_expression="послезавтра", time_expression="с 7 до 9 вечера")})) as client:
        first = post(client, old_request)
        assert first["requires_input"]
        result = post(client, message, first["conversation_id"])
    assert not result["requires_input"] and result["events"][0]["type"] == "meeting"
    with Session(integration_engine) as session:
        assert session.get(Conversation, UUID(result["conversation_id"])).pending_action is None
