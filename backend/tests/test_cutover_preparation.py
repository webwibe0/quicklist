"""Fail-closed preparation guards; no source DB access or Docker required."""
from copy import deepcopy

import pytest
from scripts.cutover_guard import (
    CONTAINER,
    PORT,
    PROJECT,
    SMOKE,
    VOLUME,
    SafetyError,
    assert_integrity,
    assert_write_freeze,
    validate_container,
    validate_url,
)
from sqlalchemy import URL


def valid_url():
    return URL.create('postgresql+psycopg', username='cutover_app', password='synthetic',
                      host='127.0.0.1', port=PORT, database=SMOKE)


@pytest.mark.parametrize('change', [
    {'port': 5432}, {'port': 55435}, {'database': 'assistant'}, {'database': 'assistant_test'},
    {'host': 'localhost'}, {'host': 'remote.example'}, {'username': 'assistant'},
])
def test_wrong_application_target_is_rejected_before_connecting(change):
    with pytest.raises(SafetyError, match='STOP'):
        validate_url(valid_url().set(**change), SMOKE, application=True)


def test_exact_explicit_application_target_is_accepted():
    assert validate_url(valid_url(), SMOKE, application=True).port == PORT


def container_metadata():
    return {'Name': '/' + CONTAINER, 'Config': {'Labels': {'com.docker.compose.project': PROJECT},
        'Image': 'pgvector/pgvector@sha256:7a7e9f22015b67edb4bef5c59daeebcd7e74bfa570df6ce60ae01237c8648a84'},
        'Mounts': [{'Type': 'volume', 'Name': VOLUME}],
        'HostConfig': {'PortBindings': {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(PORT)}]}}}


@pytest.mark.parametrize('field', ['volume', 'project', 'image', 'port'])
def test_unknown_or_source_container_configuration_is_rejected(field):
    metadata = container_metadata()
    if field == 'volume':
        metadata['Mounts'][0]['Name'] = 'ai-task-assistant_postgres_data'
    elif field == 'project':
        metadata['Config']['Labels']['com.docker.compose.project'] = 'ai-task-assistant'
    elif field == 'image':
        metadata['Config']['Image'] = 'postgres:17'
    else:
        metadata['HostConfig']['PortBindings']['5432/tcp'][0]['HostPort'] = '5432'
    with pytest.raises(SafetyError, match='STOP'):
        validate_container(metadata)


@pytest.mark.parametrize('connections,running', [(1, False), (0, True), (2, True)])
def test_split_brain_guard_requires_stopped_app_and_no_other_sessions(connections, running):
    with pytest.raises(SafetyError, match='freeze'):
        assert_write_freeze(connections, running)


def test_frozen_state_is_accepted():
    assert_write_freeze(0, False)


@pytest.mark.parametrize('component', ['events', 'conversations', 'recurrence', 'status_counts'])
def test_fingerprint_mismatch_is_always_no_go(component):
    expected = {key: [] for key in ('events', 'conversations', 'columns', 'constraints', 'indexes',
                'recurrence', 'integrity', 'status_counts', 'extensions', 'all_indexes', 'all_constraints')}
    expected.update(user_tables={}, migration=[{'version_num': '0005'}])
    actual = deepcopy(expected)
    actual[component] = ['different']
    with pytest.raises(SafetyError, match='mismatch'):
        assert_integrity(actual, expected)
