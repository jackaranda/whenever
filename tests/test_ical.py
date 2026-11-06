import pytest
from icalendar import Calendar

from whenever import ical

from .conftest import make_ics


def test_parse_extracts_span():
    parsed = ical.parse_event(make_ics("e1"))
    assert parsed.uid == "e1"
    assert parsed.dtstart_utc.isoformat() == "2026-11-10T09:00:00"
    assert parsed.dtend_utc.isoformat() == "2026-11-10T09:30:00"
    assert not parsed.is_recurring


def test_parse_timezone_and_all_day():
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:tz\r\n"
        b"DTSTART;TZID=Africa/Johannesburg:20261110T090000\r\nDTEND;TZID=Africa/Johannesburg:20261110T100000\r\n"
        b"RRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    parsed = ical.parse_event(body)
    assert parsed.dtstart_utc.isoformat() == "2026-11-10T07:00:00"
    assert parsed.is_recurring

    allday = make_ics("ad").replace(b"DTSTART:20261110T090000Z", b"DTSTART;VALUE=DATE:20261110")
    allday = allday.replace(b"DTEND:20261110T093000Z\r\n", b"")
    parsed = ical.parse_event(allday)
    assert parsed.dtend_utc.isoformat() == "2026-11-11T00:00:00"


@pytest.mark.parametrize("body, precondition", [
    (b"not a calendar", "valid-calendar-data"),
    (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:x\r\nEND:VTODO\r\nEND:VCALENDAR\r\n", "supported-calendar-component"),
    (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nDTSTART:20261110T090000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n", "valid-calendar-object-resource"),
])
def test_parse_rejects(body, precondition):
    with pytest.raises(ical.InvalidCalendarData) as exc:
        ical.parse_event(body)
    assert exc.value.precondition == precondition


def test_annotations_round_trip():
    stored = ical.parse_event(make_ics("e1", description="Agenda")).ical
    served = ical.annotated_calendar(stored, "alice", "Alice", prefix_title=True).to_ical()
    ev = Calendar.from_ical(served).walk("VEVENT")[0]
    assert str(ev["SUMMARY"]) == "[Alice] Standup"
    assert str(ev["DESCRIPTION"]) == "Agenda\n\n— Created by Alice"
    assert str(ev[ical.OWNER_PROP]) == "alice"

    # A client sends the annotated event back unchanged: nothing piles up.
    again = ical.parse_event(served)
    ev2 = Calendar.from_ical(again.ical).walk("VEVENT")[0]
    assert str(ev2["SUMMARY"]) == "Standup"
    assert str(ev2["DESCRIPTION"]) == "Agenda"
    assert ical.OWNER_PROP not in ev2


def test_strip_without_x_props_uses_marker():
    body = make_ics("e1", summary="[Bob] Lunch", description="— Created by Bob")
    ev = Calendar.from_ical(ical.parse_event(body).ical).walk("VEVENT")[0]
    assert str(ev["SUMMARY"]) == "Lunch"
    assert "DESCRIPTION" not in ev
