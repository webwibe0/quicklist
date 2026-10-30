"""Deterministic CREATE confirmations and dialogue regressions; no live LLM."""
import json
from copy import deepcopy
from datetime import UTC, date, time, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.ai.provider import IntentParser, ProviderError
from app.models.entities import Event
from app.schemas.dialogue import PendingAction
from tests import test_manual_ux_regressions
from tests.fakes import FakeLLMProvider
from tests.test_manual_ux_regressions import NOW, TOMORROW, handle, reminder, rows

ux = test_manual_ux_regressions.ux
PAST = "напомни сегодня в 9 утра позвонить"
SINGLE = "напомни завтра в 8 утра позвонить"
BATCH = "напомни завтра поесть в 7 утра и в 7 вечера"


def past(ux):
    reply = handle(ux, PAST, reminder(date_expression="сегодня", time_expression="в 9 утра"))
    assert reply.missing_fields == ["confirmation"] and not rows(ux)
    assert "09:00" in reply.message and "прошло" in reply.message
    pending = PendingAction.model_validate(ux[1].pending_action)
    assert pending.confirmation_kind == "past"
    assert pending.confirmation.event_date == NOW.date()
    assert pending.alternative.event_date == TOMORROW
    return reply


def batch(ux, *, followup=False, array=True):
    if followup:
        reply = handle(ux, "напомни завтра поесть в 7", reminder(
            title="Поесть", time_expression="в 7",
        ))
        assert reply.missing_fields == ["time"]
    text = "и в 7 утра и в 7 вечера" if followup else BATCH
    fields = ({"time_expressions": ["в 7 утра", "в 7 вечера"]} if array else
              {"time_expression": "и в 7 утра и в 7 вечера" if followup else
               "в 7 утра и в 7 вечера"})
    command = {"intent": "CREATE_EVENT", **fields} if followup else reminder(title="Поесть", **fields)
    reply = handle(ux, text, command)
    assert reply.missing_fields == ["confirmation"] and not rows(ux)
    assert "Создать два напоминания" in reply.message
    assert "07:00" in reply.message and "19:00" in reply.message
    assert PendingAction.model_validate(ux[1].pending_action).confirmation_kind == "batch"


