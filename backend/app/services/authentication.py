"""Opaque DB sessions, synchronizer CSRF and atomic PostgreSQL login throttles."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import case, delete, select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.db import session as database
from app.models.accounts import AuthSession, AuthThrottle, User

SESSION_COOKIE = "quicklist_session"
CSRF_COOKIE = "quicklist_csrf"


def session_cookie():
    return ("__Host-" if get_settings().auth_cookie_secure else "") + SESSION_COOKIE


def csrf_cookie():
    return ("__Host-" if get_settings().auth_cookie_secure else "") + CSRF_COOKIE


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def now():
    return datetime.now(UTC)


@dataclass(frozen=True)
class Identity:
    id: UUID
    email: str
    is_legacy_owner: bool = False
    email_verified: bool = False
    access_allowed: bool = True


def check_origin(request):
    if (
        request.headers.get("origin") != get_settings().auth_origin
        or request.headers.get("sec-fetch-site") == "cross-site"
    ):
        raise HTTPException(403, "Запрос с этого сайта не разрешён.")


def token_hash(request):
    token = request.cookies.get(session_cookie(), "")
    if not token or len(token) > 128:
        raise HTTPException(401, "Войдите в аккаунт.")
    return digest(token)


def lookup_session(session, request, *, mutation=False):
    if mutation:
        check_origin(request)
    row = session.scalar(
        select(AuthSession).where(
            AuthSession.token_hash == token_hash(request),
            AuthSession.expires_at > now(),
        )
    )
    if row is None:
        raise HTTPException(401, "Сессия завершена. Войдите снова.")
    if mutation:
        csrf = request.headers.get("x-csrf-token", "")
        cookie = request.cookies.get(csrf_cookie(), "")
        if (
            not csrf
            or len(csrf) > 128
            or not hmac.compare_digest(csrf, cookie)
            or not hmac.compare_digest(digest(csrf), row.csrf_hash)
        ):
            raise HTTPException(403, "Обновите страницу и повторите запрос.")
    return row


def require_account(request: Request):
    # Separate short session: endpoint transaction must start AFTER auth lookup closes.
    with database.SessionLocal() as session:
        row = lookup_session(
            session, request, mutation=request.method not in ("GET", "HEAD", "OPTIONS")
        )
        user = session.get(User, row.user_id) if row.user_id else None
        if user is None:
            raise HTTPException(401, "Войдите в аккаунт.")
        verified = user.email_verified_at is not None
        allowed = verified or user.is_legacy_owner or not get_settings().auth_require_email_verification
        return Identity(user.id, user.email, user.is_legacy_owner, verified, allowed)


def require_user(request: Request):
    user = require_account(request)
    if not user.access_allowed:
        raise HTTPException(403, 'Подтвердите email, чтобы пользоваться QuickList.')
    if get_settings().app_environment != 'local' and request.url.path.startswith(('/api/v1/voice/', '/api/v1/assistant/messages')):
        reserve_window('paid:user:' + str(user.id), 60, 900)
        reserve_window('paid:ip:' + digest(request.client.host if request.client else 'unknown'), 120, 900)
    return user


def reserve_window(key, limit, seconds):
    """Database clock + atomic upsert: shared by processes, not an in-memory counter."""
    from sqlalchemy import func
    with database.SessionLocal.begin() as session:
        current = session.scalar(select(func.clock_timestamp()))
        expired = AuthThrottle.expires_at <= current
        attempts = session.scalar(insert(AuthThrottle).values(
            key=key, attempts=1, expires_at=current + timedelta(seconds=seconds)
        ).on_conflict_do_update(index_elements=[AuthThrottle.key], set_={
            'attempts': case((expired, 1), else_=AuthThrottle.attempts + 1),
            'expires_at': case((expired, current + timedelta(seconds=seconds)), else_=AuthThrottle.expires_at),
        }).returning(AuthThrottle.attempts))
    if attempts > limit:
        raise HTTPException(429, 'Слишком много запросов. Повторите позже.', headers={'Retry-After': str(seconds)})


def issue(session, response, user_id=None):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    ttl = get_settings().auth_session_days * 86400 if user_id else 600
    session.add(
        AuthSession(
            token_hash=digest(token),
            csrf_hash=digest(csrf),
            user_id=user_id,
            expires_at=now() + timedelta(seconds=ttl),
        )
    )
    secure = get_settings().auth_cookie_secure
    response.set_cookie(
        session_cookie(), token, max_age=ttl, httponly=True, secure=secure, samesite="lax", path="/"
    )
    response.set_cookie(
        csrf_cookie(), csrf, max_age=ttl, httponly=False, secure=secure, samesite="lax", path="/"
    )
    response.headers["Cache-Control"] = "no-store"
    return csrf


def reserve_attempt(request, email, *, registration=False):
    current = now()
    settings = get_settings()
    prefix = "register" if registration else "login"
    limits = [
        (
            prefix + ":ip:" + digest(request.client.host if request.client else "unknown"),
            5 if registration else settings.auth_login_ip_limit,
        )
    ]
    if not registration:
        limits.append((prefix + ":email:" + digest(email), settings.auth_login_account_limit))
    retry_after = 0
    with database.SessionLocal.begin() as session:
        for key, limit in limits:
            expired = AuthThrottle.expires_at <= current
            statement = (
                insert(AuthThrottle)
                .values(key=key, attempts=1, expires_at=current + timedelta(minutes=15))
                .on_conflict_do_update(
                    index_elements=[AuthThrottle.key],
                    set_={
                        "attempts": case((expired, 1), else_=AuthThrottle.attempts + 1),
                        "expires_at": case(
                            (expired, current + timedelta(minutes=15)),
                            else_=AuthThrottle.expires_at,
                        ),
                    },
                )
                .returning(AuthThrottle.attempts, AuthThrottle.expires_at)
            )
            attempts, expires_at = session.execute(statement).one()
            if attempts > limit:
                retry_after = max(retry_after, 1, ceil((expires_at - current).total_seconds()))
    if retry_after:
        raise HTTPException(
            429, f"Слишком много попыток. Повторите через {ceil(retry_after / 60)} мин.",
            headers={"Retry-After": str(retry_after)},
        )


def clear_account_attempts(session, email):
    session.execute(delete(AuthThrottle).where(AuthThrottle.key == "login:email:" + digest(email)))
