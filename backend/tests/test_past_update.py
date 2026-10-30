"""Past updates require explicit consent; all time checks and replies are deterministic."""
from copy import deepcopy
from datetime import UTC, date, time, timedelta

import pytest

from app.models.entities import Conversation, Event
from app.schemas.commands import EventResponse
from tests import test_context_updates
from tests.test_user_regressions import LIST, NOW, ORDER, seed_meetings

harness = test_context_updates.harness  # Also registers SQLite JSONB compilation.


def other_event_context(conversation, session):
    # Change the surrounding stored context without issuing a new command that cancels consent.
    other = Event(type="task", title="Другое событие", event_date=NOW.date(), event_time=time(12, 59))
    session.add(other)
    session.flush()
    conversation.last_event_id = other.id
    conversation.context = (conversation.context or {}) | {
        "last_created_event": EventResponse.model_validate(other).model_dump(mode="json"),
        "last_event_scope": "last_created",
    }
    return other


def request_shift(harness, expression="на два часа назад", now=NOW):
    service, conversation, session, provider = harness
    created = service.handle(conversation, ORDER, NOW).events[0]
    provider.commands[expression] = {"intent": "UPDATE_EVENT", "shift_expression": expression}
    return created, service.handle(conversation, expression, now)


@pytest.mark.parametrize("field,expression,day,clock", [
    ("shift_expression", "на два часа назад", date(2026, 10, 15), time(10, 59)),
    ("shift_expression", "на день назад", date(2026, 10, 14), time(12, 59)),
    ("time_expression", "на два часа раньше", date(2026, 10, 15), time(10, 59)),
    ("time_expression", "в 10:00", date(2026, 10, 15), time(10)),
])
def test_past_update_requires_confirmation_before_any_db_write(harness, field, expression, day, clock):
    service, conversation, session, provider = harness
    created = service.handle(conversation, ORDER, NOW).events[0]
    context = deepcopy(conversation.context)
    provider.commands[expression] = {"intent": "UPDATE_EVENT", field: expression}
    reply = service.handle(conversation, expression, NOW.astimezone(UTC))
    assert reply.requires_input and reply.missing_fields == ["confirmation"]
    assert not reply.events
    assert day.strftime("%d.%m.%Y") in reply.message
    assert clock.strftime("%H:%M") in reply.message
    assert "прошло" in reply.message and "перенести" in reply.message.lower()
    assert conversation.pending_action is not None
    assert conversation.context == context
    session.expire_all()
    event = session.get(Event, created.id)
    assert (event.event_date, event.event_time) == (created.event_date, created.event_time)


def test_forward_shift_of_old_event_also_requires_confirmation(harness):
    _, reply = request_shift(harness, "на час вперед", NOW + timedelta(days=2))
    assert reply.requires_input and reply.missing_fields == ["confirmation"]


@pytest.mark.parametrize("answer", ["да", "Да, всё равно перенеси!", "подтверждаю"])
def test_confirm_restored_pending_applies_frozen_time_to_original_event(harness, monkeypatch, answer):
    service, conversation, session, provider = harness
    original, reply = request_shift(harness)
    assert reply.requires_input
    # Persist and reload the actual JSON state; change both the anchor and context.
    Conversation.__table__.create(session.get_bind())
    session.add(conversation)
    conversation_id = conversation.id
    session.commit()
    session.expunge_all()
    conversation = session.get(Conversation, conversation_id)
    session.get(Event, original.id).event_time = time(18)
    session.flush()
    other = other_event_context(conversation, session)

    def unexpected(*args, **kwargs):
        pytest.fail("Confirmation must use saved data without LLM or temporal recomputation")

    monkeypatch.setattr(provider, "parse_command", unexpected)
    monkeypatch.setattr(service.resolver, "resolve_update", unexpected)
    result = service.handle(conversation, answer, NOW + timedelta(days=3))
    assert not result.requires_input and result.events[0].id == original.id
    assert (result.events[0].event_date, result.events[0].event_time) == (
        date(2026, 10, 15), time(10, 59),
    )
    assert conversation.pending_action is None
    assert session.get(Event, other.id).event_time == time(12, 59)
    assert conversation.context["last_created_event"]["id"] == str(other.id)


@pytest.mark.parametrize("answer", ["нет", "не надо", "отмена", "отмени", "нет, не переноси"])
def test_decline_clears_pending_without_changing_event(harness, monkeypatch, answer):
    service, conversation, session, provider = harness
    original, reply = request_shift(harness)
    assert reply.requires_input
    monkeypatch.setattr(provider, "parse_command", lambda *args: pytest.fail("LLM called"))
    result = service.handle(conversation, answer, NOW)
    assert not result.requires_input and not result.events
    assert conversation.pending_action is None
    assert session.get(Event, original.id).event_time == time(12, 59)
    # A later yes must not revive the cancelled update.
    monkeypatch.undo()
    service.handle(conversation, "да", NOW)
    assert session.get(Event, original.id).event_time == time(12, 59)