def no_more_nlu(ux, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Confirmation must execute frozen state without LLM or temporal recomputation")
    monkeypatch.setattr(ux[3], "parse_command", unexpected)
    monkeypatch.setattr(ux[0].resolver, "resolve", unexpected)


@pytest.mark.parametrize("answer", ["да", "всё равно", "Да, всё равно создай!", "подтверждаю"])
def test_past_create_consent_preserves_frozen_datetime_after_json_restore(ux, monkeypatch, answer):
    past(ux)
    ux[1].pending_action = json.loads(json.dumps(ux[1].pending_action))
    no_more_nlu(ux, monkeypatch)
    reply = ux[0].handle(ux[1], answer, NOW + timedelta(days=2))
    assert not reply.requires_input and len(rows(ux)) == 1
    event, = reply.events
    assert (event.event_date, event.event_time) == (NOW.date(), time(9))
    assert ux[1].pending_action is None
    assert event.id == ux[1].last_event_id


@pytest.mark.parametrize("answer", ["нет", "отмена", "не надо", "не создавай"])
def test_past_create_decline_or_cancel_writes_nothing_and_clears_state(ux, monkeypatch, answer):
    past(ux)
    no_more_nlu(ux, monkeypatch)
    reply = ux[0].handle(ux[1], answer, NOW)
    assert reply.action == "CREATE_EVENT" and not reply.requires_input and not reply.events
    assert not rows(ux) and ux[1].pending_action is None and ux[1].last_event_id is None
    monkeypatch.undo()
    ux[0].handle(ux[1], "да", NOW)
    assert not rows(ux)


@pytest.mark.parametrize("answer", ["завтра", "на завтра", "перенеси на завтра"])
def test_past_create_alternative_is_the_offered_date_not_recomputed(ux, monkeypatch, answer):
    past(ux)
    no_more_nlu(ux, monkeypatch)
    # Crossing midnight must not turn the saved tomorrow into the day after tomorrow.
    reply = ux[0].handle(ux[1], answer, NOW + timedelta(hours=13))
    assert not reply.requires_input
    assert (reply.events[0].event_date, reply.events[0].event_time) == (TOMORROW, time(9))


@pytest.mark.parametrize("answer", ["возможно", "да нет", "да, но завтра"])
def test_past_create_ambiguous_reply_does_not_confirm(ux, answer):
    past(ux)
    pending = deepcopy(ux[1].pending_action)
    reply = ux[0].handle(ux[1], answer, NOW)
    assert reply.requires_input and not rows(ux) and ux[1].pending_action == pending


@pytest.mark.parametrize("offset,confirmation", [(0, False), (1, True), (-1, False)])
def test_create_strict_past_boundary_uses_moscow_even_with_utc_clock(ux, offset, confirmation):
    text = "напомни сегодня в 12:00 позвонить"
    ux[3].commands[text] = reminder(date_expression="сегодня", time_expression="в 12:00")
    reply = ux[0].handle(ux[1], text, (NOW + timedelta(seconds=offset)).astimezone(UTC))
    assert reply.requires_input is confirmation
    assert len(rows(ux)) == (0 if confirmation else 1)


@pytest.mark.parametrize("event_type", ["TASK", "MEETING", "REMINDER"])
def test_past_guard_applies_to_all_scheduled_creates(ux, event_type):
    reply = handle(ux, PAST, reminder(
        event_type=event_type, date_expression="сегодня", time_expression="в 9 утра",
    ))
    assert reply.missing_fields == ["confirmation"] and not rows(ux)


def test_unscheduled_task_has_no_invented_midnight_past_check(ux):
    reply = handle(ux, "задача вчера купить хлеб", {
        "intent": "CREATE_EVENT", "event_type": "TASK", "title": "Купить хлеб",
        "date_expression": "вчера",
    })
    assert not reply.requires_input and reply.events[0].event_time is None


@pytest.mark.parametrize("answer,count", [("да", 2), ("нет", 1), ("отмена", 1)])
def test_exact_duplicate_confirm_decline_cancel_after_json_restore(ux, monkeypatch, answer, count):
    command = reminder(time_expression="в 8 утра")
    original = handle(ux, SINGLE, command).events[0]
    reply = handle(ux, SINGLE, command)
    assert reply.requires_input and len(rows(ux)) == 1
    assert PendingAction.model_validate(ux[1].pending_action).confirmation_kind == "duplicate"
    context = deepcopy(ux[1].context)
    ux[1].pending_action = json.loads(json.dumps(ux[1].pending_action))
    no_more_nlu(ux, monkeypatch)
    reply = ux[0].handle(ux[1], answer, NOW)
    assert not reply.requires_input and ux[1].pending_action is None
    assert len(rows(ux)) == count and original.id in {event.id for event in rows(ux)}
    if count == 2:
        assert reply.events[0].id != original.id
    else:
        assert not reply.events and ux[1].context == context


@pytest.mark.parametrize("title", ["  ПОЗВОНИТЬ  ", "Позвонить", "позвонить"])
def test_duplicate_title_normalization_is_deterministic(ux, title):
    handle(ux, SINGLE, reminder(time_expression="в 8 утра"))
    reply = handle(ux, SINGLE, reminder(title=title, time_expression="в 8 утра"))
    assert reply.missing_fields == ["confirmation"] and len(rows(ux)) == 1


@pytest.mark.parametrize("changed", [
    {"title": "Позвонить Ивану"}, {"type": "task"}, {"event_date": date(2026, 10, 17)},
    {"event_time": time(9)}, {"end_time": time(10)}, {"status": "completed"},
    {"status": "cancelled"},
])
def test_similar_or_inactive_events_are_not_exact_duplicates(ux, changed):
    existing = Event(type="reminder", title="Позвонить", event_date=TOMORROW, event_time=time(8))
    for field, value in changed.items():
        setattr(existing, field, value)
    ux[2].add(existing)
    ux[2].flush()
    reply = handle(ux, SINGLE, reminder(time_expression="в 8 утра"))
    assert not reply.requires_input and len(rows(ux)) == 2


def test_unscheduled_task_duplicate_checks_nullable_fingerprint(ux):
    command = {"intent": "CREATE_EVENT", "event_type": "TASK", "title": "Купить хлеб"}
    handle(ux, "Купить хлеб", command)
    reply = handle(ux, "Купить хлеб", command)
    assert reply.missing_fields == ["confirmation"] and len(rows(ux)) == 1


@pytest.mark.parametrize("followup,array", [(False, True), (False, False), (True, True), (True, False)])
def test_multi_time_confirm_atomically_creates_two_reminders(ux, monkeypatch, followup, array):
    batch(ux, followup=followup, array=array)
    ux[1].pending_action = json.loads(json.dumps(ux[1].pending_action))
    no_more_nlu(ux, monkeypatch)
    reply = ux[0].handle(ux[1], "да", NOW)
    assert not reply.requires_input and ux[1].pending_action is None
    assert len(rows(ux)) == len(reply.events) == 2
    assert {(event.event_date, event.event_time, event.end_time) for event in reply.events} == {
        (TOMORROW, time(7), None), (TOMORROW, time(19), None),
    }
    assert all(event.title == "Поесть" and event.type == "reminder" for event in reply.events)
    assert "2" in reply.message


@pytest.mark.parametrize("answer", ["нет", "отмена"])
def test_multi_time_decline_or_cancel_leaves_no_partial_events(ux, monkeypatch, answer):
    batch(ux)
    no_more_nlu(ux, monkeypatch)
    reply = ux[0].handle(ux[1], answer, NOW)
    assert not reply.events and not rows(ux) and ux[1].pending_action is None


def test_batch_second_insert_integrity_failure_rolls_back_first_and_can_retry(ux, monkeypatch):
    batch(ux)
    pending = deepcopy(ux[1].pending_action)
    original_create = ux[0].router.repository.create
    calls = []

    def fail_second(command):
        calls.append(command)
        if len(calls) == 2:
            command = command.model_copy(update={"title": ""})  # Real DB CHECK failure.
        return original_create(command)

    monkeypatch.setattr(ux[0].router.repository, "create", fail_second)
    with pytest.raises(IntegrityError):
        ux[0].handle(ux[1], "да", NOW)
    assert len(calls) == 2 and not rows(ux)
    assert ux[1].pending_action == pending and ux[1].last_event_id is None
    monkeypatch.undo()
    result = ux[0].handle(ux[1], "да", NOW)
    assert len(result.events) == len(rows(ux)) == 2


@pytest.mark.parametrize("times", [["в 7 утра", "25:00"], ["в 7", "в 7 вечера"]])
def test_invalid_or_ambiguous_batch_times_never_partially_create(ux, times):
    text = "напомни завтра поесть " + " и ".join(times)
    reply = handle(ux, text, reminder(title="Поесть", time_expressions=times))
    assert reply.requires_input and "time" in reply.missing_fields and not rows(ux)
    corrected = handle(ux, "в 19:00", {"intent": "CREATE_EVENT", "time_expression": "в 19:00"})
    assert not corrected.requires_input and len(rows(ux)) == 1
    assert corrected.events[0].event_time == time(19)


def test_batch_date_clarification_preserves_both_typed_times(ux):
    text = "напомни поесть в 7 утра и в 7 вечера"
    reply = handle(ux, text, reminder(
        title="Поесть", date_expression=None, time_expressions=["в 7 утра", "в 7 вечера"],
    ))
    assert reply.missing_fields == ["date"]
    reply = handle(ux, "завтра", {"intent": "CREATE_EVENT", "date_expression": "завтра"})
    assert reply.missing_fields == ["confirmation"]
    reply = ux[0].handle(ux[1], "да", NOW)
    assert {event.event_time for event in reply.events} == {time(7), time(19)}


def test_array_times_have_same_grounding_guard_as_single_time():
    text = "напомни завтра поесть в 7 утра"
    provider = FakeLLMProvider({text: reminder(time_expressions=["в 7 утра", "в 7 вечера"])})
    with pytest.raises(ProviderError):
        IntentParser(provider).parse(text, {})


@pytest.mark.parametrize("title", ["Позвонить ему", "Позвонить", "Позвонить Ивану"])
def test_unresolved_pronoun_is_not_silently_dropped_or_guessed_by_model(ux, title):
    text = "напомни завтра в 8 утра позвонить ему"
    reply = handle(ux, text, reminder(title=title, time_expression="в 8 утра"))
    assert "referent" in reply.missing_fields and "Кому именно позвонить?" in reply.message
    assert not rows(ux)


@pytest.mark.parametrize("answer", ["Ивану", "Позвонить Ивану", "маме"])
def test_explicit_referent_reply_resolves_pending_without_losing_date_and_time(ux, answer):
    handle(ux, "напомни завтра в 8 утра позвонить ему", reminder(
        title="Позвонить ему", time_expression="в 8 утра",
    ))
    ux[1].pending_action = json.loads(json.dumps(ux[1].pending_action))
    reply = handle(ux, answer, {"intent": "CREATE_EVENT", "title": "LLM-invented recipient"})
    assert not reply.requires_input and len(rows(ux)) == 1
    assert reply.events[0].title == ("Позвонить маме" if answer == "маме" else "Позвонить Ивану")
    assert (reply.events[0].event_date, reply.events[0].event_time) == (TOMORROW, time(8))


def test_referent_date_reply_does_not_become_recipient(ux):
    handle(ux, "напомни позвонить ему", reminder(title="Позвонить ему", date_expression=None))
    reply = handle(ux, "Завтра", {"intent": "CREATE_EVENT", "date_expression": "Завтра"})
    assert "referent" in reply.missing_fields and not rows(ux)
    assert ux[1].pending_action["command"]["title"] == "Позвонить ему"


@pytest.mark.parametrize("scope", ["created", "listed"])
def test_unique_active_named_context_resolves_pronoun_deterministically(ux, scope):
    existing = Event(type="meeting", title="Встреча с Иваном", event_date=TOMORROW, event_time=time(15))
    ux[2].add(existing)
    ux[2].flush()
    if scope == "created":
        ux[1].last_event_id = existing.id
    else:
        handle(ux, "покажи завтра", {"intent": "LIST_EVENTS", "period_expression": "завтра"})
    reply = handle(ux, "напомни завтра в 8 утра позвонить ему", reminder(
        title="Позвонить ему", time_expression="в 8 утра",
    ))
    assert not reply.requires_input and len(rows(ux)) == 2
    assert "Иваном" in reply.events[0].title and "ему" not in reply.events[0].title.casefold()


@pytest.mark.parametrize("status", ["ambiguous", "completed", "cancelled", "unseen"])
def test_ambiguous_inactive_or_unseen_context_never_resolves_pronoun(ux, status):
    existing = Event(type="meeting", title="Встреча с Иваном", event_date=TOMORROW, event_time=time(15))
    ux[2].add(existing)
    ux[2].flush()
    if status != "unseen":
        handle(ux, "покажи завтра", {"intent": "LIST_EVENTS", "period_expression": "завтра"})
    if status in {"completed", "cancelled"}:
        existing.status = status
        ux[2].flush()
    if status == "ambiguous":
        ux[2].add(Event(type="meeting", title="Встреча с Петром",
                       event_date=TOMORROW, event_time=time(16)))
        ux[2].flush()
        handle(ux, "покажи завтра", {"intent": "LIST_EVENTS", "period_expression": "завтра"})
    count = len(rows(ux))
    reply = handle(ux, "напомни завтра в 8 утра позвонить ему", reminder(
        title="Позвонить ему", time_expression="в 8 утра",
    ))
    assert "referent" in reply.missing_fields and len(rows(ux)) == count


def test_multiple_people_in_one_event_are_not_one_resolved_recipient(ux):
    existing = Event(type="meeting", title="Встреча с Иваном и с Петром",
                     event_date=TOMORROW, event_time=time(15))
    ux[2].add(existing)
    ux[2].flush()
    ux[1].last_event_id = existing.id
    reply = handle(ux, "напомни завтра в 8 утра позвонить ему", reminder(
        title="Позвонить ему", time_expression="в 8 утра",
    ))
    assert "referent" in reply.missing_fields and len(rows(ux)) == 1


def test_past_meeting_confirmation_preserves_interval(ux):
    reply = handle(ux, "встреча сегодня с 09:00 до 10:00", {
        "intent": "CREATE_EVENT", "event_type": "MEETING", "title": "Совещание",
        "date_expression": "сегодня", "time_expression": "с 09:00 до 10:00",
    })
    assert reply.missing_fields == ["confirmation"] and "10:00" in reply.message
    reply = ux[0].handle(ux[1], "да", NOW)
    assert (reply.events[0].event_time, reply.events[0].end_time) == (time(9), time(10))


def test_past_and_duplicate_require_both_confirmations_before_write(ux):
    ux[2].add(Event(type="reminder", title="Позвонить", event_date=NOW.date(), event_time=time(9)))
    ux[2].flush()
    reply = handle(ux, PAST, reminder(date_expression="сегодня", time_expression="в 9 утра"))
    assert reply.missing_fields == ["confirmation"]
    reply = ux[0].handle(ux[1], "да", NOW)
    assert reply.missing_fields == ["confirmation"] and len(rows(ux)) == 1
    assert ux[1].pending_action["confirmation_kind"] == "duplicate"
    reply = ux[0].handle(ux[1], "да", NOW)
    assert not reply.requires_input and len(rows(ux)) == 2
    assert reply.events[0].event_date == NOW.date() and reply.events[0].event_time == time(9)


def test_full_listing_replaces_create_confirmation(ux):
    past(ux)
    listed = handle(ux, "что завтра", {"intent": "LIST_EVENTS", "period_expression": "завтра"})
    assert listed.action == "LIST_EVENTS" and ux[1].pending_action is None
    reply = ux[0].handle(ux[1], "да", NOW)
    assert not reply.events and not rows(ux)


def test_batch_and_duplicate_each_need_confirmation(ux):
    ux[2].add(Event(type="reminder", title="Поесть", event_date=TOMORROW, event_time=time(7)))
    ux[2].flush()
    reply = handle(ux, BATCH, reminder(title="Поесть", time_expressions=["в 7 утра", "в 7 вечера"]))
    assert reply.missing_fields == ["confirmation"]
    assert ux[1].pending_action["confirmation_kind"] == "duplicate"
    reply = ux[0].handle(ux[1], "да", NOW)
    assert reply.missing_fields == ["confirmation"]
    assert ux[1].pending_action["confirmation_kind"] == "batch" and len(rows(ux)) == 1
    reply = ux[0].handle(ux[1], "да", NOW)
    assert not reply.requires_input and len(rows(ux)) == 3
def test_meeting_overlap_requires_conflict_confirmation(ux):
    existing = Event(
        type="meeting",
        title="Созвон с командой",
        event_date=TOMORROW,
        event_time=time(15, 0),
        end_time=time(16, 0),
    )
    ux[2].add(existing)
    ux[2].flush()

    reply = handle(ux, "создай встречу завтра с 15:30 до 16:30", {
        "intent": "CREATE_EVENT",
        "event_type": "MEETING",
        "title": "Встреча с клиентом",
        "date_expression": "завтра",
        "time_expression": "с 15:30 до 16:30",
    })

    assert reply.requires_input
    assert reply.missing_fields == ["confirmation"]

    pending = PendingAction.model_validate(ux[1].pending_action)
    assert pending.confirmation_kind == "conflict"

    # Новая встреча ещё не записана; в базе остаётся только существующая.
    assert len(rows(ux)) == 1
def test_meeting_conflict_confirmation_names_existing_event_and_yes_creates(ux):
    existing = Event(
        type="meeting",
        title="Созвон с командой",
        event_date=TOMORROW,
        event_time=time(15, 0),
        end_time=time(16, 0),
    )
    ux[2].add(existing)
    ux[2].flush()

    reply = handle(ux, "создай встречу завтра с 15:30 до 16:30", {
        "intent": "CREATE_EVENT",
        "event_type": "MEETING",
        "title": "Встреча с клиентом",
        "date_expression": "завтра",
        "time_expression": "с 15:30 до 16:30",
    })

    assert reply.requires_input
    assert "Созвон с командой" in reply.message
    assert "15:00" in reply.message
    assert "16:00" in reply.message

    reply = ux[0].handle(ux[1], "да", NOW)

    assert not reply.requires_input
    assert len(rows(ux)) == 2
    assert reply.events[0].title == "Встреча с клиентом"
    assert ux[1].pending_action is None
def test_meeting_conflict_decline_creates_nothing(ux):
    existing = Event(
        type="meeting",
        title="Созвон с командой",
        event_date=TOMORROW,
        event_time=time(15, 0),
        end_time=time(16, 0),
    )
    ux[2].add(existing)
    ux[2].flush()

    reply = handle(ux, "создай встречу завтра с 15:30 до 16:30", {
        "intent": "CREATE_EVENT",
        "event_type": "MEETING",
        "title": "Встреча с клиентом",
        "date_expression": "завтра",
        "time_expression": "с 15:30 до 16:30",
    })

    assert reply.requires_input

    reply = ux[0].handle(ux[1], "нет", NOW)

    assert not reply.requires_input
    assert len(rows(ux)) == 1
    assert rows(ux)[0].title == "Созвон с командой"
    assert ux[1].pending_action is None
def test_adjacent_meetings_do_not_conflict(ux):
    ux[2].add(Event(
        type="meeting",
        title="Созвон с командой",
        event_date=TOMORROW,
        event_time=time(15, 0),
        end_time=time(16, 0),
    ))
    ux[2].flush()

    reply = handle(ux, "создай встречу завтра с 16:00 до 17:00", {
        "intent": "CREATE_EVENT",
        "event_type": "MEETING",
        "title": "Встреча с клиентом",
        "date_expression": "завтра",
        "time_expression": "с 16:00 до 17:00",
    })

    assert not reply.requires_input
    assert len(rows(ux)) == 2
    assert reply.events[0].title == "Встреча с клиентом"
def test_same_start_meeting_without_end_time_conflicts(ux):
    ux[2].add(Event(
        type="meeting",
        title="Созвон с командой",
        event_date=TOMORROW,
        event_time=time(15, 0),
        end_time=None,
    ))
    ux[2].flush()

    reply = handle(ux, "создай встречу завтра в 15:00", {
        "intent": "CREATE_EVENT",
        "event_type": "MEETING",
        "title": "Встреча с клиентом",
        "date_expression": "завтра",
        "time_expression": "в 15:00",
    })

    assert reply.requires_input
    assert PendingAction.model_validate(
        ux[1].pending_action
    ).confirmation_kind == "conflict"
    assert len(rows(ux)) == 1