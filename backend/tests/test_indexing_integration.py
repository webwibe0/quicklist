import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from threading import Barrier
from threading import Event as StopEvent
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event as sql_events
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.ai.embeddings import EmbeddingError, EmbeddingProfile, FakeEmbeddingProvider
from app.core.config import Settings
from app.core.search_content import SearchContent, search_content
from app.models.entities import Event
from app.models.search import EmbeddingCache, EventSearchDocument
from app.repositories.events import EventRepository
from app.repositories.indexing import IndexingRepository
from app.schemas.commands import SearchEventsCommand
from app.services.indexing import IndexingWorker
from app.services.indexing_target import IndexingTargetError, verify_indexing_target
from tests.test_embedding_provider import unit

OWNER = UUID("20000000-0000-4000-8000-000000000001")

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def config(**changes):
    return Settings(
        _env_file=None,
        embedding_worker_enabled=True,
        embedding_allowed_cluster_id=os.environ["EMBEDDING_ALLOWED_CLUSTER_ID"],
        **changes,
    )


@pytest.fixture
def store(integration_engine):
    verify_indexing_target(integration_engine, os.environ["EMBEDDING_ALLOWED_CLUSTER_ID"])
    with integration_engine.begin() as connection:
        connection.exec_driver_sql(
            "TRUNCATE conversations, google_event_links, event_search_documents, events, embedding_cache"
        )
    return sessionmaker(integration_engine, expire_on_commit=False)


def add(store, *titles, **changes):
    ids = []
    with store.begin() as session:
        for title in titles:
            event = Event(type="task", title=title, **changes)
            session.add(event)
            session.flush()
            ids.append(event.id)
    return ids


def repo(engine, settings=None, **kwargs):
    settings = settings or config()
    return IndexingRepository(
        engine, EmbeddingProfile(settings.embedding_model), settings, **kwargs
    )


def rows(store):
    with store() as session:
        return list(session.scalars(select(EmbeddingCache).order_by(EmbeddingCache.id)))


