import os
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from alembic import command
from app.core.config import get_settings


@pytest.fixture(scope="session")
def integration_engine():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run real PostgreSQL integration tests")
    database = make_url(url).database or ""
    if not database.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must use a disposable database ending in _test")
    # Stage 8.2: a suffix alone does not prove isolation. Check endpoint + real cluster/database marker
    # BEFORE allowing even the existing migration/TRUNCATE fixture to touch a database.
    from app.services.indexing_target import verify_indexing_target
    probe = create_engine(url, connect_args={"connect_timeout": 3}, hide_parameters=True)
    try:
        verify_indexing_target(probe, os.environ.get("EMBEDDING_ALLOWED_CLUSTER_ID", ""))
    finally:
        probe.dispose()
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(config, "head")
        engine = create_engine(url)
        yield engine
        engine.dispose()
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
        get_settings.cache_clear()


@pytest.fixture
def db(integration_engine):
    with integration_engine.begin() as conn:
        # The projection has an FK to events, even when empty. All tables are disposable here.
        conn.execute(text("TRUNCATE conversations, google_event_links, event_search_documents, events, embedding_cache"))
    with Session(integration_engine, expire_on_commit=False) as session:
        yield session
        session.rollback()


@pytest.fixture(autouse=True)
def authenticated_legacy_harness(request, monkeypatch):
    """Old regression scenarios explicitly run as one synthetic owner, not as anonymous prod access.

    New account/leakage tests deliberately do NOT get this authentication override.
    """
    if request.module.__name__.endswith(('test_accounts', 'test_production_accounts', 'test_critical_accounts', 'test_google_calendar', 'test_today_planner', 'test_csv_account_seed')):
        yield
        return
    from uuid import UUID

    from app.db.ownership import bind_owner
    from app.main import app
    from app.models.accounts import User
    from app.services.authentication import Identity, require_user
    original = Session.__init__
    owner = UUID('20000000-0000-4000-8000-000000000001')
    def scoped(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if not self.info.get('system_access'):
            bind_owner(self, owner)
        bind = self.get_bind()
        if getattr(bind.dialect, 'name', None) == 'sqlite':
            User.__table__.create(bind, checkfirst=True)
    monkeypatch.setattr(Session, '__init__', scoped)
    previous = app.dependency_overrides.get(require_user)
    app.dependency_overrides[require_user] = lambda: Identity(owner, 'legacy-test@example.test')
    try:
        yield
    finally:
        if previous is None:
            app.dependency_overrides.pop(require_user, None)
        else:
            app.dependency_overrides[require_user] = previous
