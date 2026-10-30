"""Explicit controlled real E2E, after primary migration approval and user OAuth consent.

Creates/changes/soft-deletes ONLY its own freshly allocated test event. No LLM or embeddings.
"""

import argparse
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def same_instant(value, expected):
    """Google may serialize a Moscow event as UTC Z; compare the actual instant."""
    try:
        actual = datetime.fromisoformat(value)
        return actual.utcoffset() is not None and actual == expected
    except (ValueError, TypeError):
        return False


def remote_deleted(response):
    """Google GET can retain the deleted event as HTTP 200 / cancelled."""
    if response.status_code in {404, 410}:
        return True
    if response.status_code != 200:
        return False
    try:
        data = response.json()
        return isinstance(data, dict) and data.get("status") == "cancelled"
    except ValueError:
        return False


def main():
    from sqlalchemy import select, text

    from app.core.config import get_settings
    from app.db.ownership import bind_owner
    from app.db.session import SessionLocal, engine
    from app.models.entities import Event
    from app.models.google_calendar import GoogleConnection, GoogleEventLink
    from app.services.google_calendar import GoogleCalendarService
    from app.services.google_client import GoogleClient

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-id", type=UUID, required=True)
    parser.add_argument("--execute-controlled-test", action="store_true", required=True)
    args = parser.parse_args()
    with engine.connect() as c, c.begin():
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        assert engine.url.database == "assistant" and engine.url.port == 5432
        assert (
            str(c.scalar(text("SELECT system_identifier FROM pg_control_system()")))
            == "7699165401457610790"
        )
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0008"
    settings = get_settings()
    client = GoogleClient(settings)
    service = GoogleCalendarService(SessionLocal, client, settings)
    assert service.status(args.owner_id)["sync_enabled"], (
        "Connect the approved owner and enable sync first"
    )
    identifier = uuid4()
    title = "QuickList Google Calendar E2E " + identifier.hex[:8]
    day = datetime.now(ZoneInfo(settings.app_timezone)).date() + timedelta(days=1)

    def remote_state():
        with service.session(args.owner_id) as s:
            connection = s.scalar(select(GoogleConnection))
            link = s.scalar(
                select(GoogleEventLink).where(
                    GoogleEventLink.event_id == identifier,
                    GoogleEventLink.google_subject == connection.google_subject,
                )
            )
            assert link is not None
            credentials = service.cipher.decrypt(
                connection.credentials_cipher, service.context(connection)
            )
            remote_id = link.remote_event_id
        return client.event_request("GET", remote_id, credentials["access"])

    try:
        with SessionLocal() as s, s.begin():
            bind_owner(s, args.owner_id)
            s.add(
                Event(
                    id=identifier,
                    type="meeting",
                    title=title,
                    event_date=day,
                    event_time=time(12),
                    end_time=time(12, 15),
                    status="pending",
                    source="user",
                    owner_id=args.owner_id,
                )
            )
        assert service.sync_event(args.owner_id, identifier)["status"] == "synced"
        created = remote_state()
        assert created.status_code == 200 and created.json()["summary"] == title
        zone = ZoneInfo(settings.app_timezone)
        assert same_instant(created.json()["start"]["dateTime"], datetime.combine(day, time(12), zone))
        assert same_instant(created.json()["end"]["dateTime"], datetime.combine(day, time(12, 15), zone))
        with SessionLocal() as s, s.begin():
            bind_owner(s, args.owner_id)
            s.info.update(calendar_capture=True, calendar_timezone=settings.app_timezone)
            item = s.get(Event, identifier)
            assert item.title == title
            item.title = title + " updated"
            item.event_time, item.end_time = time(12, 30), time(12, 45)
        service.drain(args.owner_id, event_id=identifier, limit=1)
        assert service.event_status(args.owner_id, identifier)["status"] == "synced"
        updated = remote_state()
        assert updated.status_code == 200 and updated.json()["summary"] == title + " updated"
        assert same_instant(updated.json()["start"]["dateTime"], datetime.combine(day, time(12, 30), zone))
        assert same_instant(updated.json()["end"]["dateTime"], datetime.combine(day, time(12, 45), zone))
        print("Controlled Google CREATE and UPDATE: PASS")
    finally:
        # The only mutable target is the UUID generated in THIS invocation.
        with SessionLocal() as s, s.begin():
            bind_owner(s, args.owner_id)
            item = s.get(Event, identifier)
            if item is not None:
                assert item.title in {title, title + " updated"}
                s.info.update(calendar_capture=True, calendar_timezone=settings.app_timezone)
                item.status = "deleted"
        service.drain(args.owner_id, event_id=identifier, limit=1)
        assert remote_deleted(remote_state())
        client.close()
    assert service.event_status(args.owner_id, identifier)["status"] == "synced"
    print(
        "Controlled Google DELETE: PASS. One local soft-deleted test trace retained; no other events touched."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Controlled Calendar E2E STOP:", type(error).__name__)
        raise SystemExit(2) from None
