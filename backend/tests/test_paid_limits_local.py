"""Local runtime uses the real adapters/SDK against mock HTTP and temporary ledgers only."""
import io
import wave

import httpx
import pytest
from openai import OpenAI
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import embeddings, openai_provider, speech_provider
from app.ai.paid_limits import client_options
from app.api import routes
from app.core.config import Settings
from app.core.spend_budget import SpendBudget
from app.main import app
from app.models.entities import Conversation, Event
from app.schemas.commands import ParsedCommand
from app.services.message_transaction import execute_message
from tests.test_hybrid_search import chat as chat
from tests.test_hybrid_search import event


def local_settings(tmp_path, amount, **extra):
    return Settings(_env_file=None, app_environment='local', openai_api_key=SecretStr('synthetic-only'),
                    interactive_budget_usd=amount, interactive_budget_max_calls=1,
                    interactive_budget_path=tmp_path / 'interactive.sqlite3',
                    embedding_real_api_enabled=True, semantic_search_enabled=True, **extra)


def wav():
    output = io.BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b'\x01\x00' * 16000)
    return output.getvalue()


@pytest.fixture
def mock_http(monkeypatch):
    calls = []

    def reply(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/responses'):
            command = ParsedCommand(intent='CREATE_EVENT', event_type='TASK', title='Synthetic task')
            return httpx.Response(200, json={
                'id': 'resp_synthetic', 'object': 'response', 'created_at': 0, 'status': 'completed',
                'model': 'gpt-5.6-luna', 'usage': {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12},
                'output': [{'id': 'msg_synthetic', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                            'content': [{'type': 'output_text', 'text': command.model_dump_json(), 'annotations': []}]}],
            })
        if request.url.path.endswith('/embeddings'):
            return httpx.Response(200, json={'object': 'list', 'model': 'text-embedding-3-large',
                'data': [{'index': 0, 'object': 'embedding', 'embedding': [1.0] + [0.0] * 1535}],
                'usage': {'prompt_tokens': 10, 'total_tokens': 10}})
        if request.url.path.endswith('/audio/transcriptions'):
            return httpx.Response(200, json={'text': 'Synthetic speech', 'usage': {'type': 'duration', 'seconds': 1}})
        if request.url.path.endswith('/audio/speech'):
            return httpx.Response(200, content=b'ID3synthetic-only', headers={'content-type': 'audio/mpeg'})
        pytest.fail('Unexpected paid endpoint')

    def forbidden(*args, **kwargs):
        pytest.fail('Live HTTP transport is forbidden in offline budget tests')

    # A regression bypassing LimitedTransport must still never reach a real socket.
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(reply))
    return calls


def adapter_call(kind, settings, monkeypatch):
    if kind == 'nlu':
        monkeypatch.setattr(openai_provider, 'get_settings', lambda: settings)
        command = openai_provider.OpenAILLMProvider().parse_command('Создай задачу Synthetic task', {})
        assert command.title == 'Synthetic task'
    elif kind == 'embedding':
        provider = embeddings.OpenAIEmbeddingProvider(settings, allow_paid_api=True)
        try:
            assert len(provider.embed(['Synthetic query'])[0]) == 1536
        finally:
            provider.close()
    elif kind == 'stt':
        assert speech_provider.OpenAISpeechProvider(settings).transcribe(wav()) == 'Synthetic speech'
    else:
        assert speech_provider.OpenAISpeechProvider(settings).speak('Synthetic speech').startswith(b'ID3')


def expected_error(kind):
    if kind == 'nlu':
        return pytest.raises(openai_provider.ProviderError, match='Лимит')
    if kind == 'embedding':
        return pytest.raises(embeddings.EmbeddingError, match='BUDGET_BLOCKED')
    return pytest.raises(speech_provider.AudioError, match='Лимит')


def test_local_client_attaches_limiter_even_with_zero_allowance(tmp_path):
    settings = Settings(_env_file=None, app_environment='local',
                        interactive_budget_path=tmp_path / 'interactive.sqlite3')
    options = client_options(settings)
    assert 'http_client' in options, 'Local interactive API must not bypass the limiter'
    options['http_client'].close()


