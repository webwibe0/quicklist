"""Collected temporal/entity fragments survive turns; business rules remain deterministic."""
import json
from datetime import time, timedelta

import pytest

from app.models.entities import Event
from app.schemas.dialogue import PendingAction
from tests import test_manual_ux_regressions
from tests.test_manual_ux_regressions import NOW, TOMORROW, handle, reminder, rows

ux = test_manual_ux_regressions.ux


@pytest.mark.parametrize("hour,daypart,expected", [
    (9, "утра", 9), (9, "вечера", 21), (7, "утра", 7), (7, "вечера", 19),
])
def test_hour_then_daypart_merges_and_runs_past_create_guard(ux, hour, daypart, expected):
    text = f"напомни сегодня в {hour} заснуть"
    reply = handle(ux, text, reminder(
        title="Заснуть", date_expression="сегодня", time_expression=f"в {hour}",
    ))
    assert reply.missing_fields == ["time"] and not rows(ux)
    draft = PendingAction.model_validate(ux[1].pending_action)
    assert draft.temporal.time_fragments.hour == hour
    ux[1].pending_action = json.loads(json.dumps(ux[1].pending_action))
    reply = handle(ux, daypart, {"intent": "CREATE_EVENT", "time_expression": daypart})
    if expected < NOW.hour:
        assert reply.missing_fields == ["confirmation"] and not rows(ux)
        assert ux[1].pending_action["confirmation"]["event_time"] == f"{expected:02}:00:00"
        reply = ux[0].handle(ux[1], "да", NOW)
    assert not reply.requires_input
    assert (reply.events[0].event_date, reply.events[0].event_time) == (NOW.date(), time(expected))
    assert ux[1].pending_action is None


@pytest.mark.parametrize("daypart,hour,expected", [
    ("вечером", 8, 20), ("утром", 8, 8), ("вечером", 7, 19), ("утром", 7, 7),
])
def test_daypart_then_hour_survives_json_and_midnight(ux, daypart, hour, expected):
    text = f"напомни завтра {daypart} позвонить"
    reply = handle(ux, text, reminder(time_expression=daypart))
    assert reply.missing_fields == ["time"]
    assert PendingAction.model_validate(ux[1].pending_action).temporal.time_fragments.daypart in {
        "morning", "evening",
    }
    ux[1].pending_action = json.loads(json.dumps(ux[1].pending_action))
    text = f"в {hour}"
    ux[3].commands[text] = {"intent": "CREATE_EVENT", "time_expression": text}
    reply = ux[0].handle(ux[1], text, NOW + timedelta(hours=13))
    assert not reply.requires_input
    assert (reply.events[0].event_date, reply.events[0].event_time) == (TOMORROW, time(expected))


def test_legacy_pending_without_fragments_still_merges_hour_and_daypart(ux):
    handle(ux, "напомни завтра в 9 позвонить", reminder(time_expression="в 9"))
    ux[1].pending_action["temporal"].pop("time_fragments")
    reply = handle(ux, "утром", {"intent": "CREATE_EVENT", "time_expression": "утром"})
    assert not reply.requires_input and reply.events[0].event_time == time(9)


def test_explicit_exact_time_overrides_previous_daypart(ux):
    handle(ux, "напомни завтра вечером позвонить", reminder(time_expression="вечером"))
    reply = handle(ux, "в 08:30", {"intent": "CREATE_EVENT", "time_expression": "в 08:30"})
    assert not reply.requires_input and reply.events[0].event_time == time(8, 30)


@pytest.mark.parametrize("event_type", [None, "REMINDER"])
def test_complete_new_request_replaces_pending_referent_instead_of_grafting_old_title(ux, event_type):
    first = handle(ux, "напомни завтра в 18:00 позвонить ему", reminder(
        title="Позвонить ему", time_expression="в 18:00",
    ))
    assert first.missing_fields == ["referent"] and not rows(ux)
    reply = handle(ux, "напомни завтра в 18:00 позвонить", reminder(
        event_type=event_type, title="Позвонить", time_expression="в 18:00",
    ))
    assert not reply.requires_input and len(rows(ux)) == 1
    assert reply.events[0].title == "Позвонить" and reply.events[0].event_time == time(18)
    assert ux[1].pending_action is None


