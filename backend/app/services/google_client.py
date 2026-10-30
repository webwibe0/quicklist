"""Pinned Google endpoints, no SDK retries, redacted errors. Never log responses/credentials."""

import base64
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SCOPES = "openid email https://www.googleapis.com/auth/calendar.events.owned"
TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


class CalendarError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class TokenCipher:
    def __init__(self, key):
        try:
            raw = base64.urlsafe_b64decode(key)
            if len(raw) != 32:
                raise ValueError()
            self.aes = AESGCM(raw)
        except (ValueError, TypeError):
            raise CalendarError("TOKEN_KEY_INVALID") from None

    def encrypt(self, value, context):
        nonce = os.urandom(12)
        data = self.aes.encrypt(nonce, json.dumps(value).encode(), context.encode())
        return base64.urlsafe_b64encode(nonce + data).decode()

    def decrypt(self, value, context):
        try:
            data = base64.urlsafe_b64decode(value)
            return json.loads(self.aes.decrypt(data[:12], data[12:], context.encode()))
        except Exception:
            raise CalendarError("TOKEN_DECRYPT_FAILED") from None


class GoogleClient:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.http = httpx.Client(timeout=5, transport=transport, follow_redirects=False)

    def close(self):
        self.http.close()

    def authorization_url(self, state, verifier):
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
            {
                "client_id": self.settings.google_client_id,
                "redirect_uri": self.settings.google_redirect_uri,
                "response_type": "code",
                "scope": SCOPES,
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )

    def tokens(self, code=None, verifier=None, refresh=None):
        data = {
            "client_id": self.settings.google_client_id,
            "client_secret": self.settings.google_client_secret.get_secret_value(),
        }
        if refresh:
            data.update(grant_type="refresh_token", refresh_token=refresh)
        else:
            data.update(
                grant_type="authorization_code",
                code=code,
                code_verifier=verifier,
                redirect_uri=self.settings.google_redirect_uri,
            )
        response = self.request("POST", TOKEN_URL, data=data)
        if response.status_code in (400, 401):
            raise CalendarError("REAUTH_REQUIRED")
        if response.status_code != 200:
            raise CalendarError("GOOGLE_TOKEN_UNAVAILABLE")
        try:
            result = response.json()
            access = result["access_token"]
            refresh = result.get("refresh_token") or refresh
            expires = int(result["expires_in"])
            if not access or not refresh or expires <= 0:
                raise ValueError()
            granted = result.get("scope")
            if (
                granted
                and "https://www.googleapis.com/auth/calendar.events.owned" not in granted.split()
            ):
                raise CalendarError("CALENDAR_SCOPE_REQUIRED")
            return {
                "access": access,
                "refresh": refresh,
                "expires_at": (datetime.now(UTC) + timedelta(seconds=expires)).isoformat(),
            }
        except (ValueError, KeyError, TypeError):
            raise CalendarError("GOOGLE_TOKEN_INVALID") from None

    def identity(self, access):
        response = self.request(
            "GET",
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": "Bearer " + access},
        )
        try:
            data = response.json()
            if (
                response.status_code != 200
                or not data.get("sub")
                or not data.get("email")
                or data.get("email_verified") is not True
            ):
                raise ValueError()
            return data["sub"], data["email"]
        except (ValueError, TypeError):
            raise CalendarError("GOOGLE_IDENTITY_INVALID") from None

    def request(self, method, url, **kwargs):
        try:
            return self.http.request(method, url, **kwargs)
        except httpx.RequestError:
            raise CalendarError("GOOGLE_UNAVAILABLE") from None

    def revoke(self, token):
        try:
            return (
                self.request(
                    "POST", "https://oauth2.googleapis.com/revoke", data={"token": token}
                ).status_code
                == 200
            )
        except CalendarError:
            return False

    def event_request(self, method, remote_id, access, payload=None, etag=None):
        # IDs are generated and stored by QuickList; never accept a remote URL from clients.
        if remote_id and (
            len(remote_id) > 1024
            or any(c not in "0123456789abcdefghijklmnopqrstuv" for c in remote_id)
        ):
            raise CalendarError("REMOTE_ID_INVALID")
        headers = {"Authorization": "Bearer " + access}
        if etag:
            headers["If-Match"] = etag
        return self.request(
            method,
            EVENTS_URL + ("/" + remote_id if remote_id else ""),
            headers=headers,
            json=payload,
            params={"sendUpdates": "none"} if method != "GET" else None,
        )