@pytest.mark.parametrize('kind', ['nlu', 'stt', 'tts', 'embedding'])
def test_zero_local_budget_blocks_each_actual_adapter_before_http(tmp_path, monkeypatch, mock_http, kind):
    settings = local_settings(tmp_path, 0)
    with expected_error(kind):
        adapter_call(kind, settings, monkeypatch)
    assert mock_http == []
    assert SpendBudget(settings.interactive_budget_path, limit_nanos=0, max_calls=1).summary()['calls'] == 0


@pytest.mark.parametrize('kind', ['nlu', 'stt', 'tts', 'embedding'])
def test_positive_local_budget_exhausts_and_survives_adapter_restart(tmp_path, monkeypatch, mock_http, kind):
    settings = local_settings(tmp_path, 2)
    adapter_call(kind, settings, monkeypatch)
    path = settings.interactive_budget_path
    first = SpendBudget(path, limit_nanos=2_000_000_000, max_calls=1).summary()
    assert first['calls'] == 1 and first['committed_nanos'] > 0
    # A new adapter/SDK/ledger instance cannot renew the finite allowance.
    with expected_error(kind):
        adapter_call(kind, settings, monkeypatch)
    assert len(mock_http) == 1
    assert SpendBudget(path, limit_nanos=2_000_000_000, max_calls=1).summary() == first
    if kind == 'tts':
        assert first['committed_nanos'] == 1_000_000_000 and first['unknown'] == 1


def test_local_clients_share_one_interactive_allowance(tmp_path, monkeypatch, mock_http):
    settings = local_settings(tmp_path, 2)
    adapter_call('nlu', settings, monkeypatch)
    for kind in ('stt', 'tts', 'embedding'):
        with expected_error(kind):
            adapter_call(kind, settings, monkeypatch)
    assert mock_http == ['/v1/responses']


def test_local_dollar_cap_is_not_renewed_with_unused_call_slots(tmp_path, monkeypatch, mock_http):
    settings = local_settings(tmp_path, 1.01).model_copy(update={'interactive_budget_max_calls': 100})
    adapter_call('tts', settings, monkeypatch)
    with expected_error('tts'):
        adapter_call('tts', settings, monkeypatch)
    summary = SpendBudget(settings.interactive_budget_path, limit_nanos=1_010_000_000, max_calls=100).summary()
    assert summary['calls'] == 1 and summary['committed_nanos'] == 1_000_000_000
    assert mock_http == ['/v1/audio/speech']


def test_background_keeps_its_existing_finite_budget_separate(tmp_path, monkeypatch, mock_http):
    settings = local_settings(tmp_path, 0)
    assert client_options(settings, background=True) == {}
    assert not settings.interactive_budget_path.exists()
    worker = SpendBudget(tmp_path / 'worker.sqlite3', limit_nanos=50_000_000, max_calls=500, min_interval=30)
    # Inspect actual constructor routing while supplying only a mock SDK transport.
    def sdk(**options):
        assert 'http_client' not in options  # Worker has its own pre-send SpendBudget.
        return OpenAI(**options, http_client=httpx.Client(transport=httpx.HTTPTransport()))
    monkeypatch.setattr(embeddings, 'OpenAI', sdk)
    provider = embeddings.OpenAIEmbeddingProvider(settings, allow_paid_api=True, budget=worker)
    try:
        assert len(provider.embed(['Synthetic worker item'])[0]) == 1536
    finally:
        provider.close()
    summary = SpendBudget(worker.path, limit_nanos=50_000_000, max_calls=500, min_interval=30).summary()
    assert summary['calls'] == 1 and summary['committed_nanos'] == 1300
    assert not settings.interactive_budget_path.exists()
    assert mock_http == ['/v1/embeddings']


