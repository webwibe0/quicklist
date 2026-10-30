from unittest.mock import MagicMock

import httpx
import openai
import pytest
from pydantic import ValidationError

from app.ai.openai_provider import SYSTEM_PROMPT, OpenAILLMProvider
from app.ai.provider import IntentParser, ProviderError
from app.core.config import Settings
from app.schemas.commands import ParsedCommand
from tests.fakes import FakeLLMProvider


@pytest.mark.parametrize("intent", ["UPDATE_EVENT", "DELETE_EVENT"])
@pytest.mark.parametrize("scope", ["series", "occurrence"])
def test_recurrence_scope_cannot_be_invented_even_if_context_is_a_series(intent, scope):
    text = "измени встречу" if intent == "UPDATE_EVENT" else "удали встречу"
    provider = FakeLLMProvider({text: {"intent": intent, "recurrence_scope": scope}})
    with pytest.raises(ProviderError, match="область серии"):
        IntentParser(provider).parse(text, {"last_created_event": {"recurrence_series_id": "context-only"}})


@pytest.mark.parametrize("phrase,scope", [
    ("только эту встречу", "occurrence"), ("только это событие", "occurrence"),
    ("только это напоминание", "occurrence"), ("всю серию", "series"),
    ("все повторения", "series"), ("все такие встречи", "series"),
])
def test_explicit_recurrence_scope_is_accepted_and_normalized_when_model_omits_it(phrase, scope):
    text = "удали " + phrase
    for model_scope in (scope, None):
        provider = FakeLLMProvider({text: {"intent": "DELETE_EVENT", "recurrence_scope": model_scope}})
        assert IntentParser(provider).parse(text, {}).recurrence_scope == scope


@pytest.mark.parametrize("text", [
    "удали только эту встречу и всю серию", "удали встречу «всю серию»", "удали встречу не всю серию",
])
def test_ambiguous_negated_or_quoted_scope_is_not_permission_for_series(text):
    provider = FakeLLMProvider({text: {"intent": "DELETE_EVENT", "recurrence_scope": "series"}})
    with pytest.raises(ProviderError, match="область серии"):
        IntentParser(provider).parse(text, {})


def test_model_cannot_substitute_occurrence_for_explicit_series_scope():
    text = "удали всю серию"
    with pytest.raises(ProviderError):
        IntentParser(FakeLLMProvider({text: {"intent": "DELETE_EVENT", "recurrence_scope": "occurrence"}})).parse(text, {})


def test_new_title_must_be_explicit_in_user_text():
    text = "переименуй эту встречу"
    with pytest.raises(ProviderError, match="новое название"):
        IntentParser(FakeLLMProvider({text: {"intent": "UPDATE_EVENT", "new_title": "Придумано"}})).parse(text, {})


def test_recurrence_scope_prompt_contract_is_explicit():
    assert 'recurrence_scope=null' in SYSTEM_PROMPT
    assert 'только эту встречу' in SYSTEM_PROMPT and 'всю серию' in SYSTEM_PROMPT
    assert 'new_title' in SYSTEM_PROMPT


def test_openai_adapter_uses_model_env_and_structured_schema(monkeypatch):
    client = MagicMock()
    command = ParsedCommand(intent="UNKNOWN")
    client.responses.parse.return_value.output_parsed = command
    client.__enter__.return_value = client
    monkeypatch.setattr("app.ai.openai_provider.OpenAI", lambda **kwargs: client)
    provider = OpenAILLMProvider()
    provider.settings = Settings(openai_api_key="test-key", openai_model="gpt-5.6-luna")
    assert provider.parse_command("Привет", {}) == command
    arguments = client.responses.parse.call_args.kwargs
    assert arguments["model"] == "gpt-5.6-luna"
    assert arguments["text_format"] is ParsedCommand and arguments["store"] is False
    assert arguments["input"][0] == {"role": "system", "content": SYSTEM_PROMPT}


def test_missing_key_is_explicit_error():
    provider = OpenAILLMProvider()
    provider.settings = Settings(openai_api_key="")
    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        provider.parse_command("Привет", {})


