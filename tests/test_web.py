"""The web calendar page and its JSON API."""

from icalendar import Calendar

from .conftest import basic, make_ics

B = basic("bob", "bobpw")
A = basic("alice", "alicepw")
LONDON = "Europe/London"


def api(seed_user):
    return f"/cal/{seed_user.token}/api/events"


def create(client, user, **overrides):
    body = {"calendar": "team", "title": "Planning", "start": "2026-11-10T09:00", "end": "2026-11-10T10:00",
            "tz": LONDON, **overrides}
    return client.post(api(user), json=body)


def listing(client, user, start="2026-11-01T00:00:00Z", end="2026-12-01T00:00:00Z"):
    r = client.get(api(user), params={"start": start, "end": end})
    assert r.status_code == 200, r.text
    return r.json()


def dav_vevents(client, ev_id, app):
    from whenever.models import Event
    with app.state.sessionmaker() as db:
        ev = db.get(Event, ev_id)
        r = client.get(f"/dav/u/bob/team/{ev.name}", headers=B)
    assert r.status_code == 200
    return Calendar.from_ical(r.content).walk("VEVENT"), r.content


def test_page(client, seed):
    r = client.get(f"/cal/{seed.bob.token}/")
    assert r.status_code == 200
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer"
    assert f"/cal/{seed.bob.token}/api/events" in r.text
    assert "/static/fullcalendar/index.global.min.js" in r.text
    assert client.get("/static/calendar.js").status_code == 200
    assert client.get("/cal/bogus/").status_code == 404


def test_token_urls_disabled(client, seed):
    client.app.state.settings.allow_token_urls = False
    assert client.get(f"/cal/{seed.bob.token}/").status_code == 404
    assert client.get(api(seed.bob), params={"start": "2026-11-01", "end": "2026-12-01"}).status_code == 404


def test_create_single_event_visible_everywhere(client, seed, app):
    r = create(client, seed.bob, location="Room 1", description="Agenda")
    assert r.status_code == 201, r.text
    ev_id = r.json()["id"]

    items = listing(client, seed.carol)
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Planning"
    assert item["start"] == "2026-11-10T09:00:00+00:00"
    assert item["extendedProps"]["owner"] == "Bob"
    assert item["extendedProps"]["can_edit"] is False  # carol is read-only
    assert item["editable"] is False

    # CalDAV clients see it, credited to Bob, with a timezone definition
    vevents, raw = dav_vevents(client, ev_id, app)
    assert b"BEGIN:VTIMEZONE" in raw and b"TZID=Europe/London" in raw
    assert str(vevents[0]["DESCRIPTION"]).endswith("— Created by Bob")
    assert str(vevents[0]["LOCATION"]) == "Room 1"


def test_all_day_end_is_inclusive_in_the_form(client, seed, app):
    r = create(client, seed.bob, all_day=True, start="2026-11-10", end="2026-11-11")
    vevents, raw = dav_vevents(client, r.json()["id"], app)
    assert b"DTSTART;VALUE=DATE:20261110" in raw
    assert b"DTEND;VALUE=DATE:20261112" in raw
    item = listing(client, seed.bob)[0]
    assert item["allDay"] is True and item["end"] == "2026-11-12"


def test_weekly_series_keeps_local_time_across_dst(client, seed):
    # London clocks go back on 25 Oct 2026: 09:00 is 08:00Z before and 09:00Z after.
    r = create(client, seed.bob, start="2026-10-13T09:00", end="2026-10-13T10:00",
               repeat={"freq": "WEEKLY", "ends": "count", "count": 4})
    assert r.status_code == 201, r.text
    items = listing(client, seed.bob, "2026-10-01T00:00:00Z", "2026-12-01T00:00:00Z")
    assert [i["start"] for i in items] == [
        "2026-10-13T09:00:00+01:00", "2026-10-20T09:00:00+01:00",
        "2026-10-27T09:00:00+00:00", "2026-11-03T09:00:00+00:00",
    ]
    assert all(i["extendedProps"]["recurring"] for i in items)
    assert all(i["editable"] is False for i in items)  # repeating events aren't draggable


def _series(client, seed):
    r = create(client, seed.bob, title="Standup", repeat={"freq": "WEEKLY", "byday": ["TU", "TH"], "ends": "never"})
    assert r.status_code == 201, r.text
    ev_id = r.json()["id"]
    items = [i for i in listing(client, seed.bob) if i["extendedProps"]["event_id"] == ev_id]
    return ev_id, items


