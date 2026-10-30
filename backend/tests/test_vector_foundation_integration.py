"""Real pgvector operations with synthetic data and FakeLLM only."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from app.core.search_content import search_content
from app.models.entities import Event
from app.models.search import EmbeddingCache, EventSearchDocument

pytestmark = pytest.mark.integration


def test_legacy_runtime_does_not_query_optional_search_tables(db, integration_engine):
    from sqlalchemy import event as sql_event

    from tests.fakes import FakeLLMProvider
    from tests.test_integration import client_for

    statements = []

    def record(connection, cursor, statement, parameters, context, many):
        statements.append(statement.lower())

    provider = FakeLLMProvider({
        "Создай задачу проверить основу": dict(intent='CREATE_EVENT', event_type='TASK', title='проверить основу'),
        "Покажи планы на завтра": dict(intent='LIST_EVENTS', period_expression='завтра'),
        "Найди основу": dict(intent='SEARCH_EVENTS', search_query='основу'),
    })
    sql_event.listen(integration_engine, 'before_cursor_execute', record)
    try:
        with client_for(integration_engine, provider) as client:
            for message in provider.commands:
                response = client.post('/api/v1/assistant/messages', json={'message': message})
                assert response.status_code == 200
                assert not response.json()['requires_input']
    finally:
        sql_event.remove(integration_engine, 'before_cursor_execute', record)
    assert statements
    assert not any('event_search_documents' in sql or 'embedding_cache' in sql for sql in statements)


def cache(title="Тест", **changes):
    content = search_content(title)
    return EmbeddingCache(content_hash=content.content_hash, canonical_text=content.canonical_text,
                          model="text-embedding-3-small", text_version=1, **changes)


def document(event):
    content = search_content(event.title)
    return EventSearchDocument(event_id=event.id, source_title=event.title,
                               content_hash=content.content_hash, text_version=1)


def test_extension_schema_and_fk_are_real(db):
    assert db.connection().scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
    inspector = inspect(db.connection())
    assert {'event_search_documents', 'embedding_cache'} <= set(inspector.get_table_names())
    fk = inspector.get_foreign_keys('event_search_documents')[0]
    assert fk['referred_table'] == 'events' and fk['options']['ondelete'] == 'CASCADE'
    assert not any('hnsw' in row.indexdef or 'ivfflat' in row.indexdef for row in
                   db.connection().execute(text("SELECT indexdef FROM pg_indexes WHERE schemaname='public'")))


def test_exact_cosine_scan_through_sqlalchemy(db):
    a, b = [1.0] + [0.0] * 1535, [0.0, 1.0] + [0.0] * 1534
    first, other = cache("Хостинг", status='ready', embedding=a), cache("Обед", status='ready', embedding=b)
    db.add_all([other, first])
    db.flush()
    rows = db.execute(select(EmbeddingCache.id, EmbeddingCache.embedding.cosine_distance(a).label('distance'))
                      .order_by(EmbeddingCache.embedding.cosine_distance(a))).all()
    assert rows[0].id == first.id
    assert [row.distance for row in rows] == pytest.approx([0, 1])


def test_same_content_is_unique_but_model_and_version_are_isolated(db):
    db.add(cache())
    db.flush()
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(cache())
        db.flush()
    another_model, another_version = cache(), cache()
    another_model.model = 'text-embedding-3-large'
    another_version.text_version = 2
    db.add_all([another_model, another_version])
    db.flush()
    assert len(list(db.scalars(select(EmbeddingCache)))) == 3


@pytest.mark.parametrize('changes', [
    {'dimensions': 3072}, {'text_version': 0}, {'model': ''}, {'status': 'unknown'},
    {'attempts': -1}, {'content_hash': '0' * 64}, {'status': 'ready'},
    {'status': 'processing'}, {'lease_token': uuid4()}, {'last_error_code': 'secret response text'},
])
def test_invalid_cache_metadata_is_rejected(db, changes):
    entry = cache()
    for key, value in changes.items():
        setattr(entry, key, value)
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(entry)
        db.flush()


@pytest.mark.parametrize('size', [3, 1535, 1537])
def test_wrong_vector_dimensions_are_rejected(db, size):
    with pytest.raises((StatementError, IntegrityError)), db.begin_nested():
        db.add(cache(status='ready', embedding=[1.0] * size))
        db.flush()


def test_zero_vector_is_rejected(db):
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(cache(status='ready', embedding=[0.0] * 1536))
        db.flush()


@pytest.mark.parametrize('status', ['pending', 'processing', 'ready', 'error'])
def test_valid_indexing_states_roundtrip(db, status):
    entry = cache(status=status)
    if status == 'processing':
        entry.lease_token = uuid4()
        entry.lease_until = datetime.now(timezone.utc) + timedelta(minutes=1)
    if status == 'ready':
        entry.embedding = [1.0] + [0.0] * 1535
    db.add(entry)
    db.flush()
    db.expire_all()
    assert db.get(EmbeddingCache, entry.id).status == status


def test_projection_pk_cascade_and_reverse_safety(db):
    event = Event(type='task', title='Не звонить Семёну')
    db.add(event)
    db.flush()
    projection = document(event)
    db.add(projection)
    db.flush()
    db.delete(projection)
    db.flush()
    assert db.get(Event, event.id) is event
    db.add(document(event))
    db.flush()
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(document(event))
        db.flush()
    db.delete(event)
    db.flush()
    assert not list(db.scalars(select(EventSearchDocument)))


@pytest.mark.parametrize('invalid', ['missing', 'renamed', 'deleted'])
def test_stale_projection_cannot_be_published(db, invalid):
    event = Event(type='task', title='Исходное название')
    db.add(event)
    db.flush()
    projection = document(event)
    if invalid == 'missing':
        projection.event_id = uuid4()
    elif invalid == 'renamed':
        event.title = 'Новое название'
    else:
        event.status = 'deleted'
    db.flush()
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(projection)
        db.flush()


@pytest.mark.parametrize('change', ['rename', 'delete'])
def test_existing_projection_is_ineligible_after_source_changes(db, change):
    event = Event(type='task', title='Первое название')
    db.add(event)
    db.flush()
    db.add(document(event))
    db.flush()
    current = select(EventSearchDocument).join(Event, EventSearchDocument.current_event_condition())
    assert len(list(db.scalars(current))) == 1
    if change == 'rename':
        event.title = 'Другое название'
    else:
        event.status = 'deleted'
    db.flush()
    assert not list(db.scalars(current))
