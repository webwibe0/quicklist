"""Listing filters misclassified by Luna must not require a lexical search query."""
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from scripts.run_evals import assess

from app.schemas.commands import ListEventsCommand, ParsedCommand, SearchEventsCommand
from app.services.dialogue import DialogueService
from app.services.responses import ResponseBuilder
from app.services.validator import CommandValidator, normalize_action
from app.temporal.resolver import TemporalResolver, TemporalResult
from tests.fakes import FakeLLMProvider

CASES = {
    case["id"]: case
    for case in json.loads(
        (Path(__file__).resolve().parents[2] / "evals/command_cases.json").read_text(
            encoding="utf-8",
        )
    )
}
NOW = datetime.fromisoformat("2026-10-14T12:00:00+03:00")


def service_for(commands):
    router = Mock()
    router.execute.return_value = []
    service = DialogueService(
        FakeLLMProvider(commands), TemporalResolver("Europe/Moscow"), router, ResponseBuilder(50),
    )
    conversation = SimpleNamespace(id=uuid4(), pending_action=None, last_event_id=None)
    return service, conversation, router


@pytest.mark.parametrize("query", [None, "", " \t "])
def test_period_range_search_without_query_routes_to_list_and_passes_unchanged_eval(query):
    case = CASES["period_range"]
    raw = case["parsed_fixture"] | {"intent": "SEARCH_EVENTS", "search_query": query}
    service, conversation, router = service_for({case["message"]: raw})
    response = service.handle(conversation, case["message"], NOW)
    assert response.action == "LIST_EVENTS" and not response.requires_input
    assert response.missing_fields == [] and conversation.pending_action is None
    command = router.execute.call_args.args[0]
    assert isinstance(command, ListEventsCommand)
    assert command.event_type == "MEETING"
    assert command.start == date(2026, 10, 16) and command.end == date(2026, 10, 20)
    report = assess(case, ParsedCommand.model_validate(raw))
    assert report["passed"] and report["actual"]["intent"] == "LIST_EVENTS"
    assert report["actual"]["missing_fields"] == []
    assert raw["intent"] == "SEARCH_EVENTS"


@pytest.mark.parametrize("changes", [
    {"search_query": "Иванова"},
    {"title": "Встреча с Иваном"},
    {"time_expression": "15:00"},
])
def test_search_content_is_not_silently_discarded_as_list_filters(changes):
    raw = {"intent": "SEARCH_EVENTS", "event_type": "MEETING", "search_query": None,
           "period_expression": "с 16.10.2026 по 20.10.2026"} | changes
    # Assess evaluates the typed output, including the absence of a usable query.
    case = {"message": "test", "now": NOW.isoformat(), "id": "search_content",
            "expected": {"intent": "SEARCH_EVENTS"}}
    report = assess(case, ParsedCommand.model_validate(raw))
    assert report["passed"]
    if not raw["search_query"]:
        assert "search_query" in report["actual"]["missing_fields"]


def test_type_filter_alone_keeps_existing_no_search_query_fixture():
    case = CASES["no_search_query"]
    service, conversation, router = service_for({case["message"]: case["parsed_fixture"]})
    response = service.handle(conversation, case["message"], NOW)
    assert response.action == "SEARCH_EVENTS" and response.requires_input
    assert response.missing_fields == ["search_query"]
    router.execute.assert_not_called()
    assert assess(case, ParsedCommand.model_validate(case["parsed_fixture"]))["passed"]


def test_both_resolved_bounds_are_sufficient_for_listing():
    raw = ParsedCommand(intent="SEARCH_EVENTS", event_type="MEETING")
    temporal = TemporalResult(period_start=date(2026, 10, 16), period_end=date(2026, 10, 20))
    command = normalize_action(raw, temporal)
    business, missing = CommandValidator().validate(command, temporal, "Покажи встречи")
    assert command.intent == "LIST_EVENTS" and raw.intent == "SEARCH_EVENTS"
    assert isinstance(business, ListEventsCommand) and not missing
    assert business.start == temporal.period_start and business.end == temporal.period_end