def test_series_details(client, seed):
    ev_id, items = _series(client, seed)
    assert len(items) == 6  # Tue/Thu from 10 Nov: 10, 12, 17, 19, 24, 26
    occ = items[1]["extendedProps"]["occurrence"]
    r = client.get(f"{api(seed.bob)}/{ev_id}", params={"occurrence": occ})
    data = r.json()
    assert data["repeat_kind"] == "simple"
    assert data["repeat_text"] == "Weekly on Tue, Thu"
    assert data["repeat"]["byday"] == ["TU", "TH"]
    assert data["occurrence"]["start"] == "2026-11-12T09:00:00+00:00"
    assert data["can_edit"] is True


def test_edit_one_occurrence(client, seed, app):
    ev_id, items = _series(client, seed)
    occ = items[1]["extendedProps"]["occurrence"]
    etag = items[1]["extendedProps"]["etag"]
    r = client.put(f"{api(seed.bob)}/{ev_id}", json={
        "scope": "this", "occurrence": occ, "etag": etag, "title": "Standup (moved)",
        "start": "2026-11-12T11:00", "end": "2026-11-12T11:30", "tz": LONDON,
    })
    assert r.status_code == 200, r.text

    titles = {i["start"]: i["title"] for i in listing(client, seed.bob)}
    assert titles["2026-11-12T11:00:00+00:00"] == "Standup (moved)"
    assert "2026-11-12T09:00:00+00:00" not in titles
    assert titles["2026-11-17T09:00:00+00:00"] == "Standup"

    vevents, raw = dav_vevents(client, ev_id, app)
    assert len(vevents) == 2 and b"RECURRENCE-ID;TZID=Europe/London:20261112T090000" in raw

    # editing the same occurrence again updates the override instead of adding another
    r = client.get(f"{api(seed.bob)}/{ev_id}", params={"occurrence": occ})
    assert r.json()["occurrence"]["title"] == "Standup (moved)"
    client.put(f"{api(seed.bob)}/{ev_id}", json={
        "scope": "this", "occurrence": occ, "title": "Again",
        "start": "2026-11-12T12:00", "end": "2026-11-12T12:30", "tz": LONDON,
    })
    vevents, _ = dav_vevents(client, ev_id, app)
    assert len(vevents) == 2


def test_delete_one_occurrence(client, seed, app):
    ev_id, items = _series(client, seed)
    occ = items[2]["extendedProps"]["occurrence"]
    r = client.delete(f"{api(seed.bob)}/{ev_id}", params={"scope": "this", "occurrence": occ})
    assert r.status_code == 204
    starts = [i["start"] for i in listing(client, seed.bob)]
    assert len(starts) == 5 and occ not in starts
    _, raw = dav_vevents(client, ev_id, app)
    assert b"EXDATE;TZID=Europe/London:20261117T090000" in raw


def test_series_title_edit_keeps_exceptions_but_time_change_clears_them(client, seed):
    ev_id, items = _series(client, seed)
    occ = items[1]["extendedProps"]["occurrence"]
    client.put(f"{api(seed.bob)}/{ev_id}", json={
        "scope": "this", "occurrence": occ, "title": "Special",
        "start": "2026-11-12T11:00", "end": "2026-11-12T11:30", "tz": LONDON})
    client.delete(f"{api(seed.bob)}/{ev_id}", params={"scope": "this", "occurrence": items[2]["extendedProps"]["occurrence"]})
    series = {"scope": "all", "start": "2026-11-10T09:00", "end": "2026-11-10T10:00", "tz": LONDON,
              "repeat": {"freq": "WEEKLY", "byday": ["TU", "TH"]}}

    r = client.put(f"{api(seed.bob)}/{ev_id}", json={**series, "title": "Daily standup"})
    assert r.json()["exceptions_cleared"] is False
    titles = sorted({i["title"] for i in listing(client, seed.bob)})
    assert titles == ["Daily standup", "Special"]
    assert len(listing(client, seed.bob)) == 5

    r = client.put(f"{api(seed.bob)}/{ev_id}", json={**series, "title": "Daily standup",
                                                    "start": "2026-11-10T09:30", "end": "2026-11-10T10:00"})
    assert r.json()["exceptions_cleared"] is True
    items = listing(client, seed.bob)
    assert len(items) == 6 and {i["title"] for i in items} == {"Daily standup"}


def test_stop_repeating(client, seed):
    ev_id, _ = _series(client, seed)
    r = client.put(f"{api(seed.bob)}/{ev_id}", json={
        "title": "Once", "start": "2026-11-10T09:00", "end": "2026-11-10T10:00", "tz": LONDON, "repeat": None})
    assert r.status_code == 200
    items = listing(client, seed.bob)
    assert len(items) == 1 and items[0]["extendedProps"]["recurring"] is False


