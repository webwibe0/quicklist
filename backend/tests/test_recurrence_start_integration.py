"""Implicit recurrence starts use the request clock, never an LLM-invented date."""

from datetime import date, datetime, time, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.models.entities import Conversation, Event
from app.schemas.commands import ParsedCommand
from app.schemas.dialogue import PendingAction
from app.temporal.resolver import TemporalResolver, TemporalResult
from tests.fakes import FakeLLMProvider
from tests.test_integration import client_for

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    current = [datetime.fromisoformat("2026-10-19T11:00:00+03:00")]
    monkeypatch.setattr(
        TemporalResolver, "local_now",
        lambda self, now=None: (now or current[0]).astimezone(self.zone),
    )
    monkeypatch.setattr("app.ai.openai_provider.OpenAI", lambda *args, **kwargs: pytest.fail("No live OpenAI"))
    yield current
    app.dependency_overrides.clear()


def post(client, message, conversation_id=None):
    response = client.post("/api/v1/assistant/messages", json={
        "message": message, "conversation_id": conversation_id,
    })
    assert response.status_code == 200, response.text
    return response.json()


def stored(engine):
    with Session(engine) as session:
        return session.scalars(select(Event).order_by(Event.event_date, Event.event_time, Event.id)).all()


def pending_for(engine, conversation_id):
    with Session(engine) as session:
        return session.get(Conversation, UUID(conversation_id)).pending_action


def assert_created_series(result, engine, first_date, first_time, *, count=None):
    assert not result["requires_input"], result
    assert result["missing_fields"] == []
    assert result["action"] == "CREATE_EVENT"
    assert result["events"]
    assert "Создана серия" in result["message"]
    assert "На какую дату" not in result["message"]
    assert "FREQ=" not in result["message"]
    events = stored(engine)
    assert events[0].event_date == first_date
    assert events[0].event_time == first_time
    series_ids = {event.recurrence_series_id for event in events}
    assert len(series_ids) == 1 and None not in series_ids
    assert all(event.recurrence_rule for event in events)
    if count is not None:
        assert len(events) == count
        assert f"Событий: {count}." in result["message"]
    assert pending_for(engine, result["conversation_id"]) is None
    return events


