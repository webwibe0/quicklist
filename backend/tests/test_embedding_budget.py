from unittest.mock import Mock

import pytest

from app.ai.embeddings import EmbeddingError, EmbeddingProfile, OpenAIEmbeddingProvider
from app.core.config import Settings
from app.core.spend_budget import SpendBudget


def test_budget_blocks_before_any_paid_sdk_call(tmp_path):
    budget = SpendBudget(tmp_path / "budget.sqlite3", limit_nanos=1, max_calls=10)
    client = Mock()
    provider = OpenAIEmbeddingProvider(Settings(_env_file=None), client=client, budget=budget)
    with pytest.raises(EmbeddingError, match="BUDGET_BLOCKED"):
        provider.embed(["новая задача"])
    client.embeddings.create.assert_not_called()


def test_unknown_model_is_not_charged_at_large_price(tmp_path):
    budget = SpendBudget(tmp_path / "budget.sqlite3", limit_nanos=1000000, max_calls=10)
    client = Mock()
    provider = OpenAIEmbeddingProvider(
        Settings(_env_file=None),
        profile=EmbeddingProfile("different-model"),
        client=client,
        budget=budget,
    )
    with pytest.raises(EmbeddingError, match="BUDGET_BLOCKED"):
        provider.embed(["hello"])
    client.embeddings.create.assert_not_called()


def test_provider_timeout_keeps_full_reservation_across_restart(tmp_path):
    import httpx
    from openai import APITimeoutError

    path = tmp_path / "budget.sqlite3"
    budget = SpendBudget(path, limit_nanos=100000, max_calls=10)
    client = Mock()
    client.embeddings.create.side_effect = APITimeoutError(
        request=httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    )
    provider = OpenAIEmbeddingProvider(Settings(_env_file=None), client=client, budget=budget)
    with pytest.raises(EmbeddingError, match="TIMEOUT"):
        provider.embed(["hello"])
    assert (
        SpendBudget(path, limit_nanos=100000, max_calls=10).summary()["committed_nanos"] == 37 * 130
    )
