"""Original CSV ownership/init/search: pinned disposable PostgreSQL only, no paid API."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.passwords import hash_password
from app.db import session as database
from app.db.ownership import OwnershipError
from app.main import app
from app.models.accounts import User
from app.models.entities import Event
from app.models.google_calendar import GoogleEventLink
from app.models.search import EmbeddingCache, EventSearchDocument
from app.repositories.events import EventRepository
from app.schemas.commands import SearchEventsCommand


@pytest.fixture
def owners(db, integration_engine):
    ids=[uuid4(),uuid4()]
    with Session(integration_engine,info={'system_access':True}) as s,s.begin():
        s.add_all([User(id=uid,email=f'seed-{uid.hex}@example.test') for uid in ids])
    return ids


def seed(engine,owner):
    from app.services.dataset import ensure_original_dataset
    with Session(engine,info={'owner_id':owner}) as s,s.begin():
        return ensure_original_dataset(s)


def test_each_owner_has_independent_original_ids_and_no_google_or_paid_queue(owners,integration_engine):
    assert seed(integration_engine,owners[0])['inserted']==1000
    assert seed(integration_engine,owners[1])['inserted']==1000
    with Session(integration_engine,info={'system_access':True}) as s:
        assert s.scalar(select(func.count()).select_from(Event))==2000
        assert s.scalar(select(func.count()).select_from(EventSearchDocument))==2000
        assert s.scalar(select(func.count()).select_from(EmbeddingCache))==0
        assert s.scalar(select(func.count()).select_from(GoogleEventLink))==0
    for owner in owners:
        with Session(integration_engine,info={'owner_id':owner}) as s:
            rows=s.scalars(select(Event)).all()
            assert len(rows)==1000 and all(e.source=='dataset' and e.type_inferred for e in rows)
            assert len({e.source_id for e in rows})==1000


def test_relogin_never_restores_modified_or_soft_deleted_seed_records(owners,integration_engine):
    for owner in owners:
        seed(integration_engine,owner)
    with Session(integration_engine,info={'owner_id':owners[0]}) as s,s.begin():
        rows=s.scalars(select(Event).order_by(Event.source_id).limit(2)).all()
        changed,deleted=rows[0].source_id,rows[1].source_id
        rows[0].title='Synthetic edited original'
        rows[1].status='deleted'
    assert seed(integration_engine,owners[0])['inserted']==0
    with Session(integration_engine,info={'owner_id':owners[0]}) as s:
        assert s.scalar(select(Event).where(Event.source_id==changed)).title=='Synthetic edited original'
        assert s.scalar(select(Event).where(Event.source_id==deleted)).status=='deleted'
    with Session(integration_engine,info={'owner_id':owners[1]}) as s:
        assert s.scalar(select(Event).where(Event.source_id==changed)).title!='Synthetic edited original'
        assert s.scalar(select(Event).where(Event.source_id==deleted)).status=='pending'


def test_arbitrary_owner_and_partial_prior_import_are_not_overwritten(owners,integration_engine):
    from app.services.dataset import ensure_original_dataset, read_rows
    with Session(integration_engine) as s,s.begin():
        with pytest.raises(OwnershipError):
            ensure_original_dataset(s)
    item=read_rows()[0]
    with Session(integration_engine,info={'owner_id':owners[0]}) as s,s.begin():
        s.add(Event(**(item|{'owner_id':owners[0],'status':'deleted'})))
    result=seed(integration_engine,owners[0])
    assert result['inserted']==0 and result['state']=='existing_import_requires_review'


@pytest.mark.parametrize('source_index',[99,949])
def test_search_filters_all_thousand_before_result_limit_and_excludes_other_owner(owners,integration_engine,source_index):
    from app.services.dataset import read_rows
    for owner in owners:
        seed(integration_engine,owner)
    original=read_rows()[source_index]
    with Session(integration_engine,info={'owner_id':owners[0]}) as s:
        expected=s.scalar(select(Event).where(Event.source_id==original['source_id']))
        result=EventRepository(s,50).search(SearchEventsCommand(query=expected.title,start=expected.event_date,end=expected.event_date))
        assert expected.id in [e.id for e in result]
        assert all(e.owner_id==owners[0] for e in result)
    with Session(integration_engine,info={'owner_id':owners[0]}) as s,s.begin():
        item=s.scalar(select(Event).where(Event.source_id==original['source_id']))
        item.title='Synthetic уникальное обновление'
    with Session(integration_engine,info={'owner_id':owners[0]}) as s:
        assert EventRepository(s,50).search(SearchEventsCommand(query='уникальное обновление'))
    with Session(integration_engine,info={'owner_id':owners[1]}) as s:
        assert not EventRepository(s,50).search(SearchEventsCommand(query='уникальное обновление'))
    with Session(integration_engine,info={'owner_id':owners[0]}) as s,s.begin():
        s.scalar(select(Event).where(Event.source_id==original['source_id'])).status='deleted'
    with Session(integration_engine,info={'owner_id':owners[0]}) as s:
        assert not EventRepository(s,50).search(SearchEventsCommand(query='уникальное обновление'))


def test_successful_login_initializes_existing_unseeded_account_once(owners,integration_engine,monkeypatch):
    from app.api import auth
    from app.services import authentication
    owner=owners[0]
    password='Synthetic-seed-login-42'
    with Session(integration_engine,info={'system_access':True}) as s,s.begin():
        user=s.get(User,owner)
        user.password_hash=hash_password(password)
        email=user.email
    settings=Settings(_env_file=None,auth_origin='http://127.0.0.1:3001')
    monkeypatch.setattr(database,'SessionLocal',sessionmaker(integration_engine,expire_on_commit=False))
    monkeypatch.setattr(auth,'get_settings',lambda:settings)
    monkeypatch.setattr(authentication,'get_settings',lambda:settings)
    for _ in range(2):
        with TestClient(app) as client:
            assert client.get('/api/v1/auth/csrf').status_code==200
            headers={'Origin':settings.auth_origin,'X-CSRF-Token':client.cookies['quicklist_csrf']}
            response=client.post('/api/v1/auth/login',json={'email':email,'password':password},headers=headers)
            assert response.status_code==200
        with Session(integration_engine,info={'owner_id':owner}) as s:
            assert s.scalar(select(func.count()).select_from(Event))==1000


def test_seed_transaction_rolls_back_events_and_search_projection_together(owners,integration_engine):
    from app.services.dataset import ensure_original_dataset
    with pytest.raises(RuntimeError,match='Synthetic rollback'):
        with Session(integration_engine,info={'owner_id':owners[0]}) as s,s.begin():
            ensure_original_dataset(s)
            raise RuntimeError('Synthetic rollback')
    with Session(integration_engine,info={'owner_id':owners[0]}) as s:
        assert s.scalar(select(func.count()).select_from(Event))==0
        assert s.scalar(select(func.count()).select_from(EventSearchDocument))==0


def test_concurrent_first_login_seed_is_serialized_per_owner(owners,integration_engine):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:seed(integration_engine,owners[0]),range(2)))
    assert sorted(row['inserted'] for row in results)==[0,1000]


def test_source_seed_is_not_a_mass_paid_indexing_job_or_google_export(owners,integration_engine):
    from app.ai.embeddings import FakeEmbeddingProvider
    from app.models.google_calendar import GoogleConnection
    from app.services.indexing import IndexingWorker
    with Session(integration_engine,info={'owner_id':owners[0],'calendar_capture':True}) as s,s.begin():
        s.add(GoogleConnection(owner_id=owners[0],sync_enabled=True,google_subject='synthetic',
                               credentials_cipher='synthetic-not-a-token'))
    seed(integration_engine,owners[0])
    provider=FakeEmbeddingProvider({})
    settings=Settings(_env_file=None,embedding_worker_enabled=True,
                      embedding_allowed_cluster_id='7698854534603636773')
    report=IndexingWorker(integration_engine,provider,settings).run_once(changes_only=True)
    assert report['provider_calls']==0 and report['cache_created']==0
    with Session(integration_engine,info={'owner_id':owners[0]}) as s:
        assert s.scalar(select(func.count()).select_from(GoogleEventLink))==0