@pytest.mark.parametrize("message,event_type,title,rule,clock,first_date,event_time", [
    ("каждый день в 12:00 напоминай мне чистить зубы", "REMINDER", "Чистить зубы", "каждый день",
     "2026-10-19T11:00:00+03:00", date(2026, 10, 19), time(12)),
    ("напоминай мне каждый день в 12:00 чистить зубы", "REMINDER", "Чистить зубы", "каждый день",
     "2026-10-19T11:00:00+03:00", date(2026, 10, 19), time(12)),
    ("каждый день в 12:00 напоминай мне чистить зубы", "REMINDER", "Чистить зубы", "каждый день",
     "2026-10-19T12:00:01+03:00", date(2026, 10, 20), time(12)),
    # Implicit starts must be strictly future; equality advances to the next matching date.
    ("каждый день в 12:00 напоминай мне чистить зубы", "REMINDER", "Чистить зубы", "каждый день",
     "2026-10-19T12:00:00+03:00", date(2026, 10, 20), time(12)),
    ("по будням в 9 утра напоминай проверить почту", "REMINDER", "Проверить почту", "по будням",
     "2026-10-19T08:00:00+03:00", date(2026, 10, 19), time(9)),
    ("по будням в 09:00 напоминай проверить почту", "REMINDER", "Проверить почту", "по будням",
     "2026-10-23T10:00:00+03:00", date(2026, 10, 26), time(9)),
    ("по будням в 09:00 напоминай проверить почту", "REMINDER", "Проверить почту", "по будням",
     "2026-10-24T08:00:00+03:00", date(2026, 10, 26), time(9)),
    ("по будням в 09:00 напоминай проверить почту", "REMINDER", "Проверить почту", "по будням",
     "2026-10-25T08:00:00+03:00", date(2026, 10, 26), time(9)),
    ("каждый понедельник в 10 утра встреча команды", "MEETING", "Встреча команды", "каждый понедельник",
     "2026-10-19T09:00:00+03:00", date(2026, 10, 19), time(10)),
    ("каждый понедельник в 10:00 встреча команды", "MEETING", "Встреча команды", "каждый понедельник",
     "2026-10-19T10:00:01+03:00", date(2026, 10, 26), time(10)),
    ("каждый понедельник в 10:00 встреча команды", "MEETING", "Встреча команды", "каждый понедельник",
     "2026-10-20T09:00:00+03:00", date(2026, 10, 26), time(10)),
    ("каждый месяц 15 числа в 10 утра напомни оплатить счёт", "REMINDER", "Оплатить счёт",
     "каждый месяц 15 числа", "2026-10-15T09:00:00+03:00", date(2026, 10, 15), time(10)),
    ("каждый месяц 15 числа в 10 утра напомни оплатить счёт", "REMINDER", "Оплатить счёт",
     "каждый месяц 15 числа", "2026-10-15T11:00:00+03:00", date(2026, 11, 15), time(10)),
    ("каждый месяц 31 числа в 10:00 напомни оплатить счёт", "REMINDER", "Оплатить счёт",
     "каждый месяц 31 числа", "2026-04-20T09:00:00+03:00", date(2026, 5, 31), time(10)),
    ("каждый месяц 31 числа в 10:00 напомни оплатить счёт", "REMINDER", "Оплатить счёт",
     "каждый месяц 31 числа", "2026-01-31T11:00:00+03:00", date(2026, 3, 31), time(10)),
    ("каждую неделю в 12:00 напоминай проверить планы", "REMINDER", "Проверить планы", "каждую неделю",
     "2026-10-19T11:00:00+03:00", date(2026, 10, 19), time(12)),
    ("каждую неделю в 12:00 напоминай проверить планы", "REMINDER", "Проверить планы", "каждую неделю",
     "2026-10-19T13:00:00+03:00", date(2026, 10, 20), time(12)),
    # A UTC request clock must still use the Moscow calendar and local time.
    ("каждый день в 12:00 напоминай мне чистить зубы", "REMINDER", "Чистить зубы", "каждый день",
     "2026-10-19T09:00:01+00:00", date(2026, 10, 20), time(12)),
], ids=[
    "daily-before", "daily-natural-word-order", "daily-after", "daily-equal", "weekdays-before",
    "weekdays-friday-after", "weekdays-saturday", "weekdays-sunday", "monday-before", "monday-after",
    "monday-from-tuesday", "monthly-before", "monthly-after", "monthly-skip-april", "monthly-skip-february",
    "weekly-before", "weekly-after", "moscow-from-utc-clock",
])
def test_real_phrases_derive_first_occurrence_from_calendar_and_request_clock(
    db, integration_engine, fixed_clock, message, event_type, title, rule, clock, first_date, event_time,
):
    fixed_clock[0] = datetime.fromisoformat(clock)
    expression = "в 9 утра" if "в 9 утра" in message else "в 10 утра" if "в 10 утра" in message else f"в {event_time:%H:%M}"
    parsed = dict(intent="CREATE_EVENT", event_type=event_type, title=title,
                  recurrence_expression=rule, time_expression=expression)
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        result = post(client, message)
    events = assert_created_series(result, integration_engine, first_date, event_time)
    assert all(datetime.combine(event.event_date, event.event_time, tzinfo=TemporalResolver("Europe/Moscow").zone)
               > fixed_clock[0] for event in events)
    if rule == "каждую неделю":
        assert all((later.event_date - earlier.event_date).days == 7 for earlier, later in zip(events, events[1:]))


