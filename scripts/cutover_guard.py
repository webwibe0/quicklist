"""Preparation-only guards. This module cannot switch production or edit .env."""
import json
import os
import sys
from pathlib import Path

from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
PRIVATE = ROOT / '.local/stage8-foundation/cutover-preparation'
PORT = 55434
PROJECT = 'quicklist-cutover-rehearsal'
CONTAINER = PROJECT + '-postgres_vector-1'
VOLUME = 'quicklist_cutover_rehearsal_20261020_data'
REFERENCE = 'quicklist_rehearsal'
SMOKE = 'quicklist_smoke'
REGRESSION = 'cutover_regression_test'
FAULT = 'cutover_fault_test'
ADMIN = 'postgres'
DATABASES = {REFERENCE, SMOKE, REGRESSION, FAULT, ADMIN}


class SafetyError(RuntimeError):
    pass


def candidate_url(database, *, application=False):
    if database not in DATABASES:
        raise SafetyError('STOP: database is outside rehearsal allowlist')
    role = 'cutover_app' if application else 'stage8_owner'
    password_file = 'app-password.txt' if application else 'owner-password.txt'
    return URL.create('postgresql+psycopg', username=role,
                      password=(PRIVATE / password_file).read_text(), host='127.0.0.1',
                      port=PORT, database=database)


def validate_url(url, expected_database, *, application=False):
    parsed = make_url(url) if isinstance(url, str) else url
    if (expected_database not in DATABASES or parsed.database != expected_database
            or parsed.host != '127.0.0.1' or parsed.port != PORT
            or parsed.drivername != 'postgresql+psycopg'
            or parsed.username != ('cutover_app' if application else 'stage8_owner')):
        raise SafetyError('STOP: unexpected database endpoint or role')
    return parsed


def validate_container(metadata):
    if metadata.get('Name') != '/' + CONTAINER:
        raise SafetyError('STOP: unexpected container')
    config = metadata['Config']
    if config.get('Labels', {}).get('com.docker.compose.project') != PROJECT:
        raise SafetyError('STOP: unexpected Compose project')
    volumes = [m.get('Name') for m in metadata['Mounts'] if m['Type'] == 'volume']
    ports = metadata['HostConfig']['PortBindings'].get('5432/tcp', [])
    if volumes != [VOLUME] or ports != [{'HostIp': '127.0.0.1', 'HostPort': str(PORT)}]:
        raise SafetyError('STOP: unexpected volume or port binding')
    if config['Image'] != 'pgvector/pgvector@sha256:7a7e9f22015b67edb4bef5c59daeebcd7e74bfa570df6ce60ae01237c8648a84':
        raise SafetyError('STOP: unexpected image digest')


def checked_engine(database, *, readonly=False):
    url = validate_url(candidate_url(database), database)
    options = '-c timezone=UTC -c statement_timeout=15000 -c application_name=quicklist-cutover-qa'
    if readonly:
        options += ' -c default_transaction_read_only=on'
    engine = create_engine(url, pool_pre_ping=True, hide_parameters=True,
                           connect_args={'options': options, 'connect_timeout': 2})
    try:
        with engine.connect() as connection:
            actual = connection.scalar(text('SELECT current_database()'))
            identity = str(connection.scalar(text('SELECT system_identifier FROM pg_control_system()')))
        source = json.loads((PRIVATE / 'source-baseline.json').read_text())
        target = json.loads((PRIVATE / 'target-identity.json').read_text())
        if (actual != database or identity == str(source['database_identity'][0]['system_identifier'])
                or identity != target['system_identifier']):
            raise SafetyError('STOP: database/cluster identity mismatch')
        return engine
    except Exception:
        engine.dispose()
        raise


def assert_integrity(actual, expected, *, migrated=False):
    keys = ('events', 'conversations', 'columns', 'constraints', 'indexes', 'recurrence',
            'integrity', 'status_counts')
    differences = [key for key in keys if actual[key] != expected[key]]
    for name, fingerprint in expected['user_tables'].items():
        if actual['user_tables'].get(name) != fingerprint:
            differences.append('user_table:' + name)
    if actual['migration'] != [{'version_num': '0006' if migrated else '0005'}]:
        differences.append('migration')
    old_extensions = {row['extname']: row['extversion'] for row in expected['extensions']}
    extensions = {row['extname']: row['extversion'] for row in actual['extensions']}
    if any(extensions.get(name) != version for name, version in old_extensions.items()):
        differences.append('extensions')
    if migrated and extensions.get('vector') != '0.8.7':
        differences.append('vector')
    old_indexes = {row['indexname']: row for row in actual['all_indexes']}
    old_constraints = {(row['table_name'], row['conname']): row for row in actual['all_constraints']}
    if any(old_indexes.get(row['indexname']) != row for row in expected['all_indexes']):
        differences.append('all_indexes')
    if any(old_constraints.get((row['table_name'], row['conname'])) != row for row in expected['all_constraints']):
        differences.append('all_constraints')
    if differences:
        raise SafetyError('STOP: data/schema mismatch: ' + ','.join(differences))


def assert_write_freeze(other_connections, app_running):
    """Fail closed even for idle sessions: they could resume writing after the check."""
    if other_connections or app_running:
        raise SafetyError('STOP: exclusive write freeze has not been established')


def child_environment(database, *, application=False):
    url = validate_url(candidate_url(database, application=application), database, application=application)
    env = os.environ.copy()
    env.update(DATABASE_URL=url.render_as_string(hide_password=False),
               OPENAI_API_KEY='', SEMANTIC_SEARCH_ENABLED='false',
               PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(ROOT / 'backend'),
               PGAPPNAME='quicklist-cutover-isolated')
    # Never inherit a source test URL from the caller.
    env['TEST_DATABASE_URL'] = candidate_url(REGRESSION).render_as_string(hide_password=False)
    return env
