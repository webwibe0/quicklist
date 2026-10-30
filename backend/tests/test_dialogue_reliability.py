"""Real parser/temporal/dialogue/repository paths; scripted NLU, no network calls."""
import json
from datetime import date, datetime, time

import pytest

from app.models.entities import Event
from app.schemas.dialogue import PendingAction
from app.services.targets import title_score
from app.temporal.resolver import TemporalResolver, normalize_temporal_words
from tests import test_manual_ux_regressions
from tests.test_manual_ux_regressions import handle, reminder, rows

ux = test_manual_ux_regressions.ux
FRIDAY = datetime.fromisoformat("2026-10-16T12:00:00+03:00")


def seed(ux, title="Сходит в школу", **fields):
    event = Event(type="task", title=title, event_date=date(2026, 10, 17), **fields)
    ux[2].add(event)
    ux[2].flush()
    return event


@pytest.mark.parametrize("query", ["сходить в школу", "сходит в школуу", "сходит в школе", "сходит в шклоу"])
@pytest.mark.parametrize("intent", ["DELETE_EVENT", "UPDATE_EVENT"])
def test_ranked_mutation_needs_selection_before_execution(ux, query, intent):
    event = seed(ux)
    text = ("удали " if intent == "DELETE_EVENT" else "перенеси ") + query
    command = {"intent": intent, "search_query": query}
    if intent == "UPDATE_EVENT":
        text += " завтра в 16:00"
        command.update(date_expression="завтра", time_expression="в 16:00")
    reply = handle(ux, text, command)
    assert reply.missing_fields == ["event_reference"]
    assert reply.events[0].id == event.id and event.title == "Сходит в школу"
    assert event.status == "pending" and event.event_time is None
    # A bare yes is not permission to pick a fuzzy candidate.
    assert ux[0].handle(ux[1], "да", FRIDAY).requires_input
    reply = handle(ux, "первую", {"intent": intent})
    if intent == "DELETE_EVENT":
        assert reply.missing_fields == ["confirmation"] and event.status == "pending"
        reply = ux[0].handle(ux[1], "да", FRIDAY)
        assert event.status == "deleted"
    else:
        assert event.event_time == time(16)
    assert reply.events[0].id == event.id and event.title == "Сходит в школу"


def test_exact_candidate_ranked_first_but_multiple_matches_never_auto_selected(ux):
    fuzzy = seed(ux)
    exact = seed(ux, "Сходить в школу")
    reply = handle(ux, "удали сходить в школу", {"intent": "DELETE_EVENT", "search_query": "сходить в школу"})
    assert [x.id for x in reply.events] == [exact.id, fuzzy.id]
    assert reply.missing_fields == ["event_reference"]
    assert all(x.status == "pending" for x in rows(ux))


@pytest.mark.parametrize("query,title", [
    ("сходить в школу", "Не сходить в школу"),
    ("не сходить в школу", "Сходить в школу"),
    ("сходить в школу", "Сходить из школы"),
    ("Иван", "Игорь"), ("План 12", "План 13"),
    ("Купить хлеб", "Купить билеты"),
])
def test_fuzzy_negative_pairs(query, title):
    assert title_score(query, title) == 0


@pytest.mark.parametrize("answer", ["без разницы", "неважно", "любое время", "не важно"])
def test_no_time_reminder_conversion_preserves_date_and_recipient(ux, answer):
    handle(ux, "напомни завтра позвонить Ивану", reminder(title="Позвонить Ивану"))
    reply = ux[0].handle(ux[1], answer, FRIDAY)
    assert reply.missing_fields == ["confirmation"] and not rows(ux)
    pending = PendingAction.model_validate(ux[1].pending_action)
    assert pending.confirmation.event_type == "TASK"
    assert pending.confirmation.title == "Позвонить Ивану"
    reply = ux[0].handle(ux[1], "да", FRIDAY)
    assert reply.events[0].type == "task" and reply.events[0].event_time is None
    assert reply.events[0].event_date == date(2026, 10, 16)