class Clock:
    def __init__(self):
        self.value = datetime(2027, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def test_e2e_ready_cache_idempotency_rename_soft_delete(store, integration_engine):
    event_id = add(store, "Позвонить Семёну")[0]
    provider = FakeEmbeddingProvider({"Позвонить Семёну": unit(0), "Не звонить Семёну": unit(1)})
    worker = IndexingWorker(integration_engine, provider, config())
    assert worker.run_once()["completed"] == 1
    assert repo(integration_engine).ready_for_event(event_id, owner_id=OWNER) == unit(0)
    assert worker.run_once()["provider_calls"] == 0 and len(provider.calls) == 1
    with store.begin() as session:
        session.execute(update(Event).where(Event.id == event_id).values(title="Не звонить Семёну"))
    assert repo(integration_engine).ready_for_event(event_id, owner_id=OWNER) is None
    assert worker.run_once()["completed"] == 1
    assert repo(integration_engine).ready_for_event(event_id, owner_id=OWNER) == unit(1)
    with store.begin() as session:
        session.execute(update(Event).where(Event.id == event_id).values(status="deleted"))
    assert repo(integration_engine).ready_for_event(event_id, owner_id=OWNER) is None
    assert worker.run_once()["provider_calls"] == 0
    with store() as session:
        assert not list(session.scalars(select(EventSearchDocument)))
        assert session.scalar(select(func.count()).select_from(EmbeddingCache)) == 2


def test_recurrence_and_identical_independent_events_share_one_vector(store, integration_engine):
    series = uuid4()
    ids = add(
        store,
        *(["Проверить счёт 42"] * 5),
        recurrence_series_id=series,
        recurrence_rule="FREQ=DAILY",
        event_date=date(2027, 1, 1),
    )
    ids += add(store, "Проверить счёт 42", event_date=date(2027, 3, 3))
    provider = FakeEmbeddingProvider()
    stats = IndexingWorker(integration_engine, provider, config()).run_once()
    assert stats["inputs"] == 1 and len(rows(store)) == 1
    with store.begin() as session:
        session.execute(update(Event).where(Event.id == ids[0]).values(title="Проверить счёт 43"))
    IndexingWorker(integration_engine, provider, config()).run_once()
    assert len(rows(store)) == 2
    with store.begin() as session:
        session.execute(
            update(Event).where(Event.recurrence_series_id == series).values(status="deleted")
        )
    IndexingWorker(integration_engine, provider, config()).run_once()
    assert repo(integration_engine).ready_for_event(ids[-1], owner_id=OWNER) is not None


def test_date_time_or_non_deleted_status_change_does_not_recompute(store, integration_engine):
    event_id = add(store, "Заплатить за хостинг")[0]
    provider = FakeEmbeddingProvider()
    worker = IndexingWorker(integration_engine, provider, config())
    worker.run_once()
    with store.begin() as session:
        session.execute(
            update(Event)
            .where(Event.id == event_id)
            .values(event_date=date(2028, 2, 3), status="completed")
        )
    assert worker.run_once()["provider_calls"] == 0
    assert (
        len(provider.calls) == 1 and repo(integration_engine).ready_for_event(event_id, owner_id=OWNER) is not None
    )


def test_mass_recurrence_keyset_dedup_never_changes_event_rows(store, integration_engine):
    series = uuid4()
    with store.begin() as session:
        session.add_all(
            [
                Event(
                    type="reminder",
                    title="Проверить счёт 42",
                    recurrence_series_id=series,
                    recurrence_rule="FREQ=DAILY",
                    event_date=date(2027, 1, 1) + timedelta(days=index),
                    event_time=time(9, 0),
                )
                for index in range(200)
            ]
        )
    snapshot_sql = text("SELECT to_jsonb(e)::text FROM events e ORDER BY id")
    with store() as session:
        before = list(session.connection().scalars(snapshot_sql))

    class NoWait(StopEvent):
        def wait(self, timeout=None):
            return False

    provider = FakeEmbeddingProvider()
    worker = IndexingWorker(integration_engine, provider, config(embedding_max_documents=32))
    results = list(worker.run_periodic(NoWait(), cycles=7))
    assert all(item["scanned"] <= 32 for item in results)
    assert sum(item["inputs"] for item in results) == 1 and len(provider.calls) == 1
    with store() as session:
        assert list(session.connection().scalars(snapshot_sql)) == before
        assert session.scalar(select(func.count()).select_from(EventSearchDocument)) == 200
        assert session.scalar(select(func.count()).select_from(EmbeddingCache)) == 1


def test_imported_rows_discovered_without_endpoint_signal(store, integration_engine, tmp_path):
    from scripts.import_dataset import import_dataset

    path = tmp_path / "synthetic.csv"
    path.write_text(
        "id,date,time,text\nqa-1,2027-02-01,09:00:00,Проверить импорт\n", encoding="utf-8"
    )
    with store.begin() as session:
        assert import_dataset(session, path)["inserted"] == 1
    provider = FakeEmbeddingProvider()
    assert IndexingWorker(integration_engine, provider, config()).run_once()["completed"] == 1
    with store() as session:
        event = session.scalar(select(Event))
        assert (
            event.source == "dataset"
            and repo(integration_engine).ready_for_event(event.id, owner_id=OWNER) is not None
        )


def test_no_transaction_or_row_locks_during_provider_io(store, integration_engine):
    event_id = add(store, "Нет блокировки сети")[0]
    checks = []

    def network_boundary(texts):
        with integration_engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE pid<>pg_backend_pid() "
                        "AND datname=current_database() AND state='idle in transaction'"
                    )
                )
                == 0
            )
            # Both Event and leased cache rows are unlocked while the provider is running.
            connection.execute(select(EmbeddingCache).with_for_update(nowait=True)).all()
            connection.execute(
                update(Event).where(Event.id == event_id).values(event_date=date(2029, 1, 1))
            )
            checks.append(True)

    provider = FakeEmbeddingProvider(on_call=network_boundary)
    assert IndexingWorker(integration_engine, provider, config()).run_once()["completed"] == 1
    assert checks == [True]


