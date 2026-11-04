from icalendar import Calendar

from .conftest import basic, make_ics

B = basic("bob", "bobpw")


def test_feed(client, seed):
    client.put("/dav/u/bob/team/e1.ics", headers=B, content=make_ics("e1"))
    client.put(f"/dav/t/{seed.alice.token}/team/e2.ics", content=make_ics("e2"))

    r = client.get(f"/feed/{seed.carol.token}/team.ics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    cal = Calendar.from_ical(r.content)
    assert str(cal["X-WR-CALNAME"]) == "Team"
    descs = sorted(str(e["DESCRIPTION"]) for e in cal.walk("VEVENT"))
    assert descs == ["— Created by Alice", "— Created by Bob"]

    etag = r.headers["etag"]
    assert client.get(f"/feed/{seed.carol.token}/team.ics", headers={"If-None-Match": etag}).status_code == 304
    client.put("/dav/u/bob/team/e3.ics", headers=B, content=make_ics("e3"))
    assert client.get(f"/feed/{seed.carol.token}/team.ics", headers={"If-None-Match": etag}).status_code == 200


def test_feed_requires_membership(client, seed):
    assert client.get(f"/feed/{seed.dave.token}/team.ics").status_code == 404
    assert client.get("/feed/bogus/team.ics").status_code == 404
