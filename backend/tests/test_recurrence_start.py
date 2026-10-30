"""Implicit recurrence dates use the request clock; explicit dates remain authoritative."""
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.schemas.commands import ParsedCommand
from app.services.validator import CommandValidator
from app.temporal.recurrence import RecurrenceLimitError, parse_recurrence, resolve_recurrence_start
from app.temporal.resolver import TemporalResolver

MOSCOW = ZoneInfo("Europe/Moscow")
RESOLVER = TemporalResolver("Europe/Moscow")


def command(**changes):
    return ParsedCommand.model_validate(dict(
        intent="CREATE_EVENT", event_type="REMINDER", title="Чистить зубы",
        recurrence_expression="каждый день", time_expression="12:00",
    ) | changes)


@pytest.mark.parametrize("expression,now,clock,expected", [
    ("каждый день", "2026-10-19T11:59:59", "12:00", "2026-10-19"),
    ("каждый день", "2026-10-19T12:00:00", "12:00", "2026-10-20"),
    ("каждый день", "2026-10-19T12:00:00.000001", "12:00", "2026-10-20"),
    ("каждый день", "2026-10-19T23:59:59", "12:00", "2026-10-20"),
    ("каждый день", "2026-12-31T23:59:59", "00:00", "2027-01-01"),
    ("по будням", "2026-10-19T08:00:00", "09:00", "2026-10-19"),
    ("по будням", "2026-10-23T10:00:00", "09:00", "2026-10-26"),
    ("по будням", "2026-10-24T08:00:00", "09:00", "2026-10-26"),
    ("по будням", "2026-10-25T08:00:00", "09:00", "2026-10-26"),
    ("каждый понедельник", "2026-10-19T09:00:00", "10:00", "2026-10-19"),
    ("каждый понедельник", "2026-10-19T11:00:00", "10:00", "2026-10-26"),
    ("каждый понедельник", "2026-10-20T09:00:00", "10:00", "2026-10-26"),
    ("каждую неделю", "2026-10-19T11:00:00", "12:00", "2026-10-19"),
    ("каждую неделю", "2026-10-19T13:00:00", "12:00", "2026-10-20"),
    ("каждый месяц 15 числа", "2026-10-15T09:00:00", "10:00", "2026-10-15"),
    ("каждый месяц 15 числа", "2026-10-15T11:00:00", "10:00", "2026-11-15"),
    ("каждый месяц 15 числа", "2026-10-19T09:00:00", "10:00", "2026-11-15"),
    ("каждый месяц 31 числа", "2026-10-31T11:00:00", "10:00", "2026-12-31"),
    ("каждый месяц 31 числа", "2026-11-01T09:00:00", "10:00", "2026-12-31"),
    ("каждый месяц 30 числа", "2027-02-01T09:00:00", "10:00", "2027-03-30"),
    ("каждый месяц 29 числа", "2028-02-01T09:00:00", "10:00", "2028-02-29"),
    ("каждый месяц 29 числа", "2027-02-01T09:00:00", "10:00", "2027-03-29"),
])
def test_implicit_first_date_matches_rule_and_is_strictly_future(expression, now, clock, expected):
    now = datetime.fromisoformat(now).replace(tzinfo=MOSCOW)
    parsed = command(recurrence_expression=expression, time_expression=clock)
    temporal = RESOLVER.resolve(parsed, now)
    assert not temporal.issues
    assert temporal.event_date == date.fromisoformat(expected)
    assert datetime.combine(temporal.event_date, temporal.event_time, MOSCOW) > now
    business, missing = CommandValidator().validate(parsed, temporal, "запрос")
    assert missing == [] and business.event_date == temporal.event_date


@pytest.mark.parametrize("expression,expected", [
    ("каждый понедельник", "2026-10-26"), ("каждый вторник", "2026-10-20"),
    ("каждую среду", "2026-10-21"), ("каждый четверг", "2026-10-22"),
    ("каждую пятницу", "2026-10-23"), ("каждую субботу", "2026-10-24"),
    ("каждое воскресенье", "2026-10-25"),
])
def test_all_supported_weekdays(expression, expected):
    temporal = RESOLVER.resolve(command(recurrence_expression=expression),
                                datetime(2026, 10, 19, 13, tzinfo=MOSCOW))
    assert temporal.event_date == date.fromisoformat(expected)


