"""Safety and persistence checks for the first contextual update package."""
import json
from datetime import date, time

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from app.ai.provider import ProviderError
from app.models.entities import Conversation, Event
from app.schemas.commands import ParsedCommand
from tests import test_user_regressions
from tests.test_user_regressions import (
    CORRECT,
    LIST,
    MOVE,
    NOW,
    ORDER,
    REMIND,
    create_dima,
    seed_meetings,
)

harness = test_user_regressions.harness


@compiles(JSONB, "sqlite")
def sqlite_jsonb_for_context_test(element, compiler, **kw):
    # SQLite verifies ORM persistence only, not PostgreSQL-specific JSON operators.
    return "JSON"


@pytest.mark.parametrize("matches", [0, 2])
def test_update_requires_unique_match_and_can_be_clarified(harness, matches):
    service, conversation, session, provider = harness
    team, other, outside = seed_meetings(session)
    team.title = "Встреча с командой альфа" if matches else "Совещание альфа"
    other.title = "Встреча с командой бета" if matches else "Совещание бета"
    session.flush()
    service.handle(conversation, LIST, NOW)
    reply = service.handle(conversation, MOVE, NOW)
    assert reply.requires_input and reply.missing_fields == ["event_reference"]
    assert {event.id for event in reply.events} == ({team.id, other.id} if matches else set())
    assert (team.event_time, other.event_time, outside.event_time) == (
        time(15), time(15, 30), time(10),
    )
    provider.commands["альфа"] = {"intent": "UPDATE_EVENT", "search_query": "альфа"}
    reply = service.handle(conversation, "альфа", NOW)
    assert not reply.requires_input and reply.events[0].id == team.id
    assert team.event_time == time(16)
    assert other.event_time == time(15, 30)


def test_empty_new_list_replaces_previous_candidates(harness):
    service, conversation, session, provider = harness
    team, _, _ = seed_meetings(session)
    service.handle(conversation, LIST, NOW)
    provider.commands["послезавтра"] = {
        "intent": "LIST_EVENTS", "period_expression": "послезавтра",
    }
    assert service.handle(conversation, "послезавтра", NOW).events == []
    assert service.handle(conversation, MOVE, NOW).requires_input
    assert team.event_time == time(15)


def test_last_created_reference_does_not_search_unrelated_records(harness):
    service, conversation, session, provider = harness
    seed_meetings(session)
    assert service.handle(conversation, CORRECT, NOW).requires_input
    created = service.handle(conversation, ORDER, NOW).events[0]
    provider.commands["чужую через час"] = {
        "intent": "UPDATE_EVENT", "event_reference": "last_created",
        "search_query": "чужую", "date_expression": "через час",
    }
    assert service.handle(conversation, "чужую через час", NOW).requires_input
    assert session.get(Event, created.id).event_time == time(12, 59)


def test_relative_shift_crosses_midnight_and_refreshes_context(harness):
    service, conversation, _, provider = harness
    created = service.handle(conversation, ORDER, NOW).events[0]
    provider.commands["на два часа вперед"] = {
        "intent": "UPDATE_EVENT", "shift_expression": "на два часа вперед",
    }
    provider.commands["в 23:30"] = {"intent": "UPDATE_EVENT", "time_expression": "в 23:30"}
    assert service.handle(conversation, "в 23:30", NOW).events[0].id == created.id
    updated = service.handle(conversation, "на два часа вперед", NOW).events[0]
    assert (updated.event_date, updated.event_time) == (date(2026, 10, 16), time(1, 30))
    context = conversation.context["last_created_event"]
    assert context["event_date"] == "2026-10-16" and context["event_time"] == "01:30:00"


def test_update_without_change_asks_and_new_time_replaces_pending_shift(harness):
    service, conversation, _, provider = harness
    service.handle(conversation, ORDER, NOW)
    provider.commands["перенеси"] = {"intent": "UPDATE_EVENT"}
    assert service.handle(conversation, "перенеси", NOW).requires_input
    provider.commands["на сколько-то позже"] = {
        "intent": "UPDATE_EVENT", "shift_expression": "на сколько-то позже",
    }
    assert service.handle(conversation, "на сколько-то позже", NOW).requires_input
    assert service.handle(conversation, CORRECT, NOW).events[0].event_time == time(13, 59)


