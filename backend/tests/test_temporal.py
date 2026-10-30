from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.schemas.commands import ParsedCommand
from app.temporal.recurrence import generate_recurrence_dates, parse_recurrence
from app.temporal.resolver import TemporalAmbiguity, TemporalResolver, normalize_temporal_words

NOW = datetime(2026, 10, 14, 23, 30, tzinfo=ZoneInfo("Europe/Moscow"))
resolver = TemporalResolver("Europe/Moscow")


@pytest.mark.parametrize("expression,expected", [
    ("сегодня", date(2026, 10, 14)), ("завтра", date(2026, 10, 15)),
    ("послезавтра", date(2026, 10, 16)), ("в пятницу", date(2026, 10, 16)),
    ("в следующую пятницу", date(2026, 10, 23)),
    ("через два дня", date(2026, 10, 16)), ("до конца недели", date(2026, 10, 18)),
    ("16.10.2026", date(2026, 10, 16)), ("16 октября", date(2026, 10, 16)),
    ("2024-02-29", date(2024, 2, 29)), ("1 января", date(2027, 1, 1)),
])
def test_dates(expression, expected):
    assert resolver.resolve_date(expression, NOW)[0] == expected


def test_elapsed_hours_cross_midnight():
    assert resolver.resolve_date("через два часа", NOW) == (date(2026, 10, 15), time(1, 30))


@pytest.mark.parametrize("expression,start,end", [
    ("на неделю вперед", "2026-10-14", "2026-10-20"),
    ("на этой неделе", "2026-10-12", "2026-10-18"),
    ("на следующей неделе", "2026-10-19", "2026-10-25"),
    ("до конца недели", "2026-10-14", "2026-10-18"),
    ("завтра", "2026-10-15", "2026-10-15"),
    ("с 16.10.2026 по 20.10.2026", "2026-10-16", "2026-10-20"),
    ("за последние 3 дня", "2026-10-12", "2026-10-14"),
    ("в прошлом месяце", "2026-09-01", "2026-09-30"),
])
def test_periods(expression, start, end):
    assert resolver.resolve_period(expression, NOW) == (date.fromisoformat(start), date.fromisoformat(end))


@pytest.mark.parametrize("expression,expected", [
    ("В 15:00", time(15)), ("в 04:00", time(4)), ("в 10 утра", time(10)),
    ("в четыре вечера", time(16)), ("в 17", time(17)), ("в полдень", time(12)),
    ("в полночь", time(0)), ("12 ночи", time(0)), ("12 дня", time(12)),
])
def test_times(expression, expected):
    assert resolver.resolve_time(expression) == expected


@pytest.mark.parametrize("expression", ["в пять", "в 11", "часов в пять", "25:00", "23:60", "12 утра", "около 15:00"])
def test_ambiguous_times(expression):
    with pytest.raises(TemporalAmbiguity):
        resolver.resolve_time(expression)


@pytest.mark.parametrize("expression", ["31.02.2026", "2026-02-29", "скоро", "в октябре", "05/06", "когда-нибудь"])
def test_ambiguous_dates(expression):
    with pytest.raises(TemporalAmbiguity):
        resolver.resolve_date(expression, NOW)


def test_friday_today_is_ambiguous():
    friday = NOW.replace(day=16)
    with pytest.raises(TemporalAmbiguity):
        resolver.resolve_date("в пятницу", friday)


def test_timezone_converts_utc_and_rejects_naive_now():
    assert resolver.resolve_date("сегодня", datetime(2026, 10, 14, 22, tzinfo=UTC))[0] == date(2026, 10, 15)
    with pytest.raises(ValueError):
        resolver.local_now(datetime(2026, 10, 14))


def test_dst_gap_and_overlap_are_ambiguous():
    ny = TemporalResolver("America/New_York")
    with pytest.raises(TemporalAmbiguity):
        ny.validate_local_datetime(date(2026, 3, 8), time(2, 30))
    with pytest.raises(TemporalAmbiguity):
        ny.validate_local_datetime(date(2026, 11, 1), time(1, 30))


def test_resolve_collects_issues_without_inventing_values():
    result = resolver.resolve(ParsedCommand(intent="CREATE_EVENT", date_expression="скоро", time_expression="в пять"), NOW)
    assert set(result.issues) == {"date", "time"}
    assert result.event_date is None and result.event_time is None


def test_conflicting_relative_time_and_explicit_time_requires_clarification():
    result = resolver.resolve(ParsedCommand(intent="CREATE_EVENT", date_expression="через два часа", time_expression="15:00"), NOW)
    assert "time" in result.issues


def test_alternative_dates_and_periods_are_not_guessed():
    with pytest.raises(TemporalAmbiguity):
        resolver.resolve_date("завтра или в пятницу", NOW)
    with pytest.raises(TemporalAmbiguity):
        resolver.resolve_period("эта неделя или следующая", NOW)


@pytest.mark.parametrize("word,canonical", [
    ("зааавтрааа", "завтра"), ("сегооодняяя", "сегодня"),
    ("пооослезаавтрааа", "послезавтра"), ("ууутрооом", "утром"),
    ("вееечероом", "вечером"), ("нооочьюю", "ночью"),
    ("пооолнооочь", "полночь"), ("ПОООЛДЕЕЕНЬ", "полдень"),
])
def test_only_repeated_letters_of_known_whole_temporal_words_are_normalized(word, canonical):
    assert normalize_temporal_words(word) == canonical
    assert normalize_temporal_words(canonical) == canonical


@pytest.mark.parametrize("text", ["помпезавтра", "завтраками", "сегодя", "позвонить маааме", "утрм"])
def test_normalization_does_not_autocorrect_other_words(text):
    assert normalize_temporal_words(text) == text


