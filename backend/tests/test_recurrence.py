"""Stage 7 safety policy and pure expansion. No live provider calls."""
from datetime import date, time, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.models.entities import Event
from app.schemas.commands import BatchCreateEventCommand, CreateEventCommand, Intent
from app.services.responses import ResponseBuilder
from app.temporal.recurrence import (
    MAX_RECURRENCE_OCCURRENCES,
    RecurrenceError,
    RecurrenceLimitError,
    generate_recurrence_dates,
    parse_recurrence,
)
from app.use_cases.actions import CreateEventUseCase


def test_unbounded_daily_materializes_exactly_90_calendar_days():
    start = date(2026, 10, 20)
    dates = generate_recurrence_dates(start, parse_recurrence("каждый день"))
    assert dates == [start + timedelta(days=i) for i in range(90)]


def test_count_extends_beyond_default_horizon_and_skips_invalid_month_days():
    assert generate_recurrence_dates(date(2026, 10, 31), parse_recurrence(
        "каждый месяц 31 числа 3 раза",
    )) == [date(2026, 10, 31), date(2026, 12, 31), date(2027, 1, 31)]


@pytest.mark.parametrize("expression", [
    "каждый день 99999999 раз", "каждый день до 19.10.2026",
    "каждый день до 20.10.9999", "каждый понедельник до 20.10.2026",
])
def test_invalid_or_oversized_expansion_is_rejected_not_empty_or_truncated(expression):
    with pytest.raises(ValueError):
        generate_recurrence_dates(date(2026, 10, 20), parse_recurrence(expression))


def create_command(**changes):
    return CreateEventCommand(event_type="REMINDER", title="Пить воду", event_date=date(2026, 10, 20),
                              event_time=time(10), raw_user_text="исходный запрос", **changes)


def test_pure_preview_has_no_persistence_and_generates_one_id_only():
    command = create_command(recurrence_expression="каждый вторник 3 раза")
    series_id = uuid4()
    with patch("app.use_cases.actions.uuid4", return_value=series_id) as make_id:
        preview = CreateEventUseCase.prepare(command)
        assert make_id.call_count == 1
    assert len(preview.occurrences) == 3
    assert {event.recurrence_series_id for event in preview.occurrences} == {series_id}
    assert all(event.recurrence_expression is None for event in preview.occurrences)
    assert command.recurrence_expression == "каждый вторник 3 раза" and command.recurrence_series_id is None
    with patch("app.use_cases.actions.uuid4", side_effect=AssertionError("No regeneration")):
        assert CreateEventUseCase.prepare(preview) is preview


def test_nonrecurring_preview_preserves_the_original_command():
    command = create_command()
    assert CreateEventUseCase.expand(command) == [command]
    assert CreateEventUseCase.prepare(command) is command


def test_aggregate_materialization_cap_applies_to_multiple_clock_times():
    command = create_command(recurrence_expression=f"каждый день {MAX_RECURRENCE_OCCURRENCES} раз")
    batch = BatchCreateEventCommand(events=[command, command.model_copy(update={"event_time": time(19)})])
    with pytest.raises(RecurrenceLimitError):
        CreateEventUseCase.prepare(batch)


def test_count_can_reach_hard_cap_without_silent_truncation():
    assert len(generate_recurrence_dates(date(2026, 10, 20), parse_recurrence(
        f"каждый день {MAX_RECURRENCE_OCCURRENCES} раз",
    ))) == MAX_RECURRENCE_OCCURRENCES


def test_sparse_monthly_count_can_reach_row_cap_independently_of_until_policy():
    dates = generate_recurrence_dates(date(2026, 10, 31), parse_recurrence(
        f"каждый месяц 31 числа {MAX_RECURRENCE_OCCURRENCES} раз",
    ))
    assert len(dates) == MAX_RECURRENCE_OCCURRENCES and all(day.day == 31 for day in dates)


def test_until_is_inclusive_beyond_default_horizon():
    dates = generate_recurrence_dates(date(2026, 10, 20), parse_recurrence("каждый день до 28.01.2027"))
    assert dates[-1] == date(2027, 1, 28) and len(dates) == 101


def test_until_that_exceeds_row_cap_is_rejected_not_silently_cut():
    with pytest.raises(RecurrenceLimitError):
        generate_recurrence_dates(date(2026, 10, 20), parse_recurrence("каждый день до 20.10.2027"))


@pytest.mark.parametrize("expression", ["каждый месяц 32 числа", "каждый день 0 раз", "через день", "каждый день до 31.02.2027"])
def test_errors_are_domain_specific(expression):
    with pytest.raises(RecurrenceError):
        parse_recurrence(expression)


def test_date_boundary_does_not_overflow_or_return_incomplete_count():
    with pytest.raises(RecurrenceLimitError):
        generate_recurrence_dates(date.max, parse_recurrence("каждый день 2 раза"))


@pytest.mark.parametrize("count,limit", [(30, 3), (1, 3), (3, 1)])
def test_series_response_counts_before_slicing_and_exposes_metadata(count, limit):
    series_id = uuid4()
    events = [Event(id=uuid4(), type="reminder", title="Пить воду", event_date=date(2026, 10, 20),
                    event_time=time(10), end_time=None, status="pending", source="user", type_inferred=False,
                    recurrence_series_id=series_id, recurrence_rule=f"FREQ=DAILY;COUNT={count}") for _ in range(count)]
    response = ResponseBuilder(limit).success(uuid4(), Intent.CREATE_EVENT, events)
    assert f"Событий: {count}." in response.message and "Создана серия" in response.message
    assert len(response.events) == min(count, limit)
    assert all(event.recurrence_series_id == series_id for event in response.events)


@pytest.mark.parametrize("intent", [Intent.CREATE_EVENT, Intent.UPDATE_EVENT, Intent.DELETE_EVENT, Intent.COMPLETE_EVENT])
def test_empty_mutation_response_does_not_index_events_zero(intent):
    assert not ResponseBuilder(50).success(uuid4(), intent, []).events
