"""End-to-end test using the `caldav` client library against a real server."""

import socket
import threading
import time
from datetime import datetime, timedelta

import pytest
import uvicorn

caldav = pytest.importorskip("caldav")
from caldav.lib import error as caldav_error  # noqa: E402

from .conftest import make_ics  # noqa: E402


@pytest.fixture
def server(app, seed):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)


def test_real_client_round_trip(server, seed):
    bob = caldav.DAVClient(url=f"{server}/dav/u/bob/", username="bob", password="bobpw")
    cal = next(c for c in bob.principal().calendars() if c.get_display_name() == "Team")
    cal.save_event(make_ics("rt1", summary="Planning", start="20261112T100000Z", end="20261112T110000Z").decode())
    cal.save_event(make_ics("rt0", summary="Earlier", start="20261001T100000Z", end="20261001T110000Z").decode())

    alice = caldav.DAVClient(url=f"{server}/", username="alice", password="alicepw")
    acal = next(c for c in alice.principal().calendars() if c.get_display_name() == "Team")
    found = acal.search(start=datetime(2026, 11, 1), end=datetime(2026, 12, 1), event=True)
    assert len(found) == 1
    data = found[0].data
    assert "Created by Bob" in data

    # carol uses a token URL with no credentials and can read but not write
    carol = caldav.DAVClient(url=f"{server}/dav/t/{seed.carol.token}/")
    ccal = next(c for c in carol.principal().calendars() if c.get_display_name() == "Team")
    assert len(ccal.events()) == 2
    with pytest.raises(caldav_error.DAVError):
        ccal.save_event(make_ics("rt2").decode())

    # alice is admin, so she may edit bob's event; bob sees the change via sync
    updates = cal.objects_by_sync_token(load_objects=False)
    ev = acal.event_by_uid("rt1")
    ev.icalendar_component["SUMMARY"] = "Planning (moved)"
    ev.icalendar_component["DTSTART"].dt = ev.icalendar_component["DTSTART"].dt + timedelta(hours=1)
    ev.save()
    changed = cal.objects_by_sync_token(sync_token=updates.sync_token, load_objects=True)
    assert [o.icalendar_component["SUMMARY"] for o in changed] == ["Planning (moved)"]
