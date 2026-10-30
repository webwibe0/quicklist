import importlib
import json

import pytest
from openai import DefaultHttpxClient, OpenAI
from pydantic import SecretStr

from app.ai.embeddings import (
    EmbeddingError,
    EmbeddingProfile,
    FakeEmbeddingProvider,
    OpenAIEmbeddingProvider,
    validate_batch,
    validate_inputs,
    validate_vector,
)
from app.core.config import Settings


def unit(index=0):
    result = [0.0] * 1536
    result[index] = 1.0
    return result


def settings(**changes):
    return Settings(_env_file=None, **({"embedding_model": "text-embedding-3-small"} | changes))


def sdk_provider(handler):
    # SDK v2 uses httpx, installed SDK v3 uses httpx2; use the SDK's existing client dependency.
    module = next(
        base.__module__.split(".")[0]
        for base in DefaultHttpxClient.__mro__
        if base.__module__.split(".")[0] in {"httpx", "httpx2"}
    )
    http = importlib.import_module(module)
    client = OpenAI(
        api_key="offline-test-key",
        max_retries=0,
        _strict_response_validation=True,
        http_client=DefaultHttpxClient(
            transport=http.MockTransport(lambda request: handler(http, request))
        ),
    )
    return OpenAIEmbeddingProvider(settings(), client=client)


def response(data, model="text-embedding-3-small"):
    return dict(object="list", model=model, data=data, usage=dict(prompt_tokens=3, total_tokens=3))


def test_sdk_mock_transport_preserves_order_and_profile():
    calls = []

    def handler(http, request):
        body = json.loads(request.content)
        calls.append(body)
        return http.Response(
            200,
            json=response(
                [
                    dict(object="embedding", index=1, embedding=unit(1)),
                    dict(object="embedding", index=0, embedding=unit(0)),
                ]
            ),
        )

    provider = sdk_provider(handler)
    try:
        assert provider.embed(["первый", "второй"]) == [unit(0), unit(1)]
        assert calls == [
            dict(
                model="text-embedding-3-small",
                input=["первый", "второй"],
                dimensions=1536,
                encoding_format="float",
            )
        ]
    finally:
        provider.close()


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (429, "RATE_LIMIT", True),
        (500, "SERVER_ERROR", True),
        (503, "SERVER_ERROR", True),
        (408, "API_REJECTED", True),
        (401, "AUTHORIZATION", False),
        (403, "AUTHORIZATION", False),
        (400, "API_REJECTED", False),
    ],
)
def test_sdk_errors_are_classified_without_sdk_retry_or_secret_logs(
    status, code, retryable, caplog
):
    calls = []

    def handler(http, request):
        calls.append(1)
        return http.Response(
            status, json={"error": {"message": "SENSITIVE_BODY_DO_NOT_LOG", "type": "api_error"}}
        )

    provider = sdk_provider(handler)
    try:
        with pytest.raises(EmbeddingError) as caught:
            provider.embed("текст")
        assert str(caught.value) == code and caught.value.retryable == retryable
        assert len(calls) == 1
        assert (
            "SENSITIVE_BODY_DO_NOT_LOG" not in caplog.text and "offline-test-key" not in caplog.text
        )
    finally:
        provider.close()


@pytest.mark.parametrize("kind,code", [("timeout", "TIMEOUT"), ("connection", "CONNECTION")])
def test_sdk_transport_failure(kind, code):
    def handler(http, request):
        error = http.ReadTimeout if kind == "timeout" else http.ConnectError
        raise error("SENSITIVE transport details", request=request)

    provider = sdk_provider(handler)
    try:
        with pytest.raises(EmbeddingError, match=code) as caught:
            provider.embed("текст")
        assert caught.value.retryable
    finally:
        provider.close()


@pytest.mark.parametrize(
    "data,model",
    [
        ([], "text-embedding-3-small"),
        ([dict(object="embedding", index=0, embedding=unit())], "another-model"),
        ([dict(object="embedding", index=3, embedding=unit())], "text-embedding-3-small"),
        ([dict(object="embedding", index=0, embedding=[1.0])], "text-embedding-3-small"),
        ([dict(object="embedding", embedding=unit())], "text-embedding-3-small"),
    ],
)
def test_malformed_sdk_responses(data, model):
    provider = sdk_provider(lambda http, request: http.Response(200, json=response(data, model)))
    try:
        with pytest.raises(EmbeddingError):
            provider.embed("текст")
    finally:
        provider.close()


