import xml.etree.ElementTree as ET

from icalendar import Calendar

from .conftest import basic, make_ics

NS = {"D": "DAV:", "C": "urn:ietf:params:xml:ns:caldav"}
A = basic("alice", "alicepw")
B = basic("bob", "bobpw")
URL = "/dav/u/{user}/team/{name}"


def put(client, auth, user, name, body, **headers):
    return client.put(URL.format(user=user, name=name), headers={**auth, "Content-Type": "text/calendar", **headers}, content=body)


def test_create_get_and_creator_annotation(client, seed):
    r = put(client, B, "bob", "e1.ics", make_ics("e1", description="Bring snacks"))
    assert r.status_code == 201
    assert "etag" not in r.headers  # the stored form differs from what was sent

    # Alice sees Bob as the creator
    r = client.get(URL.format(user="alice", name="e1.ics"), headers=A)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    ev = Calendar.from_ical(r.content).walk("VEVENT")[0]
    assert str(ev["DESCRIPTION"]).endswith("— Created by Bob")
    assert str(ev["X-SHAREDCAL-OWNER"]) == "bob"


def test_update_with_etag(client, seed):
    put(client, B, "bob", "e1.ics", make_ics("e1"))
    etag = client.get(URL.format(user="bob", name="e1.ics"), headers=B).headers["etag"]

    assert put(client, B, "bob", "e1.ics", make_ics("e1", summary="Moved"), **{"If-Match": '"stale"'}).status_code == 412
    assert put(client, B, "bob", "e1.ics", make_ics("e1", summary="Moved"), **{"If-Match": etag}).status_code == 204
    new = client.get(URL.format(user="bob", name="e1.ics"), headers=B)
    assert new.headers["etag"] != etag
    assert b"SUMMARY:Moved" in new.content

    # If-None-Match: * refuses to overwrite
    assert put(client, B, "bob", "e1.ics", make_ics("e1"), **{"If-None-Match": "*"}).status_code == 412


def test_uid_rules(client, seed):
    put(client, B, "bob", "e1.ics", make_ics("e1"))
    # same UID under a different name
    r = put(client, B, "bob", "other.ics", make_ics("e1"))
    assert r.status_code == 403 and b"no-uid-conflict" in r.content
    # changing the UID of an existing resource
    r = put(client, B, "bob", "e1.ics", make_ics("e2"))
    assert r.status_code == 403 and b"no-uid-conflict" in r.content


def test_invalid_data_rejected(client, seed):
    r = put(client, B, "bob", "bad.ics", b"hello")
    assert r.status_code == 403 and b"valid-calendar-data" in r.content


def test_too_large_rejected(client, seed):
    client.app.state.settings.max_event_size = 1000
    r = put(client, B, "bob", "big.ics", make_ics("big", description="x" * 2000))
    assert r.status_code == 413


def test_delete(client, seed):
    put(client, B, "bob", "e1.ics", make_ics("e1"))
    assert client.delete(URL.format(user="bob", name="e1.ics"), headers=B).status_code == 204
    assert client.get(URL.format(user="bob", name="e1.ics"), headers=B).status_code == 404
    assert client.delete(URL.format(user="bob", name="e1.ics"), headers=B).status_code == 404


