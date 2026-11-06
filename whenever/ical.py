"""iCalendar parsing, validation and the creator annotations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from icalendar import Calendar as ICalendar

PRODID = "-//whenever//whenever 0.1//EN"
OWNER_PROP = "X-WHENEVER-OWNER"
OWNER_NAME_PROP = "X-WHENEVER-OWNER-NAME"
MARKER = "— Created by "  # "— Created by "
_MARKER_RE = re.compile(r"\s*" + re.escape(MARKER) + r"([^\n]*?)\s*\Z")
_ALLOWED_COMPONENTS = {"VEVENT", "VTIMEZONE"}


class InvalidCalendarData(ValueError):
    """Rejected PUT body. `precondition` names the CalDAV precondition that failed."""

    def __init__(self, message: str, precondition: str = "valid-calendar-data"):
        super().__init__(message)
        self.precondition = precondition


@dataclass
class ParsedEvent:
    ical: str
    uid: str
    dtstart_utc: datetime | None
    dtend_utc: datetime | None
    is_recurring: bool


def parse_event(data: bytes) -> ParsedEvent:
    """Validate an incoming calendar object resource and normalise it for storage."""
    try:
        cal = ICalendar.from_ical(data)
    except Exception as exc:  # icalendar raises a variety of errors on bad input
        raise InvalidCalendarData(f"Could not parse calendar data: {exc}") from exc
    if not isinstance(cal, ICalendar) or cal.name != "VCALENDAR":
        raise InvalidCalendarData("Body is not a VCALENDAR")

    for comp in cal.subcomponents:
        if comp.name not in _ALLOWED_COMPONENTS:
            raise InvalidCalendarData(f"Unsupported component {comp.name}", "supported-calendar-component")
    events = [c for c in cal.subcomponents if c.name == "VEVENT"]
    if not events:
        raise InvalidCalendarData("No VEVENT in calendar data", "supported-calendar-component")

    uids = {str(e.get("UID", "")).strip() for e in events}
    if len(uids) != 1 or "" in uids:
        raise InvalidCalendarData("All VEVENTs must share one non-empty UID", "valid-calendar-object-resource")

    for e in events:
        strip_annotations(e)

    is_recurring = any("RRULE" in e or "RDATE" in e for e in events)
    starts, ends = [], []
    for e in events:
        start, end = _span(e)
        if start is not None:
            starts.append(start)
            ends.append(end)

    return ParsedEvent(
        ical=cal.to_ical().decode("utf-8"),
        uid=uids.pop(),
        dtstart_utc=min(starts) if starts else None,
        dtend_utc=max(ends) if ends else None,
        is_recurring=is_recurring,
    )


def _to_utc(value) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:  # floating time: approximate as UTC
            return value
        return value.astimezone(UTC).replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return None


def _span(event) -> tuple[datetime | None, datetime | None]:
    """Start and end of a VEVENT in naive UTC, used for time-range queries."""
    try:
        start = event.decoded("DTSTART")
    except Exception:
        return None, None
    try:
        if "DTEND" in event:
            end = event.decoded("DTEND")
        elif "DURATION" in event:
            end = start + event.decoded("DURATION")
        elif isinstance(start, datetime):
            end = start
        else:
            end = start + timedelta(days=1)
    except Exception:
        end = start
    start_utc, end_utc = _to_utc(start), _to_utc(end)
    if start_utc is not None and (end_utc is None or end_utc < start_utc):
        end_utc = start_utc
    return start_utc, end_utc


def _replace(component, name: str, value: str) -> None:
    component.pop(name, None)
    component.add(name, value)


def strip_annotations(event) -> None:
    """Remove what annotate() added, so it doesn't pile up when a client saves the event back."""
    name = str(event.pop(OWNER_NAME_PROP, "") or "")
    event.pop(OWNER_PROP, None)

    if "DESCRIPTION" in event:
        desc = str(event["DESCRIPTION"])
        match = _MARKER_RE.search(desc)
        if match:
            name = name or match.group(1)
            desc = desc[: match.start()]
            if desc.strip():
                _replace(event, "DESCRIPTION", desc)
            else:
                event.pop("DESCRIPTION")

    if name and "SUMMARY" in event:
        summary = str(event["SUMMARY"])
        prefix = f"[{name}] "
        if summary.startswith(prefix):
            _replace(event, "SUMMARY", summary[len(prefix):])


def annotate(event, owner_username: str, owner_name: str, prefix_title: bool) -> None:
    """Add the creator's name to a VEVENT."""
    event.add(OWNER_PROP, owner_username)
    event.add(OWNER_NAME_PROP, owner_name)
    desc = str(event.get("DESCRIPTION", "") or "")
    _replace(event, "DESCRIPTION", f"{desc}\n\n{MARKER}{owner_name}" if desc.strip() else f"{MARKER}{owner_name}")
    if prefix_title:
        _replace(event, "SUMMARY", f"[{owner_name}] {event.get('SUMMARY', '') or ''}")


def annotated_calendar(stored: str, owner_username: str, owner_name: str, prefix_title: bool) -> ICalendar:
    cal = ICalendar.from_ical(stored)
    for comp in cal.subcomponents:
        if comp.name == "VEVENT":
            annotate(comp, owner_username, owner_name, prefix_title)
    return cal


def build_feed(
    name: str,
    description: str,
    timezone: str,
    calendars: list[ICalendar],
    refresh_minutes: int,
) -> bytes:
    """Merge per-event calendars into one VCALENDAR for the read-only ICS feed."""
    out = ICalendar()
    out.add("PRODID", PRODID)
    out.add("VERSION", "2.0")
    out.add("CALSCALE", "GREGORIAN")
    out.add("METHOD", "PUBLISH")
    out.add("X-WR-CALNAME", name)
    if description:
        out.add("X-WR-CALDESC", description)
    out.add("X-WR-TIMEZONE", timezone)
    out.add("REFRESH-INTERVAL", timedelta(minutes=refresh_minutes), parameters={"VALUE": "DURATION"})
    out.add("X-PUBLISHED-TTL", timedelta(minutes=refresh_minutes))

    seen_tz: set[str] = set()
    for cal in calendars:
        for comp in cal.subcomponents:
            if comp.name == "VTIMEZONE":
                tzid = str(comp.get("TZID", ""))
                if tzid in seen_tz:
                    continue
                seen_tz.add(tzid)
            out.add_component(comp)
    return out.to_ical()