@pytest.mark.parametrize("error_name,description", [
    ("APIConnectionError", "Ошибка соединения с OpenAI"),
    ("APIStatusError", "Ошибка HTTP API OpenAI"),
    ("APITimeoutError", "Превышено время ожидания OpenAI"),
    ("AuthenticationError", "Ошибка аутентификации OpenAI"),
    ("BadRequestError", "Некорректный запрос к OpenAI"),
    ("RateLimitError", "Превышен лимит запросов OpenAI"),
    ("ValidationError", "Ошибка проверки структуры ответа LLM"),
    ("UnknownSDKError", "Ошибка SDK OpenAI"),
])
def test_openai_errors_log_only_type_and_safe_description(
    monkeypatch, caplog, error_name, description,
):
    secrets = (
        "sk-test-secret-key", "postgresql://user:secret@host/database",
        "private-user-message", "private-dialogue-context", "private-llm-response",
        "raw-exception-text",
    )
    raw = " | ".join(secrets)
    request = httpx.Request("POST", "https://example.invalid", content=raw)
    response = httpx.Response(503, request=request, text=raw)
    if error_name == "ValidationError":
        exc = ValidationError.from_exception_data("ParsedCommand", [{
            "type": "value_error", "loc": ("intent",), "input": raw,
            "ctx": {"error": ValueError(raw)},
        }])
    elif error_name == "UnknownSDKError":
        class UnknownSDKError(openai.OpenAIError):
            pass
        exc = UnknownSDKError(raw)
    else:
        error_type = getattr(openai, error_name)
        if issubclass(error_type, openai.APIStatusError):
            exc = error_type(raw, response=response, body={"secret": raw})
        elif error_type is openai.APITimeoutError:
            exc = error_type(request=request)
            exc.args = (raw,)
        else:
            exc = error_type(message=raw, request=request)
    exc.body = {"secret": raw}
    exc.response = response
    client = MagicMock()
    client.__enter__.return_value = client
    client.responses.parse.side_effect = exc
    monkeypatch.setattr("app.ai.openai_provider.OpenAI", lambda **kwargs: client)
    provider = OpenAILLMProvider()
    provider.settings = Settings(openai_api_key=secrets[0], database_url=secrets[1])

    with caplog.at_level("ERROR", logger="app.ai.openai_provider"):
        with pytest.raises(ProviderError) as caught:
            provider.parse_command(secrets[2], {"dialogue_context": secrets[3]})

    assert str(caught.value) == "Не удалось получить корректный ответ LLM. Попробуйте ещё раз."
    assert caught.value.__cause__ is exc
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.getMessage() == f"LLM error: type={error_name}; description={description}"
    assert record.args == (error_name, description)
    assert record.exc_info is None and record.stack_info is None
    assert all(secret not in caplog.text for secret in secrets)


def test_refusal_and_invalid_schema_fail_closed(monkeypatch):
    client = MagicMock()
    client.responses.parse.return_value.output_parsed = None
    client.__enter__.return_value = client
    monkeypatch.setattr("app.ai.openai_provider.OpenAI", lambda **kwargs: client)
    provider = OpenAILLMProvider()
    provider.settings = Settings(openai_api_key="test-key")
    with pytest.raises(ProviderError):
        provider.parse_command("Привет", {})

    class InvalidProvider:
        def parse_command(self, text, context):
            return {"intent": "DELETE_EVENT", "sql": "DROP TABLE events"}
    with pytest.raises(ProviderError):
        IntentParser(InvalidProvider()).parse("x", {})


def test_provider_cannot_invent_absolute_date_or_time():
    class InventingProvider:
        def parse_command(self, text, context):
            return ParsedCommand(intent="CREATE_EVENT", event_type="MEETING",
                                 title="Встреча", date_expression="2026-10-15", time_expression="15:00")
    with pytest.raises(ProviderError, match="неуказанную"):
        IntentParser(InventingProvider()).parse("Поставь завтра встречу", {})


