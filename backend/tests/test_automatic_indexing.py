import time
from types import SimpleNamespace
from unittest.mock import Mock

from sqlalchemy import func, select

from app.ai.embeddings import FakeEmbeddingProvider, OpenAIEmbeddingProvider
from app.core.config import Settings
from app.models.entities import Event
from app.models.search import EmbeddingCache, EventSearchDocument
from app.services import automatic_indexing as automatic


def until(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("Automatic indexing did not reach expected durable state")


def test_disabled_lifecycle_has_no_connection_or_provider():
    engine = Mock()
    worker = automatic.AutomaticIndexing(engine, Settings(_env_file=None))
    worker.start()
    worker.stop()
    engine.connect.assert_not_called()


def test_lifecycle_indexes_changes_reuses_cache_and_removes_deleted(
    db, integration_engine, tmp_path, monkeypatch
):
    import os

    settings = Settings(
        _env_file=None,
        embedding_worker_enabled=True,
        embedding_real_api_enabled=True,
        embedding_allowed_cluster_id=os.environ["EMBEDDING_ALLOWED_CLUSTER_ID"],
        embedding_budget_usd=0.01,
        embedding_budget_path=tmp_path / "worker.sqlite3",
        embedding_min_call_interval_seconds=0,
        embedding_poll_seconds=1,
    )
    fake = FakeEmbeddingProvider()

    def create(**kw):
        vectors = fake.embed(kw["input"])
        return SimpleNamespace(
            model="text-embedding-3-large",
            data=[SimpleNamespace(index=i, embedding=v) for i, v in enumerate(vectors)],
            usage=SimpleNamespace(total_tokens=1),
        )

    client = SimpleNamespace(embeddings=SimpleNamespace(create=create), close=lambda: None)
    monkeypatch.setattr(
        automatic,
        "OpenAIEmbeddingProvider",
        lambda config, **kw: OpenAIEmbeddingProvider(config, client=client, **kw),
    )
    item = Event(type="task", title="Automatic alpha", status="pending", source="user")
    db.add(item)
    db.commit()
    identifier = item.id
    worker = automatic.AutomaticIndexing(integration_engine, settings)

    def ready(title):
        db.expire_all()
        return (
            db.scalar(
                select(func.count())
                .select_from(EventSearchDocument)
                .join(
                    EmbeddingCache, EmbeddingCache.content_hash == EventSearchDocument.content_hash
                )
                .where(
                    EventSearchDocument.event_id == identifier,
                    EventSearchDocument.source_title == title,
                    EmbeddingCache.status == "ready",
                )
            )
            == 1
        )

    worker.start()
    try:
        until(lambda: ready("Automatic alpha"))
        item = db.get(Event, identifier)
        item.title = "Automatic beta"
        db.commit()
        until(lambda: ready("Automatic beta"))
        assert len(fake.calls) == 2
        db.add(Event(type="task", title="Automatic beta", status="pending", source="user"))
        db.commit()
        until(lambda: db.scalar(select(func.count()).select_from(EventSearchDocument)) == 2)
        assert len(fake.calls) == 2
        item.status = "completed"
        db.commit()
        time.sleep(1.2)
        assert len(fake.calls) == 2
        item.status = "deleted"
        db.commit()
        until(
            lambda: (
                db.scalar(
                    select(EventSearchDocument.event_id).where(
                        EventSearchDocument.event_id == identifier
                    )
                )
                is None
            )
        )
    finally:
        worker.stop()
    assert not worker.thread.is_alive()
