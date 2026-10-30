"""Explicit operation container. Refuses local primary and mismatched cluster/marker."""
import os
import sys
from pathlib import Path
from uuid import UUID

from app.core.config import Settings
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

settings = Settings()
initialize_production = '--initialize-empty-production' in sys.argv
initial_owner = None
if initialize_production:
    assert settings.app_environment == 'production'
    assert os.environ.get('INITIAL_OWNER_CONFIRMED') == 'yes'
    initial_owner = {
        'legacy_owner_confirmed': 'yes',
        'legacy_owner_id': str(UUID(os.environ['INITIAL_OWNER_ID'])),
        'legacy_owner_email': os.environ['INITIAL_OWNER_EMAIL'].strip().casefold(),
    }
    assert '@' in initial_owner['legacy_owner_email'] and len(initial_owner['legacy_owner_email']) <= 254
admin_url = Path('/run/secrets/migration_database_url').read_text().strip()
url = make_url(admin_url)
assert url.host == 'postgres' and url.port == 5432 and url.database == 'quicklist' and not url.query
assert url.username == 'quicklist_admin' and settings.app_environment in {'staging', 'production'}
engine = create_engine(url, hide_parameters=True)
with engine.connect() as conn:
    cluster = str(conn.scalar(text('SELECT system_identifier FROM pg_control_system()')))
    assert cluster == settings.embedding_allowed_cluster_id
    assert cluster not in {'7699165401457610790', '7696627159100764198', '7698854534603636773'}
    conn.rollback()
    with conn.begin():
        if '--initialize-staging' in sys.argv:
            assert settings.app_environment == 'staging'
            assert conn.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='public'")) == 0
            conn.exec_driver_sql('CREATE EXTENSION vector')
            conn.exec_driver_sql("COMMENT ON DATABASE quicklist IS 'QUICKLIST_STAGING'")
        marker = conn.scalar(text("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()"))
        assert marker == 'QUICKLIST_' + settings.app_environment.upper()
        if initialize_production:
            assert conn.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='public'")) == 0
            assert conn.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'")) == '0.8.7'
        app_url = make_url(settings.database_url)
        assert app_url.username == 'quicklist_app' and app_url.host == 'postgres' and app_url.database == 'quicklist'
        if not conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname='quicklist_app'")):
            from psycopg import sql
            raw = conn.connection.driver_connection
            command = sql.SQL('CREATE ROLE quicklist_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}').format(sql.Literal(app_url.password))
            conn.exec_driver_sql(command.as_string(raw))

os.environ['DATABASE_URL'] = admin_url
from alembic import command
from alembic.config import Config
from app.core.config import get_settings

get_settings.cache_clear()
config = Config('alembic.ini')
if '--initialize-staging' in sys.argv:
    config.attributes['legacy_owner'] = {'legacy_owner_confirmed': 'yes',
        'legacy_owner_id': '20000000-0000-4000-8000-000000000001',
        'legacy_owner_email': 'staging-owner@example.test'}
elif initialize_production:
    config.attributes['legacy_owner'] = initial_owner
command.upgrade(config, '0009')
with engine.begin() as conn:
    conn.exec_driver_sql('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
    conn.exec_driver_sql('GRANT CONNECT ON DATABASE quicklist TO quicklist_app')
    conn.exec_driver_sql('GRANT USAGE ON SCHEMA public TO quicklist_app')
    conn.exec_driver_sql('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO quicklist_app')
    conn.exec_driver_sql('REVOKE INSERT,UPDATE,DELETE ON alembic_version FROM quicklist_app')
    conn.exec_driver_sql('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO quicklist_app')
    conn.exec_driver_sql('GRANT EXECUTE ON FUNCTION pg_control_system() TO quicklist_app')
    assert conn.scalar(text('SELECT version_num FROM alembic_version')) == '0009'
engine.dispose()
print('Verified isolated migration 0009 complete; restricted application role granted')
