"""Reported transcripts through parser, resolver, pending state and storage; no API."""
import json
from datetime import datetime, time, timedelta

import pytest

from app.ai.provider import IntentParser, ProviderError
from app.schemas.commands import ParsedCommand
from app.temporal.resolver import TemporalAmbiguity, TemporalResolver
from tests import test_manual_ux_regressions as manual
from tests.fakes import FakeLLMProvider

ux = manual.ux
NOW = datetime.fromisoformat('2026-10-30T12:03:00+03:00')


def send(ux, text, command, now=NOW):
    service, conversation, _, provider = ux
    provider.commands[text] = command
    return service.handle(conversation, text, now)


@pytest.mark.parametrize('expression,minutes', [
    ('через пять минут', 5), ('через 5 минут', 5), ('через полчаса', 30),
    ('через два часа', 120), ('через полтора часа', 90),
])
@pytest.mark.parametrize('field', ['date_expression', 'time_expression'])
def test_elapsed_interval_is_valid_in_either_structured_slot(ux, expression, minutes, field):
    reply = send(ux, 'Создай напоминание '+expression+' почистить зубы', {
        'intent': 'CREATE_EVENT', 'event_type': 'REMINDER', 'title': 'почистить зубы', field: expression,
    })
    assert not reply.requires_input, reply.message
    event, = manual.rows(ux)
    expected = NOW + timedelta(minutes=minutes)
    assert (event.event_date, event.event_time) == (expected.date(), expected.time().replace(tzinfo=None))
    assert ux[1].pending_action is None


@pytest.mark.parametrize('anchor', ['2026-10-30T23:45:00+03:00', '2026-10-31T23:45:00+03:00', '2026-12-31T23:45:00+03:00'])
def test_relative_calendar_rollover(ux, anchor):
    now = datetime.fromisoformat(anchor)
    reply = send(ux, 'Напомни через два часа почистить зубы', {
        'intent': 'CREATE_EVENT', 'event_type': 'REMINDER', 'title': 'почистить зубы',
        'time_expression': 'через два часа',
    }, now)
    assert not reply.requires_input
    event, = manual.rows(ux)
    expected = now + timedelta(hours=2)
    assert (event.event_date, event.event_time) == (expected.date(), expected.time().replace(tzinfo=None))


@pytest.mark.parametrize('time_slot', ['через пять минут', 'сегодня через пять минут'])
def test_explicit_today_and_relative_interval(ux, time_slot):
    reply = send(ux, 'Создай напоминание сегодня через пять минут почистить зубы', {
        'intent': 'CREATE_EVENT', 'event_type': 'REMINDER', 'title': 'почистить зубы',
        'date_expression': 'сегодня', 'time_expression': time_slot,
    })
    assert not reply.requires_input
    assert manual.rows(ux)[0].event_time == time(12, 8)


@pytest.mark.parametrize('expression,clock', [
    ('в шестнадцать сорок восемь', time(16, 48)), ('в шестнадцать пятьдесят', time(16, 50)),
    ('в шестнадцать часов сорок восемь минут', time(16, 48)), ('в 16:48', time(16, 48)),
    ('в двадцать три пятьдесят девять', time(23, 59)),
])
def test_word_minutes_full_command_and_canonical_provenance(ux, expression, clock):
    text = 'Напомни сегодня '+expression+' почистить зубы'
    reply = send(ux, text, {'intent': 'CREATE_EVENT', 'event_type': 'REMINDER',
                          'title': 'почистить зубы', 'date_expression': 'сегодня', 'time_expression': expression})
    assert not reply.requires_input, reply.message
    assert manual.rows(ux)[0].event_time == clock
    parsed = IntentParser(FakeLLMProvider({text: {'intent': 'CREATE_EVENT', 'time_expression': clock.isoformat(timespec='minutes')}})).parse(text, {})
    assert TemporalResolver('Europe/Moscow').resolve_time(parsed.time_expression) == clock


