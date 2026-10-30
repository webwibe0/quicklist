"""Capture desired Google state inside the local event transaction, without network I/O."""

import hashlib
from datetime import timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Event
from app.models.google_calendar import GoogleConnection, GoogleEventLink
from app.services.google_client import CalendarError
from app.temporal.intervals import scheduled_interval

DEFAULT_MINUTES = {"meeting": 60, "reminder": 15, "task": 15}


def event_payload(item, timezone):
    if item.status in {"deleted", "cancelled"}:
        return {"operation": "delete"}
    if item.event_date is None:
        raise CalendarError("DATE_REQUIRED")
    payload = {"summary": item.title}
    if item.event_time is None:
        payload.update(
            start={"date": item.event_date.isoformat()},
            end={"date": (item.event_date + timedelta(days=1)).isoformat()},
        )
    else:
        start, end = scheduled_interval(item.event_date, item.event_time, item.end_time)
        if item.end_time is None:
            end = start + timedelta(minutes=DEFAULT_MINUTES[item.type])
        if end <= start:
            raise CalendarError("END_TIME_INVALID")
        zone = ZoneInfo(timezone)
        payload.update(
            start={"dateTime": start.replace(tzinfo=zone).isoformat(), "timeZone": timezone},
            end={"dateTime": end.replace(tzinfo=zone).isoformat(), "timeZone": timezone},
        )
    return {"operation": "upsert", "payload": payload}


def select_link(session, connection, item):
    # Include new rows not yet autoflushed from this very before_flush hook.
    for row in session.new:
        if (
            isinstance(row, GoogleEventLink)
            and row.event_id == item.id
            and row.google_subject == connection.google_subject
        ):
            return row
    return session.scalar(
        select(GoogleEventLink)
        .where(
            GoogleEventLink.event_id == item.id,
            GoogleEventLink.google_subject == connection.google_subject,
        )
        .with_for_update()
    )


def enqueue(session, connection, item, timezone, *, explicit=False, is_new=False):
    link = select_link(session, connection, item)
    active = link is not None and link.generation == connection.generation
    if not explicit and not active and not connection.sync_enabled:
        return None
    if (
        not explicit
        and not active
        and (
            not is_new
            or item.recurrence_rule
            or item.recurrence_series_id
            or item.source == "dataset"
        )
    ):
        return None
    try:
        desired = event_payload(item, timezone)
    except CalendarError:
        if link is None:
            if explicit:
                raise
            return None
        # A linked event that becomes unscheduled must not retain an invented stale date.
        desired = {"operation": "delete"}
    if link is None:
        if desired["operation"] == "delete":
            return None
        link = GoogleEventLink(
            id=uuid4(),
            owner_id=item.owner_id,
            event_id=item.id,
            connection_id=connection.id,
            generation=connection.generation,
            google_subject=connection.google_subject,
            calendar_id="primary",
            remote_event_id="ql"
            + hashlib.sha256(
                f"{item.owner_id}:{item.id}:{connection.google_subject}".encode()
            ).hexdigest(),
            desired=desired,
            revision=1,
            synced_revision=0,
            status="pending",
        )
        session.add(link)
    elif link.desired != desired or not active:
        link.desired, link.generation = desired, connection.generation
        link.revision += 1
        link.status, link.error_code = "pending", None
    return link


@sqlalchemy_event.listens_for(Session, "before_flush")
def capture_calendar_changes(session, flush_context, instances):
    if not session.info.get("calendar_capture") or session.info.get("system_access"):
        return
    changes = [obj for obj in session.new | session.dirty if isinstance(obj, Event)]
    if not changes:
        return
    connection = session.scalar(
        select(GoogleConnection)
        .where(GoogleConnection.credentials_cipher.is_not(None))
        .with_for_update()
    )
    if connection is None:
        return
    for item in changes:
        is_new = item in session.new
        if item.id is None:
            item.id = uuid4()
        enqueue(session, connection, item, session.info["calendar_timezone"], is_new=is_new)