@pytest.mark.parametrize("change", ["rename", "soft_delete", "hard_delete"])
def test_event_changes_during_indexing_never_publish_stale_result(
    store, integration_engine, change
):
    event_id = add(store, "Исходный текст")[0]

    def during_api(texts):
        with store.begin() as session:
            event = session.get(Event, event_id)
            if change == "rename":
                event.title = "Новый текст"
            elif change == "soft_delete":
                event.status = "deleted"
            else:
                session.delete(event)

    provider = FakeEmbeddingProvider(on_call=during_api)
    IndexingWorker(integration_engine, provider, config()).run_once()
    assert repo(integration_engine).ready_for_event(event_id, owner_id=OWNER) is None
    with store() as session:
        assert session.get(EventSearchDocument, event_id) is None
    if change == "rename":
        provider.on_call = None
        assert IndexingWorker(integration_engine, provider, config()).run_once()["completed"] == 1
        assert repo(integration_engine).ready_for_event(event_id, owner_id=OWNER) is not None


def test_concurrent_reconciliation_and_claims_deduplicate(store, integration_engine):
    add(store, "Одинаковое содержание", "Одинаковое содержание")
    barrier = Barrier(2)

    def reconcile():
        repository = repo(integration_engine)
        barrier.wait(timeout=5)
        return repository.reconcile(100)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: reconcile(), range(2)))
    assert sum(item["cache_created"] for item in results) == 1 and len(rows(store)) == 1

    def claim():
        repository = repo(integration_engine)
        return repository.claim(16, 64000)[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(lambda _: claim(), range(2)))
    assert sum(map(len, claimed)) == 1


def test_expired_lease_restart_and_stale_token(store, integration_engine):
    add(store, "Пережить перезапуск")
    clock = Clock()
    repository = repo(integration_engine, clock=clock)
    repository.reconcile(10)
    first = repository.claim(10, 64000)[0]
    assert len(first) == 1
    clock.advance(61)
    assert repository.claim(10, 64000)[2] == 1
    assert repository.finish(first, vectors=[unit()])["stale_claims"] == 1
    clock.advance(3)
    second = repo(integration_engine, clock=clock).claim(10, 64000)[0]
    assert second[0].token != first[0].token
    assert repository.finish(first, vectors=[unit(2)])["stale_claims"] == 1
    assert repository.finish(second, vectors=[unit(1)])["completed"] == 1
    assert rows(store)[0].attempts == 2


def test_lease_expiring_while_waiting_for_row_lock_cannot_publish(store, integration_engine):
    add(store, "Lease истёк при ожидании блокировки")
    clock = Clock()
    repository = repo(integration_engine, clock=clock)
    repository.reconcile(10)
    claims = repository.claim(10, 64000)[0]
    lock_queries = []

    def delay_lock_result(connection, cursor, statement, parameters, context, executemany):
        if "embedding_cache" in statement and "FOR UPDATE" in statement:
            # Deterministically model a lock wait longer than the remaining lease.
            clock.advance(61)
            lock_queries.append(True)

    sql_events.listen(integration_engine, "before_cursor_execute", delay_lock_result)
    try:
        result = repository.finish(claims, vectors=[unit()])
    finally:
        sql_events.remove(integration_engine, "before_cursor_execute", delay_lock_result)
    assert lock_queries == [True]
    assert result == dict(completed=0, retried=0, failed=0, stale_claims=1)
    assert rows(store)[0].status == "processing" and rows(store)[0].embedding is None