@pytest.mark.parametrize('answer,patch,clock', [
    ('Через полчаса', {'time_expression': 'через полчаса'}, time(12, 33)),
    ('В шестнадцать сорок восемь', {'time_expression': 'в шестнадцать сорок восемь'}, time(16, 48)),
    ('В шестнадцать пятьдесят сегодня', {'date_expression': 'сегодня', 'time_expression': 'в шестнадцать пятьдесят'}, time(16, 50)),
])
def test_time_clarification_after_persisted_pending(ux, answer, patch, clock):
    first = send(ux, 'Напомни сегодня почистить зубы', {
        'intent': 'CREATE_EVENT', 'event_type': 'REMINDER', 'title': 'почистить зубы', 'date_expression': 'сегодня',
    })
    assert first.missing_fields == ['time']
    ux[1].pending_action = json.loads(json.dumps(ux[1].pending_action))
    reply = send(ux, answer, {'intent': 'CREATE_EVENT', **patch})
    assert not reply.requires_input, reply.message
    event, = manual.rows(ux)
    assert event.title == 'почистить зубы' and event.event_time == clock and event.event_date == NOW.date()
    assert ux[1].pending_action is None


def test_relative_date_followup_clears_old_missing_time_issue(ux):
    first = send(ux, 'Напомни почистить зубы', {'intent': 'CREATE_EVENT', 'event_type': 'REMINDER', 'title': 'почистить зубы'})
    assert first.requires_input
    reply = send(ux, 'Через полчаса', {'intent': 'CREATE_EVENT', 'date_expression': 'через полчаса'})
    assert not reply.requires_input, reply.message
    assert len(manual.rows(ux)) == 1


def test_conflicting_calendar_day_and_elapsed_interval_is_not_guessed(ux):
    reply = send(ux, 'Напомни завтра через два часа почистить зубы', {
        'intent': 'CREATE_EVENT', 'event_type': 'REMINDER', 'title': 'почистить зубы',
        'date_expression': 'завтра', 'time_expression': 'через два часа',
    })
    assert reply.requires_input and not manual.rows(ux)


def test_followup_relative_cannot_silently_override_tomorrow(ux):
    send(ux, 'Напомни завтра почистить зубы', {'intent': 'CREATE_EVENT', 'event_type': 'REMINDER', 'title': 'почистить зубы', 'date_expression': 'завтра'})
    reply = send(ux, 'Через полчаса', {'intent': 'CREATE_EVENT', 'time_expression': 'через полчаса'})
    assert reply.requires_input and not manual.rows(ux)


@pytest.mark.parametrize('hours,clock,ambiguous', [(2, time(2, 45), True), (3, time(3, 45), False)])
def test_relative_utc_timeline_respects_configured_zone(hours, clock, ambiguous):
    resolver = TemporalResolver('Europe/Berlin')
    now = datetime.fromisoformat('2026-10-25T01:45:00+02:00')
    result = resolver.resolve(ParsedCommand(intent='CREATE_EVENT', time_expression=f'через {hours} часа'), now)
    assert result.event_time == clock
    # Storage has local date/time, not a UTC instant: preserve the existing DST
    # ambiguity guard rather than silently discarding fold=1.
    assert bool(result.issues) == ambiguous


def test_unknown_words_or_invented_relative_duration_fail_closed():
    text = 'Напомни через пять минут почистить зубы'
    with pytest.raises(ProviderError):
        IntentParser(FakeLLMProvider({text: {'intent': 'CREATE_EVENT', 'time_expression': 'через 10 минут'}})).parse(text, {})
    with pytest.raises(TemporalAmbiguity):
        TemporalResolver('Europe/Moscow').resolve_time('в шестнадцать шестьдесят')


def test_relative_equivalent_number_spelling_is_not_absolute_date_invention():
    text = 'Напомни через пять минут почистить зубы'
    parsed = IntentParser(FakeLLMProvider({text: {'intent': 'CREATE_EVENT', 'time_expression': 'через 5 минут'}})).parse(text, {})
    assert parsed.time_expression == 'через 5 минут'


def test_date_slot_followup_clears_preexisting_invalid_time_issue(ux):
    send(ux, 'Напомни сегодня вечером почистить зубы', {
        'intent': 'CREATE_EVENT', 'event_type': 'REMINDER', 'title': 'почистить зубы',
        'date_expression': 'сегодня', 'time_expression': 'вечером',
    })
    reply = send(ux, 'Через полчаса', {'intent': 'CREATE_EVENT', 'date_expression': 'через полчаса'})
    assert not reply.requires_input and len(manual.rows(ux)) == 1