@pytest.mark.parametrize("expression,expected", [
    ("25 октября", "2026-10-25"), ("с 25 октября", "2026-10-25"),
    ("с 1 ноября", "2026-11-01"), ("с завтрашнего дня", "2026-10-20"),
    ("18.10.2026", "2026-10-18"), ("сегодня", "2026-10-19"),
])
def test_explicit_date_is_never_rolled_forward(expression, expected):
    temporal = RESOLVER.resolve(command(date_expression=expression),
                                datetime(2026, 10, 19, 13, tzinfo=MOSCOW))
    assert not temporal.issues
    assert temporal.event_date == date.fromisoformat(expected)


def test_ambiguous_explicit_date_is_not_replaced_by_implicit_date():
    parsed = command(date_expression="скоро")
    temporal = RESOLVER.resolve(parsed, datetime(2026, 10, 19, 9, tzinfo=MOSCOW))
    assert temporal.event_date is None and "date" in temporal.issues
    _, missing = CommandValidator().validate(parsed, temporal, "запрос")
    assert missing == ["date"]


def test_no_time_requests_only_time_then_recomputes_date_from_same_clock():
    parsed = command(time_expression=None)
    now = datetime(2026, 10, 19, 13, tzinfo=MOSCOW)
    temporal = RESOLVER.resolve(parsed, now)
    _, missing = CommandValidator().validate(parsed, temporal, "запрос")
    assert missing == ["time"]
    temporal = RESOLVER.resolve(parsed.model_copy(update={"time_expression": "12:00"}), now)
    assert temporal.event_date == date(2026, 10, 20)


def test_start_uses_moscow_calendar_when_request_clock_is_utc():
    temporal = RESOLVER.resolve(command(time_expression="01:00"),
                                datetime(2026, 10, 18, 22, 30, tzinfo=UTC))
    assert temporal.event_date == date(2026, 10, 20)


@pytest.mark.parametrize("expression", [
    "через день", "каждый месяц 32 числа", "каждый день 99999999 раз",
    "каждый день до 18.10.2026",
])
def test_invalid_recurrence_without_explicit_date_is_domain_clarification(expression):
    parsed = command(recurrence_expression=expression)
    temporal = RESOLVER.resolve(parsed, datetime(2026, 10, 19, 9, tzinfo=MOSCOW))
    assert "recurrence" in temporal.issues
    _, missing = CommandValidator().validate(parsed, temporal, "запрос")
    assert missing == ["recurrence"]


def test_non_create_recurrence_does_not_invent_date():
    temporal = RESOLVER.resolve(command(intent="UPDATE_EVENT"),
                                datetime(2026, 10, 19, 9, tzinfo=MOSCOW))
    assert temporal.event_date is None


def test_date_only_task_recurrence_does_not_invent_time():
    parsed = command(event_type="TASK", time_expression=None)
    temporal = RESOLVER.resolve(parsed, datetime(2026, 10, 19, 13, tzinfo=MOSCOW))
    business, missing = CommandValidator().validate(parsed, temporal, "запрос")
    assert missing == []
    assert business.event_date == date(2026, 10, 19) and business.event_time is None


def test_implicit_start_cannot_roll_past_inclusive_until():
    parsed = command(recurrence_expression="каждый день до 19.10.2026")
    result = RESOLVER.resolve(parsed, datetime(2026, 10, 19, 13, tzinfo=MOSCOW))
    assert result.event_date is None and list(result.issues) == ["recurrence"]


def test_no_future_representable_date_is_domain_error_not_overflow():
    with pytest.raises(RecurrenceLimitError):
        resolve_recurrence_start(parse_recurrence("каждый день"),
                                 datetime(9999, 12, 31, 13, tzinfo=MOSCOW), time(12))


def test_start_helper_requires_request_timezone_and_honours_explicit_date():
    rule = parse_recurrence("каждый день")
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_recurrence_start(rule, datetime(2026, 10, 19, 13), time(12))
    assert resolve_recurrence_start(rule, datetime(2026, 10, 19, 13, tzinfo=MOSCOW),
                                    time(12), explicit_date=date(2026, 10, 18)) == date(2026, 10, 18)
