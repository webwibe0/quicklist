"""Real HTTP and psycopg against a silent loopback TCP peer, never production."""
import socketserver
import threading
import time

import pytest
from sqlalchemy import URL, event
from sqlalchemy.orm import sessionmaker

from app.db import session as sessions
from tests import test_transaction_http_integration as transaction_http

pytestmark = pytest.mark.integration
http_api = transaction_http.http_api


@pytest.fixture
def silent_peer():
    release = threading.Event()
    accepted = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            accepted.set()
            # Accept TCP but send no PostgreSQL/SSL handshake response.
            release.wait(15)

    with socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler) as server:
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05})
        thread.start()
        try:
            yield server.server_address[1], accepted
        finally:
            release.set()
            server.shutdown()
            thread.join(2)
            assert not thread.is_alive()


@pytest.mark.parametrize('method,path,body', [
    ('GET', '/health', None),
    ('POST', '/api/v1/assistant/messages', {'message': 'OUTAGE no provider call'}),
])
@pytest.mark.parametrize('url_timeout', [None, '2'])
def test_stalled_pg_handshake_returns_503_within_existing_http_budget(
    http_api, silent_peer, monkeypatch, method, path, body, url_timeout,
):
    client, provider, _ = http_api
    port, accepted = silent_peer
    # Dummy credentials, dynamic loopback listener: no PostgreSQL data can be touched.
    engine = sessions.build_engine(URL.create('postgresql+psycopg', username='probe',
        password='synthetic-test-value', host='127.0.0.1', port=port, database='outage_probe_test',
        query={'connect_timeout': url_timeout} if url_timeout else {}))
    attempts = []

    @event.listens_for(engine, 'do_connect')
    def connecting(dialect, record, args, params):
        attempts.append(params.get('connect_timeout'))

    monkeypatch.setattr(sessions, 'SessionLocal', sessionmaker(engine, expire_on_commit=False))
    started = time.monotonic()
    try:
        response = client.request(method, path, json=body)
        elapsed = time.monotonic() - started
        assert accepted.is_set() and len(attempts) == 1
        assert int(attempts[0]) == (int(url_timeout) if url_timeout else 3)
        assert response.status_code == 503, response.text
        assert 2 <= elapsed < 10  # Real driver timeout; not immediate mocked failure.
        assert provider.contexts == []
        assert response.json()['detail'] in {
            'База данных или схема недоступна.',
            'База данных временно недоступна. Попробуйте ещё раз.',
        }
    finally:
        engine.dispose()