def test_elapsed_followup_unknown_intent_from_model_is_not_needed(ux):
    send(ux, 'Напомни сегодня почистить зубы', {
        'intent': 'CREATE_EVENT', 'event_type': 'REMINDER', 'title': 'почистить зубы', 'date_expression': 'сегодня',
    })
    reply = send(ux, 'Через полчаса', {'intent': 'UNKNOWN'})
    assert not reply.requires_input and len(manual.rows(ux)) == 1


@pytest.mark.parametrize('label,date_expression,time_expression,expected_date,clock,missing', [
    ('01', None, 'через пять минут', '2026-10-30', '12:08', None),
    ('02', None, 'через 5 минут', '2026-10-30', '12:08', None),
    ('03', None, 'через полчаса', '2026-10-30', '12:33', None),
    ('04', None, 'через два часа', '2026-10-30', '14:03', None),
    ('05', 'сегодня', 'через пять минут', '2026-10-30', '12:08', None),
    ('06', 'завтра', 'через два часа', None, None, 'date'),
    ('07', 'сегодня', 'в девять часов вечера', '2026-10-30', '21:00', None),
    ('08', None, 'в девять вечера', None, None, 'date'),
    ('09', 'сегодня', 'в шестнадцать сорок восемь', '2026-10-30', '16:48', None),
    ('10', 'сегодня', 'в шестнадцать пятьдесят', '2026-10-30', '16:50', None),
    ('11', 'сегодня', 'в шестнадцать часов сорок восемь минут', '2026-10-30', '16:48', None),
    ('12', 'сегодня', 'в 16:48', '2026-10-30', '16:48', None),
    ('13', 'сегодня', 'в половине десятого вечера', '2026-10-30', '21:30', None),
    ('14', 'сегодня', 'в четверть десятого утра', '2026-10-30', '09:15', None),
    ('15', 'сегодня', 'в час дня', '2026-10-30', '13:00', None),
    ('16', 'сегодня', 'в час ночи', '2026-10-30', '01:00', None),
    ('17', 'сегодня', 'в час', None, None, 'time'),
    ('18', 'сегодня', 'вечером', None, None, 'time'),
    ('19', None, 'через полчаса', '2026-10-31', '00:15', None),
    ('20', None, 'через два часа', '2026-11-01', '01:45', None),
    ('21', None, 'через два часа', '2027-01-01', '01:45', None),
    ('22', 'в пятницу', None, None, None, 'date'),
    ('23', 'на следующей неделе', None, None, None, 'LIST'),
    ('24', 'послезавтра', None, '2026-11-01', None, None),
    ('25', 'до конца недели', None, '2026-11-01', None, None),
])
def test_requested_25_temporal_cases_through_dialogue_and_storage(ux, label, date_expression, time_expression, expected_date, clock, missing):
    now = datetime.fromisoformat({
        '19': '2026-10-30T23:45:00+03:00', '20': '2026-10-31T23:45:00+03:00',
        '21': '2026-12-31T23:45:00+03:00',
    }.get(label, NOW.isoformat()))
    event_type = 'TASK' if label in ('22', '24', '25') else 'REMINDER'
    text = 'Создай '+('задачу' if event_type == 'TASK' else 'напоминание')+' проверить материалы'
    text += ' '+ ' '.join(value for value in (date_expression, time_expression) if value)
    command = {'intent': 'CREATE_EVENT', 'event_type': event_type, 'title': 'проверить материалы',
               'date_expression': date_expression, 'time_expression': time_expression}
    if label == '23':
        text = 'Покажи планы на следующей неделе'
        command = {'intent': 'LIST_EVENTS', 'period_expression': date_expression}
    reply = send(ux, text, command, now)
    if missing == 'LIST':
        assert reply.action == 'LIST_EVENTS' and not reply.requires_input and not manual.rows(ux)
        assert TemporalResolver('Europe/Moscow').resolve_period(date_expression, now)[0].isoformat() == '2026-11-02'
    elif missing:
        assert reply.requires_input and missing in reply.missing_fields and not manual.rows(ux)
    else:
        if reply.requires_input:
            assert reply.missing_fields == ['confirmation'] and 'прош' in reply.message.casefold()
            reply = send(ux, 'да', {}, now)
        assert not reply.requires_input
        event, = manual.rows(ux)
        assert event.event_date.isoformat() == expected_date
        assert event.event_time == (time.fromisoformat(clock) if clock else None)
        assert ux[1].pending_action is None
