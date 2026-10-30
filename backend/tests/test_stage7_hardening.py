"""User-reported temporal regressions. Clock is fixed; no live language model."""
from datetime import date, datetime, time

import pytest

from app.schemas.commands import ParsedCommand
from app.temporal.resolver import TemporalResolver

NOW = datetime.fromisoformat("2026-10-19T11:00:00+03:00")
RESOLVER = TemporalResolver("Europe/Moscow")


@pytest.mark.parametrize("expression,start,end", [
    ("с 7 до 9 вечера", "19:00", "21:00"),
    ("с 7 вечера до 9", "19:00", "21:00"),
    ("с 7 вечера до 9 вечера", "19:00", "21:00"),
    ("с 7 до 9 утра", "07:00", "09:00"),
    ("с 7 утра до 9", "07:00", "09:00"),
    ("с 2 до 4 дня", "14:00", "16:00"),
    ("с 19 до 21", "19:00", "21:00"),
    ("с 19:00 до 21:00", "19:00", "21:00"),
    ("с 7:30 до 9:15 вечера", "19:30", "21:15"),
    ("с 11 вечера до 1 ночи", "23:00", "01:00"),
    ("с 7 утра до 9 вечера", "07:00", "21:00"),
    ("с 7 вечера до 9 утра", "19:00", "09:00"),
])
def test_range_daypart_is_shared_without_overwriting_explicit_other_daypart(expression, start, end):
    result = RESOLVER.resolve(ParsedCommand(intent="CREATE_EVENT", event_type="MEETING",
        title="Созвон", date_expression="послезавтра", time_expression=expression), NOW)
    assert result.issues == {}
    assert result.event_date == date(2026, 10, 21)
    assert (result.event_time, result.end_time) == (time.fromisoformat(start), time.fromisoformat(end))


@pytest.mark.parametrize("expression", [
    "с 7 до 9", "с 25 до 27 вечера", "с 19:90 до 21:00", "с 19:00 до 19:00",
    "с 21:00 до 19:00", "с 9 вечера до 7 вечера", "с 0 утра до 1 вечера",
])
def test_invalid_or_ambiguous_range_is_not_silently_overnight(expression):
    result = RESOLVER.resolve(ParsedCommand(intent="CREATE_EVENT", event_type="MEETING",
        title="Созвон", date_expression="завтра", time_expression=expression), NOW)
    assert result.issues


@pytest.mark.parametrize("expression,start,end", [
    ("за 2 недели", "2026-10-05", "2026-10-19"),
    ("за последние две недели", "2026-10-05", "2026-10-19"),
    ("за последние 7 дней", "2026-10-13", "2026-10-19"),
    ("за последние 30 дней", "2026-09-20", "2026-10-19"),
    ("за последние три месяца", "2026-07-19", "2026-10-19"),
    ("за прошлую неделю", "2026-10-12", "2026-10-18"),
    ("в прошлом месяце", "2026-09-01", "2026-09-30"),
    ("за эту неделю", "2026-10-19", "2026-10-25"),
    ("на следующей неделе", "2026-10-26", "2026-11-01"),
    ("с 10.10 по 19.10", "2026-10-10", "2026-10-19"),
    ("с 10 октября по 19 октября", "2026-10-10", "2026-10-19"),
    ("с 10.10.2026 по 19.10.2026", "2026-10-10", "2026-10-19"),
    ("с 28 декабря по 5 января", "2026-12-28", "2027-01-05"),
    ("с 30.12 по 02.01", "2026-12-30", "2027-01-02"),
    ("с 01.01 по 31.12", "2026-01-01", "2026-12-31"),
])
def test_periods_resolve_as_a_range_not_independent_future_dates(expression, start, end):
    assert RESOLVER.resolve_period(expression, NOW) == (date.fromisoformat(start), date.fromisoformat(end))


@pytest.mark.parametrize("expression", ["за 999999999 дней", "за последние ноль недель", "с 31.02 по 05.03"])
def test_bad_periods_are_controlled_issues(expression):
    result = RESOLVER.resolve(ParsedCommand(intent="LIST_EVENTS", period_expression=expression), NOW)
    assert "period" in result.issues


@pytest.mark.parametrize("expression,direction,start,end", [
    ("с 28 декабря по 5 января", "past", "2025-12-28", "2026-01-05"),
    ("с 28 декабря по 5 января", "future", "2026-12-28", "2027-01-05"),
    ("с 30.12 по 02.01", "past", "2025-12-30", "2026-01-02"),
    ("с 10.10 по 19.10", "past", "2026-10-10", "2026-10-19"),
    ("с 28.12.2025 по 05.01", "future", "2025-12-28", "2026-01-05"),
    ("с 28.12 по 05.01.2026", "future", "2025-12-28", "2026-01-05"),
    ("15 октября", "past", "2026-10-15", "2026-10-15"),
    ("25 октября", "future", "2026-10-25", "2026-10-25"),
    ("в январе", "past", "2026-01-01", "2026-01-31"),
    ("в январе", "future", "2027-01-01", "2027-01-31"),
    ("29 февраля", "past", "2024-02-29", "2024-02-29"),
    ("29 февраля", "future", "2028-02-29", "2028-02-29"),
])
def test_partial_dates_respect_direction_and_explicit_year(expression, direction, start, end):
    assert RESOLVER.resolve_period(expression, NOW, direction=direction) == (date.fromisoformat(start), date.fromisoformat(end))


@pytest.mark.parametrize("expression", ["с 20.10 по 10.10", "29.02.2026", "с 30.12.2026 по 02.01.2026"])
def test_explicit_invalid_year_or_unjustified_reversal_is_not_silently_repaired(expression):
    result = RESOLVER.resolve(ParsedCommand(intent="LIST_EVENTS", period_expression=expression), NOW)
    assert result.issues


def test_create_missing_year_stays_future_and_uses_moscow_calendar():
    assert RESOLVER.resolve_date("5 ноября", NOW)[0] == date(2026, 11, 5)
    assert RESOLVER.resolve_date("15 октября", NOW)[0] == date(2027, 10, 15)
    utc_clock = datetime.fromisoformat("2026-10-18T22:00:00+00:00")
    assert RESOLVER.resolve_period("за 2 недели", utc_clock) == (date(2026, 10, 5), date(2026, 10, 19))