@pytest.mark.parametrize("expression,expected", [
    ("сегооодняяя", date(2026, 10, 14)),
    ("зааавтрааа", date(2026, 10, 15)),
    ("пооослезаавтрааа", date(2026, 10, 16)),
])
def test_stretched_dates_resolve_before_dateparser(expression, expected):
    assert resolver.resolve_date(expression, NOW) == (expected, None)


@pytest.mark.parametrize("expression,expected", [
    ("в 9 уутрааа", time(9)), ("в 7 вееечерааа", time(19)),
    ("в пооолнооочь", time(0)),
])
def test_stretched_time_words(expression, expected):
    assert resolver.resolve_time(expression) == expected


@pytest.mark.parametrize("expression,start,end", [
    ("на зааавтрааа и послезавтра", date(2026, 10, 15), date(2026, 10, 16)),
    ("16.10 и 17.10", date(2026, 10, 16), date(2026, 10, 17)),
    ("01.01 и 02.01", date(2026, 1, 1), date(2026, 1, 2)),
])
def test_two_day_periods_and_yearless_pairs_use_current_year(expression, start, end):
    assert resolver.resolve_period(expression, NOW) == (start, end)


@pytest.mark.parametrize("expression", ["31.02 и 01.03", "17.10 и 16.10", "16.10 и 17.10 и 18.10"])
def test_invalid_reversed_and_more_than_two_date_pairs_require_clarification(expression):
    with pytest.raises(TemporalAmbiguity):
        resolver.resolve_period(expression, NOW)
def test_parse_daily_recurrence():
    rule = parse_recurrence("каждый день")

    assert rule.frequency == "daily"
    assert rule.weekdays == ()
    assert rule.month_day is None
def test_parse_weekdays_recurrence():
    rule = parse_recurrence("по будням")

    assert rule.frequency == "weekly"
    assert rule.weekdays == (0, 1, 2, 3, 4)
    assert rule.month_day is None


def test_parse_weekly_weekday_recurrence():
    rule = parse_recurrence("каждый понедельник")

    assert rule.frequency == "weekly"
    assert rule.weekdays == (0,)
    assert rule.month_day is None


def test_parse_weekly_recurrence():
    rule = parse_recurrence("каждую неделю")

    assert rule.frequency == "weekly"
    assert rule.weekdays == ()
    assert rule.month_day is None


def test_parse_monthly_day_recurrence():
    rule = parse_recurrence("каждый месяц 15 числа")

    assert rule.frequency == "monthly"
    assert rule.weekdays == ()
    assert rule.month_day == 15


def test_parse_recurrence_with_count():
    rule = parse_recurrence("каждый день 5 раз")

    assert rule.frequency == "daily"
    assert rule.count == 5
    assert rule.until is None


def test_parse_recurrence_with_until_date():
    rule = parse_recurrence("каждый понедельник до 30.11.2026")

    assert rule.frequency == "weekly"
    assert rule.weekdays == (0,)
    assert rule.until == date(2026, 11, 30)


@pytest.mark.parametrize("expression", [
    "каждый месяц 32 числа",
    "каждый день 0 раз",
    "иногда",
])
def test_invalid_recurrence_is_rejected(expression):
    with pytest.raises(ValueError):
        parse_recurrence(expression)
def test_generate_daily_recurrence_with_count():
    rule = parse_recurrence("каждый день 5 раз")

    assert generate_recurrence_dates(date(2026, 10, 14), rule) == [
        date(2026, 10, 14),
        date(2026, 10, 15),
        date(2026, 10, 16),
        date(2026, 10, 17),
        date(2026, 10, 18),
    ]


def test_generate_weekdays_skips_weekend():
    rule = parse_recurrence("по будням")

    assert generate_recurrence_dates(
        date(2026, 10, 16),
        rule,
        horizon_days=4,
    ) == [
        date(2026, 10, 16),
        date(2026, 10, 19),
        date(2026, 10, 20),
    ]


def test_generate_specific_weekday_from_start_date():
    rule = parse_recurrence("каждый понедельник")

    assert generate_recurrence_dates(
        date(2026, 10, 14),
        rule,
        horizon_days=14,
    ) == [
        date(2026, 10, 19),
        date(2026, 10, 26),
    ]


def test_generate_weekly_recurrence_uses_start_as_anchor():
    rule = parse_recurrence("каждую неделю")

    assert generate_recurrence_dates(
        date(2026, 10, 14),
        rule,
        horizon_days=14,
    ) == [
        date(2026, 10, 14),
        date(2026, 10, 21),
        date(2026, 10, 28),
    ]


def test_generate_monthly_recurrence_skips_month_without_day():
    rule = parse_recurrence("каждый месяц 31 числа")

    assert generate_recurrence_dates(
        date(2026, 10, 14),
        rule,
        horizon_days=110,
    ) == [
        date(2026, 10, 31),
        date(2026, 12, 31),
        date(2027, 1, 31),
    ]


def test_generate_recurrence_until_is_inclusive():
    rule = parse_recurrence("каждый день до 17.10.2026")

    assert generate_recurrence_dates(date(2026, 10, 14), rule) == [
        date(2026, 10, 14),
        date(2026, 10, 15),
        date(2026, 10, 16),
        date(2026, 10, 17),
    ]


def test_generate_recurrence_count_wins_before_horizon():
    rule = parse_recurrence("каждый понедельник 2 раза")

    assert generate_recurrence_dates(
        date(2026, 10, 14),
        rule,
        horizon_days=90,
    ) == [
        date(2026, 10, 19),
        date(2026, 10, 26),
    ]