def test_duplicate_indices_and_partial_batch_rejected():
    provider = sdk_provider(
        lambda http, request: http.Response(
            200,
            json=response(
                [
                    dict(object="embedding", index=0, embedding=unit()),
                    dict(object="embedding", index=0, embedding=unit(1)),
                ]
            ),
        )
    )
    try:
        with pytest.raises(EmbeddingError, match="INVALID_RESPONSE"):
            provider.embed(["один", "два"])
    finally:
        provider.close()


def test_broken_json_is_safe_failure():
    provider = sdk_provider(
        lambda http, request: http.Response(
            200, content=b"{broken", headers={"content-type": "application/json"}
        )
    )
    try:
        with pytest.raises(EmbeddingError, match="INVALID_RESPONSE"):
            provider.embed("текст")
    finally:
        provider.close()


@pytest.mark.parametrize(
    "vector",
    [
        [],
        [1.0],
        [1.0] * 1535,
        [1.0] * 1537,
        [0.0] * 1536,
        [1e-100] * 1536,
        [1e100] * 1536,
        [float("nan")] * 1536,
        [float("inf")] * 1536,
        [float("-inf")] * 1536,
        ["1"] * 1536,
        [True] * 1536,
    ],
)
def test_invalid_vectors_rejected_before_pgvector(vector):
    with pytest.raises(EmbeddingError, match="INVALID_VECTOR"):
        validate_vector(vector)


@pytest.mark.parametrize("texts", ["", " \t", [], ["ok", ""], ["ok"] * 65])
def test_invalid_input_and_batch(texts):
    with pytest.raises(EmbeddingError):
        validate_inputs(texts)


def test_utf8_limit_is_hard_and_batch_validation_is_atomic():
    assert validate_inputs("я" * 4000) == ["я" * 4000]
    with pytest.raises(EmbeddingError, match="INPUT_TOO_LONG"):
        validate_inputs("я" * 4001)
    with pytest.raises(EmbeddingError):
        validate_batch([unit(), [float("nan")] * 1536], 2)
    assert validate_vector(unit()) == unit()


def test_live_provider_requires_both_opt_ins_and_key():
    with pytest.raises(EmbeddingError, match="PAID_API_DISABLED"):
        OpenAIEmbeddingProvider(settings(openai_api_key=SecretStr("unused")))
    with pytest.raises(EmbeddingError, match="PAID_API_DISABLED"):
        OpenAIEmbeddingProvider(settings(embedding_real_api_enabled=True))
    with pytest.raises(EmbeddingError, match="PAID_API_DISABLED"):
        OpenAIEmbeddingProvider(settings(), allow_paid_api=True)
    with pytest.raises(EmbeddingError, match="MISSING_API_KEY"):
        OpenAIEmbeddingProvider(settings(embedding_real_api_enabled=True), allow_paid_api=True)


def test_fake_deterministic_controlled_batch_and_fault_injection():
    provider = FakeEmbeddingProvider(
        {"первый": unit(0), "второй": unit(1)},
        failures=[EmbeddingError("TIMEOUT", retryable=True), None],
    )
    with pytest.raises(EmbeddingError, match="TIMEOUT"):
        provider.embed(["второй", "первый"])
    assert provider.embed(["второй", "первый"]) == [unit(1), unit(0)]
    assert provider.embed("первый") == provider.embed("первый")
    assert FakeEmbeddingProvider().embed("стабильный") == FakeEmbeddingProvider().embed(
        "стабильный"
    )


@pytest.mark.parametrize(
    "changes", [{"dimensions": 3}, {"dimensions": 3072}, {"text_version": 2}, {"model": ""}]
)
def test_profile_fails_closed_for_unknown_schema_or_canonicalization(changes):
    with pytest.raises(ValueError):
        EmbeddingProfile(**changes)
