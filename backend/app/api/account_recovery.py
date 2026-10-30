"""One-use, hashed, expiring verification/recovery tokens. No token-bearing GET API."""
import logging
import secrets
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import delete, select

from app.api.auth import Credentials, password_work
from app.core.config import get_settings
from app.core.passwords import hash_password
from app.db import session as database
from app.models.accounts import AccountToken, AuthSession, User
from app.services.account_mail import MailUnavailable, get_mail
from app.services.authentication import (
    clear_account_attempts,
    digest,
    lookup_session,
    now,
    reserve_window,
)

router = APIRouter(prefix='/api/v1/auth', tags=['account-security'])
logger = logging.getLogger(__name__)
GENERIC = {'message': 'Если адрес подходит для этой операции, письмо будет отправлено. Проверьте почту и папку «Спам».'}


class EmailRequest(BaseModel):
    email: str

    @field_validator('email')
    @classmethod
    def address(cls, value):
        return Credentials.email_address(value)


class TokenRequest(BaseModel):
    token: str = Field(min_length=43, max_length=43, pattern=r'^[A-Za-z0-9_-]+$')


class ResetRequest(TokenRequest):
    password: SecretStr

    @field_validator('password')
    @classmethod
    def length(cls, value):
        return Credentials.password_length(value)


def throttle(request, purpose, email=''):
    ip = request.client.host if request.client else 'unknown'
    reserve_window(f'account:{purpose}:ip:{digest(ip)}', 20, 900)
    if email:
        reserve_window(f'account:{purpose}:email:{digest(email)}', 1, 60)


def checked_request(request):
    with database.SessionLocal() as session:
        lookup_session(session, request, mutation=True)


def send(mail, recipient, subject, body):
    try:
        mail.send(recipient, subject, body)
    except MailUnavailable:
        # Same public response for unknown address and delivery failures; no automatic
        # timeout retry. User may request another link after the shared cooldown.
        logger.warning('Account mail delivery unconfirmed; no automatic retry')


def issue_link(email, purpose, mail):
    raw = secrets.token_urlsafe(32)
    with database.SessionLocal.begin() as session:
        user = session.scalar(select(User).where(User.email == email).with_for_update())
        if user is None or (purpose == 'verify' and user.email_verified_at is not None):
            return
        # Latest request invalidates earlier links, even if SMTP outcome was unknown.
        session.execute(delete(AccountToken).where(AccountToken.user_id == user.id, AccountToken.purpose == purpose))
        minutes = 30 if purpose == 'reset' else 60
        session.add(AccountToken(token_hash=digest(raw), user_id=user.id, purpose=purpose,
                                 expires_at=now() + timedelta(minutes=minutes)))
    path = 'reset-password' if purpose == 'reset' else 'verify-email'
    subject = 'QuickList — восстановление пароля' if purpose == 'reset' else 'QuickList — подтвердите email'
    body = (f'{subject}\n\n{get_settings().auth_origin}/{path}#token={raw}\n\n'
            f'Ссылка действует {minutes} минут и только один раз. Если вы не запрашивали это действие, проигнорируйте письмо.\n')
    send(mail, email, subject, body)


@router.post('/resend-verification')
def resend(body: EmailRequest, request: Request, background_tasks: BackgroundTasks, mail=Depends(get_mail)):
    checked_request(request)
    throttle(request, 'verify', body.email)
    background_tasks.add_task(issue_link, body.email, 'verify', mail)
    return GENERIC


@router.post('/forgot-password')
def forgot(body: EmailRequest, request: Request, background_tasks: BackgroundTasks, mail=Depends(get_mail)):
    checked_request(request)
    throttle(request, 'reset', body.email)
    background_tasks.add_task(issue_link, body.email, 'reset', mail)
    return GENERIC


def token_user(session, raw, purpose):
    owner = session.scalar(select(AccountToken.user_id).where(AccountToken.token_hash == digest(raw)))
    if owner is None:
        raise HTTPException(400, 'Ссылка недействительна или истекла. Запросите новое письмо.')
    user = session.scalar(select(User).where(User.id == owner).with_for_update())
    token = session.scalar(select(AccountToken).where(AccountToken.token_hash == digest(raw),
        AccountToken.purpose == purpose).with_for_update())
    if token is None or token.used_at is not None or token.expires_at <= now():
        raise HTTPException(400, 'Ссылка недействительна или истекла. Запросите новое письмо.')
    token.used_at = now()
    return user


@router.post('/verify-email')
def verify(body: TokenRequest, request: Request):
    checked_request(request)
    throttle(request, 'consume')
    with database.SessionLocal.begin() as session:
        user = token_user(session, body.token, 'verify')
        user.email_verified_at = now()
    return {'message': 'Email подтверждён. Теперь можно войти.'}


@router.post('/reset-password')
def reset(body: ResetRequest, request: Request, response: Response, background_tasks: BackgroundTasks, mail=Depends(get_mail)):
    checked_request(request)
    throttle(request, 'consume')
    encoded = password_work(lambda: hash_password(body.password.get_secret_value()))
    with database.SessionLocal.begin() as session:
        user = token_user(session, body.token, 'reset')
        user.password_hash = encoded
        # A confirmed, one-use reset clears this account's old login lock only.
        # Independent IP/global abuse limits remain in force.
        clear_account_attempts(session, user.email)
        # Includes the current session. Reset never automatically signs a user in.
        session.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
        session.execute(delete(AccountToken).where(AccountToken.user_id == user.id))
        email = user.email
    from app.services.authentication import csrf_cookie, session_cookie
    for name in (csrf_cookie(), session_cookie()):
        response.delete_cookie(name, path='/', secure=name.startswith('__Host-'), samesite='lax',
                               httponly=name == session_cookie())
    background_tasks.add_task(send, mail, email, 'QuickList — пароль изменён',
         'Пароль QuickList изменён. Все прежние сессии завершены. Если это были не вы, воспользуйтесь восстановлением пароля и обратитесь в поддержку.')
    return {'message': 'Пароль изменён. Войдите снова.'}
