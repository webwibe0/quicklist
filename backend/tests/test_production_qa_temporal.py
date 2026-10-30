"""Regressions from the recorded 2026-10-30 production P0 pass; no live AI."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.ai.provider import IntentParser
from app.schemas.commands import ParsedCommand
from app.temporal.resolver import TemporalResolver
from tests import test_manual_ux_regressions
from tests.fakes import FakeLLMProvider
from tests.test_manual_ux_regressions import handle, rows

ux = test_manual_ux_regressions.ux

NOW = datetime(2026, 10, 30, 14, 28, tzinfo=ZoneInfo("Europe/Moscow"))


@pytest.mark.parametrize("expression", ["до понедельника", "к понедельнику"])
def test_inflected_weekday_deadline_resolves_nearest_monday(expression):
    assert TemporalResolver("Europe/Moscow").resolve_date(expression, NOW) == (date(2026, 11, 2), None)


def test_deadline_in_provider_period_is_canonicalized_without_changing_expression():
    text = "Добавь задачу подготовить презентацию до понедельника"
    command = IntentParser(FakeLLMProvider({text: {
        "intent": "CREATE_EVENT", "event_type": "TASK", "title": "подготовить презентацию",
        "period_expression": "до понедельника",
    }})).parse(text, {})
    assert command.date_expression == "до понедельника"
    assert command.period_expression is None


@pytest.mark.parametrize("expression", ["на весь день", "без указания времени"])
def test_explicit_all_day_meeting_is_confirmed_without_inventing_a_clock(ux, expression):
    reply = handle(ux, f"Создай встречу Обзор завтра {expression}", {
        "intent": "CREATE_EVENT", "event_type": "MEETING", "title": "Обзор",
        "date_expression": "завтра", "time_expression": expression,
    })
    assert reply.missing_fields == ["confirmation"]
    assert not rows(ux)
    created = ux[0].handle(ux[1], "да", NOW)
    assert not created.requires_input
    assert len(rows(ux)) == 1 and rows(ux)[0].event_time is None


def test_all_day_marker_survives_missing_title_and_date_clarification(ux):
    reply = handle(ux, "Добавь встречу на весь день", {
        "intent": "CREATE_EVENT", "event_type": "MEETING", "time_expression": "на весь день",
    })
    assert set(reply.missing_fields) == {"title", "date"}
    reply = handle(ux, "Обзор завтра", {
        "intent": "CREATE_EVENT", "title": "Обзор", "date_expression": "завтра",
    })
    assert reply.missing_fields == ["confirmation"]
    assert not rows(ux)


def test_explicit_raw_all_day_marker_is_not_lost_when_provider_omits_it():
    text = "Добавь встречу на весь день"
    parsed = IntentParser(FakeLLMProvider({text: {
        "intent": "CREATE_EVENT", "event_type": "MEETING",
    }})).parse(text, {})
    assert parsed.time_expression == "на весь день"


def test_negated_all_day_request_does_not_become_an_all_day_meeting():
    text = "Создай встречу завтра, не на весь день"
    parsed = IntentParser(FakeLLMProvider({text: {
        "intent": "CREATE_EVENT", "event_type": "MEETING", "date_expression": "завтра",
    }})).parse(text, {})
    assert parsed.time_expression is None


def test_all_day_reminder_still_requires_exact_time():
    result = TemporalResolver("Europe/Moscow").resolve(ParsedCommand(
        intent="CREATE_EVENT", event_type="REMINDER", time_expression="на весь день",
    ), NOW)
    assert not result.time_unspecified and "time" in result.issues


def test_recorded_old_tasks_search_uses_past_scope_without_inventing_a_date():
    result = TemporalResolver("Europe/Moscow").resolve(ParsedCommand(
        intent="SEARCH_EVENTS", event_type="TASK", search_query="по работе", period_expression="старые",
    ), NOW)
    assert not result.issues
    assert result.search_direction == "past"
    assert result.period_start is None and result.period_end is None


def test_implicit_recurrence_horizon_is_not_presented_as_unlimited(ux):
    reply = handle(ux, "Создай задачу QA обзор каждый день начиная с 2 ноября", {
        "intent": "CREATE_EVENT", "event_type": "TASK", "title": "QA обзор",
        "date_expression": "2 ноября", "recurrence_expression": "каждый день",
    })
    assert not reply.requires_input and len(rows(ux)) == 90
    end = max(item.event_date for item in rows(ux))
    assert end.strftime("%d.%m.%Y") in reply.message
    assert "не продлевается автоматически" in reply.message
