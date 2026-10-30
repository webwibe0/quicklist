from threading import Event
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from app.ai.embeddings import EmbeddingProfile, FakeEmbeddingProvider
from app.core.config import Settings
from app.core.search_content import search_content
from app.repositories.indexing import retry_delay
from app.services.indexing import IndexingWorker
from app.services.indexing_target import IndexingTargetError, verify_indexing_target


def test_existing_versioned_canonicalization_preserves_meaning():
    assert search_content("  Не\tзвонить Семёну 42 ").canonical_text == "Не звонить Семёну 42"
    assert search_content("Йога") == search_content("И\u0306ога")
    assert search_content("все") != search_content("всё")
    assert search_content("Не звонить Ивану") != search_content("Звонить Ивану")
    assert search_content("Иван 1") != search_content("Иван 2")
    assert search_content("Иван").text_version == 1


def test_disabled_or_stopped_worker_never_connects_or_calls_provider():
    engine = Mock()
    provider = FakeEmbeddingProvider()
    worker = IndexingWorker(
        engine, provider, Settings(_env_file=None, embedding_worker_enabled=False)
    )
    assert worker.run_once()["disabled"] and provider.calls == []
    engine.connect.assert_not_called()
    stop = Event()
    stop.set()
    enabled = IndexingWorker(
        engine, provider, Settings(_env_file=None, embedding_worker_enabled=True)
    )
    assert enabled.run_once(stop=stop)["inputs"] == 0
    engine.connect.assert_not_called()


def test_profile_mismatch_and_unsafe_lease_rejected():
    with pytest.raises(ValueError, match="profiles"):
        IndexingWorker(
            Mock(),
            FakeEmbeddingProvider(profile=EmbeddingProfile(model="another")),
            Settings(_env_file=None),
        )
    with pytest.raises(ValueError, match="Lease"):
        IndexingWorker(
            Mock(), FakeEmbeddingProvider(), Settings(_env_file=None, embedding_lease_seconds=1)
        )


def test_backoff_is_positive_exponential_and_bounded():
    config = Settings(_env_file=None)
    assert retry_delay(1, config) == 2 and retry_delay(2, config) == 4
    assert retry_delay(100000, config) == config.embedding_retry_max_seconds


@pytest.mark.parametrize(
    "url,cluster",
    [
        ("postgresql+psycopg://assistant:unused@127.0.0.1:5432/assistant", "7699165401457610790"),
        (
            "postgresql+psycopg://assistant:unused@127.0.0.1:55434/assistant_test",
            "7698854534603636773",
        ),
        (
            "postgresql+psycopg://assistant:unused@127.0.0.1:55434/stage82_unknown_test",
            "7699165401457610790",
        ),
        (
            "postgresql+psycopg://assistant:unused@localhost:55434/stage82_unknown_test",
            "7698854534603636773",
        ),
        ("postgresql+psycopg://assistant:unused@127.0.0.1:55434/stage82_unknown_test", ""),
    ],
)
def test_primary_unknown_or_implicit_target_rejected_before_connection(url, cluster, monkeypatch):
    engine = create_engine(url)
    monkeypatch.setattr(
        engine, "connect", Mock(side_effect=AssertionError("Must not connect to unsafe target"))
    )
    try:
        with pytest.raises(IndexingTargetError):
            verify_indexing_target(engine, cluster)
    finally:
        engine.dispose()
