"""Recurring CREATE extracts spoken fields; only backend derives its first date."""

import pytest

from app.ai.openai_provider import SYSTEM_PROMPT
from app.ai.provider import IntentParser, ProviderError
from app.schemas.commands import Intent
from app.temporal.resolver import TemporalAmbiguity, TemporalResolver
from tests.fakes import FakeLLMProvider


@pytest.mark.parametrize("text,event_type,title,recurrence,clock", [
    ("каждый день в 12:00 напоминай мне чистить зубы", "REMINDER", "чистить зубы",
     "каждый день", "в 12:00"),
    ("напоминай мне каждый день в 12:00 чистить зубы", "REMINDER", "чистить зубы",
     "каждый день", "в 12:00"),
    ("по будням в 9 утра напоминай проверить почту", "REMINDER", "проверить почту",
     "по будням", "в 9 утра"),
    ("каждый понедельник в 10 утра встреча команды", "MEETING", "встреча команды",
     "каждый понедельник", "в 10 утра"),
    ("каждый месяц 15 числа в 10 утра напомни оплатить счёт", "REMINDER", "оплатить счёт",
     "каждый месяц 15 числа", "в 10 утра"),
])
def test_natural_recurring_create_keeps_unspoken_start_date_null(
    text, event_type, title, recurrence, clock,
):
    provider = FakeLLMProvider({text: {
        "intent": "CREATE_EVENT", "event_type": event_type, "title": title,
        "recurrence_expression": recurrence, "time_expression": clock,
    }})

    command = IntentParser(provider).parse(text, {})

    assert command.intent == Intent.CREATE_EVENT
    assert command.event_type == event_type
    assert command.title == title
    assert command.recurrence_expression == recurrence
    assert command.time_expression == clock
    assert command.date_expression is None
    assert command.period_expression is None


@pytest.mark.parametrize("invented", ["сегодня", "завтра", "19.10.2026"])
def test_provider_cannot_invent_start_date_for_recurring_create(invented):
    text = "каждый день в 12:00 напоминай мне чистить зубы"
    provider = FakeLLMProvider({text: {
        "intent": "CREATE_EVENT", "event_type": "REMINDER", "title": "чистить зубы",
        "recurrence_expression": "каждый день", "time_expression": "в 12:00",
        "date_expression": invented,
    }})

    with pytest.raises(ProviderError, match="неуказанную дату"):
        IntentParser(provider).parse(text, {})


def test_recurring_create_preserves_explicit_start_date_literal():
    text = "с 1 ноября каждый день в 12:00 напомни принимать витамины"
    provider = FakeLLMProvider({text: {
        "intent": "CREATE_EVENT", "event_type": "REMINDER", "title": "принимать витамины",
        "recurrence_expression": "каждый день", "time_expression": "в 12:00",
        "date_expression": "1 ноября",
    }})

    command = IntentParser(provider).parse(text, {})

    assert command.date_expression == "1 ноября"
    assert command.recurrence_expression == "каждый день"


@pytest.mark.parametrize("clock", ["в 10", "в 12"])
def test_recurring_create_does_not_guess_ambiguous_hour(clock):
    text = f"каждый день {clock} напоминай чистить зубы"
    provider = FakeLLMProvider({text: {
        "intent": "CREATE_EVENT", "event_type": "REMINDER", "title": "чистить зубы",
        "recurrence_expression": "каждый день", "time_expression": clock,
    }})

    command = IntentParser(provider).parse(text, {})

    assert command.time_expression == clock
    with pytest.raises(TemporalAmbiguity, match="Укажите ЧЧ:ММ"):
        TemporalResolver("Europe/Moscow").resolve_time(command.time_expression)


def test_recurring_create_prompt_assigns_first_date_to_backend():
    # Limit this assertion to recurrence instructions, not unrelated LIST/SEARCH rules.
    recurrence_contract = SYSTEM_PROMPT.split(
        "Повторяющиеся события поддерживаются для CREATE_EVENT.", 1,
    )[1].split("Для UPDATE_EVENT/DELETE_EVENT", 1)[0]

    assert "date_expression=null" in recurrence_contract
    assert "backend" in recurrence_contract.casefold()
    assert "перв" in recurrence_contract.casefold()
    assert "Не вычисляй" in recurrence_contract