def test_explicit_reminder_subject_is_not_overwritten(harness):
    service, conversation, _, provider = harness
    create_dima(service, conversation)
    provider.commands["напомни купить хлеб через час"] = {
        "intent": "CREATE_EVENT", "event_type": "REMINDER", "title": "Купить хлеб",
        "date_expression": "через час",
    }
    result = service.handle(conversation, "напомни купить хлеб через час", NOW)
    assert result.events[0].title == "Купить хлеб"


def test_update_clarification_keeps_explicit_date(harness):
    service, conversation, _, provider = harness
    provider.commands["купить хлеб"] = {
        "intent": "CREATE_EVENT", "event_type": "TASK", "title": "Купить хлеб",
    }
    provider.commands["перенеси на завтра"] = {
        "intent": "UPDATE_EVENT", "date_expression": "на завтра",
    }
    provider.commands["в 16:00"] = {"intent": "UPDATE_EVENT", "time_expression": "в 16:00"}
    service.handle(conversation, "купить хлеб", NOW)
    assert service.handle(conversation, "перенеси на завтра", NOW).missing_fields == ["time"]
    updated = service.handle(conversation, "в 16:00", NOW).events[0]
    assert (updated.event_date, updated.event_time) == (date(2026, 10, 16), time(16))


@pytest.mark.parametrize("mode", ["created", "listed"])
def test_context_survives_commit_and_new_conversation_instance(harness, mode):
    service, conversation, session, _ = harness
    Conversation.__table__.create(session.get_bind())
    session.add(conversation)
    if mode == "created":
        original = create_dima(service, conversation)
    else:
        original, _, _ = seed_meetings(session)
        service.handle(conversation, LIST, NOW)
    conversation_id, event_id = conversation.id, original.id
    session.commit()
    session.expunge_all()
    restored = session.get(Conversation, conversation_id)
    result = service.handle(restored, REMIND if mode == "created" else MOVE, NOW)
    assert not result.requires_input
    if mode == "created":
        assert "Дим" in result.events[0].title and result.events[0].id != event_id
    else:
        assert result.events[0].id == event_id and result.events[0].event_time == time(16)


def test_only_visible_events_are_candidates_and_deleted_event_cannot_be_updated(harness):
    service, conversation, session, provider = harness
    service.responses.limit = 1
    team, other, _ = seed_meetings(session)
    service.handle(conversation, LIST, NOW)
    assert str(other.id) not in json.dumps(conversation.context)
    session.delete(team)
    session.flush()
    assert service.handle(conversation, MOVE, NOW).requires_input
    assert other.event_time == time(15, 30)


def test_shift_expression_has_same_anti_hallucination_guard(harness):
    service, conversation, _, provider = harness
    provider.commands["перенеси"] = {
        "intent": "UPDATE_EVENT", "shift_expression": "на два часа вперед",
    }
    with pytest.raises(ProviderError):
        service.handle(conversation, "перенеси", NOW)


def test_structured_contract_rejects_model_selected_id():
    with pytest.raises(ValueError):
        ParsedCommand.model_validate({"intent": "UPDATE_EVENT", "event_id": "invented"})
def test_update_named_event_uses_recent_listed_context(harness):
    service, conversation, session, provider = harness

    dima = Event(
        type="meeting",
        title="Встреча с Димой",
        event_date=date(2026, 10, 18),
        event_time=time(12, 0),
        source="user",
    )
    other = Event(
        type="meeting",
        title="Встреча с командой",
        event_date=date(2026, 10, 18),
        event_time=time(14, 0),
        source="user",
    )
    session.add_all([dima, other])
    session.flush()

    provider.commands["Покажи планы на 18 октября"] = {
        "intent": "LIST_EVENTS",
        "period_expression": "18 октября",
    }
    listed = service.handle(conversation, "Покажи планы на 18 октября", NOW)

    assert {event.id for event in listed.events} == {dima.id, other.id}

    provider.commands["Встречу с Димой перенеси на 20 октября в 15:00"] = {
        "intent": "UPDATE_EVENT",
        "title": "Встреча с Димой",
        "date_expression": "20 октября",
        "time_expression": "15:00",
    }

    reply = service.handle(
        conversation,
        "Встречу с Димой перенеси на 20 октября в 15:00",
        NOW,
    )

    assert not reply.requires_input
    assert reply.events[0].id == dima.id
    assert dima.event_date == date(2026, 10, 20)
    assert dima.event_time == time(15, 0)
    assert other.event_date == date(2026, 10, 18)
    assert other.event_time == time(14, 0)
