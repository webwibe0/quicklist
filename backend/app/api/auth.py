import re
from threading import BoundedSemaphore

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.passwords import DUMMY_HASH, hash_password, verify_password
from app.db import session as database
from app.db.ownership import bind_owner
from app.models.accounts import AuthSession, User
from app.services.account_mail import get_mail
from app.services.authentication import (
    Identity,
    check_origin,
    clear_account_attempts,
    csrf_cookie,
    digest,
    issue,
    lookup_session,
    now,
    require_account,
    reserve_attempt,
    reserve_window,
    session_cookie,
)
from app.services.dataset import ensure_original_dataset

router = APIRouter(prefix="/api/v1/auth", tags=["accounts"])
password_slots = BoundedSemaphore(2)


def initialize_usable_account(session,user):
    settings=get_settings()
    if user.email_verified_at or user.is_legacy_owner or not settings.auth_require_email_verification:
        bind_owner(session,user.id)
        try:
            ensure_original_dataset(session)
        except (OSError,ValueError):
            raise HTTPException(503,'Не удалось подготовить исходные данные. Повторите вход позже.') from None


def password_work(operation):
    if get_settings().app_environment != 'local':
        reserve_window('password:global', 60, 60)
    if not password_slots.acquire(blocking=False):
        raise HTTPException(
            429,
            "Сервис входа занят. Повторите через несколько секунд.",
            headers={"Retry-After": "2"},
        )
    try:
        return operation()
    finally:
        password_slots.release()


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    password: SecretStr

    @field_validator("email")
    @classmethod
    def email_address(cls, value):
        value = value.strip().casefold()
        if len(value) > 254 or not re.fullmatch(
            r"[a-z0-9][a-z0-9._%+\-]{0,63}@[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?"
            r"(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)+",
            value,
        ):
            raise ValueError("Укажите корректный email.")
        return value

    @field_validator("password")
    @classmethod
    def password_length(cls, value):
        if not 12 <= len(value.get_secret_value()) <= 128:
            raise ValueError("Пароль должен содержать от 12 до 128 символов.")
        return value


def public_user(user):
    verified = getattr(user, 'email_verified', getattr(user, 'email_verified_at', None) is not None)
    allowed = verified or user.is_legacy_owner or not get_settings().auth_require_email_verification
    return {"id": str(user.id), "email": user.email, "is_legacy_owner": user.is_legacy_owner,
            "email_verified": verified, "access_allowed": allowed}


@router.get("/csrf")
def csrf(request: Request, response: Response):
    if request.headers.get("origin") or request.headers.get("sec-fetch-site") == "cross-site":
        check_origin(request)
    if get_settings().app_environment != 'local':
        reserve_window('csrf:ip:' + digest(request.client.host if request.client else 'unknown'),
                       get_settings().auth_csrf_ip_limit, 900)
    with database.SessionLocal.begin() as session:
        if get_settings().app_environment != 'local':
            expired = select(AuthSession.token_hash).where(AuthSession.expires_at <= now()).limit(100)
            session.execute(delete(AuthSession).where(AuthSession.token_hash.in_(expired)))
        existing = None
        try:
            existing = lookup_session(session, request)
        except HTTPException as error:
            if error.status_code != 401:
                raise
        value = request.cookies.get(csrf_cookie(), "")
        if existing and value and len(value) <= 128 and digest(value) == existing.csrf_hash:
            response.headers["Cache-Control"] = "no-store"
            return {"csrf_token": value}
        if existing:
            # A lost CSRF cookie rotates only this browser session, never user identity.
            session.delete(existing)
        token = issue(session, response, existing.user_id if existing else None)
    return {"csrf_token": token}


@router.get("/me")
def me(user: Identity = Depends(require_account)):
    return public_user(user)


@router.post("/register", status_code=201)
def register(body: Credentials, request: Request, response: Response, background_tasks: BackgroundTasks, mail=Depends(get_mail)):
    with database.SessionLocal() as session:
        previous = lookup_session(session, request, mutation=True)
        old_hash, old_csrf = previous.token_hash, previous.csrf_hash
    if not get_settings().auth_registration_enabled:
        raise HTTPException(403, 'Регистрация пока закрыта.')
    reserve_attempt(request, body.email, registration=True)
    if get_settings().auth_require_email_verification:
        reserve_window('register:email:' + digest(body.email), 1, 60)
    encoded = password_work(lambda: hash_password(body.password.get_secret_value()))
    try:
        with database.SessionLocal.begin() as session:
            row = lookup_session(session, request, mutation=True)
            if row.token_hash != old_hash or row.csrf_hash != old_csrf:
                raise HTTPException(403, "Сессия изменилась. Обновите страницу.")
            user = User(email=body.email, password_hash=encoded)
            session.add(user)
            session.flush()
            result = public_user(user)
            if not get_settings().auth_require_email_verification:
                initialize_usable_account(session,user)
                session.delete(row)
                issue(session, response, user.id)
    except IntegrityError:
        if not get_settings().auth_require_email_verification:
            raise HTTPException(409, "Не удалось зарегистрировать этот email. Используйте вход.") from None
    if get_settings().auth_require_email_verification:
        from app.api.account_recovery import GENERIC, issue_link
        background_tasks.add_task(issue_link, body.email, 'verify', mail)
        return {**GENERIC, 'verification_required': True}
    return result


@router.post("/login")
def login(body: Credentials, request: Request, response: Response):
    with database.SessionLocal() as session:
        lookup_session(session, request, mutation=True)
        user = session.scalar(select(User).where(User.email == body.email))
        encoded = user.password_hash if user and user.password_hash else DUMMY_HASH
        identity = user.id if user else None
        has_password = bool(user and user.password_hash)
    reserve_attempt(request, body.email)
    valid = password_work(lambda: verify_password(body.password.get_secret_value(), encoded))
    if not valid or not has_password:
        raise HTTPException(401, "Неверный email или пароль.")
    with database.SessionLocal.begin() as session:
        previous = lookup_session(session, request, mutation=True)
        # Serializes session issuance with password reset's user lock: a login
        # verified against the previous password cannot survive a completed reset.
        current = session.scalar(select(User).where(User.id == identity).with_for_update())
        if current is None or current.password_hash != encoded:
            raise HTTPException(401, "Повторите вход.")
        initialize_usable_account(session,current)
        result = public_user(current)
        session.delete(previous)
        issue(session, response, current.id)
        clear_account_attempts(session, body.email)
    return result


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response):
    with database.SessionLocal.begin() as session:
        previous = lookup_session(session, request, mutation=True)
        session.execute(delete(AuthSession).where(AuthSession.token_hash == previous.token_hash))
    for name in (session_cookie(), csrf_cookie()):
        response.delete_cookie(
            name,
            path="/",
            secure=name.startswith("__Host-"),
            httponly=name == session_cookie(),
            samesite="lax",
        )
