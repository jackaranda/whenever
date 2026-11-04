"""Password hashing, tokens and HTTP Basic authentication."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    return hmac.compare_digest(candidate, bytes.fromhex(digest))


class _VerifiedCache:
    """Remembers recent successful Basic-auth checks.

    Calendar clients send credentials on every request and scrypt is slow on
    purpose, so successful checks are cached for a short time. The key
    includes the stored hash, so changing the password invalidates the entry.
    """

    def __init__(self, ttl: float = 300.0, max_entries: int = 1024):
        self.ttl = ttl
        self.max_entries = max_entries
        self._entries: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(username: str, password: str, stored: str) -> str:
        return hashlib.sha256(f"{username}\0{password}\0{stored}".encode()).hexdigest()

    def hit(self, username: str, password: str, stored: str) -> bool:
        key = self._key(username, password, stored)
        with self._lock:
            expiry = self._entries.get(key)
            if expiry is None:
                return False
            if expiry < time.monotonic():
                del self._entries[key]
                return False
            return True

    def add(self, username: str, password: str, stored: str) -> None:
        with self._lock:
            if len(self._entries) >= self.max_entries:
                self._entries.clear()
            self._entries[self._key(username, password, stored)] = time.monotonic() + self.ttl


_verified = _VerifiedCache()


def check_user_secret(username: str, secret: str, password_hash: str | None, token: str) -> bool:
    """Accept either the account password or the user's personal token."""
    if hmac.compare_digest(secret.encode(), token.encode()):
        return True
    if not password_hash:
        return False
    if _verified.hit(username, secret, password_hash):
        return True
    if verify_password(secret, password_hash):
        _verified.add(username, secret, password_hash)
        return True
    return False


def parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    username, sep, password = decoded.partition(":")
    if not sep:
        return None
    return username, password