@pytest.mark.parametrize("bounds", [
    {"period_start": date(2026, 10, 16)}, {"period_end": date(2026, 10, 20)},
])
def test_partial_resolved_bounds_do_not_turn_incomplete_search_into_listing(bounds):
    command = ParsedCommand(intent="SEARCH_EVENTS", event_type="MEETING")
    assert normalize_action(command, TemporalResult(**bounds)).intent == "SEARCH_EVENTS"


def test_invalid_listing_period_requires_period_instead_of_search_query():
    message = "Покажи встречи с 16.10.2026 по 31.02.2026"
    service, conversation, router = service_for({message: {
        "intent": "SEARCH_EVENTS", "event_type": "MEETING",
        "period_expression": "с 16.10.2026 по 31.02.2026",
    }})
    response = service.handle(conversation, message, NOW)
    assert response.action == "LIST_EVENTS" and response.requires_input
    assert response.missing_fields == ["period"]
    router.execute.assert_not_called()


def test_single_day_date_normalization_composes_with_search_to_list():
    message = "Покажи встречи завтра"
    service, conversation, router = service_for({message: {
        "intent": "SEARCH_EVENTS", "event_type": "MEETING", "date_expression": "завтра",
    }})
    response = service.handle(conversation, message, NOW)
    assert response.action == "LIST_EVENTS" and response.missing_fields == []
    command = router.execute.call_args.args[0]
    assert isinstance(command, ListEventsCommand)
    assert command.start == command.end == date(2026, 10, 15)


def test_period_reply_retains_known_pending_search_query():
    service, conversation, router = service_for({
        "Найди Иванова скоро": {"intent": "SEARCH_EVENTS", "search_query": "Иванова",
                               "period_expression": "скоро"},
        "Завтра": {"intent": "SEARCH_EVENTS", "date_expression": "Завтра"},
    })
    assert service.handle(conversation, "Найди Иванова скоро", NOW).requires_input
    response = service.handle(conversation, "Завтра", NOW)
    assert response.action == "SEARCH_EVENTS" and not response.requires_input
    command = router.execute.call_args.args[0]
    assert isinstance(command, SearchEventsCommand) and command.query == "Иванова"
    assert command.start == command.end == date(2026, 10, 15)
    assert conversation.pending_action is None


def test_pending_search_without_query_is_cleared_after_successful_listing():
    service, conversation, router = service_for({
        "Найди задачу": {"intent": "SEARCH_EVENTS", "event_type": "TASK"},
        "с 16.10.2026 по 20.10.2026": {
            "intent": "SEARCH_EVENTS", "period_expression": "с 16.10.2026 по 20.10.2026",
        },
    })
    assert service.handle(conversation, "Найди задачу", NOW).missing_fields == ["search_query"]
    response = service.handle(conversation, "с 16.10.2026 по 20.10.2026", NOW)
    assert response.action == "LIST_EVENTS" and not response.requires_input
    command = router.execute.call_args.args[0]
    assert isinstance(command, ListEventsCommand) and command.event_type == "TASK"
    assert conversation.pending_action is None


@pytest.mark.parametrize("intent,field", [
    ("LIST_EVENTS", "period_expression"), ("LIST_EVENTS", "date_expression"),
    ("SEARCH_EVENTS", "period_expression"),
])
def test_rephrased_model_range_reaches_listing_router(intent, field):
    message = "Покажи мои встречи с 16.10.2026 по 20.10.2026"
    service, conversation, router = service_for({message: {
        "intent": intent, "event_type": "MEETING",
        field: "с 16 октября 2026 по 20 октября 2026",
    }})
    response = service.handle(conversation, message, NOW)
    assert response.action == "LIST_EVENTS" and not response.requires_input
    command = router.execute.call_args.args[0]
    assert isinstance(command, ListEventsCommand)
    assert command.start == date(2026, 10, 16) and command.end == date(2026, 10, 20)