def test_short_recipient_reply_continues_existing_draft(ux):
    handle(ux, "напомни завтра в 18:00 позвонить ему", reminder(
        title="Позвонить ему", time_expression="в 18:00",
    ))
    reply = handle(ux, "Ивану", {"intent": "CREATE_EVENT", "title": "Ивану"})
    assert not reply.requires_input and reply.events[0].title == "Позвонить Ивану"
    assert (reply.events[0].event_date, reply.events[0].event_time) == (TOMORROW, time(18))


@pytest.mark.parametrize("llm_date", [None, "завтр", "завтра"])
def test_whitelisted_truncation_keeps_tomorrow_and_only_needs_time(ux, llm_date):
    reply = handle(ux, "напомни мне завтр сходить ко врачу", reminder(
        title="Сходить ко врачу", date_expression=llm_date,
    ))
    assert reply.missing_fields == ["time"] and not rows(ux)
    draft = PendingAction.model_validate(ux[1].pending_action)
    assert draft.temporal.event_date == TOMORROW and draft.command.title == "Сходить ко врачу"


@pytest.mark.parametrize("expression", ["завт", "помпезавтра", "завтрами"])
def test_no_general_fuzzy_autocorrection(ux, expression):
    reply = handle(ux, f"напомни {expression} позвонить", reminder(date_expression=expression))
    assert "date" in reply.missing_fields and not rows(ux)


def flexible_start(ux):
    reply = handle(ux, "напомни завтра купить лекарства в любое время", reminder(
        title="Купить лекарства", time_expression="в любое время",
    ))
    assert reply.missing_fields == ["time"] and not rows(ux)


@pytest.mark.parametrize("answer", ["любое", "в любое время", "без разницы", "когда угодно", "неважно"])
def test_flexible_reply_offers_typed_task_inheriting_content_and_date(ux, monkeypatch, answer):
    flexible_start(ux)
    monkeypatch.setattr(ux[3], "parse_command", lambda *args: pytest.fail("No LLM for type conversion"))
    reply = ux[0].handle(ux[1], answer, NOW)
    assert reply.missing_fields == ["confirmation"] and not rows(ux)
    assert "Купить лекарства" in reply.message and "без времени" in reply.message
    draft = PendingAction.model_validate(ux[1].pending_action)
    assert draft.confirmation_kind == "task_conversion"
    assert draft.confirmation.event_type == "TASK" and draft.confirmation.event_date == TOMORROW
    assert draft.confirmation.event_time is None
    ux[1].pending_action = json.loads(json.dumps(ux[1].pending_action))
    reply = ux[0].handle(ux[1], "да", NOW + timedelta(hours=13))
    assert not reply.requires_input and len(rows(ux)) == 1
    event, = reply.events
    assert (event.type, event.title, event.event_date, event.event_time, event.end_time) == (
        "task", "Купить лекарства", TOMORROW, None, None,
    )


@pytest.mark.parametrize("answer", ["нет", "отмена"])
def test_task_conversion_decline_or_cancel_creates_nothing(ux, answer):
    flexible_start(ux)
    ux[0].handle(ux[1], "любое", NOW)
    reply = ux[0].handle(ux[1], answer, NOW)
    assert not reply.events and not rows(ux) and ux[1].pending_action is None


@pytest.mark.parametrize("proposed", [False, True])
def test_explicit_task_switch_inherits_collected_entities_without_new_title_question(ux, proposed):
    flexible_start(ux)
    if proposed:
        ux[0].handle(ux[1], "без разницы", NOW)
    reply = ux[0].handle(ux[1], "тогда создай задачу без времени", NOW)
    assert not reply.requires_input and ux[1].pending_action is None
    event, = reply.events
    assert (event.type, event.title, event.event_date, event.event_time) == (
        "task", "Купить лекарства", TOMORROW, None,
    )


