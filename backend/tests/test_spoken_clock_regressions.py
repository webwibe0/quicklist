"""Exact Russian-time report and pending-slot regressions, with no external API."""
from datetime import date, time

import pytest

from app.ai.provider import IntentParser, ProviderError
from app.temporal.resolver import TemporalAmbiguity, TemporalResolver
from tests import test_manual_ux_regressions
from tests.fakes import FakeLLMProvider
from tests.test_manual_ux_regressions import NOW, handle, rows

ux=test_manual_ux_regressions.ux


@pytest.mark.parametrize('expression,clock',[
 ('в девять часов вечера',time(21)),('в девять вечера',time(21)),
 ('в семь утра',time(7)),('в половине десятого вечера',time(21,30)),
 ('в четверть десятого утра',time(9,15)),('в двадцать один ноль-ноль',time(21)),
 ('в 21:00',time(21)),('в час дня',time(13)),('в час ночи',time(1)),
])
def test_existing_resolver_accepts_unambiguous_spoken_clock(expression,clock):
    assert TemporalResolver('Europe/Moscow').resolve_time(expression)==clock


@pytest.mark.parametrize('expression',['в час','вечером','часов в девять','в половине десятого'])
def test_missing_daypart_or_approximate_clock_still_requires_clarification(expression):
    with pytest.raises(TemporalAmbiguity):
        TemporalResolver('Europe/Moscow').resolve_time(expression)


@pytest.mark.parametrize('verb',['Напомни','Напомню','Поставь напоминание','Создай напоминание'])
def test_complete_command_with_correct_structured_slots_is_not_rejected_downstream(ux,verb):
    reply=handle(ux,verb+' сходить помыться сегодня в девять часов вечера',{
        'intent':'CREATE_EVENT','event_type':'REMINDER','title':'сходить помыться',
        'date_expression':'сегодня','time_expression':'в девять часов вечера',
    })
    assert not reply.requires_input and len(rows(ux))==1
    assert rows(ux)[0].event_time==time(21) and rows(ux)[0].event_date==NOW.date()


def test_date_only_followup_retains_known_spoken_time_and_title(ux):
    reply=handle(ux,'Напомни сходить помыться в девять часов вечера',{
        'intent':'CREATE_EVENT','event_type':'REMINDER','title':'сходить помыться',
        'time_expression':'в девять часов вечера',
    })
    assert reply.missing_fields==['date']
    reply=handle(ux,'Сегодня',{'intent':'CREATE_EVENT','date_expression':'сегодня'})
    assert not reply.requires_input and len(rows(ux))==1
    assert rows(ux)[0].title=='сходить помыться' and rows(ux)[0].event_time==time(21)


def test_time_only_followup_retains_known_date_and_title(ux):
    reply=handle(ux,'Напомни сходить помыться сегодня',{
        'intent':'CREATE_EVENT','event_type':'REMINDER','title':'сходить помыться','date_expression':'сегодня',
    })
    assert reply.missing_fields==['time']
    reply=handle(ux,'В девять вечера',{'intent':'CREATE_EVENT','time_expression':'в девять вечера'})
    assert not reply.requires_input and len(rows(ux))==1
    assert rows(ux)[0].event_date==NOW.date() and rows(ux)[0].event_time==time(21)


@pytest.mark.parametrize('text,clock',[('в девять часов вечера','21:00'),
 ('в половине десятого вечера','21:30'),('в четверть десятого утра','09:15'),
 ('в двадцать один ноль-ноль','21:00')])
def test_equivalent_clock_is_validated_by_the_same_parser_not_a_substring_bypass(text,clock):
    parsed=IntentParser(FakeLLMProvider({text:{'intent':'CREATE_EVENT','time_expression':clock}})).parse(text,{})
    assert TemporalResolver('Europe/Moscow').resolve_time(parsed.time_expression)==time.fromisoformat(clock)


def test_absent_time_still_fails_provenance():
    text='Напомни завтра помыться'
    with pytest.raises(ProviderError):
        IntentParser(FakeLLMProvider({text:{'intent':'CREATE_EVENT','time_expression':'21:00'}})).parse(text,{})


def test_spoken_zero_zero_is_not_mistaken_for_an_interval(ux):
    reply=handle(ux,'Напомни завтра помыться в двадцать один ноль-ноль',{
        'intent':'CREATE_EVENT','event_type':'REMINDER','title':'помыться',
        'date_expression':'завтра','time_expression':'в двадцать один ноль-ноль',
    })
    assert not reply.requires_input and len(rows(ux))==1 and rows(ux)[0].event_time==time(21)


@pytest.mark.parametrize('expression,expected',[
    ('за 25.10.2027',date(2027,10,25)),('за 25 октября 2027 года',date(2027,10,25)),
    ('за вчера',NOW.date().replace(day=NOW.day-1)),
])
def test_recorded_history_single_day_preposition_is_resolved_without_another_question(expression,expected):
    assert TemporalResolver('Europe/Moscow').resolve_period(expression,NOW)==(expected,expected)
