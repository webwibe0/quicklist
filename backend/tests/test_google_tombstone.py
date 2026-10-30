"""Cancelled remote events are not silently resurrected or repeatedly deleted."""

import base64
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from app.core.config import Settings
from app.services.google_calendar import GoogleCalendarService
from app.services.google_client import CalendarError


@pytest.mark.parametrize("operation", ["upsert", "delete"])
def test_cancelled_remote_event_is_explicit_conflict_or_already_deleted(operation):
    settings = Settings(
        _env_file=None, google_token_key=base64.urlsafe_b64encode(b"x" * 32).decode()
    )
    client = Mock()
    service = GoogleCalendarService(None, client, settings)
    link = uuid4()
    client.event_request.return_value = httpx.Response(
        200,
        json={
            "status": "cancelled",
            "etag": "synthetic-etag",
            "extendedProperties": {"private": {"quicklist_link": str(link)}},
        },
    )
    claim = {
        "context": "unit",
        "link": link,
        "remote_id": "ql0123",
        "desired": {"operation": operation, "payload": {"summary": "Synthetic"}},
    }
    claim["cipher"] = service.cipher.encrypt(
        {
            "access": "synthetic",
            "refresh": "synthetic",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        claim["context"],
    )
    if operation == "upsert":
        with pytest.raises(CalendarError, match="REMOTE_EVENT_DELETED"):
            service.deliver(claim)
    else:
        service.deliver(claim)
    assert [call.args[0] for call in client.event_request.call_args_list] == ["GET"]


@pytest.mark.parametrize("to_timed", [True, False])
def test_patch_clears_mutually_exclusive_date_fields(to_timed):
    settings = Settings(
        _env_file=None, google_token_key=base64.urlsafe_b64encode(b"x" * 32).decode()
    )
    client = Mock()
    service = GoogleCalendarService(None, client, settings)
    link = uuid4()
    old = (
        {"date": "2026-11-02"}
        if to_timed
        else {"dateTime": "2026-11-02T10:00:00+03:00", "timeZone": "Europe/Moscow"}
    )
    new = (
        {"dateTime": "2026-11-02T10:15:00+03:00", "timeZone": "Europe/Moscow"}
        if to_timed
        else {"date": "2026-11-02"}
    )
    calls = []

    def request(method, remote_id, access, *, payload=None, etag=None):
        calls.append(method)
        if method == "GET":
            return httpx.Response(
                200,
                json={
                    "start": dict(old),
                    "end": dict(old),
                    "etag": "v1",
                    "extendedProperties": {"private": {"quicklist_link": str(link)}},
                },
            )
        assert method == "PATCH" and etag == "v1"
        for field in ("start", "end"):
            merged = {k: v for k, v in (old | payload[field]).items() if v is not None}
            assert bool(merged.get("date")) != bool(merged.get("dateTime")), (
                "Google rejects mixed all-day/timed fields"
            )
            if not to_timed:
                assert "timeZone" not in merged
        return httpx.Response(200, json={})

    client.event_request.side_effect = request
    claim = {
        "context": "unit",
        "link": link,
        "remote_id": "ql0123",
        "desired": {"operation": "upsert", "payload": {"start": dict(new), "end": dict(new)}},
    }
    claim["cipher"] = service.cipher.encrypt(
        {
            "access": "synthetic",
            "refresh": "synthetic",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        claim["context"],
    )
    service.deliver(claim)
    assert calls == ["GET", "PATCH"]
    assert claim["desired"]["payload"]["start"] == new  # Never mutate frozen desired state.
