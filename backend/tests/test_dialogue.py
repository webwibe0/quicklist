from datetime import date, datetime, time
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.services.dialogue import DialogueService
from app.services.responses import ResponseBuilder
from app.temporal.resolver import TemporalResolver
from tests.fakes import FakeLLMProvider

NOW = datetime(2026, 10, 14, 23, 50, tzinfo=ZoneInfo("Europe/Moscow"))


class RecordingRouter:
    def __init__(self):
        self.commands = []
        # Match ActionRouter's optional retrieval dependency; this create-only
        # double deliberately has no search implementation or external provider.
        self.search = None
        self.repository = SimpleNamespace(
    find_duplicate=lambda command: None,
    find_conflicts=lambda command, **kwargs: [],
)

    def execute(self, command):
        self.commands.append(command)
        return [SimpleNamespace(
            id=uuid4(), type=command.event_type.value.lower(), title=command.title,
            event_date=command.event_date, event_time=command.event_time,
            end_time=None, status="pending", source="user", type_inferred=False,
        )]


def setup(commands):
    provider = FakeLLMProvider(commands)
    router = RecordingRouter()
    service = DialogueService(provider, TemporalResolver("Europe/Moscow"), router, ResponseBuilder(50))
    conversation = SimpleNamespace(id=uuid4(), pending_action=None, last_event_id=None)
    return service, conversation, router


def test_clarification_preserves_resolved_date_across_midnight():
    service, conversation, router = setup({
        "Поставь завтра встречу с Иваном": {"intent": "CREATE_EVENT", "event_type": "MEETING",
                                          "title": "Встреча с Иваном", "date_expression": "завтра"},
        "В 15:00": {"intent": "CREATE_EVENT", "time_expression": "В 15:00"},
    })
    response = service.handle(conversation, "Поставь завтра встречу с Иваном", NOW)
    assert response.requires_input and response.missing_fields == ["time"]
    assert not router.commands
    response = service.handle(conversation, "В 15:00", NOW.replace(day=15, hour=0))
    assert not response.requires_input
    assert router.commands[0].event_date == date(2026, 10, 15)
    assert router.commands[0].event_time == time(15)
    assert conversation.pending_action is None


def test_ambiguous_task_date_is_not_silently_dropped():
    service, conversation, router = setup({
        "Скоро купить билеты": {"intent": "CREATE_EVENT", "event_type": "TASK",
                               "title": "Купить билеты", "date_expression": "скоро"},
        "Завтра": {"intent": "CREATE_EVENT", "date_expression": "Завтра"},
    })
    assert service.handle(conversation, "Скоро купить билеты", NOW).missing_fields == ["date"]
    assert not router.commands
    response = service.handle(conversation, "Завтра", NOW)
    assert response.events[0].event_date == date(2026, 10, 15)


@pytest.mark.parametrize("text,expression", [
    ("Скоро нужно купить билеты", "Скоро"),
    ("На днях нужно купить билеты", "На днях"),
    ("В ближайшее время нужно купить билеты", "В ближайшее время"),
])
def test_preserved_vague_date_asks_for_input_and_only_creates_after_clarification(text, expression):
    service, conversation, router = setup({
        text: {"intent": "CREATE_EVENT", "event_type": "TASK", "title": "Купить билеты",
               "date_expression": expression},
        "Завтра": {"intent": "CREATE_EVENT", "date_expression": "Завтра"},
    })
    result = service.handle(conversation, text, NOW)
    assert result.requires_input and result.missing_fields == ["date"]
    assert not router.commands
    assert conversation.pending_action["command"]["date_expression"] == expression
    result = service.handle(conversation, "Завтра", NOW)
    assert not result.requires_input and len(router.commands) == 1
    assert result.events[0].event_date == date(2026, 10, 15)


def test_task_without_schedule_and_unknown_do_not_need_llm_defaults():
    service, conversation, router = setup({
        "Купить билеты": {"intent": "CREATE_EVENT", "event_type": "TASK", "title": "Купить билеты"},
    })
    assert service.handle(conversation, "Какая погода?", NOW).action == "UNKNOWN"
    assert not router.commands
    result = service.handle(conversation, "Купить билеты", NOW)
    assert result.events[0].event_date is None and result.events[0].event_time is None


def test_invalid_time_followed_by_correction():
    service, conversation, router = setup({
        "Встреча завтра в пять": {"intent": "CREATE_EVENT", "event_type": "MEETING",
                                 "title": "Встреча", "date_expression": "завтра", "time_expression": "в пять"},
        "В 17:00": {"intent": "CREATE_EVENT", "time_expression": "В 17:00"},
    })
    assert service.handle(conversation, "Встреча завтра в пять", NOW).missing_fields == ["time"]
    assert not router.commands
    assert service.handle(conversation, "В 17:00", NOW).events[0].event_time == time(17)


def test_new_create_does_not_inherit_previous_schedule():
    service, conversation, router = setup({
        "Встреча с Иваном завтра": {"intent": "CREATE_EVENT", "event_type": "MEETING",
                                    "title": "Встреча с Иваном", "date_expression": "завтра"},
        "Поставь встречу с командой": {"intent": "CREATE_EVENT", "event_type": "MEETING",
                                      "title": "Встреча с командой"},
    })
    service.handle(conversation, "Встреча с Иваном завтра", NOW)
    result = service.handle(conversation, "Поставь встречу с командой", NOW)
    assert set(result.missing_fields) == {"date", "time"}
    assert conversation.pending_action["command"]["title"] == "Встреча с командой"
    assert not router.commands


def test_title_only_reply_completes_pending_meeting():
    service, conversation, router = setup({
        "Встреча завтра в 15:00": {"intent": "CREATE_EVENT", "event_type": "MEETING",
                                   "date_expression": "завтра", "time_expression": "в 15:00"},
        "Обсудить договор": {"intent": "CREATE_EVENT", "title": "Обсудить договор"},
    })
    assert service.handle(conversation, "Встреча завтра в 15:00", NOW).missing_fields == ["title"]
    result = service.handle(conversation, "Обсудить договор", NOW)
    assert result.events[0].title == "Обсудить договор"
    assert result.events[0].event_time == time(15)
