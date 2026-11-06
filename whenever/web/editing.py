"""Building and changing calendar objects for the web calendar.

A repeating event is one resource holding a master VEVENT (with the RRULE)
and optionally overrides: VEVENTs with a RECURRENCE-ID that replace single
occurrences. Deleted occurrences are listed as EXDATEs on the master.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import recurring_ical_events
from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent

from .. import ical
from .recurrence import RecurrenceError, Repeat, build_rrule, zone

# Properties that describe the series rather than one occurrence.
_SERIES_ONLY = {"RRULE", "RDATE", "EXDATE", "EXRULE", "DTSTART", "DTEND", "DURATION", "RECURRENCE-ID", "SEQUENCE"}


class EditError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Times:
    start: date | datetime
    end: date | datetime  # exclusive, as in iCalendar
    all_day: bool


@dataclass
class Fields:
    title: str
    location: str
    description: str


def is_date(value) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def parse_times(start: str, end: str, all_day: bool, tz: str) -> Times:
    """Parse the form's times. All-day ends are inclusive in the form and exclusive in iCalendar."""
    try:
        if all_day:
            first, last = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
            if last < first:
                raise EditError("The event ends before it starts.")
            return Times(first, last + timedelta(days=1), True)
        tzinfo = zone(tz)
        s, e = datetime.fromisoformat(start), datetime.fromisoformat(end)
    except RecurrenceError as exc:
        raise EditError(str(exc)) from exc
    except ValueError as exc:
        raise EditError("Dates and times must look like 2026-11-10 and 2026-11-10T09:30.") from exc
    s = s.replace(tzinfo=tzinfo) if s.tzinfo is None else s.astimezone(tzinfo)
    e = e.replace(tzinfo=tzinfo) if e.tzinfo is None else e.astimezone(tzinfo)
    if e < s:
        raise EditError("The event ends before it starts.")
    return Times(s.replace(second=0, microsecond=0), e.replace(second=0, microsecond=0), False)


def parse_occurrence(value: str) -> date | datetime:
    try:
        return date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)
    except ValueError as exc:
        raise EditError("Invalid occurrence.") from exc


def same_instant(a, b) -> bool:
    if isinstance(a, datetime) != isinstance(b, datetime):
        return False
    if isinstance(a, datetime) and (a.tzinfo is None) != (b.tzinfo is None):
        return a.replace(tzinfo=None) == b.replace(tzinfo=None)
    return a == b


# ---------------------------------------------------------------- components


def vevents(cal: ICalendar) -> list:
    return [c for c in cal.subcomponents if c.name == "VEVENT"]


def master_of(cal: ICalendar):
    for comp in vevents(cal):
        if "RECURRENCE-ID" not in comp:
            return comp
    raise EditError("This event can only be changed in a calendar app.", 409)


def overrides_of(cal: ICalendar) -> list:
    return [c for c in vevents(cal) if "RECURRENCE-ID" in c]


def _set_fields(vevent, fields: Fields) -> None:
    for name, value in (("SUMMARY", fields.title.strip() or "(no title)"),
                        ("LOCATION", fields.location.strip()),
                        ("DESCRIPTION", fields.description.strip())):
        vevent.pop(name, None)
        if value:
            vevent.add(name, value)


def _set_times(vevent, times: Times) -> None:
    for name in ("DTSTART", "DTEND", "DURATION"):
        vevent.pop(name, None)
    vevent.add("DTSTART", times.start)
    vevent.add("DTEND", times.end)


def _touch(vevent) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    for name in ("DTSTAMP", "LAST-MODIFIED"):
        vevent.pop(name, None)
        vevent.add(name, now)
    sequence = int(vevent.pop("SEQUENCE", 0) or 0)
    vevent.add("SEQUENCE", sequence + 1)


def _times_of(vevent) -> Times:
    start = vevent.decoded("DTSTART")
    if "DTEND" in vevent:
        end = vevent.decoded("DTEND")
    elif "DURATION" in vevent:
        end = start + vevent.decoded("DURATION")
    else:
        end = start + timedelta(days=1) if is_date(start) else start
    return Times(start, end, is_date(start))


def _same_times(a: Times, b: Times) -> bool:
    return same_instant(a.start, b.start) and same_instant(a.end, b.end)


def _rrule(rep: Repeat | None, times: Times):
    if rep is None:
        return None
    try:
        return build_rrule(rep, times.start)
    except RecurrenceError as exc:
        raise EditError(str(exc)) from exc


def _finish(cal: ICalendar) -> ICalendar:
    cal.add_missing_timezones()
    return cal


# ---------------------------------------------------------------- operations


