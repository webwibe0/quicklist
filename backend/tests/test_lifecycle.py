"""DELETE/COMPLETE resolution, soft deletion and strict typed confirmation regressions."""
import json
from copy import deepcopy
from datetime import time

import pytest

from app.ai.provider import ProviderError
from app.models.entities import Event
from app.schemas.commands import Intent
from tests import test_manual_ux_regressions
from tests.test_clarification_fragments import flexible_start
from tests.test_create_business_regressions import batch, past
from tests.test_manual_ux_regressions import NOW, TOMORROW, handle, reminder, rows

ux = test_manual_ux_regressions.ux


def seed(ux, title="Купить лекарства", event_type="task", clock=None, status="pending"):
    event = Event(type=event_type, title=title, event_date=TOMORROW, event_time=clock,
                  status=status, source="user")
    ux[2].add(event)
    ux[2].flush()
    return event


def delete_request(ux, event):
    text = f"удали {event.title} завтра"
    return handle(ux, text, {"intent": "DELETE_EVENT", "search_query": event.title,
                            "period_expression": "завтра"})


@pytest.mark.parametrize("answer,status", [("да", "deleted"), ("нет", "pending"), ("отмена", "pending")])
def test_unique_delete_requires_confirmation_and_yes_no_cancel(ux, monkeypatch, answer, status):
    event = seed(ux, "Встреча с Димой", "meeting", time(15))
    event.end_time = time(16)
    ux[2].flush()
    reply = delete_request(ux, event)
    assert reply.requires_input and reply.missing_fields == ["confirmation"]
    assert event.status == "pending" and "Димой" in reply.message and "16:00" in reply.message
    ux[1].pending_action = json.loads(json.dumps(ux[1].pending_action))
    monkeypatch.setattr(ux[3], "parse_command", lambda *args: pytest.fail("No LLM for consent"))
    reply = ux[0].handle(ux[1], answer, NOW)
    assert not reply.requires_input and ux[1].pending_action is None
    assert event.status == status and len(rows(ux)) == 1  # Soft deletion retains the row.
    assert event.end_time == time(16)
    if status == "deleted":
        assert reply.events[0].id == event.id and reply.events[0].status == "deleted"


def test_ambiguous_delete_shows_candidates_and_short_filter_selects_one(ux):
    mother = seed(ux, "Позвонить маме", "reminder", time(7))
    father = seed(ux, "Позвонить папе", "reminder", time(19))
    reply = handle(ux, "удали напоминание позвонить", {
        "intent": "DELETE_EVENT", "event_type": "REMINDER", "search_query": "Позвонить",
    })
    assert reply.missing_fields == ["event_reference"] and len(reply.events) == 2
    assert "1." in reply.message and "2." in reply.message
    reply = handle(ux, "маме", {"intent": "DELETE_EVENT", "search_query": "маме"})
    assert reply.missing_fields == ["confirmation"]
    assert ux[1].pending_action["confirmation"]["event_id"] == str(mother.id)
    ux[0].handle(ux[1], "да", NOW)
    assert mother.status == "deleted" and father.status == "pending"


@pytest.mark.parametrize("intent,text,expected", [
    ("DELETE_EVENT", "удали вторую", "deleted"),
    ("COMPLETE_EVENT", "первую я уже сделал", "completed"),
    ("COMPLETE_EVENT", "пометь вторую выполненной", "completed"),
])
def test_list_ordinals_use_original_visible_order(ux, intent, text, expected):
    first = seed(ux, "Первая задача", clock=time(7))
    second = seed(ux, "Вторая задача", clock=time(19))
    listed = handle(ux, "покажи завтра", {"intent": "LIST_EVENTS", "period_expression": "завтра"})
    assert [event.id for event in listed.events] == [first.id, second.id]
    # A changed DB sort order must never change the meaning of a displayed ordinal.
    first.event_time, second.event_time = time(20), time(6)
    ux[2].flush()
    reply = handle(ux, text, {"intent": intent})  # Backend grounds the explicit ordinal.
    target = first if "первую" in text else second
    if intent == "DELETE_EVENT":
        assert reply.missing_fields == ["confirmation"] and target.status == "pending"
        reply = ux[0].handle(ux[1], "да", NOW)
    assert reply.events[0].id == target.id and target.status == expected


