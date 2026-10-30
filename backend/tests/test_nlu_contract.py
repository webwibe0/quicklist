"""Regressions from the reported 26/34 Luna run; no live model calls."""
import json
from datetime import date, datetime
from pathlib import Path

import pytest
from scripts.run_evals import assess

from app.ai.openai_provider import SYSTEM_PROMPT
from app.ai.provider import IntentParser
from app.schemas.commands import (
    CreateEventCommand,
    ListEventsCommand,
    ParsedCommand,
    SearchEventsCommand,
)
from app.services.validator import CommandValidator
from app.temporal.resolver import TemporalResolver
from tests.fakes import FakeLLMProvider

CASES = {
    case["id"]: case
    for case in json.loads(
        (Path(__file__).resolve().parents[2] / "evals/command_cases.json").read_text(
            encoding="utf-8",
        )
    )
}
LIST_FAILURES = [
    "pdf_today", "pdf_type_filter", "pdf_task_filter", "pdf_plans", "pdf_show",
    "colloquial_tomorrow",
]
NOW = datetime.fromisoformat("2026-10-14T12:00:00+03:00")
resolver = TemporalResolver("Europe/Moscow")


def parse_case(case, raw):
    return IntentParser(FakeLLMProvider({case["message"]: raw})).parse(case["message"], {})


@pytest.mark.parametrize("case_id", LIST_FAILURES)
def test_list_single_day_in_date_expression_is_normalized_for_runtime_and_evals(case_id):
    case = CASES[case_id]
    # Reproduce the reported model shape without changing the canonical fixture.
    raw = case["parsed_fixture"] | {
        "date_expression": case["parsed_fixture"]["period_expression"].removeprefix("на "),
        "period_expression": None,
    }
    command = parse_case(case, raw)
    assert command.date_expression is None
    assert command.period_expression == raw["date_expression"]
    assert command.event_type == raw.get("event_type")
    temporal = resolver.resolve(command, NOW)
    business, missing = CommandValidator().validate(command, temporal, case["message"])
    assert isinstance(business, ListEventsCommand) and not missing
    assert business.start.isoformat() == case["expected"]["period_start"]
    assert business.end.isoformat() == case["expected"].get(
        "period_end", case["expected"]["period_start"],
    )
    assert assess(case, ParsedCommand.model_validate(raw))["passed"]
    assert raw["period_expression"] is None  # Normalization doesn't mutate the provider output.


def test_week_deadline_in_period_expression_resolves_to_sunday_without_time():
    case = CASES["pdf_week_deadline"]
    raw = case["parsed_fixture"] | {
        "date_expression": None, "period_expression": "до конца недели",
    }
    command = parse_case(case, raw)
    assert command.date_expression == "до конца недели"
    assert command.period_expression is None
    temporal = resolver.resolve(command, NOW)
    business, missing = CommandValidator().validate(command, temporal, case["message"])
    assert isinstance(business, CreateEventCommand) and not missing
    assert business.event_date == date(2026, 10, 18)
    assert business.event_time is None
    assert temporal.period_start is None and temporal.period_end is None
    assert assess(case, ParsedCommand.model_validate(raw))["passed"]


def test_ambiguous_date_requires_clarification_instead_of_unscheduled_task():
    case = CASES["ambiguous_date"]
    command = parse_case(case, case["parsed_fixture"])
    assert command.date_expression.casefold() == "скоро"
    temporal = resolver.resolve(command, NOW)
    business, missing = CommandValidator().validate(command, temporal, case["message"])
    assert business is None and missing == ["date"]
    assert temporal.event_date is None and "date" in temporal.issues
    assert assess(case, command)["passed"]


def test_prompt_preserves_ambiguous_temporal_expressions_and_intent_field_contract():
    assert "CREATE_EVENT → date_expression/time_expression" in SYSTEM_PROMPT
    assert "LIST_EVENTS → period_expression" in SYSTEM_PROMPT
    assert "SEARCH_EVENTS → period_expression" in SYSTEM_PROMPT
    assert "не отбрасывай" in SYSTEM_PROMPT.casefold()
    assert all(expression in SYSTEM_PROMPT.casefold() for expression in (
        "скоро", "на днях", "в ближайшее время", "до конца недели",
    ))


def test_search_single_day_is_an_inclusive_period_filter():
    message = "Найди встречу с Иваном вчера"
    raw = {"intent": "SEARCH_EVENTS", "event_type": "MEETING", "search_query": "Иваном",
           "date_expression": "вчера"}
    command = parse_case({"message": message}, raw)
    assert command.date_expression is None and command.period_expression == "вчера"
    business, missing = CommandValidator().validate(command, resolver.resolve(command, NOW), message)
    assert isinstance(business, SearchEventsCommand) and not missing
    assert business.start == business.end == date(2026, 10, 13)
    assert business.query == "Иваном" and business.event_type == "MEETING"


@pytest.mark.parametrize("raw,message", [
    ({"intent": "LIST_EVENTS", "date_expression": "завтра", "period_expression": "на следующую неделю"},
     "Покажи планы завтра и на следующую неделю"),
    ({"intent": "CREATE_EVENT", "event_type": "TASK", "title": "Купить билеты",
      "date_expression": "завтра", "period_expression": "до конца недели"},
     "Купить билеты завтра до конца недели"),
    ({"intent": "CREATE_EVENT", "event_type": "TASK", "title": "Купить билеты",
      "period_expression": "на следующую неделю"},
     "Купить билеты на следующую неделю"),
    ({"intent": "SEARCH_EVENTS", "search_query": "Иваном"}, "Найди встречу с Иваном"),
    ({"intent": "CREATE_EVENT", "event_type": "TASK", "title": "Купить билеты"}, "Купить билеты"),
])
def test_normalization_preserves_explicit_fields_other_periods_and_absent_dates(raw, message):
    command = parse_case({"message": message}, raw)
    assert command == ParsedCommand.model_validate(raw)


@pytest.mark.parametrize("case_id", LIST_FAILURES + ["pdf_week_deadline", "ambiguous_date"])
def test_canonical_fixtures_remain_unchanged_when_normalized_twice(case_id):
    case = CASES[case_id]
    first = parse_case(case, case["parsed_fixture"])
    second = parse_case(case, first.model_dump())
    assert first == second == ParsedCommand.model_validate(case["parsed_fixture"])