def test_conversion_remembers_refusal_while_only_date_is_missing(ux, monkeypatch):
    handle(ux, "напомни позвонить Ивану", reminder(title="Позвонить Ивану", date_expression=None))
    reply = ux[0].handle(ux[1], "любое время", FRIDAY)
    assert reply.missing_fields == ["date"] and not rows(ux)
    ux[1].pending_action = json.loads(json.dumps(ux[1].pending_action))
    monkeypatch.setattr(ux[3], "parse_command", lambda *a: pytest.fail("Short date is deterministic"))
    reply = ux[0].handle(ux[1], "через неделю", FRIDAY)
    assert reply.missing_fields == ["confirmation"] and not rows(ux)
    reply = ux[0].handle(ux[1], "да", FRIDAY)
    assert (reply.events[0].title, reply.events[0].type, reply.events[0].event_date) == (
        "Позвонить Ивану", "task", date(2026, 10, 23),
    )


@pytest.mark.parametrize("expression,start,end", [
    ("с 5 утра до 7 вечера", time(5), time(19)),
    ("с 5 утра до 7 утра", time(5), time(7)),
    ("с 14:00 до 16:00", time(14), time(16)),
])
def test_meeting_intervals_resolve_through_parser(ux, expression, start, end):
    reply = handle(ux, f"поставь встречу завтра {expression}", {
        "intent": "CREATE_EVENT", "event_type": "MEETING", "title": "Встреча",
        "date_expression": "завтра", "time_expression": expression,
    })
    assert not reply.requires_input
    assert (reply.events[0].event_time, reply.events[0].end_time) == (start, end)


@pytest.mark.parametrize("expression", ["с 5 утра до 7 вечера", "с 5 утра до 7 утра",
                                         "в любое время между 10 и 12", "с 14:00 до 16:00"])
def test_reminder_range_does_not_become_an_arbitrary_alarm(ux, expression):
    reply = handle(ux, f"напомни завтра позвонить {expression}", reminder(time_expression=expression))
    assert reply.missing_fields == ["time"] and not rows(ux)
    assert "один момент" in reply.message
    reply = handle(ux, "в 11:00", {"intent": "CREATE_EVENT", "time_expression": "в 11:00"})
    assert not reply.requires_input and reply.events[0].event_time == time(11)
    assert reply.events[0].end_time is None


@pytest.mark.parametrize("model", [None, "помлезавтра", "послезавтра"])
def test_whitelisted_day_after_tomorrow_typo(ux, model):
    reply = handle(ux, "напомни помлезавтра позвонить в 19:00", reminder(
        date_expression=model, time_expression="в 19:00",
    ))
    assert not reply.requires_input and reply.events[0].event_date == date(2026, 10, 17)


@pytest.mark.parametrize("word", ["помпезавтра", "помлезавтрами", "помлезавтрак", "непомлезавтра", "сегодя"])
def test_typo_policy_is_whole_word_and_allowlisted(word):
    assert normalize_temporal_words(word) == word


def test_same_weekday_answer_is_deterministic_and_preserves_title_across_json(ux, monkeypatch):
    text = "В пятницу нужно отправить отчёт"
    ux[3].commands[text] = {"intent": "CREATE_EVENT", "event_type": "TASK",
                            "title": "Отправить отчёт", "date_expression": "В пятницу"}
    reply = ux[0].handle(ux[1], text, FRIDAY)
    assert reply.missing_fields == ["date"] and "через неделю" in reply.message
    ux[1].pending_action = json.loads(json.dumps(ux[1].pending_action))
    monkeypatch.setattr(ux[3], "parse_command", lambda *a: pytest.fail("No NLU for grounded date reply"))
    reply = ux[0].handle(ux[1], "Через неделю", FRIDAY)
    assert not reply.requires_input
    assert (reply.events[0].title, reply.events[0].event_date) == ("Отправить отчёт", date(2026, 10, 23))


@pytest.mark.parametrize("answer", ["да", "нет", "отмена"])
def test_meeting_without_clock_requires_explicit_confirmation_and_keeps_type(ux, answer):
    handle(ux, "поставь встречу с Иваном завтра", {
        "intent": "CREATE_EVENT", "event_type": "MEETING", "title": "Встреча с Иваном",
        "date_expression": "завтра",
    })
    reply = ux[0].handle(ux[1], "Без разницы", FRIDAY)
    assert reply.missing_fields == ["confirmation"] and not rows(ux)
    assert "встречу" in reply.message and "без точного времени" in reply.message
    ux[1].pending_action = json.loads(json.dumps(ux[1].pending_action))
    reply = ux[0].handle(ux[1], answer, FRIDAY)
    assert ux[1].pending_action is None
    if answer == "да":
        assert (reply.events[0].type, reply.events[0].title, reply.events[0].event_time) == (
            "meeting", "Встреча с Иваном", None,
        )
    else:
        assert not rows(ux)