def test_retry_backoff_no_hot_loop_and_later_success(store, integration_engine):
    add(store, "Повторить позднее")
    clock = Clock()
    provider = FakeEmbeddingProvider(failures=[EmbeddingError("TIMEOUT", retryable=True), None])
    worker = IndexingWorker(integration_engine, provider, config(), clock=clock)
    assert worker.run_once()["retried"] == 1
    assert worker.run_once()["provider_calls"] == 0
    clock.advance(3)
    assert worker.run_once()["completed"] == 1 and len(provider.calls) == 2


def test_attempt_limit_and_permanent_failure(store, integration_engine):
    add(store, "Лимит расходов")
    clock = Clock()
    provider = FakeEmbeddingProvider(failures=[EmbeddingError("RATE_LIMIT", retryable=True)] * 3)
    worker = IndexingWorker(integration_engine, provider, config(), clock=clock)
    for _ in range(3):
        worker.run_once()
        clock.advance(400)
    assert rows(store)[0].status == "error" and rows(store)[0].attempts == 3
    assert worker.run_once()["provider_calls"] == 0 and len(provider.calls) == 3


@pytest.mark.parametrize(
    "vectors", [[], [unit()], [unit(), [1.0]], [unit(), [float("nan")] * 1536]]
)
def test_bad_partial_batch_is_never_partially_ready(store, integration_engine, vectors):
    add(store, "Первый", "Второй")
    provider = FakeEmbeddingProvider(raw_batches=[vectors])
    stats = IndexingWorker(integration_engine, provider, config()).run_once()
    assert stats["failed"] == 2 and all(
        row.status == "error" and row.embedding is None for row in rows(store)
    )