def test_named_update_survives_unrelated_turn_after_list(harness):
    service, conversation, session, provider = harness

    dima = Event(
        type="meeting",
        title="Встреча с Димой",
        event_date=date(2026, 10, 18),
        event_time=time(12, 0),
        source="user",
    )
    other = Event(
        type="task",
        title="Купить продукты",
        event_date=date(2026, 10, 18),
        source="user",
    )
    session.add_all([dima, other])
    session.flush()

    provider.commands["Покажи планы на 18 октября"] = {
        "intent": "LIST_EVENTS",
        "period_expression": "18 октября",
    }
    listed = service.handle(conversation, "Покажи планы на 18 октября", NOW)
    assert {event.id for event in listed.events} == {dima.id, other.id}

    # Реплика на другую тему не должна уничтожать контекст показанных событий.
    provider.commands["Расскажи что-нибудь интересное"] = {
        "intent": "UNKNOWN",
    }
    service.handle(conversation, "Расскажи что-нибудь интересное", NOW)

    provider.commands["Встречу с Димой перенеси на 19 октября в 15:00"] = {
        "intent": "UPDATE_EVENT",
        "title": "Встреча с Димой",
        "date_expression": "19 октября",
        "time_expression": "15:00",
    }

    reply = service.handle(
        conversation,
        "Встречу с Димой перенеси на 19 октября в 15:00",
        NOW,
    )

    assert not reply.requires_input
    assert reply.events[0].id == dima.id
    assert dima.event_date == date(2026, 10, 19)
    assert dima.event_time == time(15, 0)
def test_update_clarification_keeps_original_date_and_time(harness):
    service, conversation, session, provider = harness

    dima = Event(
        type="meeting",
        title="Встреча с Димой",
        event_date=date(2026, 10, 18),
        event_time=time(12, 0),
        source="user",
    )
    team = Event(
        type="meeting",
        title="Встреча с командой",
        event_date=date(2026, 10, 18),
        event_time=time(14, 0),
        source="user",
    )
    session.add_all([dima, team])
    session.flush()

    provider.commands["Перенеси встречу на 19 октября в 15:00"] = {
        "intent": "UPDATE_EVENT",
        "search_query": "встречу",
        "date_expression": "19 октября",
        "time_expression": "15:00",
    }

    first = service.handle(
        conversation,
        "Перенеси встречу на 19 октября в 15:00",
        NOW,
    )

    assert first.requires_input
    assert first.missing_fields == ["event_reference"]

    provider.commands["Встречу с Димой"] = {
        "intent": "UPDATE_EVENT",
        "title": "Встреча с Димой",
    }

    second = service.handle(
        conversation,
        "Встречу с Димой",
        NOW,
    )

    assert not second.requires_input
    assert second.events[0].id == dima.id
    assert dima.event_date == date(2026, 10, 19)
    assert dima.event_time == time(15, 0)

    assert team.event_date == date(2026, 10, 18)
    assert team.event_time == time(14, 0)
def test_update_meeting_overlap_requires_conflict_confirmation(harness):
    service, conversation, session, provider = harness

    target = Event(
        type="meeting",
        title="Планирование",
        event_date=date(2026, 10, 16),
        event_time=time(14, 0),
        end_time=time(15, 0),
    )
    blocker = Event(
        type="meeting",
        title="Созвон с командой",
        event_date=date(2026, 10, 16),
        event_time=time(15, 30),
        end_time=time(16, 30),
    )
    session.add_all([target, blocker])
    session.flush()

    text = "планирование перенеси с 16:00 до 17:00"
    provider.commands[text] = {
        "intent": "UPDATE_EVENT",
        "search_query": "Планирование",
        "time_expression": "с 16:00 до 17:00",
    }

    reply = service.handle(conversation, text, NOW)

    assert reply.requires_input
    assert reply.missing_fields == ["confirmation"]
    assert conversation.pending_action["confirmation_kind"] == "conflict"
    assert target.event_time == time(14, 0)
    assert target.end_time == time(15, 0)
