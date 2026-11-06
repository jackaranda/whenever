"""Settings, read from WHENEVER_* environment variables."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path
    database_url: str
    secret_key: str
    # Public base URL (e.g. https://cal.example.org) used when showing
    # connection details in the admin UI. Falls back to the request URL.
    base_url: str = ""
    # URL prefix when served from a sub-path, e.g. "/whenever". Empty when served from the root.
    root_path: str = ""
    # Allow /dav/t/{token}/ URLs that authenticate by the token in the path.
    allow_token_urls: bool = True
    # Mark the admin session cookie Secure (set this when served over HTTPS).
    cookie_secure: bool = False
    max_event_size: int = 256 * 1024
    feed_refresh_minutes: int = 15

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        data_dir = Path(overrides.pop("data_dir", None) or os.environ.get("WHENEVER_DATA_DIR", "./data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        database_url = os.environ.get("WHENEVER_DATABASE_URL") or f"sqlite:///{data_dir / 'whenever.db'}"
        settings = cls(
            data_dir=data_dir,
            database_url=database_url,
            secret_key=os.environ.get("WHENEVER_SECRET_KEY") or _load_or_create_secret(data_dir),
            base_url=os.environ.get("WHENEVER_BASE_URL", "").rstrip("/"),
            root_path=normalize_root_path(os.environ.get("WHENEVER_ROOT_PATH", "")),
            allow_token_urls=_env_bool("WHENEVER_ALLOW_TOKEN_URLS", True),
            cookie_secure=_env_bool("WHENEVER_COOKIE_SECURE", False),
            max_event_size=int(os.environ.get("WHENEVER_MAX_EVENT_SIZE", 256 * 1024)),
            feed_refresh_minutes=int(os.environ.get("WHENEVER_FEED_REFRESH_MINUTES", 15)),
        )
        for key, value in overrides.items():
            setattr(settings, key, value)
        return settings


def normalize_root_path(value: str) -> str:
    """"/whenever/", "whenever" and "/whenever" all become "/whenever"; "/" and "" become ""."""
    value = value.strip().strip("/")
    return f"/{value}" if value else ""


def _load_or_create_secret(data_dir: Path) -> str:
    """Keep a generated session secret next to the database so restarts don't log admins out."""
    path = data_dir / "secret_key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(48)
    path.write_text(key)
    path.chmod(0o600)
    return key