@pytest.mark.parametrize("weekday,rule", [
    (0, "каждый понедельник"), (1, "каждый вторник"), (2, "каждую среду"),
    (3, "каждый четверг"), (4, "каждую пятницу"), (5, "каждую субботу"), (6, "каждое воскресенье"),
])
def test_all_supported_weekdays_derive_nearest_matching_date(db, integration_engine, weekday, rule):
    message = f"{rule} в 12:00 напомни проверить планы"
    parsed = dict(intent="CREATE_EVENT", event_type="REMINDER", title="Проверить планы",
                  recurrence_expression=rule, time_expression="в 12:00")
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        result = post(client, message)
    events = assert_created_series(result, integration_engine, date(2026, 10, 19) + timedelta(days=weekday), time(12))
    assert all(event.event_date.weekday() == weekday for event in events)


@pytest.mark.parametrize("initial,title,rule,answer,expression,first_date,event_time", [
    ("каждый день напоминай чистить зубы", "Чистить зубы", "каждый день", "в 12:00", "в 12:00",
     date(2026, 10, 19), time(12)),
    ("по будням напоминай проверить почту", "Проверить почту", "по будням", "в 9 утра", "в 9 утра",
     date(2026, 10, 20), time(9)),
])
def test_time_only_followup_continues_persisted_recurrence_without_date_question(
    db, integration_engine, initial, title, rule, answer, expression, first_date, event_time,
):
    with client_for(integration_engine, FakeLLMProvider({initial: dict(
        intent="CREATE_EVENT", event_type="REMINDER", title=title, recurrence_expression=rule,
    )})) as client:
        first = post(client, initial)
    assert first["requires_input"]
    assert first["missing_fields"] == ["time"], first
    assert "На какую дату" not in first["message"]
    assert not stored(integration_engine)
    pending = pending_for(integration_engine, first["conversation_id"])
    assert pending["command"]["recurrence_expression"] == rule
    # A new client/session verifies that the time-only reply uses persisted state.
    with client_for(integration_engine, FakeLLMProvider({answer: dict(
        intent="CREATE_EVENT", time_expression=expression,
    )})) as client:
        result = post(client, answer, first["conversation_id"])
    assert_created_series(result, integration_engine, first_date, event_time)


@pytest.mark.parametrize("message,expression,first_date", [
    ("с завтрашнего дня каждый день в 12:00 напоминай чистить зубы", "завтрашнего дня", date(2026, 10, 20)),
    ("с 25 октября каждый день в 12:00 напоминай чистить зубы", "25 октября", date(2026, 10, 25)),
    ("с 1 ноября каждый день в 12:00 напомни принимать витамины", "1 ноября", date(2026, 11, 1)),
])
def test_explicit_future_start_has_priority_over_automatic_start(db, integration_engine, message, expression, first_date):
    title = "Принимать витамины" if "витамины" in message else "Чистить зубы"
    parsed = dict(intent="CREATE_EVENT", event_type="REMINDER", title=title,
                  date_expression=expression, recurrence_expression="каждый день", time_expression="в 12:00")
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        result = post(client, message)
    assert_created_series(result, integration_engine, first_date, time(12), count=90)


@pytest.mark.parametrize("answer,expected_count", [("да", 3), ("нет", 0), ("отмена", 0)])
def test_explicit_past_start_keeps_existing_confirmation_policy(db, integration_engine, answer, expected_count):
    message = "с 18.10.2026 каждый день 3 раза в 12:00 напоминай чистить зубы"
    parsed = dict(intent="CREATE_EVENT", event_type="REMINDER", title="Чистить зубы",
                  date_expression="18.10.2026", recurrence_expression="каждый день 3 раза", time_expression="в 12:00")
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        first = post(client, message)
    assert first["missing_fields"] == ["confirmation"]
    pending = pending_for(integration_engine, first["conversation_id"])
    assert pending["confirmation_kind"] == "past"
    assert pending["confirmation"]["occurrences"][0]["event_date"] == "2026-10-18"
    assert not stored(integration_engine)
    with client_for(integration_engine, FakeLLMProvider({})) as client:
        result = post(client, answer, first["conversation_id"])
    assert not result["requires_input"]
    events = stored(integration_engine)
    assert len(events) == expected_count
    if answer == "да":
        assert events[0].event_date == date(2026, 10, 18)
    assert pending_for(integration_engine, first["conversation_id"]) is None