@pytest.mark.parametrize("answer", ["возможно", "да нет", "да, но завтра"])
def test_ambiguous_reply_does_not_count_as_consent(harness, answer):
    service, conversation, session, _ = harness
    original, _ = request_shift(harness)
    pending = deepcopy(conversation.pending_action)
    reply = service.handle(conversation, answer, NOW)
    assert reply.requires_input and conversation.pending_action == pending
    assert session.get(Event, original.id).event_time == time(12, 59)


@pytest.mark.parametrize("offset,expected_confirmation", [(0, False), (1, True), (-1, False)])
def test_strict_past_boundary_in_moscow(harness, offset, expected_confirmation):
    _, reply = request_shift(harness, "на час назад", NOW + timedelta(seconds=offset))
    assert reply.requires_input is expected_confirmation


@pytest.mark.parametrize("status", ["deleted", "cancelled", "completed"])
def test_confirmation_never_retargets_missing_or_inactive_event(harness, status):
    service, conversation, session, _ = harness
    original, reply = request_shift(harness)
    assert reply.requires_input
    event = session.get(Event, original.id)
    if status == "deleted":
        session.delete(event)
    else:
        event.status = status
    session.flush()
    other = other_event_context(conversation, session)
    reply = service.handle(conversation, "да", NOW)
    assert not reply.events and conversation.pending_action is None
    assert session.get(Event, other.id).event_time == time(12, 59)
    if status != "deleted":
        assert event.event_time == time(12, 59)


def test_confirm_refreshes_both_contexts_and_preserves_listed_selection(harness):
    service, conversation, session, provider = harness
    team, other, _ = seed_meetings(session)
    service.handle(conversation, LIST, NOW)
    provider.commands["с командой на два дня назад"] = {
        "intent": "UPDATE_EVENT", "search_query": "командой",
        "shift_expression": "на два дня назад",
    }
    reply = service.handle(conversation, "с командой на два дня назад", NOW)
    assert reply.requires_input and team.event_time == time(15)
    result = service.handle(conversation, "да", NOW)
    assert result.events[0].id == team.id
    assert team.event_date == date(2026, 10, 14)
    assert other.event_date == date(2026, 10, 16)
    snapshot = next(e for e in conversation.context["last_listed_events"] if e["id"] == str(team.id))
    assert snapshot["event_date"] == "2026-10-14"


def test_new_update_replaces_confirmation_without_reusing_old_shift(harness):
    service, conversation, _, provider = harness
    original, reply = request_shift(harness)
    assert reply.requires_input
    provider.commands["перенеси в 16:00"] = {"intent": "UPDATE_EVENT", "time_expression": "в 16:00"}
    result = service.handle(conversation, "перенеси в 16:00", NOW)
    assert not result.requires_input and result.events[0].id == original.id
    assert result.events[0].event_time == time(16)
    assert conversation.pending_action is None


def test_confirm_refreshes_last_created_and_last_listed_together(harness):
    service, conversation, session, _ = harness
    original, reply = request_shift(harness)
    assert reply.requires_input
    conversation.context = conversation.context | {
        "last_listed_events": [EventResponse.model_validate(session.get(Event, original.id)).model_dump(mode="json")],
    }
    result = service.handle(conversation, "да", NOW)
    assert result.events[0].id == original.id
    assert conversation.context["last_created_event"]["event_time"] == "10:59:00"
    assert conversation.context["last_listed_events"][0]["event_time"] == "10:59:00"


def test_target_clarification_finishes_before_past_confirmation(harness):
    service, conversation, session, provider = harness
    team, other, _ = seed_meetings(session)
    service.handle(conversation, LIST, NOW)
    provider.commands["перенеси на два дня назад"] = {
        "intent": "UPDATE_EVENT", "event_reference": "last_listed",
        "shift_expression": "на два дня назад",
    }
    provider.commands["с командой"] = {"intent": "UPDATE_EVENT", "search_query": "командой"}
    reply = service.handle(conversation, "перенеси на два дня назад", NOW)
    assert reply.missing_fields == ["event_reference"]
    reply = service.handle(conversation, "с командой", NOW)
    assert reply.missing_fields == ["confirmation"]
    assert team.event_date == date(2026, 10, 16)
    result = service.handle(conversation, "да", NOW)
    assert result.events[0].id == team.id and team.event_date == date(2026, 10, 14)
    assert other.event_date == date(2026, 10, 16)
