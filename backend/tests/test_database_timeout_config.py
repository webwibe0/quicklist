import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_connect_timeout_defaults_to_three_seconds(monkeypatch):
    monkeypatch.delenv('DB_CONNECT_TIMEOUT_SECONDS', raising=False)
    assert Settings(_env_file=None).db_connect_timeout_seconds == 3


def test_connect_timeout_can_be_configured_without_editing_code(monkeypatch):
    monkeypatch.setenv('DB_CONNECT_TIMEOUT_SECONDS', '7')
    assert Settings(_env_file=None).db_connect_timeout_seconds == 7


@pytest.mark.parametrize('value', [0, 1, -1])
def test_connect_timeout_cannot_silently_fall_back_to_driver_default(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, db_connect_timeout_seconds=value)