def test_unscheduled_meeting_collects_only_missing_date_then_confirms(ux):
    handle(ux, "поставь встречу с Иваном", {
        "intent": "CREATE_EVENT", "event_type": "MEETING", "title": "Встреча с Иваном",
    })
    reply = ux[0].handle(ux[1], "неважно", FRIDAY)
    assert reply.missing_fields == ["date"]
    reply = ux[0].handle(ux[1], "через неделю", FRIDAY)
    assert reply.missing_fields == ["confirmation"]
    reply = ux[0].handle(ux[1], "да", FRIDAY)
    assert reply.events[0].event_date == date(2026, 10, 23)
    assert reply.events[0].type == "meeting" and reply.events[0].event_time is None


def test_unscheduled_meeting_is_not_created_without_refusal(ux):
    reply = handle(ux, "поставь встречу завтра", {
        "intent": "CREATE_EVENT", "event_type": "MEETING", "title": "Встреча", "date_expression": "завтра",
    })
    assert reply.missing_fields == ["time"] and not rows(ux)


def test_date_only_reschedule_of_confirmed_unscheduled_meeting(ux):
    event = seed(ux, "Встреча с Иваном")
    event.type = "meeting"
    ux[2].flush()
    ux[1].last_event_id = event.id
    reply = handle(ux, "перенеси на послезавтра", {
        "intent": "UPDATE_EVENT", "date_expression": "послезавтра",
    })
    assert not reply.requires_input and reply.events[0].event_time is None


def test_relative_week_resolver_respects_moscow_day_boundary():
    day, clock = TemporalResolver("Europe/Moscow").resolve_date(
        "через неделю", datetime.fromisoformat("2026-10-15T22:30:00+00:00"),
    )
    assert day == date(2026, 10, 23) and clock is None


def test_meeting_between_ambiguous_hours_requests_clocks_instead_of_guessing(ux):
    reply = handle(ux, "поставь встречу завтра в любое время между 10 и 12", {
        "intent": "CREATE_EVENT", "event_type": "MEETING", "title": "Встреча",
        "date_expression": "завтра", "time_expression": "в любое время между 10 и 12",
    })
    assert set(reply.missing_fields) == {"time", "end_time"} and not rows(ux)


@pytest.mark.parametrize("expression", ["с 14:00 до 16:00", "в любое время между 14:00 и 16:00"])
def test_nlu_cannot_turn_reminder_range_into_batch_by_using_plural_field(ux, expression):
    reply = handle(ux, f"напомни завтра позвонить {expression}", reminder(
        time_expressions=["14:00", "16:00"],
    ))
    assert reply.missing_fields == ["time"] and "один момент" in reply.message
    assert not rows(ux) and PendingAction.model_validate(ux[1].pending_action).confirmation is None


@pytest.mark.parametrize("answer", ["нет", "отмена"])
def test_conversion_refusal_while_date_unknown_clears_collected_decision(ux, answer):
    handle(ux, "напомни позвонить", reminder(date_expression=None))
    ux[0].handle(ux[1], "без разницы", FRIDAY)
    ux[0].handle(ux[1], answer, FRIDAY)
    assert ux[1].pending_action is None and not rows(ux)
def test_single_update_candidate_accepts_this_yes(ux):
    event = seed(
        ux,
        "Напомнить Елене про хакатон",
        event_time=time(12, 0),
    )

    text = "Напомнить Елене про хакатонн перенеси на 29 ноября в 17:45"
    reply = handle(
        ux,
        text,
        {
            "intent": "UPDATE_EVENT",
            "search_query": "Напомнить Елене про хакатонн",
            "date_expression": "29 ноября",
            "time_expression": "17:45",
        },
    )

    # Один похожий кандидат найден, но backend пока просит подтвердить,
    # что пользователь имеет в виду именно его.
    assert reply.requires_input
    assert reply.missing_fields == ["event_reference"]
    assert [item.id for item in reply.events] == [event.id]

    # До подтверждения перенос ещё не выполнен.
    assert event.event_date == date(2026, 10, 17)
    assert event.event_time == time(12, 0)

    # Естественное подтверждение показанного единственного кандидата.
    confirmed = ux[0].handle(ux[1], "Этот, да.", FRIDAY)

    assert not confirmed.requires_input
    assert confirmed.events[0].id == event.id
    assert event.event_date == date(2026, 11, 29)
    assert event.event_time == time(17, 45)