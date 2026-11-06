import re

from sqlalchemy import select

from whenever.models import Calendar, Event, Membership, User

from .conftest import basic, make_ics


def csrf(client, path="/admin/login"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def login(client, username="alice", password="alicepw"):
    return client.post("/admin/login", data={"username": username, "password": password, "csrf": csrf(client)},
                       follow_redirects=False)


def test_login_required(client, seed):
    r = client.get("/admin/users", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/admin/login")


def test_non_admin_cannot_log_in(client, seed):
    assert login(client, "bob", "bobpw").status_code == 401
    assert login(client, "alice", "wrong").status_code == 401


def test_csrf_enforced(client, seed):
    login(client)
    r = client.post("/admin/users", data={"username": "eve", "csrf": "nope"})
    assert r.status_code == 403


def test_create_user_calendar_and_membership(client, seed):
    assert login(client).status_code == 303
    token = csrf(client, "/admin/users")

    r = client.post("/admin/users", data={"username": "Eve", "display_name": "Eve E", "csrf": token})
    assert r.status_code == 200 and "Created user" in r.text
    r = client.post("/admin/calendars", data={"name": "Board Meetings", "color": "#aa0000", "timezone": "Europe/London", "csrf": token})
    assert "Created calendar" in r.text
    r = client.post("/admin/calendars", data={"name": "Bad", "timezone": "Mars/Base", "color": "#aa0000", "csrf": token})
    assert "Unknown timezone" in r.text

    with client.app.state.sessionmaker() as db:
        eve = db.scalar(select(User).where(User.username == "eve"))
        board = db.scalar(select(Calendar).where(Calendar.slug == "board-meetings"))
        assert eve.display_name == "Eve E" and eve.password_hash is None
        keep = [(m.user_id, m.calendar_id, m.can_write) for m in db.scalars(select(Membership))]

    form = {"csrf": token, f"m_{eve.id}_{board.id}": "on"}  # eve gets read-only access
    for uid, cid, can_write in keep:
        if uid != seed.carol.id:  # drop carol from team
            form[f"m_{uid}_{cid}"] = "on"
            if can_write:
                form[f"w_{uid}_{cid}"] = "on"
    r = client.post("/admin/memberships", data=form)
    assert "1 added, 1 removed, 0 changed" in r.text

    with client.app.state.sessionmaker() as db:
        m = db.get(Membership, (eve.id, board.id))
        assert m is not None and not m.can_write
        assert db.get(Membership, (seed.carol.id, seed.team.id)) is None

    page = client.get(f"/admin/users/{eve.id}").text
    assert f"/feed/{eve.token}/board-meetings.ics" in page
    assert f"/dav/t/{eve.token}/" in page


def test_rename_changes_event_etag(client, seed):
    client.put("/dav/u/bob/team/e1.ics", headers=basic("bob", "bobpw"), content=make_ics("e1"))
    before = client.get("/dav/u/bob/team/e1.ics", headers=basic("bob", "bobpw")).headers["etag"]
    login(client)
    token = csrf(client, "/admin/users")
    client.post(f"/admin/users/{seed.bob.id}", data={"display_name": "Robert", "active": "true", "csrf": token})
    r = client.get("/dav/u/bob/team/e1.ics", headers=basic("bob", "bobpw"))
    assert r.headers["etag"] != before
    assert b"Created by Robert" in r.content


def test_delete_user_reassigns_events(client, seed):
    client.put("/dav/u/bob/team/e1.ics", headers=basic("bob", "bobpw"), content=make_ics("e1"))
    login(client)
    token = csrf(client, "/admin/users")
    r = client.post(f"/admin/users/{seed.bob.id}/delete",
                    data={"events": "reassign", "reassign_to": str(seed.alice.id), "csrf": token})
    assert "Deleted user" in r.text
    with client.app.state.sessionmaker() as db:
        ev = db.scalar(select(Event))
        assert ev.owner_id == seed.alice.id


def test_cannot_delete_self(client, seed):
    login(client)
    token = csrf(client, "/admin/users")
    r = client.post(f"/admin/users/{seed.alice.id}/delete", data={"events": "delete", "csrf": token})
    assert "can't delete your own account" in r.text.replace("&#39;", "'")


def test_rotate_token(client, seed):
    login(client)
    token = csrf(client, "/admin/users")
    old = seed.bob.token
    client.post(f"/admin/users/{seed.bob.id}/token", data={"csrf": token})
    assert client.get(f"/feed/{old}/team.ics").status_code == 404


def test_delete_calendar_requires_confirmation(client, seed):
    login(client)
    token = csrf(client, "/admin/users")
    r = client.post(f"/admin/calendars/{seed.team.id}/delete", data={"confirm": "wrong", "csrf": token})
    assert "to confirm" in r.text
    r = client.post(f"/admin/calendars/{seed.team.id}/delete", data={"confirm": "team", "csrf": token})
    assert "Deleted calendar" in r.text