@pytest.mark.parametrize("shown", [False, True])
def test_delete_this_uses_latest_created_or_shown_context(ux, shown):
    reply = handle(ux, "создай задачу купить хлеб", {
        "intent": "CREATE_EVENT", "event_type": "TASK", "title": "Купить хлеб",
    })
    created_id = reply.events[0].id
    target = seed(ux) if shown else ux[2].get(Event, created_id)
    if shown:
        handle(ux, "покажи завтра", {"intent": "LIST_EVENTS", "period_expression": "завтра"})
    reply = handle(ux, "удали это", {"intent": "DELETE_EVENT", "event_reference": "last_created"})
    assert reply.missing_fields == ["confirmation"]
    assert ux[1].pending_action["confirmation"]["event_id"] == str(target.id)
    ux[0].handle(ux[1], "да", NOW)
    assert target.status == "deleted"
    if shown:
        assert ux[2].get(Event, created_id).status == "pending"


def test_deleted_event_not_listed_and_repeat_delete_not_found(ux):
    event = seed(ux)
    delete_request(ux, event)
    ux[0].handle(ux[1], "да", NOW)
    reply = handle(ux, "покажи завтра", {"intent": "LIST_EVENTS", "period_expression": "завтра"})
    assert reply.events == []
    reply = delete_request(ux, event)
    assert not reply.requires_input and not reply.events and "не найдена" in reply.message


def test_complete_by_title_is_idempotent_and_not_an_active_plan(ux):
    event = seed(ux)
    text = "задача купить лекарства выполнена"
    command = {"intent": "COMPLETE_EVENT", "event_type": "TASK", "search_query": "Купить лекарства"}
    reply = handle(ux, text, command)
    assert reply.events[0].id == event.id and event.status == "completed"
    timestamp = event.updated_at
    reply = handle(ux, text, command)
    assert reply.events[0].id == event.id and event.updated_at == timestamp and len(rows(ux)) == 1
    reply = handle(ux, "покажи завтра", {"intent": "LIST_EVENTS", "period_expression": "завтра"})
    assert reply.events == []


def test_ambiguous_complete_clarifies_without_writes(ux):
    first, second = seed(ux, "Купить хлеб"), seed(ux, "Купить лекарства")
    reply = handle(ux, "отметь задачу купить выполненной", {
        "intent": "COMPLETE_EVENT", "search_query": "Купить", "event_type": "TASK",
    })
    assert reply.missing_fields == ["event_reference"] and len(reply.events) == 2
    assert first.status == second.status == "pending"
    reply = handle(ux, "лекарства", {"intent": "COMPLETE_EVENT", "search_query": "лекарства"})
    assert reply.events[0].id == second.id and first.status == "pending" and second.status == "completed"


@pytest.mark.parametrize("status,event_type", [("deleted", "task"), ("cancelled", "task")])
def test_complete_never_completes_deleted_or_cancelled(ux, status, event_type):
    event = seed(ux, event_type=event_type, clock=time(15), status=status)
    reply = handle(ux, "отметь купить лекарства выполненным", {
        "intent": "COMPLETE_EVENT", "search_query": "Купить лекарства",
    })
    assert not reply.requires_input and not reply.events and event.status == status


def test_ordinal_out_of_range_does_not_fall_back_to_last_created(ux):
    event = seed(ux)
    ux[1].last_event_id = event.id
    handle(ux, "покажи завтра", {"intent": "LIST_EVENTS", "period_expression": "завтра"})
    reply = handle(ux, "удали вторую", {"intent": "DELETE_EVENT", "event_index": 2})
    assert not reply.requires_input and not reply.events and event.status == "pending"


def test_confirmation_never_retargets_after_original_deleted_elsewhere(ux):
    first, second = seed(ux, "Купить хлеб"), seed(ux, "Купить лекарства")
    delete_request(ux, first)
    first.status = "deleted"
    ux[2].flush()
    reply = ux[0].handle(ux[1], "да", NOW)
    assert not reply.events and second.status == "pending" and ux[1].pending_action is None


@pytest.mark.parametrize("raw", [
    {"intent": "DELETE_EVENT", "event_index": 2},
    {"intent": "DELETE_EVENT", "event_id": "invented"},
    {"intent": "COMPLETE_EVENT", "search_query": "лекарства"},
])
def test_model_cannot_invent_id_or_ordinal_for_mutation(ux, raw):
    seed(ux)
    ux[3].commands["удали это"] = raw
    with pytest.raises(ProviderError):
        ux[0].handle(ux[1], "удали это", NOW)
    assert rows(ux)[0].status == "pending"


def confirmation(ux, kind):
    if kind == "past":
        past(ux)
    elif kind == "duplicate":
        text, command = "напомни завтра в 8 утра позвонить", reminder(time_expression="в 8 утра")
        handle(ux, text, command)
        handle(ux, text, command)
    elif kind == "batch":
        batch(ux)
    elif kind == "task_conversion":
        flexible_start(ux)
        ux[0].handle(ux[1], "любое", NOW)
    elif kind == "past_update":
        event = seed(ux, "Встреча", "meeting", time(15))
        event.event_date = NOW.date()
        ux[2].flush()
        ux[1].last_event_id = event.id
        handle(ux, "перенеси в 09:00", {"intent": "UPDATE_EVENT", "time_expression": "в 09:00"})
    else:
        delete_request(ux, seed(ux))
    assert ux[1].pending_action["missing_fields"] == ["confirmation"]


