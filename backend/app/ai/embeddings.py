"""Embedding contracts; unrelated to NLU, event mutations or semantic retrieval."""

import hashlib
import math
import struct
from collections import deque
from dataclasses import dataclass
from numbers import Real
from typing import Protocol

from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)

from app.ai.paid_limits import client_options, is_budget_error
from app.core.spend_budget import BudgetBlocked


@dataclass(frozen=True)
class EmbeddingProfile:
    model: str = "text-embedding-3-large"
    dimensions: int = 1536
    text_version: int = 1

    def __post_init__(self):
        if not self.model or self.model.strip() != self.model or len(self.model) > 200:
            raise ValueError("Invalid embedding model")
        if self.dimensions != 1536 or self.text_version != 1:
            raise ValueError("Only schema 0006 / canonicalization v1 is supported")


class EmbeddingError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        if code not in {
            "INVALID_BATCH",
            "EMPTY_INPUT",
            "INPUT_TOO_LONG",
            "INVALID_VECTOR",
            "INVALID_RESPONSE",
            "PAID_API_DISABLED",
            "MISSING_API_KEY",
            "TIMEOUT",
            "RATE_LIMIT",
            "CONNECTION",
            "SERVER_ERROR",
            "API_REJECTED",
            "AUTHORIZATION",
            "PROVIDER_ERROR",
            "BUDGET_BLOCKED",
        }:
            raise ValueError("Unknown safe embedding error code")
        self.code, self.retryable = code, retryable
        super().__init__(code)  # Fixed code only: no SDK exception, body, text or key.


def validate_inputs(texts, *, max_bytes=8000, batch_size=64):
    if isinstance(texts, str):
        texts = [texts]
    if not isinstance(texts, (list, tuple)) or not 1 <= len(texts) <= batch_size:
        raise EmbeddingError("INVALID_BATCH")
    for value in texts:
        if not isinstance(value, str) or not value.strip():
            raise EmbeddingError("EMPTY_INPUT")
        if len(value.encode("utf-8")) > max_bytes:
            raise EmbeddingError("INPUT_TOO_LONG")
    return list(texts)


def validate_vector(vector, dimensions=1536):
    try:
        if isinstance(vector, (str, bytes)) or len(vector) != dimensions:
            raise ValueError()
        if any(isinstance(value, bool) or not isinstance(value, Real) for value in vector):
            raise ValueError()
        result = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in result):
            raise ValueError()
        # pgvector stores float32: reject overflow and all-zero after underflow before SQL.
        stored = [struct.unpack("f", struct.pack("f", value))[0] for value in result]
        if not all(math.isfinite(value) for value in stored) or not any(stored):
            raise ValueError()
        return result
    except (TypeError, ValueError, OverflowError, struct.error):
        raise EmbeddingError("INVALID_VECTOR") from None


def validate_batch(vectors, count, dimensions=1536):
    if not isinstance(vectors, (list, tuple)) or len(vectors) != count:
        raise EmbeddingError("INVALID_RESPONSE")
    return [validate_vector(vector, dimensions) for vector in vectors]


class EmbeddingProvider(Protocol):
    profile: EmbeddingProfile

    def embed(self, texts: str | list[str]) -> list[list[float]]: ...