def test_task_switch_does_not_discard_unresolved_referent(ux):
    reply = handle(ux, "напомни завтра в любое время позвонить ему", reminder(
        title="Позвонить ему", time_expression="в любое время",
    ))
    assert {"referent", "time"} <= set(reply.missing_fields)
    reply = ux[0].handle(ux[1], "тогда создай задачу без времени", NOW)
    assert reply.missing_fields == ["referent"] and not rows(ux)
    assert ux[1].pending_action["command"]["event_type"] == "TASK"


def test_batch_duplicate_uses_plural_without_changing_confirmation_logic(ux):
    ux[2].add_all([
        Event(type="reminder", title="Поесть", event_date=TOMORROW, event_time=clock)
        for clock in (time(7), time(19))
    ])
    ux[2].flush()
    reply = handle(ux, "напомни завтра поесть в 7 утра и в 7 вечера", reminder(
        title="Поесть", time_expressions=["в 7 утра", "в 7 вечера"],
    ))
    assert reply.missing_fields == ["confirmation"] and len(rows(ux)) == 2
    assert "Такие напоминания уже существуют" in reply.message
    assert "Создать ещё по одному?" in reply.message
    assert "07:00" in reply.message and "19:00" in reply.message
    assert "Создать ещё одно?" not in reply.message
    reply = ux[0].handle(ux[1], "да", NOW)
    assert reply.missing_fields == ["confirmation"] and len(rows(ux)) == 2
    reply = ux[0].handle(ux[1], "да", NOW)
    assert not reply.requires_input and len(reply.events) == 2 and len(rows(ux)) == 4


def test_update_time_fragments_preserve_duration_and_past_confirmation(ux):
    existing = Event(type="meeting", title="Совещание", event_date=NOW.date(),
                     event_time=time(14), end_time=time(16))
    ux[2].add(existing)
    ux[2].flush()
    ux[1].last_event_id = existing.id
    reply = handle(ux, "перенеси в 9", {"intent": "UPDATE_EVENT", "time_expression": "в 9"})
    assert reply.missing_fields == ["time"]
    reply = handle(ux, "утра", {"intent": "UPDATE_EVENT", "time_expression": "утра"})
    assert reply.missing_fields == ["confirmation"]
    assert existing.event_time == time(14) and existing.end_time == time(16)
    reply = ux[0].handle(ux[1], "да", NOW)
    assert (reply.events[0].event_time, reply.events[0].end_time) == (time(9), time(11))


@pytest.mark.parametrize("answer", ["да", "подтверждаю", "нет", "отмена"])
def test_short_yes_no_stays_in_pending_dialogue_without_calling_model(ux, monkeypatch, answer):
    reply = handle(ux, "напомни завтра в 9 позвонить", reminder(time_expression="в 9"))
    assert reply.missing_fields == ["time"]
    monkeypatch.setattr(ux[3], "parse_command", lambda *args: pytest.fail("No model for yes/no"))
    reply = ux[0].handle(ux[1], answer, NOW)
    assert not rows(ux)
    if answer in {"да", "подтверждаю"}:
        assert reply.missing_fields == ["time"] and ux[1].pending_action is not None
        assert "09:00" in reply.message and "21:00" in reply.message
    else:
        assert not reply.requires_input and ux[1].pending_action is None


def test_pending_listing_can_be_cancelled_without_creation_message(ux):
    reply = handle(ux, "покажи планы", {"intent": "LIST_EVENTS"})
    assert reply.missing_fields == ["period"]
    reply = ux[0].handle(ux[1], "отмена", NOW)
    assert reply.action == "LIST_EVENTS" and reply.message == "Запрос отменён."
    assert ux[1].pending_action is None and not rows(ux)