@pytest.mark.parametrize("kind", ["past", "past_update", "duplicate", "batch", "delete", "task_conversion"])
@pytest.mark.parametrize("answer", ["без разницы", "неважно", "может быть", "да нет", "в 19:00"])
def test_invalid_yes_no_reply_preserves_frozen_state_without_model_call(ux, monkeypatch, kind, answer):
    confirmation(ux, kind)
    pending = deepcopy(ux[1].pending_action)
    before = [(event.id, event.status) for event in rows(ux)]
    monkeypatch.setattr(ux[3], "parse_command", lambda *args: pytest.fail("No LLM for invalid consent"))
    reply = ux[0].handle(ux[1], answer, NOW)
    assert reply.missing_fields == ["confirmation"] and "Нужно выбрать" in reply.message
    assert ux[1].pending_action == pending and [(e.id, e.status) for e in rows(ux)] == before


@pytest.mark.parametrize("kind", ["past", "past_update", "duplicate", "batch", "delete", "task_conversion"])
def test_complete_new_command_replaces_any_confirmation(ux, kind):
    confirmation(ux, kind)
    previous = [(e.id, e.status) for e in rows(ux)]
    reply = handle(ux, "создай задачу купить хлеб", {
        "intent": "CREATE_EVENT", "event_type": "TASK", "title": "Купить хлеб",
    })
    assert not reply.requires_input and reply.events[0].title == "Купить хлеб"
    assert ux[1].pending_action is None and len(rows(ux)) == len(previous) + 1
    assert all(ux[2].get(Event, event_id).status == status for event_id, status in previous)


def test_incomplete_new_request_does_not_erase_confirmation(ux):
    confirmation(ux, "duplicate")
    pending = deepcopy(ux[1].pending_action)
    reply = handle(ux, "создай напоминание", {"intent": "CREATE_EVENT", "event_type": "REMINDER"})
    assert reply.missing_fields == ["confirmation"] and ux[1].pending_action == pending


def test_card_actions_need_no_llm_and_delete_still_requires_yes(ux, monkeypatch):
    event = seed(ux)
    monkeypatch.setattr(ux[3], "parse_command", lambda *args: pytest.fail("No model for card action"))
    reply = ux[0].handle_event_action(ux[1], event.id, Intent.COMPLETE_EVENT)
    assert event.status == "completed" and reply.events[0].id == event.id
    reply = ux[0].handle_event_action(ux[1], event.id, Intent.DELETE_EVENT)
    assert reply.missing_fields == ["confirmation"] and event.status == "completed"
    ux[0].handle(ux[1], "да", NOW)
    assert event.status == "deleted"


@pytest.mark.parametrize("text,title,event_type,query", [
    ("убери эту встречу", "Встреча с Иваном", "meeting", None),
    ("отмени встречу с Иваном", "Встреча с Иваном", "meeting", "Иваном"),
    ("удали напоминание позвонить маме", "Позвонить маме", "reminder", "позвонить маме"),
])
def test_delete_aliases_keep_the_same_confirmation_rule(ux, text, title, event_type, query):
    event = seed(ux, title, event_type, time(18))
    ux[1].last_event_id = event.id
    reply = handle(ux, text, {"intent": "DELETE_EVENT", "event_type": event_type.upper(), "search_query": query})
    assert reply.missing_fields == ["confirmation"] and event.status == "pending"
    ux[0].handle(ux[1], "да", NOW)
    assert event.status == "deleted"


@pytest.mark.parametrize("text", ["я сделал задачу", "отметь это выполненным"])
def test_complete_implicit_last_created_reference(ux, text):
    event = seed(ux)
    ux[1].last_event_id = event.id
    reply = handle(ux, text, {"intent": "COMPLETE_EVENT"})
    assert reply.events[0].id == event.id and event.status == "completed"


def test_delete_interval_filter_never_ignores_end_time(ux):
    first = seed(ux, "Зум", "meeting", time(12))
    second = seed(ux, "Зум", "meeting", time(12))
    first.end_time, second.end_time = time(14), time(16)
    ux[2].flush()
    reply = handle(ux, "удали Зум завтра с 12:00 до 14:00", {
        "intent": "DELETE_EVENT", "search_query": "Зум", "period_expression": "завтра",
        "time_expression": "12:00", "end_time_expression": "14:00",
    })
    assert reply.missing_fields == ["confirmation"]
    ux[0].handle(ux[1], "да", NOW)
    assert first.status == "deleted" and second.status == "pending"
