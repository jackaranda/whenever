import xml.etree.ElementTree as ET

from .conftest import basic

NS = {"D": "DAV:", "C": "urn:ietf:params:xml:ns:caldav", "CS": "http://calendarserver.org/ns/"}

PRINCIPAL_BODY = b"""<?xml version="1.0"?>
<D:propfind xmlns:D="DAV:"><D:prop><D:current-user-principal/></D:prop></D:propfind>"""
HOME_BODY = b"""<?xml version="1.0"?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><D:prop><C:calendar-home-set/></D:prop></D:propfind>"""
CALS_BODY = b"""<?xml version="1.0"?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav" xmlns:CS="http://calendarserver.org/ns/">
<D:prop><D:resourcetype/><D:displayname/><CS:getctag/><D:sync-token/><D:current-user-privilege-set/><C:supported-calendar-component-set/><D:unknown-prop/></D:prop></D:propfind>"""


def test_well_known_redirects(client):
    r = client.request("PROPFIND", "/.well-known/caldav", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/dav/"


def test_options_advertises_caldav(client):
    r = client.options("/dav/u/alice/")
    assert "calendar-access" in r.headers["dav"]


def test_discovery_chain(client, seed):
    auth = basic("alice", "alicepw")
    r = client.request("PROPFIND", "/dav/", headers={**auth, "Depth": "0"}, content=PRINCIPAL_BODY)
    assert r.status_code == 207
    principal = ET.fromstring(r.content).find(".//D:current-user-principal/D:href", NS).text
    assert principal == "/dav/u/alice/"

    r = client.request("PROPFIND", principal, headers={**auth, "Depth": "0"}, content=HOME_BODY)
    home = ET.fromstring(r.content).find(".//C:calendar-home-set/D:href", NS).text

    r = client.request("PROPFIND", home, headers={**auth, "Depth": "1"}, content=CALS_BODY)
    assert r.status_code == 207
    root = ET.fromstring(r.content)
    responses = root.findall("D:response", NS)
    hrefs = [x.find("D:href", NS).text for x in responses]
    assert "/dav/u/alice/team/" in hrefs
    team = responses[hrefs.index("/dav/u/alice/team/")]
    assert team.find(".//D:resourcetype/C:calendar", NS) is not None
    assert team.find(".//D:displayname", NS).text == "Team"
    assert team.find(".//C:supported-calendar-component-set/C:comp", NS).get("name") == "VEVENT"
    privs = [p[0].tag for p in team.findall(".//D:current-user-privilege-set/D:privilege", NS)]
    assert "{DAV:}write-content" in privs
    # unknown props are reported as 404
    statuses = [s.text for s in team.findall(".//D:propstat/D:status", NS)]
    assert "HTTP/1.1 404 Not Found" in statuses


def test_readonly_member_privileges(client, seed):
    with client.app.state.sessionmaker() as db:
        from whenever.models import User
        token = db.get(User, seed.carol.id).token
    r = client.request("PROPFIND", f"/dav/t/{token}/team/", headers={"Depth": "0"}, content=CALS_BODY)
    privs = [p[0].tag for p in ET.fromstring(r.content).findall(".//D:current-user-privilege-set/D:privilege", NS)]
    assert privs == ["{DAV:}read"]


def test_basic_auth_required_and_scoped(client, seed):
    assert client.request("PROPFIND", "/dav/u/alice/").status_code == 401
    assert client.request("PROPFIND", "/dav/u/alice/", headers=basic("alice", "wrong")).status_code == 401
    # bob's credentials can't open alice's tree
    assert client.request("PROPFIND", "/dav/u/alice/", headers=basic("bob", "bobpw")).status_code == 401
    # the token works as a password
    assert client.request("PROPFIND", "/dav/u/bob/", headers=basic("bob", seed.bob.token)).status_code == 207


def test_non_member_sees_404(client, seed):
    assert client.request("PROPFIND", "/dav/u/dave/team/", headers=basic("dave", "davepw")).status_code == 404


def test_token_urls_can_be_disabled(client, seed):
    client.app.state.settings.allow_token_urls = False
    assert client.request("PROPFIND", f"/dav/t/{seed.bob.token}/").status_code == 404


def test_bad_token_404(client, seed):
    assert client.request("PROPFIND", "/dav/t/nope/").status_code == 404
