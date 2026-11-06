from __future__ import annotations

import base64
import warnings
from dataclasses import dataclass

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient  # noqa: E402

from whenever.config import Settings  # noqa: E402
from whenever.main import create_app  # noqa: E402
from whenever.models import Calendar, Membership, User  # noqa: E402
from whenever.security import hash_password  # noqa: E402


@dataclass
class Seed:
    alice: User  # admin, writer
    bob: User  # writer
    carol: User  # read-only member, no password
    dave: User  # not a member
    team: Calendar


@pytest.fixture
def app(tmp_path):
    return create_app(Settings.from_env(data_dir=tmp_path))


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def seed(app) -> Seed:
    with app.state.sessionmaker() as db:
        alice = User(username="alice", display_name="Alice", email="alice@example.org",
                     password_hash=hash_password("alicepw"), is_admin=True)
        bob = User(username="bob", display_name="Bob", password_hash=hash_password("bobpw"))
        carol = User(username="carol", display_name="Carol")
        dave = User(username="dave", display_name="Dave", password_hash=hash_password("davepw"))
        team = Calendar(slug="team", name="Team", timezone="Africa/Johannesburg")
        db.add_all([alice, bob, carol, dave, team])
        db.flush()
        db.add_all([
            Membership(user_id=alice.id, calendar_id=team.id, can_write=True),
            Membership(user_id=bob.id, calendar_id=team.id, can_write=True),
            Membership(user_id=carol.id, calendar_id=team.id, can_write=False),
        ])
        db.commit()
        return Seed(alice, bob, carol, dave, team)


def basic(username: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()}


def make_ics(uid: str, summary: str = "Standup", start: str = "20261110T090000Z",
             end: str = "20261110T093000Z", description: str | None = None, extra: str = "") -> bytes:
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//test//EN",
        "BEGIN:VEVENT", f"UID:{uid}", "DTSTAMP:20261101T000000Z",
        f"DTSTART:{start}", f"DTEND:{end}", f"SUMMARY:{summary}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{description}")
    if extra:
        lines.extend(extra.split("\n"))
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(lines).encode()
