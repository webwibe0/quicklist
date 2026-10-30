
import httpx
import pytest

from app.ai.paid_limits import LimitedTransport
from app.core.config import Settings
from app.core.spend_budget import BudgetBlocked


def settings(tmp_path, amount):
    return Settings(_env_file=None, app_environment='staging', auth_origin='https://example.test',
        auth_cookie_secure=True, auth_require_email_verification=True,
        interactive_budget_usd=amount, interactive_budget_path=tmp_path/'interactive.sqlite3')


def request(path, data):
    return httpx.Request('POST', 'https://api.openai.com/v1/'+path, json=data)


def test_zero_production_allowance_blocks_before_network(tmp_path):
    calls=[]
    transport=LimitedTransport(settings(tmp_path,0), httpx.MockTransport(lambda r:calls.append(r)))
    with pytest.raises(BudgetBlocked):
        transport.handle_request(request('responses',{'model':'gpt-5.6-luna','max_output_tokens':512}))
    assert calls==[]


def test_nlu_verified_usage_and_unknown_tts_remain_bound_after_restart(tmp_path):
    calls=[]
    def response(r):
        calls.append(r.url.path)
        return httpx.Response(200,request=r,json={'usage':{'input_tokens':10,'output_tokens':2}})
    config=settings(tmp_path,1.01)
    transport=LimitedTransport(config,httpx.MockTransport(response))
    transport.handle_request(request('responses',{'model':'gpt-5.6-luna','max_output_tokens':512}))
    transport.handle_request(request('audio/speech',{'model':'gpt-4o-mini-tts','input':'Проверка'}))
    reopened=LimitedTransport(config,httpx.MockTransport(response))
    with pytest.raises(BudgetBlocked):
        reopened.handle_request(request('audio/speech',{'model':'gpt-4o-mini-tts','input':'Повтор'}))
    assert len(calls)==2
    assert reopened.budget.summary()['committed_nanos']==1_000_004_900
    assert reopened.budget.summary()['unknown']==1


@pytest.mark.parametrize('model',['unpriced-model','gpt-5.6-sol'])
def test_unpriced_model_is_not_sent(tmp_path,model):
    calls=[]
    transport=LimitedTransport(settings(tmp_path,10),httpx.MockTransport(lambda r:calls.append(r)))
    with pytest.raises(BudgetBlocked):
        transport.handle_request(request('responses',{'model':model,'max_output_tokens':512}))
    assert not calls


@pytest.mark.parametrize('adapter', ['embedding', 'nlu', 'tts'])
def test_exhaustion_is_reported_as_budget_not_network_failure(tmp_path, monkeypatch, adapter):
    from openai import OpenAI

    from app.ai import embeddings, openai_provider, speech_provider
    config = settings(tmp_path, 0).model_copy(update={'openai_api_key': __import__('pydantic').SecretStr('synthetic-not-a-real-key')})
    calls=[]
    transport = LimitedTransport(config, httpx.MockTransport(lambda r: calls.append(r)))
    client = OpenAI(api_key='synthetic-not-a-real-key', max_retries=0,
                    http_client=httpx.Client(transport=transport))
    try:
        if adapter == 'embedding':
            provider = embeddings.OpenAIEmbeddingProvider(config, client=client)
            with pytest.raises(embeddings.EmbeddingError) as error:
                provider.embed(['Synthetic bounded query'])
            assert error.value.code == 'BUDGET_BLOCKED'
        elif adapter == 'nlu':
            monkeypatch.setattr(openai_provider, 'get_settings', lambda: config)
            monkeypatch.setattr(openai_provider, 'OpenAI', lambda **kw: client)
            with pytest.raises(openai_provider.ProviderError, match='Лимит'):
                openai_provider.OpenAILLMProvider().parse_command('Создай задачу проверить лимит', {})
        else:
            monkeypatch.setattr(speech_provider, 'OpenAI', lambda **kw: client)
            with pytest.raises(speech_provider.AudioError, match='Лимит'):
                speech_provider.OpenAISpeechProvider(config).speak('Проверка лимита')
        assert calls == []
    finally:
        client.close()