def test_custom_rule_from_another_client_is_kept(client, seed, app):
    ics = make_ics("custom", start="20261030T090000Z", end="20261030T100000Z",
                   extra="RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1")
    assert client.put("/dav/u/bob/team/custom.ics", headers=B, content=ics).status_code == 201
    item = listing(client, seed.bob)[0]
    assert item["start"] == "2026-11-30T09:00:00+00:00"  # last weekday of November
    ev_id = item["extendedProps"]["event_id"]
    data = client.get(f"{api(seed.bob)}/{ev_id}").json()
    assert data["repeat_kind"] == "custom" and "BYSETPOS=-1" in data["repeat_text"]

    r = client.put(f"{api(seed.bob)}/{ev_id}", json={
        "title": "Month end", "keep_repeat": True, "start": "2026-10-30T09:00", "end": "2026-10-30T10:00", "tz": "UTC"})
    assert r.status_code == 200, r.text
    _, raw = dav_vevents(client, ev_id, app)
    assert b"BYSETPOS=-1" in raw and b"SUMMARY:Month end" in raw


def test_move_single_event(client, seed, app):
    ev_id = create(client, seed.bob).json()["id"]
    r = client.post(f"{api(seed.bob)}/{ev_id}/move",
                    json={"start": "2026-11-11T14:00", "end": "2026-11-11T15:30", "tz": LONDON})
    assert r.status_code == 200, r.text
    assert listing(client, seed.bob)[0]["start"] == "2026-11-11T14:00:00+00:00"

    series_id, _ = _series(client, seed)
    r = client.post(f"{api(seed.bob)}/{series_id}/move",
                    json={"start": "2026-11-11T14:00", "end": "2026-11-11T15:30", "tz": LONDON})
    assert r.status_code == 400


def test_permissions(client, seed):
    # read-only member can't create
    assert create(client, seed.carol).status_code == 403
    # non-member calendar
    assert create(client, seed.dave).status_code == 404
    ev_id = create(client, seed.bob).json()["id"]
    body = {"title": "Hijack", "start": "2026-11-10T09:00", "end": "2026-11-10T10:00", "tz": LONDON}
    # carol (read-only) and dave (not a member) can't change it
    assert client.put(f"{api(seed.carol)}/{ev_id}", json=body).status_code == 403
    assert client.get(f"{api(seed.dave)}/{ev_id}").status_code == 404
    assert client.delete(f"{api(seed.dave)}/{ev_id}").status_code == 404
    # alice is an admin, so she may
    assert client.put(f"{api(seed.alice)}/{ev_id}", json=body).status_code == 200
    assert listing(client, seed.bob)[0]["extendedProps"]["owner"] == "Bob"  # still Bob's event


def test_other_writer_cannot_edit(client, seed, app):
    from whenever.models import User
    with app.state.sessionmaker() as db:
        db.get(User, seed.alice.id).is_admin = False
        db.commit()
    ev_id = create(client, seed.bob).json()["id"]
    r = client.delete(f"{api(seed.alice)}/{ev_id}")
    assert r.status_code == 403 and "created this event" in r.json()["error"]


def test_stale_etag_conflict(client, seed):
    ev_id = create(client, seed.bob).json()["id"]
    r = client.put(f"{api(seed.bob)}/{ev_id}", json={
        "etag": "stale", "title": "X", "start": "2026-11-10T09:00", "end": "2026-11-10T10:00", "tz": LONDON})
    assert r.status_code == 409


def test_validation_errors(client, seed):
    assert create(client, seed.bob, end="2026-11-10T08:00").status_code == 400
    assert create(client, seed.bob, tz="Mars/Base").status_code == 400
    r = create(client, seed.bob, start="2026-11-24T09:00", end="2026-11-24T10:00",
               repeat={"freq": "MONTHLY", "monthly_mode": "weekday", "nth": 2})
    assert r.status_code == 400 and "week of the month" in r.json()["error"]
    assert create(client, seed.bob, repeat={"freq": "HOURLY"}).status_code == 422
    r = client.get(api(seed.bob), params={"start": "nonsense", "end": "2026-12-01"})
    assert r.status_code == 400


def test_dav_change_appears_in_web_listing(client, seed):
    client.put("/dav/u/bob/team/x.ics", headers=B, content=make_ics("x", summary="From Thunderbird"))
    assert [i["title"] for i in listing(client, seed.alice)] == ["From Thunderbird"]