def snapshot(engine):
    with Session(engine) as session:
        return {model.__tablename__: [
            {column.key: getattr(row, column.key) for column in model.__table__.columns}
            for row in session.scalars(select(model).order_by(model.id))]
            for model in (Event, Conversation)}


def configure_chat_budget(settings, tmp_path):
    settings.openai_api_key = SecretStr('synthetic-only')
    settings.interactive_budget_usd = 0
    settings.interactive_budget_max_calls = 1
    settings.interactive_budget_path = tmp_path / 'chat-interactive.sqlite3'
    settings.embedding_real_api_enabled = True


def test_nlu_budget_refusal_rolls_back_new_and_existing_pending_dialogue(chat, tmp_path, monkeypatch, mock_http):
    client, parser, _, engine, settings = chat
    with Session(engine) as session, session.begin():
        session.add(event('Existing task'))
    parser.commands['Напомни позвонить'] = dict(intent='CREATE_EVENT', event_type='REMINDER', title='Позвонить')
    response = client.post('/api/v1/assistant/messages', json={'message': 'Напомни позвонить'})
    identity = response.json()['conversation_id']
    assert response.json()['requires_input']
    before = snapshot(engine)
    assert before['conversations'][0]['pending_action'] is not None
    configure_chat_budget(settings, tmp_path)
    monkeypatch.setattr(openai_provider, 'get_settings', lambda: settings)
    app.dependency_overrides[openai_provider.get_provider] = lambda: openai_provider.OpenAILLMProvider()
    for extra in ({}, {'conversation_id': identity}):
        response = client.post('/api/v1/assistant/messages', json={
            'message': 'Создай задачу Synthetic task', **extra})
        assert response.status_code == 503 and 'Лимит' in response.json()['detail']
        assert snapshot(engine) == before  # Events, PendingAction, messages, context and IDs unchanged.
    assert mock_http == []


def test_query_embedding_budget_refusal_keeps_lexical_results_and_events(chat, tmp_path, monkeypatch, mock_http):
    client, parser, _, engine, settings = chat
    with Session(engine) as session, session.begin():
        row = event('Отчёт')
        session.add(row)
        identity = str(row.id)
    before = snapshot(engine)['events']
    parser.commands['Найди Отчёт'] = dict(intent='SEARCH_EVENTS', search_query='Отчёт')
    configure_chat_budget(settings, tmp_path)
    # Use the real query adapter, not the scripted embedding provider from chat().
    monkeypatch.setattr(routes, 'execute_message', execute_message)
    response = client.post('/api/v1/assistant/messages', json={'message': 'Найди Отчёт'})
    assert response.status_code == 200
    body = response.json()
    assert body['search_mode'] == 'lexical_fallback'
    assert [row['id'] for row in body['events']] == [identity]
    assert snapshot(engine)['events'] == before
    assert mock_http == []


@pytest.mark.parametrize('fault', ['binding_changed', 'corrupt'])
def test_query_budget_storage_refusal_also_falls_back(chat, tmp_path, monkeypatch, mock_http, fault):
    client, parser, _, engine, settings = chat
    with Session(engine) as session, session.begin():
        row = event('Отчёт')
        session.add(row)
        identity = str(row.id)
    before = snapshot(engine)['events']
    parser.commands['Найди Отчёт'] = dict(intent='SEARCH_EVENTS', search_query='Отчёт')
    configure_chat_budget(settings, tmp_path)
    path = settings.interactive_budget_path
    if fault == 'binding_changed':
        SpendBudget(path, limit_nanos=1_000_000, max_calls=1)
    else:
        path.write_bytes(b'Synthetic corrupt SQLite')
    original_bytes = path.read_bytes()
    monkeypatch.setattr(routes, 'execute_message', execute_message)
    response = client.post('/api/v1/assistant/messages', json={'message': 'Найди Отчёт'})
    assert response.status_code == 200
    assert response.json()['search_mode'] == 'lexical_fallback'
    assert [row['id'] for row in response.json()['events']] == [identity]
    assert snapshot(engine)['events'] == before
    assert path.read_bytes() == original_bytes
    assert mock_http == []
