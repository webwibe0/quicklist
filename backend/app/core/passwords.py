"""OWASP scrypt work factor, random per-password salts, no additional dependency."""

import base64
import hashlib
import hmac
import secrets


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 128:
        raise ValueError("Пароль должен содержать от 12 до 128 символов.")
    salt = secrets.token_bytes(16)
    value = hashlib.scrypt(
        password.encode(), salt=salt, n=2**17, r=8, p=1, maxmem=256 * 1024 * 1024, dklen=64
    )
    return (
        "scrypt$131072$8$1$"
        + base64.b64encode(salt).decode()
        + "$"
        + base64.b64encode(value).decode()
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = encoded.split("$")
        if (scheme, n, r, p) != ("scrypt", "131072", "8", "1") or not 12 <= len(password) <= 128:
            return False
        salt, expected = (
            base64.b64decode(salt, validate=True),
            base64.b64decode(expected, validate=True),
        )
        if len(salt) != 16 or len(expected) != 64:
            return False
        actual = hashlib.scrypt(
            password.encode(), salt=salt, n=2**17, r=8, p=1, maxmem=256 * 1024 * 1024, dklen=64
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# Unknown accounts and reserved owners still pay the same password-check cost.
DUMMY_HASH = hash_password(secrets.token_urlsafe(32))