def test_crash_leaves_lease_then_restart_recovers(store, integration_engine):
    add(store, "Незавершённая обработка")
    clock = Clock()
    first = FakeEmbeddingProvider(failures=[KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        IndexingWorker(integration_engine, first, config(), clock=clock).run_once()
    assert rows(store)[0].status == "processing"
    clock.advance(61)
    second = FakeEmbeddingProvider()
    worker = IndexingWorker(integration_engine, second, config(), clock=clock)
    assert worker.run_once()["recovered_leases"] == 1 and not second.calls
    clock.advance(3)
    assert worker.run_once()["completed"] == 1


@pytest.mark.parametrize("failure_status", ["processing", "ready"])
def test_real_database_failure_rolls_back_claim_or_publication(
    store, integration_engine, failure_status
):
    add(store, "Атомарность 1", "Атомарность 2")
    with integration_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE FUNCTION stage82_fail_update() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF NEW.status='"
            + failure_status
            + "' THEN RAISE EXCEPTION 'SYNTHETIC_DB_FAULT'; END IF; RETURN NEW; END $$"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER stage82_fail BEFORE UPDATE ON embedding_cache "
            "FOR EACH ROW EXECUTE FUNCTION stage82_fail_update()"
        )
    provider = FakeEmbeddingProvider()
    clock = Clock()
    worker = IndexingWorker(integration_engine, provider, config(), clock=clock)
    try:
        with pytest.raises(DBAPIError):
            worker.run_once()
        assert all(
            row.status == ("pending" if failure_status == "processing" else "processing")
            for row in rows(store)
        )
        assert len(provider.calls) == (0 if failure_status == "processing" else 1)
    finally:
        with integration_engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER stage82_fail ON embedding_cache")
            connection.exec_driver_sql("DROP FUNCTION stage82_fail_update()")
    if failure_status == "ready":
        clock.advance(61)
        assert worker.run_once()["recovered_leases"] == 2
        clock.advance(3)
    assert worker.run_once()["completed"] == 2
    assert all(row.status == "ready" for row in rows(store))


def test_content_validation_rejects_poisoned_projection_and_hash_only_reuse(
    store, integration_engine
):
    ids = add(store, "Семён 42", "Все 43")
    provider = FakeEmbeddingProvider()
    worker = IndexingWorker(integration_engine, provider, config())
    worker.run_once()
    with store.begin() as session:
        doc = session.get(EventSearchDocument, ids[0])
        doc.content_hash = search_content("Все 43").content_hash
    assert repo(integration_engine).ready_for_event(ids[0], owner_id=OWNER) is None
    assert worker.run_once()["provider_calls"] == 0
    assert repo(integration_engine).ready_for_event(ids[0], owner_id=OWNER) is not None
    cached = rows(store)[0]
    with pytest.raises(ValueError, match="canonical content"):
        cached.assert_content(SearchContent("другой текст", cached.content_hash))


def test_different_models_and_versions_are_not_mixed(store, integration_engine):
    event_id = add(store, "Разные профили")[0]
    first = FakeEmbeddingProvider({"Разные профили": unit(0)})
    IndexingWorker(integration_engine, first, config()).run_once()
    content = search_content("Разные профили")
    with store.begin() as session:
        session.add(
            EmbeddingCache(
                id=uuid4(),
                canonical_text=content.canonical_text,
                content_hash=content.content_hash,
                text_version=2,
                model=first.profile.model,
                dimensions=1536,
                embedding=unit(2),
                status="ready",
            )
        )
    other = config(embedding_model="another-model")
    second = FakeEmbeddingProvider(
        {"Разные профили": unit(1)}, profile=EmbeddingProfile("another-model")
    )
    IndexingWorker(integration_engine, second, other).run_once()
    assert repo(integration_engine).ready_for_event(event_id, owner_id=OWNER) == unit(0)
    assert repo(integration_engine, other).ready_for_event(event_id, owner_id=OWNER) == unit(1)
    assert len(rows(store)) == 3


def test_expired_lease_at_attempt_limit_never_calls_provider(store, integration_engine):
    add(store, "Лимит зависшего запроса")
    clock = Clock()
    settings = config(embedding_max_attempts=1)
    repository = repo(integration_engine, settings, clock=clock)
    repository.reconcile(10)
    claims = repository.claim(10, 64000)[0]
    assert len(claims) == 1
    clock.advance(61)
    provider = FakeEmbeddingProvider()
    stats = IndexingWorker(integration_engine, provider, settings, clock=clock).run_once()
    assert stats["recovered_leases"] == 1 and provider.calls == []
    row = rows(store)[0]
    assert row.status == "error" and row.last_error_code == "LEASE_EXPIRED"
    assert row.attempts == 1 and row.lease_token is None and row.lease_until is None
    assert repository.finish(claims, vectors=[unit()])["stale_claims"] == 1


def test_bounded_batches_and_hard_byte_budget(store, integration_engine):
    add(store, "аа", "бб", "вв", "гг", "дд")
    provider = FakeEmbeddingProvider()
    stats = IndexingWorker(
        integration_engine, provider, config(embedding_batch_size=2, embedding_max_run_bytes=9)
    ).run_once()
    assert stats["input_bytes"] <= 9 and stats["inputs"] <= 2
    assert all(len(batch) <= 2 for batch in provider.calls)
    assert sum(len(value.encode()) for batch in provider.calls for value in batch) <= 9
    assert all(row.status != "processing" for row in rows(store))


def test_oversized_input_is_permanent_without_provider_call(store, integration_engine):
    add(store, "я" * 4001)
    provider = FakeEmbeddingProvider()
    assert IndexingWorker(integration_engine, provider, config()).run_once()["provider_calls"] == 0
    assert rows(store)[0].last_error_code == "INPUT_TOO_LONG"


def test_periodic_cursor_progress_and_controlled_stop(store, integration_engine):
    add(store, "Документ 1", "Документ 2", "Документ 3")

    class NoWait(StopEvent):
        def wait(self, timeout=None):
            return False

    provider = FakeEmbeddingProvider()
    worker = IndexingWorker(integration_engine, provider, config(embedding_max_documents=1))
    results = list(worker.run_periodic(NoWait(), cycles=5))
    assert sum(item["inputs"] for item in results) == 3 and len(rows(store)) == 3
    stop = StopEvent()
    stop.set()
    assert list(worker.run_periodic(stop, cycles=5)) == []


def test_existing_lexical_search_is_immediate_and_survives_provider_error(
    store, integration_engine
):
    event_id = add(store, "Проверить Sentinel")[0]
    with store() as session:
        assert (
            EventRepository(session, 10).search(SearchEventsCommand(query="Sentinel"))[0].id
            == event_id
        )
    provider = FakeEmbeddingProvider(failures=[EmbeddingError("AUTHORIZATION")])
    assert IndexingWorker(integration_engine, provider, config()).run_once()["failed"] == 1
    with store() as session:
        assert (
            EventRepository(session, 10).search(SearchEventsCommand(query="Sentinel"))[0].id
            == event_id
        )


def test_target_cluster_and_marker_are_real_not_just_url_suffix(store, integration_engine):
    with pytest.raises(IndexingTargetError):
        verify_indexing_target(integration_engine, "123456789")
    cluster = os.environ["EMBEDDING_ALLOWED_CLUSTER_ID"]
    verify_indexing_target(integration_engine, cluster)
    # Mutate only this confirmed disposable DB marker, then restore it in finally.
    with integration_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        database = connection.scalar(text("SELECT current_database()"))
        assert database == integration_engine.url.database
        marker_sql = text(
            "SELECT shobj_description(oid,'pg_database') FROM pg_database "
            "WHERE datname=current_database()"
        )
        assert connection.scalar(marker_sql) == "QUICKLIST_STAGE82_DISPOSABLE"
        quoted_database = connection.dialect.identifier_preparer.quote(database)
        try:
            connection.exec_driver_sql(
                f"COMMENT ON DATABASE {quoted_database} IS 'WRONG_MARKER'"
            )
            assert connection.scalar(marker_sql) == "WRONG_MARKER"
            with pytest.raises(IndexingTargetError):
                verify_indexing_target(integration_engine, cluster)
        finally:
            connection.exec_driver_sql(
                f"COMMENT ON DATABASE {quoted_database} IS 'QUICKLIST_STAGE82_DISPOSABLE'"
            )
        assert connection.scalar(marker_sql) == "QUICKLIST_STAGE82_DISPOSABLE"
    verify_indexing_target(integration_engine, cluster)


def test_real_worker_cli_preview_and_process_restart_idempotency(store, integration_engine):
    add(store, "CLI устойчивость")
    env = os.environ.copy()
    env.update(
        EMBEDDING_WORKER_ENABLED="true", EMBEDDING_DATABASE_URL=os.environ["TEST_DATABASE_URL"]
    )
    command = [sys.executable, "-B", str(ROOT / "scripts/index_events.py")]
    preview = subprocess.run(
        command, cwd=ROOT / "backend", env=env, capture_output=True, check=False
    )
    assert preview.returncode == 0 and json.loads(preview.stdout)["mutations"] == 0
    assert rows(store) == []
    runs = []
    for _ in range(2):
        completed = subprocess.run(
            command + ["--fake", "--execute", "--ack-disposable"],
            cwd=ROOT / "backend",
            env=env,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout.decode(errors="replace")
        runs.append(json.loads(completed.stdout))
    assert runs[0]["provider_calls"] == 1 and runs[1]["provider_calls"] == 0


def test_cli_defaults_and_real_api_optin_fail_closed(store):
    env = os.environ.copy()
    env.pop("EMBEDDING_DATABASE_URL", None)
    command = [sys.executable, "-B", str(ROOT / "scripts/index_events.py")]
    result = subprocess.run(
        command, cwd=ROOT / "backend", env=env, capture_output=True, check=False
    )
    assert result.returncode == 2
    env.update(
        EMBEDDING_DATABASE_URL=os.environ["TEST_DATABASE_URL"], EMBEDDING_WORKER_ENABLED="true"
    )
    result = subprocess.run(
        command + ["--execute", "--fake", "--ack-disposable", "--allow-paid-api"],
        cwd=ROOT / "backend",
        env=env,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2 and rows(store) == []
