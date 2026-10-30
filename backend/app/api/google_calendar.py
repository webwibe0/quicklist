"""Minimal Google controls; callback codes removed from ASGI/access-log query strings."""

from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict

from app.core.config import get_settings
from app.db import session as database
from app.services.authentication import Identity, require_user, token_hash
from app.services.google_calendar import GoogleCalendarService
from app.services.google_client import CalendarError, GoogleClient

CALLBACK = "/api/v1/calendar/google/callback"
router = APIRouter(prefix="/api/v1/calendar/google", tags=["google-calendar"])


class RedactOAuthQuery:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == CALLBACK:
            raw_query = scope.get("query_string", b"")
            try:
                parsed = (
                    parse_qs(raw_query.decode("ascii", errors="ignore"), max_num_fields=8)
                    if len(raw_query) <= 12000
                    else {}
                )
            except ValueError:
                parsed = {}
            scope.setdefault("state", {})["google_oauth_params"] = {
                key: values[0]
                for key, values in parsed.items()
                if key in {"state", "code", "error"} and len(values) == 1
            }
            # Uvicorn's access logger sees this same scope. Tokens/codes never enter log URLs.
            scope["query_string"] = b""
        await self.app(scope, receive, send)


def configured(settings):
    return bool(
        settings.google_calendar_enabled
        and settings.google_client_id
        and settings.google_client_secret.get_secret_value()
        and settings.google_token_key.get_secret_value()
    )


def get_google_client():
    client = GoogleClient(get_settings())
    try:
        yield client
    finally:
        client.close()


def service(client):
    settings = get_settings()
    if not configured(settings):
        raise HTTPException(503, "Google Calendar ещё не настроен на сервере.")
    return GoogleCalendarService(database.SessionLocal, client, settings)


def failure(error):
    code = error.code
    status = (
        404
        if code == "EVENT_NOT_FOUND"
        else 409
        if code in {"SYNC_BUSY", "ALREADY_CONNECTED", "SYNC_DISABLED", "NOT_CONNECTED"}
        else 400
    )
    raise HTTPException(
        status,
        {
            "code": code,
            "message": "Google Calendar: операция не выполнена. Локальные данные сохранены.",
        },
    )


@router.get("/status")
def status(user: Identity = Depends(require_user), client=Depends(get_google_client)):
    if not configured(get_settings()):
        return {"configured": False, "connected": False, "sync_enabled": False}
    try:
        return service(client).status(user.id)
    except CalendarError as error:
        failure(error)


@router.post("/connect")
def connect(
    request: Request, user: Identity = Depends(require_user), client=Depends(get_google_client)
):
    try:
        return {"authorization_url": service(client).connect(user.id, token_hash(request))}
    except CalendarError as error:
        failure(error)


@router.get("/callback")
def callback(
    request: Request, user: Identity = Depends(require_user), client=Depends(get_google_client)
):
    params = getattr(request.state, "google_oauth_params", {})
    outcome = "connected"
    try:
        if params.get("error"):
            service(client).cancel_oauth(user.id, token_hash(request), params.get("state"))
            raise CalendarError("OAUTH_CANCELLED")
        service(client).callback(
            user.id, token_hash(request), params.get("state"), params.get("code")
        )
    except CalendarError as error:
        outcome = error.code
    response = RedirectResponse(
        get_settings().auth_origin + "/?calendar=" + outcome, status_code=303
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


class Toggle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


@router.post("/sync")
def toggle(
    body: Toggle,
    background_tasks: BackgroundTasks,
    user: Identity = Depends(require_user),
    client=Depends(get_google_client),
):
    try:
        result = service(client).toggle(user.id, body.enabled)
        if body.enabled:
            background_tasks.add_task(drain_background, user.id)
        return result
    except CalendarError as error:
        failure(error)


@router.post("/disconnect")
def disconnect(user: Identity = Depends(require_user), client=Depends(get_google_client)):
    try:
        return service(client).disconnect(user.id)
    except CalendarError as error:
        failure(error)


@router.post("/retry")
def retry(user: Identity = Depends(require_user), client=Depends(get_google_client)):
    try:
        return service(client).retry(user.id)
    except CalendarError as error:
        failure(error)


@router.get("/events/{event_id}")
def event_status(
    event_id: UUID, user: Identity = Depends(require_user), client=Depends(get_google_client)
):
    try:
        return service(client).event_status(user.id, event_id)
    except CalendarError as error:
        failure(error)


@router.post("/events/{event_id}/sync")
def sync_event(
    event_id: UUID, user: Identity = Depends(require_user), client=Depends(get_google_client)
):
    try:
        return service(client).sync_event(user.id, event_id)
    except CalendarError as error:
        failure(error)


def capture(session, settings):
    session.info["calendar_capture"] = configured(settings)
    session.info["calendar_timezone"] = settings.app_timezone


def drain_background(owner):
    settings = get_settings()
    if configured(settings):
        client = GoogleClient(settings)
        try:
            GoogleCalendarService(database.SessionLocal, client, settings).drain(owner)
        except Exception:
            # The local event is already committed. Durable pending work is retried explicitly;
            # never turn a successful local command into a failed HTTP response here.
            import logging

            logging.getLogger(__name__).warning(
                "Google Calendar delivery deferred; local event committed"
            )
        finally:
            client.close()