class OpenAIEmbeddingProvider:
    def __init__(self, settings, profile=None, *, client=None, allow_paid_api=False, budget=None):
        self.budget = budget
        self.profile = profile or EmbeddingProfile(
            settings.embedding_model, settings.embedding_dimensions
        )
        self.settings = settings
        # Explicit SDK client injection is for offline mock transport tests. CLI never injects one.
        if client is None:
            if not (allow_paid_api and settings.embedding_real_api_enabled):
                raise EmbeddingError("PAID_API_DISABLED")
            if not settings.openai_api_key.get_secret_value():
                raise EmbeddingError("MISSING_API_KEY")
            try:
                client = OpenAI(
                    api_key=settings.openai_api_key.get_secret_value(),
                    max_retries=0,
                    timeout=settings.embedding_timeout_seconds,
                    base_url="https://api.openai.com/v1",
                    **client_options(settings, background=budget is not None),
                )
            except BudgetBlocked:
                # Ledger refusal during construction must use the existing lexical fallback too.
                raise EmbeddingError('BUDGET_BLOCKED') from None
        self.client = client

    def embed(self, texts):
        texts = validate_inputs(
            texts,
            max_bytes=self.settings.embedding_max_input_bytes,
            batch_size=self.settings.embedding_batch_size,
        )
        ticket = None
        if self.budget is not None:
            try:
                if self.profile != EmbeddingProfile('text-embedding-3-large',1536):
                    raise BudgetBlocked('UNPRICED_EMBEDDING_PROFILE')
                # v3 byte-BPE bound, plus per-item overhead; no tokenizer/network download.
                ticket = self.budget.reserve('embedding', sum(len(t.encode('utf-8')) + 32 for t in texts) * 130)
            except BudgetBlocked:
                raise EmbeddingError('BUDGET_BLOCKED') from None
        try:
            response = self.client.embeddings.create(
                model=self.profile.model,
                input=texts,
                dimensions=self.profile.dimensions,
                encoding_format="float",
                timeout=self.settings.embedding_timeout_seconds,
            )
            if ticket is not None:
                usage = response.usage.total_tokens
                if type(usage) is not int or usage < 0:
                    raise EmbeddingError('INVALID_RESPONSE')
                try:
                    self.budget.finish(ticket, usage * 130, input_tokens=usage)
                except BudgetBlocked:
                    raise EmbeddingError('BUDGET_BLOCKED') from None
            if response.model != self.profile.model or len(response.data) != len(texts):
                raise EmbeddingError("INVALID_RESPONSE")
            ordered = [None] * len(texts)
            for item in response.data:
                if (
                    type(item.index) is not int
                    or not 0 <= item.index < len(texts)
                    or ordered[item.index] is not None
                ):
                    raise EmbeddingError("INVALID_RESPONSE")
                ordered[item.index] = item.embedding
            return validate_batch(ordered, len(texts), self.profile.dimensions)
        except EmbeddingError:
            raise
        except BudgetBlocked:
            raise EmbeddingError('BUDGET_BLOCKED') from None
        except APITimeoutError:
            raise EmbeddingError("TIMEOUT", retryable=True) from None
        except RateLimitError:
            raise EmbeddingError("RATE_LIMIT", retryable=True) from None
        except APIConnectionError as error:
            if is_budget_error(error):
                raise EmbeddingError('BUDGET_BLOCKED') from None
            raise EmbeddingError("CONNECTION", retryable=True) from None
        except APIStatusError as error:
            code = "AUTHORIZATION" if error.status_code in {401, 403} else "API_REJECTED"
            raise EmbeddingError(
                "SERVER_ERROR" if error.status_code >= 500 else code,
                retryable=error.status_code >= 500 or error.status_code == 408,
            ) from None
        except (APIResponseValidationError, AttributeError, TypeError, ValueError):
            raise EmbeddingError("INVALID_RESPONSE") from None
        except OpenAIError:
            raise EmbeddingError("PROVIDER_ERROR") from None

    def close(self):
        self.client.close()


class FakeEmbeddingProvider:
    """Deterministic vectors/failures only; not evidence of semantic quality."""

    def __init__(self, vectors=None, *, profile=None, failures=(), on_call=None, raw_batches=()):
        self.profile = profile or EmbeddingProfile()
        self.vectors = vectors or {}
        self.failures, self.raw_batches = deque(failures), deque(raw_batches)
        self.on_call, self.calls = on_call, []

    def embed(self, texts):
        texts = validate_inputs(texts)
        self.calls.append(list(texts))
        if self.on_call:
            self.on_call(texts)
        if self.failures:
            error = self.failures.popleft()
            if error is not None:
                raise error
        if self.raw_batches:
            return self.raw_batches.popleft()  # Deliberately malformed provider response for QA.
        result = []
        for text in texts:
            if text in self.vectors:
                result.append(list(self.vectors[text]))
            else:
                position = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4]) % 1536
                vector = [0.0] * 1536
                vector[position] = 1.0
                result.append(vector)
        return result
