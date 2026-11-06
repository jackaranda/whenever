"""Serving the app under a URL prefix (WHENEVER_ROOT_PATH), as behind nginx at /whenever/."""

import re
import xml.etree.ElementTree as ET

import pytest

from whenever.config import Settings, normalize_root_path
from whenever.main import create_app

from .conftest import basic, make_ics

B = basic("bob", "bobpw")


@pytest.fixture
def app(tmp_path):
    return create_app(Settings.from_env(data_dir=tmp_path, root_path="/whenever"))


@pytest.mark.parametrize("value, expected", [
    ("/whenever", "/whenever"), ("/whenever/", "/whenever"), ("whenever", "/whenever"),
    ("/a/b/", "/a/b"), ("/", ""), ("", ""),
])
def test_normalize_root_path(value, expected):
    assert normalize_root_path(value) == expected


def hrefs(response):
    return [h.text for h in ET.fromstring(response.content).iter("{DAV:}href")]


def test_dav_hrefs_include_prefix(client, seed):
    r = client.request("PROPFIND", "/whenever/dav/u/bob/", headers={**B, "Depth": "1"})
    assert r.status_code == 207
    assert "/whenever/dav/u/bob/team/" in hrefs(r)
    assert all(h.startswith("/whenever/dav/") for h in hrefs(r))


def test_discovery_under_prefix(client, seed):
    # A client given https://host/whenever/ PROPFINDs it to find its principal.
    r = client.request("PROPFIND", "/whenever/", headers=B)
    assert "/whenever/dav/u/bob/" in hrefs(r)
    r = client.request("PROPFIND", "/whenever/.well-known/caldav", follow_redirects=False)
    assert r.headers["location"] == "/whenever/dav/"


def test_put_and_multiget_under_prefix(client, seed):
    assert client.put("/whenever/dav/u/bob/team/e1.ics", headers=B, content=make_ics("e1")).status_code == 201
    body = b"""<?xml version="1.0"?>
<C:calendar-multiget xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
<D:prop><C:calendar-data/></D:prop><D:href>/whenever/dav/u/bob/team/e1.ics</D:href></C:calendar-multiget>"""
    r = client.request("REPORT", "/whenever/dav/u/bob/team/", headers=B, content=body)
    assert b"UID:e1" in r.content
    assert client.get(f"/whenever/feed/{seed.carol.token}/team.ics").status_code == 200


def test_requests_without_prefix_are_treated_as_prefixed(client, seed):
    r = client.request("PROPFIND", "/dav/u/bob/", headers={**B, "Depth": "0"})
    assert set(hrefs(r)) == {"/whenever/dav/u/bob/"}


def test_admin_urls_and_cookie_scoped_to_prefix(client, seed):
    r = client.get("/whenever/", follow_redirects=False)
    assert r.headers["location"].endswith("/whenever/admin/users")

    r = client.get("/whenever/admin/login")
    assert "path=/whenever/" in r.headers["set-cookie"].lower()
    assert 'action="http://testserver/whenever/admin/login"' in r.text

    csrf = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
    client.post("/whenever/admin/login", data={"username": "alice", "password": "alicepw", "csrf": csrf})
    page = client.get(f"/whenever/admin/users/{seed.bob.id}").text
    assert f"http://testserver/whenever/feed/{seed.bob.token}/team.ics" in page
    assert "http://testserver/whenever/dav/u/bob/" in page