def new_calendar(fields: Fields, times: Times, rep: Repeat | None) -> ICalendar:
    cal = ICalendar()
    cal.add("PRODID", ical.PRODID)
    cal.add("VERSION", "2.0")
    ev = IEvent()
    ev.add("UID", f"{uuid.uuid4()}@whenever")
    ev.add("CREATED", datetime.now(UTC).replace(microsecond=0))
    _set_fields(ev, fields)
    _set_times(ev, times)
    rule = _rrule(rep, times)
    if rule:
        ev.add("RRULE", rule)
    _touch(ev)
    ev["SEQUENCE"] = 0
    cal.add_component(ev)
    return _finish(cal)


def update_series(cal: ICalendar, fields: Fields, times: Times, rep: Repeat | None, keep_repeat: bool) -> bool:
    """Change the whole event (every occurrence).

    Returns True if changes to single occurrences had to be discarded: a new
    time or repeat pattern means the old occurrences no longer line up.
    """
    master = master_of(cal)
    old_times = _times_of(master)
    old_rule = master.get("RRULE")
    old_rule_text = old_rule.to_ical() if old_rule is not None and not isinstance(old_rule, list) else None

    _set_fields(master, fields)
    _set_times(master, times)
    if not keep_repeat:
        master.pop("RRULE", None)
        rule = _rrule(rep, times)
        if rule:
            master.add("RRULE", rule)
        if rep is None:
            master.pop("RDATE", None)
    new_rule = master.get("RRULE")
    new_rule_text = new_rule.to_ical() if new_rule is not None and not isinstance(new_rule, list) else None
    _touch(master)

    changed = not _same_times(old_times, times) or (not keep_repeat and new_rule_text != old_rule_text)
    exceptions = bool(overrides_of(cal)) or "EXDATE" in master
    if changed and exceptions:
        master.pop("EXDATE", None)
        for comp in overrides_of(cal):
            cal.subcomponents.remove(comp)
    _finish(cal)
    return changed and exceptions


def find_occurrence(cal: ICalendar, rid: date | datetime):
    """Return (override or None, occurrence component) for the occurrence originally at `rid`."""
    for comp in overrides_of(cal):
        if same_instant(comp.decoded("RECURRENCE-ID"), rid):
            return comp, comp
    if is_date(rid):
        window = (rid, rid + timedelta(days=1))
    else:
        window = (rid - timedelta(minutes=1), rid + timedelta(minutes=1))
    for occ in recurring_ical_events.of(cal, skip_bad_series=True).between(*window):
        if "RECURRENCE-ID" in occ and same_instant(occ.decoded("RECURRENCE-ID"), rid):
            return None, occ
    raise EditError("That occurrence no longer exists. Reload the calendar.", 404)


def update_occurrence(cal: ICalendar, rid: date | datetime, fields: Fields, times: Times) -> None:
    """Change one occurrence of a repeating event by adding (or editing) an override."""
    master = master_of(cal)
    override, occ = find_occurrence(cal, rid)
    if override is None:
        override = IEvent()
        for name, value in master.items():
            if name not in _SERIES_ONLY:
                override[name] = copy.deepcopy(value)
        for sub in master.subcomponents:
            override.add_component(copy.deepcopy(sub))
        override.add("RECURRENCE-ID", occ.decoded("RECURRENCE-ID"))
        override.add("SEQUENCE", int(master.get("SEQUENCE", 0) or 0))
        cal.add_component(override)
    _set_fields(override, fields)
    _set_times(override, times)
    _touch(override)
    _finish(cal)


def delete_occurrence(cal: ICalendar, rid: date | datetime) -> None:
    master = master_of(cal)
    override, occ = find_occurrence(cal, rid)
    if override is not None:
        cal.subcomponents.remove(override)
    master.add("EXDATE", occ.decoded("RECURRENCE-ID"))
    _touch(master)
    _finish(cal)


def move(cal: ICalendar, times: Times) -> None:
    """Change only the time of a non-repeating event (drag and drop)."""
    master = master_of(cal)
    if "RRULE" in master or "RDATE" in master:
        raise EditError("Open a repeating event to change its time.")
    _set_times(master, times)
    _touch(master)
    _finish(cal)


# ---------------------------------------------------------------- reading


def iso(value: date | datetime) -> str:
    return value.isoformat()


def occurrences(cal: ICalendar, start: datetime, end: datetime) -> list:
    return recurring_ical_events.of(cal, skip_bad_series=True).between(start, end)


def describe_component(comp) -> dict:
    times = _times_of(comp)
    return {
        "title": str(comp.get("SUMMARY", "") or ""),
        "location": str(comp.get("LOCATION", "") or ""),
        "description": str(comp.get("DESCRIPTION", "") or ""),
        "start": iso(times.start),
        "end": iso(times.end),
        "all_day": times.all_day,
    }


def has_exceptions(cal: ICalendar) -> bool:
    try:
        return bool(overrides_of(cal)) or "EXDATE" in master_of(cal)
    except EditError:
        return False