@pytest.mark.parametrize("field,left,right", [
    ("date_expression", "16.10.2026", "16 октября 2026"),
    ("date_expression", "2026-10-16", "16 октября 2026 года"),
    ("date_expression", "на 16.10.2026", "в 16 октября 2026"),
    ("date_expression", "16.10", "16 октября"),
    ("date_expression", "29.02", "29 февраля"),
    ("date_expression", "на 16.10", "в 16 октября"),
    ("period_expression", "16.10.2026", "16 октября 2026"),
    ("period_expression", "с 16.10.2026 по 20.10.2026",
     "с 16 октября 2026 по 20 октября 2026"),
    ("period_expression", "с 16.10.2026 по 20.10.2026",
     "от 16 октября 2026 до 20 октября 2026"),
    ("time_expression", "16:00", "в четыре вечера"),
    ("time_expression", "10:00", "в 10 утра"),
    ("time_expression", "00:00", "в полночь"),
    ("time_expression", "12:00", "в полдень"),
    ("time_expression", "17:00", "в 17"),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_temporal_guard_accepts_equivalent_explicit_values(field, left, right, reverse):
    user, model = (right, left) if reverse else (left, right)
    message = f"Поставь встречу {user}"
    raw = ParsedCommand(intent="CREATE_EVENT", **{field: model})
    parser = IntentParser(FakeLLMProvider({message: raw.model_dump()}))
    assert parser.parse(message, {}) == raw
    assert raw.model_dump()[field] == model


@pytest.mark.parametrize("field,user,model", [
    ("date_expression", "16.10.2026", "17 октября 2026"),
    ("date_expression", "16.10.2026", "16 октября 2027"),
    ("date_expression", "завтра", "2026-10-15"),
    ("date_expression", "16 октября", "16 октября 2026"),
    ("date_expression", "скоро", "16 октября 2026"),
    ("date_expression", "послезавтра", "завтра"),
    ("period_expression", "с 16.10.2026 по 20.10.2026",
     "с 16 октября 2026 по 21 октября 2026"),
    ("period_expression", "с 16.10.2026 по 20.10.2026",
     "с 17 октября 2026 по 20 октября 2026"),
    ("period_expression", "16.10.2026 и 20.10.2026",
     "с 16 октября 2026 по 20 октября 2026"),
    ("period_expression", "на следующую неделю", "с 19.10.2026 по 25.10.2026"),
    ("time_expression", "завтра", "15:00"),
    ("time_expression", "15:00", "5:00"),
    ("time_expression", "в пять", "17:00"),
    ("time_expression", "16:00", "в четыре утра"),
    ("time_expression", "10:00", "10:30"),
])
def test_temporal_guard_still_blocks_inventions_and_partial_matches(field, user, model):
    message = f"Поставь встречу {user}"
    parser = IntentParser(FakeLLMProvider({message: {
        "intent": "CREATE_EVENT", field: model,
    }}))
    with pytest.raises(ProviderError, match="неуказанную дату или время"):
        parser.parse(message, {})


@pytest.mark.parametrize("field,expression", [
    ("date_expression", "скоро"), ("date_expression", "31.02.2026"),
    ("time_expression", "в пять"), ("time_expression", "25:00"),
    ("period_expression", "с 16.10.2026 по 31.02.2026"),
])
def test_guard_preserves_user_ambiguities_for_downstream_clarification(field, expression):
    message = f"Поставь встречу {expression}"
    raw = ParsedCommand(intent="CREATE_EVENT", **{field: expression})
    parser = IntentParser(FakeLLMProvider({message: raw.model_dump()}))
    assert parser.parse(message, {}) == raw


def test_temporal_guard_logs_only_field_without_user_text(caplog):
    message = "Секретный клиент встреча завтра"
    parser = IntentParser(FakeLLMProvider({message: {
        "intent": "CREATE_EVENT", "date_expression": "16.10.2026",
    }}))
    with caplog.at_level("DEBUG", logger="app.ai.provider"):
        with pytest.raises(ProviderError):
            parser.parse(message, {})
    assert "NLU temporal guard mismatch: field=date_expression" in caplog.text
    assert "Секретный" not in caplog.text and "16.10.2026" not in caplog.text


def test_explicit_date_before_sentence_punctuation_is_recognized():
    message = "Встреча 16.10.2026. Клиент Иванов"
    parser = IntentParser(FakeLLMProvider({message: {
        "intent": "CREATE_EVENT", "date_expression": "16 октября 2026",
    }}))
    assert parser.parse(message, {}).date_expression == "16 октября 2026"


@pytest.mark.parametrize("field,user,model", [
    ("date_expression", "зааавтрааа", "завтра"),
    ("date_expression", "СЕГОООДНЯЯЯ", "сегодня"),
    ("date_expression", "пооослезаавтрааа", "послезавтра"),
    ("time_expression", "в 9 уутрааа", "09:00"),
    ("time_expression", "вееечероом", "вечером"),
    ("period_expression", "зааавтрааа и послезавтрааа", "завтра и послезавтра"),
])
def test_guard_accepts_safe_stretched_temporal_equivalents(field, user, model):
    message = f"Планы {user}"
    raw = {"intent": "LIST_EVENTS" if field == "period_expression" else "CREATE_EVENT", field: model}
    parser = IntentParser(FakeLLMProvider({message: raw}))
    assert getattr(parser.parse(message, {}), field) == model


@pytest.mark.parametrize("user,model", [
    ("помпезавтра", "послезавтра"), ("послезавтрааа", "завтра"),
    ("зааавтрааа", "сегодня"), ("сегодя", "сегодня"),
])
def test_guard_does_not_accept_unrelated_corrections_or_changed_relative_dates(user, model):
    parser = IntentParser(FakeLLMProvider({user: {"intent": "CREATE_EVENT", "date_expression": model}}))
    with pytest.raises(ProviderError, match="неуказанную"):
        parser.parse(user, {})
def test_provider_cannot_invent_recurrence_expression():
    class InventingProvider:
        def parse_command(self, text, context):
            return ParsedCommand(
                intent="CREATE_EVENT",
                event_type="REMINDER",
                title="Пить воду",
                date_expression="завтра",
                time_expression="в 9 утра",
                recurrence_expression="каждый день",
            )

    with pytest.raises(ProviderError):
        IntentParser(InventingProvider()).parse(
            "Напомни завтра в 9 утра пить воду",
            {},
        )


def test_provider_accepts_recurrence_expression_from_user_text():
    class RecurrenceProvider:
        def parse_command(self, text, context):
            return ParsedCommand(
                intent="CREATE_EVENT",
                event_type="REMINDER",
                title="Пить воду",
                date_expression="завтра",
                time_expression="в 9 утра",
                recurrence_expression="каждый день",
            )

    command = IntentParser(RecurrenceProvider()).parse(
        "Напомни завтра в 9 утра пить воду каждый день",
        {},
    )

    assert command.recurrence_expression == "каждый день"