def test_multiget_and_query(client, seed):
    put(client, B, "bob", "nov.ics", make_ics("nov"))
    put(client, B, "bob", "dec.ics", make_ics("dec", start="20261210T090000Z", end="20261210T100000Z"))
    put(client, B, "bob", "weekly.ics", make_ics("weekly", start="20260101T090000Z", end="20260101T100000Z", extra="RRULE:FREQ=WEEKLY"))

    body = b"""<?xml version="1.0"?>
<C:calendar-multiget xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
<D:prop><D:getetag/><C:calendar-data/></D:prop>
<D:href>/dav/u/bob/team/nov.ics</D:href><D:href>/dav/u/bob/team/missing.ics</D:href>
</C:calendar-multiget>"""
    r = client.request("REPORT", "/dav/u/bob/team/", headers={**B, "Depth": "1"}, content=body)
    assert r.status_code == 207
    root = ET.fromstring(r.content)
    assert "UID:nov" in root.find(".//C:calendar-data", NS).text
    assert "404" in root.findall("D:response", NS)[1].find("D:status", NS).text

    body = b"""<?xml version="1.0"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
<D:prop><D:getetag/></D:prop>
<C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT">
<C:time-range start="20261101T000000Z" end="20261201T000000Z"/></C:comp-filter></C:comp-filter></C:filter>
</C:calendar-query>"""
    r = client.request("REPORT", "/dav/u/bob/team/", headers={**B, "Depth": "1"}, content=body)
    hrefs = sorted(h.text for h in ET.fromstring(r.content).iter("{DAV:}href"))
    assert hrefs == ["/dav/u/bob/team/nov.ics", "/dav/u/bob/team/weekly.ics"]

    todo = body.replace(b'name="VEVENT"', b'name="VTODO"')
    r = client.request("REPORT", "/dav/u/bob/team/", headers={**B, "Depth": "1"}, content=todo)
    assert list(ET.fromstring(r.content).iter("{DAV:}href")) == []


def _sync(client, token=""):
    body = f"""<?xml version="1.0"?>
<D:sync-collection xmlns:D="DAV:"><D:sync-token>{token}</D:sync-token><D:sync-level>1</D:sync-level>
<D:prop><D:getetag/></D:prop></D:sync-collection>""".encode()
    r = client.request("REPORT", "/dav/u/alice/team/", headers={**A}, content=body)
    return r


def test_sync_collection(client, seed):
    put(client, B, "bob", "e1.ics", make_ics("e1"))
    put(client, B, "bob", "e2.ics", make_ics("e2"))
    r = _sync(client)
    root = ET.fromstring(r.content)
    token = root.find("D:sync-token", NS).text
    assert len(root.findall("D:response", NS)) == 2

    put(client, B, "bob", "e1.ics", make_ics("e1", summary="Changed"))
    client.delete("/dav/u/bob/team/e2.ics", headers=B)
    root = ET.fromstring(_sync(client, token).content)
    by_href = {x.find("D:href", NS).text: x for x in root.findall("D:response", NS)}
    assert set(by_href) == {"/dav/u/alice/team/e1.ics", "/dav/u/alice/team/e2.ics"}
    assert "404" in by_href["/dav/u/alice/team/e2.ics"].find("D:status", NS).text
    new_token = root.find("D:sync-token", NS).text
    assert new_token != token

    # nothing changed since the new token
    assert ET.fromstring(_sync(client, new_token).content).findall("D:response", NS) == []

    r = _sync(client, "urn:sharedcal:sync:999:1")
    assert r.status_code == 403 and b"valid-sync-token" in r.content


def test_ctag_changes(client, seed):
    body = b"""<?xml version="1.0"?><D:propfind xmlns:D="DAV:" xmlns:CS="http://calendarserver.org/ns/"><D:prop><CS:getctag/></D:prop></D:propfind>"""

    def ctag():
        r = client.request("PROPFIND", "/dav/u/alice/team/", headers={**A, "Depth": "0"}, content=body)
        return ET.fromstring(r.content).find(".//{http://calendarserver.org/ns/}getctag").text

    before = ctag()
    put(client, B, "bob", "e1.ics", make_ics("e1"))
    assert ctag() != before


def test_collection_get_returns_whole_calendar(client, seed):
    put(client, B, "bob", "e1.ics", make_ics("e1"))
    put(client, A, "alice", "e2.ics", make_ics("e2"))
    r = client.get("/dav/u/bob/team/", headers=B)
    assert len(Calendar.from_ical(r.content).walk("VEVENT")) == 2


def test_unsupported_methods(client, seed):
    assert client.request("MKCALENDAR", "/dav/u/alice/new/", headers=A).status_code == 403
    assert client.delete("/dav/u/alice/team/", headers=A).status_code == 403
    r = client.request("PROPPATCH", "/dav/u/alice/team/", headers=A, content=b"""<?xml version="1.0"?>
<D:propertyupdate xmlns:D="DAV:"><D:set><D:prop><D:displayname>X</D:displayname></D:prop></D:set></D:propertyupdate>""")
    assert r.status_code == 207 and b"403" in r.content
