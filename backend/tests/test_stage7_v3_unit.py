"""V3: literal NLU evidence, recurrence grammar, targets and relative clocks."""
from datetime import date, datetime, time

import pytest

from app.ai.provider import IntentParser, ProviderError
from app.schemas.commands import ParsedCommand
from app.services.targets import title_score
from app.temporal.recurrence import generate_recurrence_dates, parse_recurrence
from app.temporal.resolver import TemporalResolver
from tests.fakes import FakeLLMProvider

NOW = datetime.fromisoformat('2026-10-19T22:00:00+03:00')


def test_noncontiguous_until_is_literal_evidence_not_an_invented_date():
    text = 'напоминай мне каждый четверг чистить зубы в 10:00 до 25.03.2027'
    provider = FakeLLMProvider({text: dict(intent='CREATE_EVENT',event_type='REMINDER',title='Чистить зубы',
        recurrence_expression='каждый четверг до 25.03.2027',time_expression='10:00')})
    parsed = IntentParser(provider).parse(text,{})
    result = TemporalResolver('Europe/Moscow').resolve(parsed,NOW)
    assert not result.issues and result.event_date == date(2026,10,22)
    assert parse_recurrence(parsed.recurrence_expression).until == date(2027,3,25)


def test_invented_until_remains_rejected():
    text = 'напоминай каждый четверг в 10:00 до 25.03.2027'
    with pytest.raises(ProviderError):
        IntentParser(FakeLLMProvider({text: dict(intent='CREATE_EVENT',event_type='REMINDER',title='Чистить зубы',
            recurrence_expression='каждый четверг до 26.03.2027',time_expression='10:00')})).parse(text,{})


@pytest.mark.parametrize('expression,days',[
    ('каждый понедельник и пятницу',(0,4)), ('по понедельникам и пятницам',(0,4)),
    ('каждый вторник и четверг',(1,3)), ('по понедельникам, средам и пятницам',(0,2,4)),
    ('каждый понедельник, среду и пятницу',(0,2,4)), ('по вторникам и четвергам',(1,3)),
])
def test_multiple_weekdays_are_one_rule(expression,days):
    rule = parse_recurrence(expression+' 5 раз')
    dates = generate_recurrence_dates(date(2026,10,19),rule)
    assert rule.weekdays == days and len(dates)==5
    assert all(day.weekday() in days for day in dates)


@pytest.mark.parametrize('expression',[
    'раз в две недели по четвергам','каждые две недели по четвергам','каждые 2 недели по четвергам',
])
def test_biweekly_anchor_and_count(expression):
    rule = parse_recurrence(expression+' 4 раза')
    assert rule.interval==2
    assert generate_recurrence_dates(date(2026,10,22),rule)==[
        date(2026,10,22),date(2026,11,5),date(2026,11,19),date(2026,12,3)]


@pytest.mark.parametrize('expression',[
    'каждый четверг 3 раза до 25.03.2027', 'каждый четверг до 25.03.2027 3 раза',
])
def test_count_and_until_work_in_either_order(expression):
    rule=parse_recurrence(expression)
    assert rule.count==3 and rule.until==date(2027,3,25)
    assert len(generate_recurrence_dates(date(2026,10,22),rule))==3


@pytest.mark.parametrize('expression',['два раза в неделю','каждый второй четверг','каждый второй четверг месяца'])
def test_different_or_ambiguous_schedules_are_not_guessed(expression):
    with pytest.raises(ValueError):
        parse_recurrence(expression)


@pytest.mark.parametrize('expression,day,clock',[
    ('через 15 минут',date(2026,10,19),time(22,15)),
    ('через полтора часа',date(2026,10,19),time(23,30)),
    ('через 90 минут',date(2026,10,19),time(23,30)),
    ('через день',date(2026,10,20),None),
])
def test_relative_clock_uses_request_time(expression,day,clock):
    assert TemporalResolver('Europe/Moscow').resolve_date(expression,NOW)==(day,clock)


def test_nominal_and_verbal_target_match_without_negation_loss():
    assert title_score('обсуждение бюджета','Обсудить бюджет по презентации')>=.9
    assert title_score('не обсуждать бюджет','Обсудить бюджет по презентации')==0


def test_zoom_range_is_already_general_and_not_specific_to_seven():
    parsed=ParsedCommand(intent='CREATE_EVENT',event_type='MEETING',title='Зум с коллегами',
        date_expression='послезавтра',time_expression='с 4 до 7 вечера')
    result=TemporalResolver('Europe/Moscow').resolve(parsed,NOW)
    assert not result.issues and (result.event_time,result.end_time)==(time(16),time(19))


@pytest.mark.parametrize('selection',['0','-1','с 5 по 2','с 1 по 51','первые 51','1, 9000000000 и 2'])
def test_candidate_selection_rejects_outside_visible_scope(selection):
    from app.services.candidate_selection import SelectionError, candidate_indices
    with pytest.raises(SelectionError):
        candidate_indices(selection,50)


def test_relative_plural_field_is_normalized_without_losing_sequential_anchor():
    text='напомни спать через час и потом еще через 3 часа'
    command=IntentParser(FakeLLMProvider({text:dict(intent='CREATE_EVENT',event_type='REMINDER',title='Спать',
        time_expressions=['через час','через 3 часа'])})).parse(text,{})
    assert len(command.actions)==2
    assert command.actions[1].relative_anchor_expression=='потом еще'


def test_child_actions_keep_temporal_hallucination_guard():
    text='напомни завтра в 8 утра проснуться и в 7 вечера поесть'
    with pytest.raises(ProviderError):
        IntentParser(FakeLLMProvider({text:dict(intent='CREATE_EVENT',actions=[
            dict(intent='CREATE_EVENT',event_type='REMINDER',title='Проснуться',date_expression='завтра',time_expression='в 8 утра'),
            dict(intent='CREATE_EVENT',event_type='REMINDER',title='Поесть',time_expression='в 6 вечера')])})).parse(text,{})



def test_sequential_relation_is_grounded_even_if_model_omits_connector():
    text='напомни спать через час и потом еще через 3 часа'
    actions=[dict(intent='CREATE_EVENT',event_type='REMINDER',title='Спать',date_expression='через час'),
        dict(intent='CREATE_EVENT',event_type='REMINDER',date_expression='через 3 часа')]
    command=IntentParser(FakeLLMProvider({text:dict(intent='CREATE_EVENT',actions=actions)})).parse(text,{})
    assert command.actions[1].relative_anchor_expression=='потом еще'



def test_large_weekly_interval_cannot_bypass_ten_year_safety_horizon():
    from app.temporal.recurrence import RecurrenceLimitError
    with pytest.raises(RecurrenceLimitError):
        generate_recurrence_dates(date(2026,10,19),parse_recurrence('каждые 52 недели 200 раз'))
