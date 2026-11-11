"""Shareable per-calendar feeds: /feed/c/{feed_token}.ics"""

import sqlite3

from icalendar import Calendar as ICalendar
from sqlalchemy import select

from whenever.config import Settings
from whenever.main import create_app
from whenever.models import Calendar

from .conftest import basic, make_ics
from .test_admin import csrf, login


def set_feed(client, seed, action):
    login(client)
    token = csrf(client, "/admin/users")
    return client.post(f"/admin/calendars/{seed.team.id}/feed", data={"action": action, "csrf": token})


def feed_token(app, seed):
    with app.state.sessionmaker() as db:
        return db.get(Calendar, seed.team.id).feed_token


def test_off_by_default(client, seed, app):
    assert feed_token(app, seed) is None
    page = (login(client), client.get(f"/admin/calendars/{seed.team.id}"))[1].text
    assert "Turn on shareable feed" in page


def test_enable_serves_feed_without_an_account(client, seed, app):
    client.put("/dav/u/bob/team/e1.ics", headers=basic("bob", "bobpw"), content=make_ics("e1", summary="Planning"))
    r = set_feed(client, seed, "enable")
    assert "Shareable feed turned on" in r.text
    token = feed_token(app, seed)
    assert token and f"/feed/c/{token}.ics" in r.text and f"webcal://testserver/feed/c/{token}.ics" in r.text

    client.cookies.clear()  # an anonymous subscriber
    r = client.get(f"/feed/c/{token}.ics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    cal = ICalendar.from_ical(r.content)
    assert str(cal["X-WR-CALNAME"]) == "Team"
    [ev] = cal.walk("VEVENT")
    assert str(ev["SUMMARY"]) == "Planning"
    assert str(ev["DESCRIPTION"]) == "— Created by Bob"

    # conditional requests work as for member feeds
    etag = r.headers["etag"]
    assert client.get(f"/feed/c/{token}.ics", headers={"If-None-Match": etag}).status_code == 304
    client.put("/dav/u/bob/team/e2.ics", headers=basic("bob", "bobpw"), content=make_ics("e2"))
    assert client.get(f"/feed/c/{token}.ics", headers={"If-None-Match": etag}).status_code == 200


def test_enable_twice_keeps_link(client, seed, app):
    set_feed(client, seed, "enable")
    first = feed_token(app, seed)
    set_feed(client, seed, "enable")
    assert feed_token(app, seed) == first


def test_rotate_and_disable(client, seed, app):
    set_feed(client, seed, "enable")
    old = feed_token(app, seed)
    set_feed(client, seed, "rotate")
    new = feed_token(app, seed)
    assert new != old
    assert client.get(f"/feed/c/{old}.ics").status_code == 404
    assert client.get(f"/feed/c/{new}.ics").status_code == 200

    set_feed(client, seed, "disable")
    assert feed_token(app, seed) is None
    assert client.get(f"/feed/c/{new}.ics").status_code == 404


def test_unknown_link_and_action(client, seed):
    assert client.get("/feed/c/nope.ics").status_code == 404
    assert client.get("/feed/c/.ics").status_code == 404
    assert set_feed(client, seed, "explode").status_code == 400


def test_member_feeds_still_work(client, seed):
    # /feed/c/... is matched first, but the member feed URL pattern must still resolve.
    assert client.get(f"/feed/{seed.carol.token}/team.ics").status_code == 200


def test_calendar_list_shows_badge(client, seed):
    set_feed(client, seed, "enable")
    assert "shared feed" in client.get("/admin/calendars").text


def test_existing_database_gets_the_new_column(tmp_path):
    # A database created before feed_token existed.
    db_file = tmp_path / "whenever.db"
    con = sqlite3.connect(db_file)
    con.executescript("""
        CREATE TABLE calendars (
            id INTEGER PRIMARY KEY, slug VARCHAR(64) UNIQUE NOT NULL, name VARCHAR(128) NOT NULL,
            description TEXT NOT NULL, color VARCHAR(16) NOT NULL, timezone VARCHAR(64) NOT NULL,
            prefix_titles BOOLEAN NOT NULL, sync_seq INTEGER NOT NULL, created_at DATETIME NOT NULL);
        INSERT INTO calendars VALUES (1, 'old', 'Old', '', '#3a87ad', 'UTC', 0, 0, '2026-01-01 00:00:00');
    """)
    con.commit()
    con.close()

    app = create_app(Settings.from_env(data_dir=tmp_path))
    with app.state.sessionmaker() as db:
        cal = db.scalar(select(Calendar).where(Calendar.slug == "old"))
        assert cal.feed_token is None
        cal.feed_token = "abc"
        db.commit()
    indexes = sqlite3.connect(db_file).execute("PRAGMA index_list(calendars)").fetchall()
    assert any(row[1] == "ix_calendars_feed_token" and row[2] == 1 for row in indexes)  # unique

    # running again is a no-op
    create_app(Settings.from_env(data_dir=tmp_path))
