"""Owner-bound OAuth and bounded outbox delivery. Google I/O never holds a DB transaction."""

import copy
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, func, select

from app.db.ownership import bind_owner
from app.models.accounts import AuthSession
from app.models.entities import Event
from app.models.google_calendar import GoogleConnection, GoogleEventLink, GoogleOAuthState
from app.services.calendar_outbox import DEFAULT_MINUTES, enqueue, event_payload
from app.services.google_client import CalendarError, TokenCipher


def now():
    return datetime.now(UTC)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class GoogleCalendarService:
    def __init__(self, sessions, client, settings):
        self.sessions, self.client, self.settings = sessions, client, settings
        self.cipher = TokenCipher(settings.google_token_key.get_secret_value())

    def session(self, owner):
        return bind_owner(self.sessions(), owner)

    def context(self, connection):
        return f"credentials:{connection.owner_id}:{connection.id}:{connection.generation}"

    def connection(self, session, *, lock=False):
        query = select(GoogleConnection)
        return session.scalar(query.with_for_update() if lock else query)

    def idle(self, connection):
        if connection.lease_until and connection.lease_until > now():
            raise CalendarError("SYNC_BUSY")

    def status(self, owner):
        with self.session(owner) as session:
            connection = self.connection(session)
            counts = {"pending": 0, "error": 0}
            if connection:
                for status, count in session.execute(
                    select(GoogleEventLink.status, func.count())
                    .where(GoogleEventLink.generation == connection.generation)
                    .group_by(GoogleEventLink.status)
                ):
                    if status in counts:
                        counts[status] = count
            return {
                "configured": True,
                "connected": bool(connection and connection.credentials_cipher),
                "account_email": connection.account_email
                if connection and connection.credentials_cipher
                else None,
                "sync_enabled": bool(connection and connection.sync_enabled),
                "error_code": connection.error_code if connection else None,
                "pending_count": counts["pending"],
                "error_count": counts["error"],
                "default_duration_minutes": DEFAULT_MINUTES,
                "recurrence_policy": "selected_occurrence_only",
            }

    def connect(self, owner, session_hash):
        raw_state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        with self.session(owner) as session, session.begin():
            connection = self.connection(session, lock=True)
            if connection is None:
                connection = GoogleConnection(id=uuid4(), owner_id=owner, generation=uuid4())
                session.add(connection)
            else:
                self.idle(connection)
                if connection.credentials_cipher and connection.error_code != "REAUTH_REQUIRED":
                    raise CalendarError("ALREADY_CONNECTED")
            hashed = digest(raw_state)
            session.add(
                GoogleOAuthState(
                    state_hash=hashed,
                    owner_id=owner,
                    connection_id=connection.id,
                    generation=connection.generation,
                    session_hash=session_hash,
                    verifier_cipher=self.cipher.encrypt(
                        {"verifier": verifier}, f"state:{owner}:{hashed}"
                    ),
                    expires_at=now() + timedelta(minutes=10),
                )
            )
        return self.client.authorization_url(raw_state, verifier)

    def callback(self, owner, session_hash, state, code):
        if not state or not code or len(state) > 256 or len(code) > 8192:
            raise CalendarError("OAUTH_STATE_INVALID")
        hashed = digest(state)
        with self.session(owner) as session, session.begin():
            row = session.scalar(
                select(GoogleOAuthState)
                .where(GoogleOAuthState.state_hash == hashed)
                .with_for_update()
            )
            if row is None or row.session_hash != session_hash or row.expires_at <= now():
                raise CalendarError("OAUTH_STATE_INVALID")
            connection = self.connection(session, lock=True)
            if (
                connection is None
                or connection.id != row.connection_id
                or connection.generation != row.generation
            ):
                raise CalendarError("OAUTH_STATE_INVALID")
            self.idle(connection)
            connection_id, old_generation = connection.id, row.generation
            verifier = self.cipher.decrypt(row.verifier_cipher, f"state:{owner}:{hashed}")[
                "verifier"
            ]
            session.delete(row)  # Consume BEFORE exchanging a one-use authorization code.
        tokens = self.client.tokens(code=code, verifier=verifier)
        try:
            subject, email = self.client.identity(tokens["access"])
            with self.session(owner) as session, session.begin():
                active_owner = session.scalar(
                    select(AuthSession.user_id).where(
                        AuthSession.token_hash == session_hash, AuthSession.expires_at > now()
                    )
                )
                if active_owner != owner:
                    raise CalendarError("OAUTH_STATE_INVALID")
                connection = self.connection(session, lock=True)
                if (
                    connection is None
                    or connection.id != connection_id
                    or connection.generation != old_generation
                ):
                    raise CalendarError("OAUTH_STATE_INVALID")
                self.idle(connection)
                connection.generation = uuid4()
                connection.google_subject, connection.account_email = subject, email
                connection.credentials_cipher = self.cipher.encrypt(
                    tokens, self.context(connection)
                )
                connection.sync_enabled, connection.error_code = False, None
        except CalendarError:
            self.client.revoke(tokens["refresh"])
            raise

    def cancel_oauth(self, owner, session_hash, state):
        if not state or len(state) > 256:
            return
        with self.session(owner) as session, session.begin():
            session.execute(
                delete(GoogleOAuthState).where(
                    GoogleOAuthState.state_hash == digest(state),
                    GoogleOAuthState.session_hash == session_hash,
                )
            )

    def toggle(self, owner, enabled):
        with self.session(owner) as session, session.begin():
            connection = self.connection(session, lock=True)
            if (
                connection is None
                or not connection.credentials_cipher
                or connection.error_code == "REAUTH_REQUIRED"
            ):
                raise CalendarError("NOT_CONNECTED")
            self.idle(connection)
            connection.sync_enabled = enabled
        # Enabling never selects all local events or reactivates old-generation links.
        return self.status(owner)

    def disconnect(self, owner):
        tokens = None
        with self.session(owner) as session, session.begin():
            connection = self.connection(session, lock=True)
            if connection:
                self.idle(connection)
                if connection.credentials_cipher:
                    try:
                        tokens = self.cipher.decrypt(
                            connection.credentials_cipher, self.context(connection)
                        )
                    except CalendarError:
                        pass
                connection.credentials_cipher = None
                connection.sync_enabled, connection.error_code = False, None
                connection.account_email = None
                connection.generation = (
                    uuid4()
                )  # Also invalidate an already-exchanging OAuth callback.
            session.execute(delete(GoogleOAuthState))
        revoked = self.client.revoke(tokens["refresh"]) if tokens else False
        return {
            "disconnected": True,
            "local_credentials_removed": True,
            "revocation_confirmed": revoked,
            "remote_events_preserved": True,
        }

    def event_status(self, owner, event_id):
        with self.session(owner) as session:
            item = session.get(Event, event_id)
            if item is None:
                raise CalendarError("EVENT_NOT_FOUND")
            connection = self.connection(session)
            link = None
            if connection and connection.google_subject:
                link = session.scalar(
                    select(GoogleEventLink).where(
                        GoogleEventLink.event_id == event_id,
                        GoogleEventLink.google_subject == connection.google_subject,
                    )
                )
            try:
                payload = event_payload(item, self.settings.app_timezone)
                eligible = payload["operation"] != "delete"
                reason = None if eligible else "EVENT_INACTIVE"
            except CalendarError as error:
                eligible, reason = False, error.code
            active = bool(link and connection and link.generation == connection.generation)
            return {
                "event_id": str(event_id),
                "eligible": eligible,
                "reason": reason,
                "status": link.status if active else "not_linked",
                "error_code": link.error_code if active else None,
                "default_duration_minutes": DEFAULT_MINUTES[item.type]
                if item.event_time and item.end_time is None
                else None,
                "recurrence_note": "Только эта дата; серия не экспортируется."
                if item.recurrence_rule or item.recurrence_series_id
                else None,
            }

    def sync_event(self, owner, event_id):
        with self.session(owner) as session, session.begin():
            connection = self.connection(session, lock=True)
            if connection is None or not connection.credentials_cipher:
                raise CalendarError("NOT_CONNECTED")
            if not connection.sync_enabled:
                raise CalendarError("SYNC_DISABLED")
            self.idle(connection)
            item = session.scalar(select(Event).where(Event.id == event_id).with_for_update())
            if item is None:
                raise CalendarError("EVENT_NOT_FOUND")
            link = enqueue(session, connection, item, self.settings.app_timezone, explicit=True)
            if link and link.status == "error":
                link.status, link.error_code = "pending", None
        self.drain(owner, event_id=event_id, limit=1)
        return self.event_status(owner, event_id)

    def retry(self, owner):
        with self.session(owner) as session, session.begin():
            connection = self.connection(session, lock=True)
            if (
                connection is None
                or not connection.sync_enabled
                or not connection.credentials_cipher
            ):
                raise CalendarError("SYNC_DISABLED")
            self.idle(connection)
            rows = session.scalars(
                select(GoogleEventLink)
                .where(
                    GoogleEventLink.generation == connection.generation,
                    GoogleEventLink.status == "error",
                )
                .order_by(GoogleEventLink.updated_at)
                .limit(3)
                .with_for_update()
            )
            for row in rows:
                row.status, row.error_code = "pending", None
        self.drain(owner)
        return self.status(owner)

    def claim(self, owner, event_id=None):
        with self.session(owner) as session, session.begin():
            connection = self.connection(session, lock=True)
            if (
                connection is None
                or not connection.credentials_cipher
                or not connection.sync_enabled
            ):
                return None
            self.idle(connection)
            query = select(GoogleEventLink).where(
                GoogleEventLink.connection_id == connection.id,
                GoogleEventLink.generation == connection.generation,
                GoogleEventLink.status == "pending",
            )
            if event_id:
                query = query.where(GoogleEventLink.event_id == event_id)
            link = session.scalar(
                query.order_by(GoogleEventLink.updated_at, GoogleEventLink.id)
                .limit(1)
                .with_for_update()
            )
            if link is None:
                return None
            lease = uuid4()
            connection.lease_token, connection.lease_until = lease, now() + timedelta(seconds=180)
            return {
                "owner": owner,
                "connection": connection.id,
                "generation": connection.generation,
                "context": self.context(connection),
                "cipher": connection.credentials_cipher,
                "lease": lease,
                "link": link.id,
                "remote_id": link.remote_event_id,
                "revision": link.revision,
                "desired": copy.deepcopy(link.desired),
            }

    def store_tokens(self, claim, tokens):
        with self.session(claim["owner"]) as session, session.begin():
            connection = self.connection(session, lock=True)
            if (
                connection is None
                or connection.generation != claim["generation"]
                or connection.lease_token != claim["lease"]
            ):
                raise CalendarError("SYNC_LEASE_LOST")
            connection.credentials_cipher = self.cipher.encrypt(tokens, claim["context"])

    def deliver(self, claim):
        tokens = self.cipher.decrypt(claim["cipher"], claim["context"])

        def refresh():
            nonlocal tokens
            tokens = self.client.tokens(refresh=tokens["refresh"])
            self.store_tokens(claim, tokens)

        if datetime.fromisoformat(tokens["expires_at"]) <= now() + timedelta(seconds=60):
            refresh()
        refreshed = False

        def request(method, payload=None, etag=None, remote=True):
            nonlocal refreshed
            response = self.client.event_request(
                method,
                claim["remote_id"] if remote else None,
                tokens["access"],
                payload=payload,
                etag=etag,
            )
            if response.status_code == 401 and not refreshed:
                refresh()
                refreshed = True
                response = self.client.event_request(
                    method,
                    claim["remote_id"] if remote else None,
                    tokens["access"],
                    payload=payload,
                    etag=etag,
                )
            return response

        current = request("GET")
        deleting = claim["desired"]["operation"] == "delete"
        marker = str(claim["link"])
        if current.status_code in (404, 410):
            if deleting:
                return
            body = copy.deepcopy(claim["desired"]["payload"])
            body.update(
                id=claim["remote_id"], extendedProperties={"private": {"quicklist_link": marker}}
            )
            result = request("POST", body, remote=False)
            if result.status_code == 409:
                current = request("GET")
            elif result.status_code in (200, 201):
                return
            else:
                raise CalendarError("GOOGLE_SYNC_FAILED")
        if current.status_code != 200:
            raise CalendarError("GOOGLE_SYNC_FAILED")
        try:
            data = current.json()
            if data.get("status") == "cancelled":
                if deleting:
                    return  # Google may retain a tombstone after a successful deletion.
                raise CalendarError("REMOTE_EVENT_DELETED")
            if (
                data.get("extendedProperties", {}).get("private", {}).get("quicklist_link")
                != marker
            ):
                raise CalendarError("REMOTE_ID_CONFLICT")
            etag = data["etag"]
        except (KeyError, ValueError, TypeError):
            raise CalendarError("GOOGLE_RESPONSE_INVALID") from None
        payload = None if deleting else copy.deepcopy(claim["desired"]["payload"])
        if payload:
            for field in ("start", "end"):
                # PATCH preserves omitted nested fields. Clear the old mutually
                # exclusive representation on all-day <-> timed transitions.
                old, new = data.get(field, {}), payload.get(field, {})
                if old.get("date") and new.get("dateTime"):
                    new["date"] = None
                elif old.get("dateTime") and new.get("date"):
                    new.update(dateTime=None, timeZone=None)
        result = request("DELETE" if deleting else "PATCH", payload, etag=etag)
        if result.status_code == 412:
            raise CalendarError("REMOTE_CHANGED_RETRY")
        if result.status_code not in ((200, 204, 404, 410) if deleting else (200,)):
            raise CalendarError("GOOGLE_SYNC_FAILED")

    def finish(self, claim, error=None):
        with self.session(claim["owner"]) as session, session.begin():
            connection = self.connection(session, lock=True)
            if connection is None or connection.lease_token != claim["lease"]:
                return
            link = session.get(GoogleEventLink, claim["link"])
            if link and link.generation == claim["generation"]:
                if link.revision != claim["revision"]:
                    link.status, link.error_code = "pending", None
                else:
                    link.status, link.error_code = ("error", error) if error else ("synced", None)
                    if not error:
                        link.synced_revision = claim["revision"]
            if error == "REAUTH_REQUIRED":
                connection.sync_enabled, connection.error_code = False, error
            connection.lease_token = connection.lease_until = None

    def drain(self, owner, *, event_id=None, limit=3):
        for _ in range(min(limit, 3)):
            try:
                claim = self.claim(owner, event_id)
            except CalendarError as error:
                if error.code == "SYNC_BUSY":
                    return
                raise
            if claim is None:
                return
            error = "SYNC_INTERRUPTED"
            try:
                self.deliver(claim)
            except CalendarError as failure:
                error = failure.code
            except Exception:
                error = "SYNC_INTERNAL_ERROR"
            else:
                error = None
            finally:
                self.finish(claim, error)
