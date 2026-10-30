from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.ai.provider import IntentParser, ProviderError
from app.schemas.commands import ParsedCommand
from app.services.validator import CommandValidator
from app.temporal.recurrence import parse_recurrence
from app.temporal.resolver import TemporalResolver
from app.use_cases.actions import CreateEventUseCase

TEXT = ('Создай напоминание проверить бета-стенд каждый день в 20:00 '
        'начиная с завтра, всего 3 раза')


def captured_command(**changes):
    # Actual temporal fields captured from the controlled production reproduction.
    return ParsedCommand(intent='CREATE_EVENT', event_type='REMINDER',
        title='проверить бета-стенд', date_expression='начиная с завтра',
        time_expression='в 20:00',
        **({'recurrence_expression':'каждый день всего 3 раза'} | changes))


def test_actual_model_fields_pass_literal_guard_and_expand_exactly_three():
    parsed = IntentParser(None)._validate(captured_command(), TEXT)
    now = datetime(2026, 10, 29, 12, tzinfo=ZoneInfo('Europe/Moscow'))
    temporal = TemporalResolver('Europe/Moscow').resolve(parsed, now)
    assert temporal.issues == {}
    command, missing = CommandValidator().validate(parsed, temporal, TEXT)
    assert not missing
    series = CreateEventUseCase.prepare(command)
    assert len(series.occurrences) == 3
    assert [x.event_date for x in series.occurrences] == [
        date(2026, 10, 30) + timedelta(days=i) for i in range(3)]
    assert all(x.event_time == time(20) for x in series.occurrences)


def test_explicit_total_count_is_grammar_not_part_of_frequency():
    assert parse_recurrence('каждый день всего 3 раза').count == 3


@pytest.mark.parametrize('expression', [
    'каждый день всего 4 раза', 'каждый понедельник всего 3 раза',
    'каждый день всего 3 раза до 31.12.2026',
])
def test_invented_count_frequency_or_until_still_rejected(expression):
    with pytest.raises(ProviderError):
        IntentParser(None)._validate(captured_command(recurrence_expression=expression), TEXT)