@pytest.mark.parametrize("rule,expected_count,last_date", [
    ("каждый день 3 раза", 3, date(2026, 10, 21)),
    ("каждый день до 21.10.2026", 3, date(2026, 10, 21)),
])
def test_automatic_start_preserves_count_and_inclusive_until(db, integration_engine, rule, expected_count, last_date):
    message = f"{rule} в 12:00 напоминай чистить зубы"
    parsed = dict(intent="CREATE_EVENT", event_type="REMINDER", title="Чистить зубы",
                  recurrence_expression=rule, time_expression="в 12:00")
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        result = post(client, message)
    events = assert_created_series(result, integration_engine, date(2026, 10, 19), time(12), count=expected_count)
    assert events[-1].event_date == last_date


@pytest.mark.parametrize("answer,expected_count", [("да", 4), ("нет", 1), ("отмена", 1)])
def test_derived_meeting_start_still_checks_future_occurrence_conflicts_before_atomic_confirmation(
    db, integration_engine, fixed_clock, answer, expected_count,
):
    fixed_clock[0] = datetime.fromisoformat("2026-10-19T09:00:00+03:00")
    db.add(Event(type="meeting", title="Внешняя встреча", event_date=date(2026, 10, 26),
                 event_time=time(10), end_time=time(11), source="user"))
    db.commit()
    message = "каждый понедельник 3 раза в 10 утра встреча команды"
    parsed = dict(intent="CREATE_EVENT", event_type="MEETING", title="Встреча команды",
                  recurrence_expression="каждый понедельник 3 раза", time_expression="в 10 утра")
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        first = post(client, message)
    assert first["missing_fields"] == ["confirmation"], first
    pending = pending_for(integration_engine, first["conversation_id"])
    assert pending["confirmation_kind"] == "conflict"
    occurrences = pending["confirmation"]["occurrences"]
    assert [event["event_date"] for event in occurrences] == ["2026-10-19", "2026-10-26", "2026-11-02"]
    assert len(stored(integration_engine)) == 1
    with client_for(integration_engine, FakeLLMProvider({})) as client:
        result = post(client, answer, first["conversation_id"])
    assert not result["requires_input"]
    assert len(stored(integration_engine)) == expected_count
    if answer == "да":
        series = [event for event in stored(integration_engine) if event.recurrence_series_id]
        assert {str(event.recurrence_series_id) for event in series} == {occurrences[0]["recurrence_series_id"]}
    assert pending_for(integration_engine, first["conversation_id"]) is None


def test_ambiguous_bare_hour_still_requires_time_without_inventing_date_or_hour(db, integration_engine):
    message = "каждый день в 12 напоминай чистить зубы"
    parsed = dict(intent="CREATE_EVENT", event_type="REMINDER", title="Чистить зубы",
                  recurrence_expression="каждый день", time_expression="в 12")
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        result = post(client, message)
    assert result["requires_input"] and result["missing_fields"] == ["time"], result
    assert not stored(integration_engine)


@pytest.mark.parametrize("reply_clock", ["2026-10-19T13:00:00+03:00", "2026-10-20T00:01:00+03:00"])
def test_time_reply_rechecks_provisional_date_after_time_passes_or_midnight(
    db, integration_engine, fixed_clock, reply_clock,
):
    initial = "каждый день напоминай чистить зубы"
    provider = FakeLLMProvider({
        initial: dict(intent="CREATE_EVENT", event_type="REMINDER", title="Чистить зубы",
                      recurrence_expression="каждый день"),
        "в 12:00": dict(intent="CREATE_EVENT", time_expression="в 12:00"),
    })
    with client_for(integration_engine, provider) as client:
        first = post(client, initial)
    assert first["missing_fields"] == ["time"]
    assert pending_for(integration_engine, first["conversation_id"])["temporal"]["event_date"] == "2026-10-19"
    fixed_clock[0] = datetime.fromisoformat(reply_clock)
    with client_for(integration_engine, provider) as client:
        result = post(client, "в 12:00", first["conversation_id"])
    assert_created_series(result, integration_engine, date(2026, 10, 20), time(12), count=90)