def test_update_meeting_conflict_yes_moves_event(harness):
    service, conversation, session, provider = harness

    target = Event(
        type="meeting",
        title="Планирование",
        event_date=date(2026, 10, 16),
        event_time=time(14, 0),
        end_time=time(15, 0),
    )
    blocker = Event(
        type="meeting",
        title="Созвон с командой",
        event_date=date(2026, 10, 16),
        event_time=time(15, 30),
        end_time=time(16, 30),
    )
    session.add_all([target, blocker])
    session.flush()

    text = "планирование перенеси с 16:00 до 17:00"
    provider.commands[text] = {
        "intent": "UPDATE_EVENT",
        "search_query": "Планирование",
        "time_expression": "с 16:00 до 17:00",
    }

    reply = service.handle(conversation, text, NOW)
    assert reply.requires_input

    reply = service.handle(conversation, "да", NOW)

    assert not reply.requires_input
    assert target.event_time == time(16, 0)
    assert target.end_time == time(17, 0)
    assert conversation.pending_action is None
def test_update_meeting_conflict_no_keeps_original_event(harness):
    service, conversation, session, provider = harness

    target = Event(
        type="meeting",
        title="Планирование",
        event_date=date(2026, 10, 16),
        event_time=time(14, 0),
        end_time=time(15, 0),
    )
    blocker = Event(
        type="meeting",
        title="Созвон с командой",
        event_date=date(2026, 10, 16),
        event_time=time(15, 30),
        end_time=time(16, 30),
    )
    session.add_all([target, blocker])
    session.flush()

    text = "планирование перенеси с 16:00 до 17:00"
    provider.commands[text] = {
        "intent": "UPDATE_EVENT",
        "search_query": "Планирование",
        "time_expression": "с 16:00 до 17:00",
    }

    reply = service.handle(conversation, text, NOW)
    assert reply.requires_input

    reply = service.handle(conversation, "нет", NOW)

    assert not reply.requires_input
    assert target.event_time == time(14, 0)
    assert target.end_time == time(15, 0)
    assert conversation.pending_action is None
def test_update_to_adjacent_meeting_does_not_conflict(harness):
    service, conversation, session, provider = harness

    target = Event(
        type="meeting",
        title="Планирование",
        event_date=date(2026, 10, 16),
        event_time=time(14, 0),
        end_time=time(15, 0),
    )
    blocker = Event(
        type="meeting",
        title="Созвон с командой",
        event_date=date(2026, 10, 16),
        event_time=time(15, 0),
        end_time=time(16, 0),
    )
    session.add_all([target, blocker])
    session.flush()

    text = "планирование перенеси с 16:00 до 17:00"
    provider.commands[text] = {
        "intent": "UPDATE_EVENT",
        "search_query": "Планирование",
        "time_expression": "с 16:00 до 17:00",
    }

    reply = service.handle(conversation, text, NOW)

    assert not reply.requires_input
    assert target.event_time == time(16, 0)
    assert target.end_time == time(17, 0)
def test_update_meeting_does_not_conflict_with_itself(harness):
    service, conversation, session, provider = harness

    target = Event(
        type="meeting",
        title="Планирование",
        event_date=date(2026, 10, 16),
        event_time=time(14, 0),
        end_time=time(15, 0),
    )
    session.add(target)
    session.flush()

    text = "планирование перенеси с 14:30 до 15:30"
    provider.commands[text] = {
        "intent": "UPDATE_EVENT",
        "search_query": "Планирование",
        "time_expression": "с 14:30 до 15:30",
    }

    reply = service.handle(conversation, text, NOW)

    assert not reply.requires_input
    assert target.event_time == time(14, 30)
    assert target.end_time == time(15, 30)