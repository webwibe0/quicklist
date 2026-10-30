"""Manual UX acceptance targets; deferred business rules remain deliberately red.

Scripted structured LLM output tests deterministic layers, NOT live NLU accuracy.
SQLite stores only Event; pending Conversation JSON is round-tripped in memory.
No PostgreSQL JSONB/search/concurrency or frontend rendering claims are made here.
"""
import json
from datetime import date, datetime, time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.ai.provider import ProviderError
from app.models.entities import Conversation, Event
from app.repositories.events import EventRepository
from app.schemas.dialogue import PendingAction
from app.services.dialogue import DialogueService
from app.services.responses import ResponseBuilder
from app.temporal.resolver import TemporalAmbiguity, TemporalResolver
from app.use_cases.actions import ActionRouter
from tests.fakes import FakeLLMProvider

NOW = datetime.fromisoformat("2026-10-15T12:00:00+03:00")
TOMORROW = date(2026, 10, 16)


@pytest.fixture
def ux(monkeypatch):
    def no_live_model(*args, **kwargs):
        pytest.fail("Live LLM calls are forbidden in manual UX regressions")

    monkeypatch.setattr("app.ai.openai_provider.OpenAI", no_live_model)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Event.__table__.create(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            provider = FakeLLMProvider({})
            service = DialogueService(
                provider, TemporalResolver("Europe/Moscow"),
                ActionRouter(EventRepository(session, 50)), ResponseBuilder(50),
            )
            conversation = Conversation(id=uuid4(), pending_action=None, last_event_id=None)
            yield service, conversation, session, provider
    finally:
        engine.dispose()


def handle(ux, text, command):
    service, conversation, _, provider = ux
    provider.commands[text] = command
    try:
        return service.handle(conversation, text, NOW)
    except ProviderError as exc:
        pytest.fail(f"parser/contract: structured output rejected: {exc}")


def reminder(**fields):
    return {"intent": "CREATE_EVENT", "event_type": "REMINDER", "title": "Позвонить",
            "date_expression": "завтра", **fields}


def rows(ux):
    return ux[2].scalars(select(Event)).all()


def waiting_for_time(ux):
    reply = handle(ux, "напомни завтра позвонить", reminder())
    assert reply.requires_input and reply.missing_fields == ["time"]
    assert not rows(ux)


@pytest.mark.parametrize("llm_date", ["зааавтрааа", "завтра"], ids=["verbatim", "canonical"])
def test_01_stretched_tomorrow_keeps_date(ux, llm_date):
    reply = handle(ux, "напомни зааавтрааа в 9 позвонить",
                   reminder(date_expression=llm_date, time_expression="в 9"))
    pending = PendingAction.model_validate(ux[1].pending_action) if ux[1].pending_action else None
    day = reply.events[0].event_date if reply.events else pending.temporal.event_date
    assert day == TOMORROW, f"normalize/temporal: lost tomorrow; {reply.message}"
    assert reply.requires_input and reply.missing_fields == ["time"]
    assert "09:00" in reply.message and "21:00" in reply.message
    reply = handle(ux, "в 9 утра", {"intent": "CREATE_EVENT", "time_expression": "в 9 утра"})
    assert not reply.requires_input
    assert (reply.events[0].event_date, reply.events[0].event_time) == (TOMORROW, time(9))
    assert rows(ux)[0].raw_user_text.startswith("напомни зааавтрааа")


def test_01_typo_policy_requires_date_correction(ux):
    # Current prompt/guard require verbatim expressions: no fuzzy autocorrection policy.
    reply = handle(ux, "напомни помпезавтра в 9 утра позвонить",
                   reminder(date_expression="помпезавтра", time_expression="в 9 утра"))
    assert reply.requires_input and reply.missing_fields == ["date"]
    assert not rows(ux)
    reply = handle(ux, "послезавтра", {"intent": "CREATE_EVENT", "date_expression": "послезавтра"})
    assert not reply.requires_input
    assert (reply.events[0].event_date, reply.events[0].event_time) == (date(2026, 10, 17), time(9))


def test_02_call_without_recipient_is_valid(ux):
    reply = handle(ux, "напомни завтра в 8 утра позвонить", reminder(time_expression="в 8 утра"))
    assert not reply.requires_input and ux[1].pending_action is None
    assert len(rows(ux)) == 1
    event, = reply.events
    assert (event.type, event.title, event.event_date, event.event_time) == (
        "reminder", "Позвонить", TOMORROW, time(8),
    )


@pytest.mark.parametrize("scheduled", [False, True], ids=["no-schedule", "complete-schedule"])
def test_02_unresolved_him_requires_referent(ux, scheduled):
    text = "напомни позвонить ему" + (" завтра в 8 утра" if scheduled else "")
    reply = handle(ux, text, reminder(
        title="Позвонить ему", date_expression="завтра" if scheduled else None,
        time_expression="в 8 утра" if scheduled else None,
    ))
    assert not rows(ux), "dialogue/service: unresolved 'ему' was written to DB"
    assert reply.requires_input
    assert any(word in reply.message.casefold() for word in ("кому", "кого", "имеете в виду")), (
        f"dialogue: missing referent clarification; {reply.message}"
    )


def test_03_exact_duplicate_needs_confirmation_before_second_write(ux):
    text = "напомни завтра в 8 утра позвонить"
    command = reminder(time_expression="в 8 утра")
    first = handle(ux, text, command)
    assert not first.requires_input and len(rows(ux)) == 1
    reply = handle(ux, text, command)
    assert len(rows(ux)) == 1, "service/storage: exact duplicate silently inserted a second row"
    assert rows(ux)[0].id == first.events[0].id
    assert reply.requires_input and ux[1].pending_action
    assert "уже" in reply.message.casefold() and "ещ" in reply.message.casefold()


def seed_days(ux):
    events = [Event(type="task", title=f"План {day}", event_date=date(2026, 10, day))
              for day in (15, 16, 17, 18)]
    ux[2].add_all(events)
    ux[2].flush()
    return {event.id for event in events[1:3]}


def test_04_list_tomorrow_and_day_after_returns_both_days(ux):
    expected = seed_days(ux)
    reply = handle(ux, "покажи планы на завтра и послезавтра", {
        "intent": "LIST_EVENTS", "period_expression": "завтра и послезавтра",
    })
    assert not reply.requires_input, f"temporal: two-day period rejected; {reply.message}"
    assert reply.action == "LIST_EVENTS" and {event.id for event in reply.events} == expected
    assert len(rows(ux)) == 4


def test_04_yearless_date_pair_completes_pending_list(ux):
    expected = seed_days(ux)
    first = handle(ux, "покажи планы", {"intent": "LIST_EVENTS"})
    assert first.requires_input and first.missing_fields == ["period"]
    reply = handle(ux, "16.10 и 17.10", {
        "intent": "LIST_EVENTS", "period_expression": "16.10 и 17.10",
    })
    assert not reply.requires_input, f"temporal/dialogue: date pair rejected again; {reply.message}"
    assert {event.id for event in reply.events} == expected
    assert ux[1].pending_action is None


@pytest.mark.parametrize("followup", [False, True], ids=["explicit-past", "clarified-past"])
def test_05_past_create_requires_confirmation_or_correction_before_write(ux, followup):
    text = "напомни сегодня в 9 заснуть" if followup else "напомни сегодня в 9 утра заснуть"
    reply = handle(ux, text, reminder(
        title="Заснуть", date_expression="сегодня",
        time_expression="в 9" if followup else "в 9 утра",
    ))
    if followup:
        assert reply.requires_input and reply.missing_fields == ["time"]
        assert not rows(ux)
        reply = handle(ux, "в 9 утра", {"intent": "CREATE_EVENT", "time_expression": "в 9 утра"})
    assert not rows(ux), "service: CREATE inserted 09:00 today although now is 12:00"
    assert reply.requires_input and ux[1].pending_action
    assert any(word in reply.message.casefold() for word in ("прош", "друг", "подтвер"))


def test_06_two_times_followup_is_batch_or_explicit_batch_confirmation(ux):
    first = handle(ux, "напомни завтра поесть в 7", reminder(title="Поесть", time_expression="в 7"))
    assert first.requires_input and first.missing_fields == ["time"]
    reply = handle(ux, "и в 7 утра и в 7 вечера", {
        "intent": "CREATE_EVENT", "time_expression": "и в 7 утра и в 7 вечера",
    })
    if reply.requires_input:
        assert not rows(ux)
        assert "confirmation" in reply.missing_fields, f"contract/dialogue: batch lost; {reply.message}"
        assert "07:00" in reply.message and "19:00" in reply.message
        assert ux[1].pending_action
    else:
        assert len(rows(ux)) == 2
        assert {(e.event_date, e.event_time, e.end_time) for e in rows(ux)} == {
            (TOMORROW, time(7), None), (TOMORROW, time(19), None),
        }
        assert all(e.type == "reminder" and e.title == "Поесть" for e in rows(ux))


def evening_followup(ux):
    waiting_for_time(ux)
    # Simulate persisted JSON restoration without pretending SQLite supports JSONB.
    ux[1].pending_action = json.loads(json.dumps(ux[1].pending_action))
    return handle(ux, "вечером", {"intent": "CREATE_EVENT", "time_expression": "вечером"})


def test_07_evening_preserves_typed_time_slot(ux):
    reply = evening_followup(ux)
    reply = handle(ux, "вечером", {"intent": "CREATE_EVENT", "time_expression": "вечером"})
    pending = PendingAction.model_validate(ux[1].pending_action)
    assert reply.requires_input and reply.missing_fields == pending.missing_fields == ["time"]
    assert pending.temporal.event_date == TOMORROW
    assert pending.command.title == "Позвонить" and pending.command.event_type == "REMINDER"
    assert not rows(ux)
    reply = handle(ux, "в 19:00", {"intent": "CREATE_EVENT", "time_expression": "в 19:00"})
    assert not reply.requires_input and len(rows(ux)) == 1
    assert (reply.events[0].event_date, reply.events[0].event_time) == (TOMORROW, time(19))


def test_07_evening_has_human_time_question(ux):
    reply = evening_followup(ux)
    message = reply.message.casefold()
    assert "вечер" in message and any(word in message for word in ("во сколько", "время")), (
        f"temporal/dialogue: expected 'во сколько вечером?'; got {reply.message}"
    )
    assert "число цифрами" not in message


def test_08_any_time_reminder_explains_requirement_without_loop(ux):
    waiting_for_time(ux)
    for _ in range(2):
        reply = handle(ux, "в любое время", {
            "intent": "CREATE_EVENT", "time_expression": "в любое время",
        })
        assert reply.requires_input and not rows(ux)
        message = reply.message.casefold()
        assert "напоминан" in message and "точн" in message and "врем" in message, (
            f"dialogue: no explanation why REMINDER needs an exact time; {reply.message}"
        )
        assert "задач" in message and "укажите" in message
    reply = handle(ux, "напомни завтра в 19:00 позвонить", reminder(time_expression="в 19:00"))
    assert not reply.requires_input and len(rows(ux)) == 1
    assert (reply.events[0].event_date, reply.events[0].event_time) == (TOMORROW, time(19))


def test_08_any_time_repetition_is_safe_and_precise_answer_recovers(ux):
    waiting_for_time(ux)
    for _ in range(2):
        reply = handle(ux, "в любое время", {
            "intent": "CREATE_EVENT", "time_expression": "в любое время",
        })
        assert reply.requires_input and reply.missing_fields == ["confirmation"]
        assert not rows(ux)
        assert ux[1].pending_action["confirmation_kind"] == "task_conversion"
    reply = handle(ux, "напомни завтра в 19:00 позвонить", reminder(time_expression="в 19:00"))
    assert not reply.requires_input and len(rows(ux)) == 1
    assert (reply.events[0].event_date, reply.events[0].event_time) == (TOMORROW, time(19))


@pytest.mark.parametrize("expression", ["25", "24:10", "от 00:00 до 23:59"])
def test_09_invalid_expression_is_not_one_exact_time(expression):
    with pytest.raises(TemporalAmbiguity):
        TemporalResolver("Europe/Moscow").resolve_time(expression)


@pytest.mark.parametrize("expression", ["25", "24:10", "от 00:00 до 23:59"])
def test_09_invalid_followup_keeps_time_slot_and_does_not_write(ux, expression):
    waiting_for_time(ux)
    reply = handle(ux, expression, {"intent": "CREATE_EVENT", "time_expression": expression})
    assert reply.requires_input and not rows(ux)
    assert reply.missing_fields == ["time"], f"dialogue: invalid time changed expected slot; {reply.message}"
    assert ux[1].pending_action["missing_fields"] == ["time"]
    assert ux[1].pending_action["temporal"]["event_date"] == TOMORROW.isoformat()


@pytest.mark.parametrize("expression", ["25", "24:10", "от 00:00 до 23:59"])
def test_09_invalid_time_diagnostic_is_contextual(ux, expression):
    waiting_for_time(ux)
    reply = handle(ux, expression, {"intent": "CREATE_EVENT", "time_expression": expression})
    message = reply.message.casefold()
    assert "врем" in message and "число цифрами" not in message, reply.message
    if expression.startswith("от"):
        assert any(word in message for word in ("одно", "один", "диапазон", "интервал")), (
            f"temporal/dialogue: explain interval versus one reminder time; {reply.message}"
        )
    else:
        assert "некоррект" in message or "недопустим" in message, reply.message


@pytest.mark.parametrize("expression", ["от 00:00 до 23:59", "с 00:00 до 23:59", "00:00–23:59"])
def test_09_range_then_exact_time_does_not_leave_interval_in_pending_or_db(ux, expression):
    waiting_for_time(ux)
    reply = handle(ux, expression, {"intent": "CREATE_EVENT", "time_expression": expression})
    assert reply.missing_fields == ["time"] and "диапазон" in reply.message
    pending = PendingAction.model_validate(ux[1].pending_action)
    assert not pending.temporal.interval_requested and pending.temporal.end_time is None
    reply = handle(ux, "19:00", {"intent": "CREATE_EVENT", "time_expression": "19:00"})
    assert not reply.requires_input and ux[1].pending_action is None
    event, = rows(ux)
    assert (event.event_date, event.event_time, event.end_time) == (TOMORROW, time(19), None)


def test_08_any_time_can_be_replaced_by_explicit_new_task(ux):
    waiting_for_time(ux)
    reply = handle(ux, "в любое время", {"intent": "CREATE_EVENT", "time_expression": "в любое время"})
    assert reply.missing_fields == ["confirmation"] and not rows(ux)
    reply = handle(ux, "создай задачу завтра позвонить без времени", {
        "intent": "CREATE_EVENT", "event_type": "TASK", "title": "Позвонить",
        "date_expression": "завтра",
    })
    assert not reply.requires_input and ux[1].pending_action is None
    event, = rows(ux)
    assert (event.type, event.event_date, event.event_time, event.end_time) == (
        "task", TOMORROW, None, None,
    )


def test_10_interval_survives_storage_and_api_response_dto(ux):
    reply = handle(ux, "встреча завтра с 12:00 до 23:00", {
        "intent": "CREATE_EVENT", "event_type": "MEETING", "title": "Встреча",
        "date_expression": "завтра", "time_expression": "с 12:00 до 23:00",
    })
    assert not reply.requires_input
    event, = reply.events
    ux[2].commit()
    # A new connection/session must read the saved interval, not the identity map.
    with Session(ux[2].bind) as fresh:
        stored = fresh.get(Event, event.id)
        assert (stored.event_date, stored.event_time, stored.end_time) == (TOMORROW, time(12), time(23))
    payload = reply.model_dump(mode="json")["events"][0]
    # API calls the interval start event_time, not start_time; keep the existing DTO.
    assert payload["event_time"] == "12:00:00" and payload["end_time"] == "23:00:00"
    assert ux[1].context["last_created_event"]["end_time"] == "23:00:00"
    listed = handle(ux, "покажи планы на завтра", {"intent": "LIST_EVENTS", "period_expression": "завтра"})
    assert [item.id for item in listed.events] == [event.id]
    listed_payload = listed.model_dump(mode="json")["events"][0]
    assert (listed_payload["event_time"], listed_payload["end_time"]) == ("12:00:00", "23:00:00")