def test_legacy_missing_date_pending_can_recover_from_repeated_recurrence_reply(db, integration_engine):
    command = ParsedCommand(
        intent="CREATE_EVENT", event_type="REMINDER", title="Чистить зубы",
        time_expression="в 12:00", recurrence_expression="каждый день",
    )
    pending = PendingAction(
        command=command, temporal=TemporalResult(event_time=time(12)),
        raw_user_text="каждый день в 12:00 напоминай мне чистить зубы", missing_fields=["date"],
    )
    conversation = Conversation(pending_action=pending.model_dump(mode="json"))
    db.add(conversation)
    db.commit()
    provider = FakeLLMProvider({"каждый день": dict(intent="CREATE_EVENT", recurrence_expression="каждый день")})
    with client_for(integration_engine, provider) as client:
        result = post(client, "каждый день", str(conversation.id))
    assert_created_series(result, integration_engine, date(2026, 10, 19), time(12), count=90)


@pytest.mark.parametrize("rule,clock", [
    ("каждый день 99999999 раз", "2026-10-19T11:00:00+03:00"),
    ("каждый день до 18.10.2026", "2026-10-19T11:00:00+03:00"),
    ("каждый день до 19.10.2026", "2026-10-19T13:00:00+03:00"),
    ("каждый день до 20.10.9999", "2026-10-19T11:00:00+03:00"),
    ("через день", "2026-10-19T11:00:00+03:00"),
], ids=["oversized-count", "until-in-past", "until-today-after-time", "until-too-far", "unsupported-pattern"])
def test_invalid_implicit_recurrence_returns_domain_clarification_without_rows(
    db, integration_engine, fixed_clock, rule, clock,
):
    fixed_clock[0] = datetime.fromisoformat(clock)
    message = f"{rule} в 12:00 напоминай чистить зубы"
    parsed = dict(intent="CREATE_EVENT", event_type="REMINDER", title="Чистить зубы",
                  time_expression="в 12:00", recurrence_expression=rule)
    with client_for(integration_engine, FakeLLMProvider({message: parsed})) as client:
        result = post(client, message)
    assert result["requires_input"]
    assert result["missing_fields"] == ["recurrence"], result
    assert not stored(integration_engine)


def test_explicit_date_repair_clears_expired_implicit_until_issue_and_keeps_past_confirmation(db, integration_engine):
    initial = "каждый день до 18.10.2026 в 12:00 напоминай чистить зубы"
    answer = "с 17.10.2026"
    provider = FakeLLMProvider({
        initial: dict(intent="CREATE_EVENT", event_type="REMINDER", title="Чистить зубы",
                      time_expression="в 12:00", recurrence_expression="каждый день до 18.10.2026"),
        answer: dict(intent="CREATE_EVENT", date_expression="с 17.10.2026"),
    })
    with client_for(integration_engine, provider) as client:
        first = post(client, initial)
    assert first["missing_fields"] == ["recurrence"]
    assert not stored(integration_engine)
    with client_for(integration_engine, provider) as client:
        repaired = post(client, answer, first["conversation_id"])
    assert repaired["missing_fields"] == ["confirmation"], repaired
    pending = pending_for(integration_engine, repaired["conversation_id"])
    assert pending["confirmation_kind"] == "past"
    assert "recurrence" not in pending["temporal"]["issues"]
    assert not stored(integration_engine)
    with client_for(integration_engine, FakeLLMProvider({})) as client:
        result = post(client, "да", repaired["conversation_id"])
    assert_created_series(result, integration_engine, date(2026, 10, 17), time(12), count=2)

