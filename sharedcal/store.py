"""Event writes shared by the CalDAV handlers and the admin UI.

Every change to a calendar's events bumps `Calendar.sync_seq`, and the
changed event (or its tombstone) records the new value. That makes the
ctag and sync-token cheap to compute.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from icalendar import Calendar as ICalendar
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from . import ical
from .ical import ParsedEvent
from .models import Calendar, Event, EventTombstone, User, utcnow

DELETED_OWNER = "(deleted user)"


def bump_seq(db: Session, cal: Calendar) -> int:
    seq = db.execute(
        update(Calendar)
        .where(Calendar.id == cal.id)
        .values(sync_seq=Calendar.sync_seq + 1)
        .returning(Calendar.sync_seq)
        .execution_options(synchronize_session=False)
    ).scalar_one()
    db.expire(cal, ["sync_seq"])
    return seq


def owner_labels(owner: User | None) -> tuple[str, str]:
    if owner is None:
        return "", DELETED_OWNER
    return owner.username, owner.display_name or owner.username


def annotated(ev: Event, cal: Calendar) -> ICalendar:
    username, name = owner_labels(ev.owner)
    return ical.annotated_calendar(ev.ical, username, name, cal.prefix_titles)


def render(ev: Event, cal: Calendar) -> bytes:
    return annotated(ev, cal).to_ical()


def _etag(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def _touch(db: Session, cal: Calendar, ev: Event) -> None:
    ev.updated_at = utcnow()
    ev.seq = bump_seq(db, cal)
    ev.etag = _etag(render(ev, cal))


def _apply(ev: Event, parsed: ParsedEvent) -> None:
    ev.ical = parsed.ical
    ev.uid = parsed.uid
    ev.dtstart_utc = parsed.dtstart_utc
    ev.dtend_utc = parsed.dtend_utc
    ev.is_recurring = parsed.is_recurring


def create_event(db: Session, cal: Calendar, name: str, parsed: ParsedEvent, owner: User) -> Event:
    ev = Event(calendar_id=cal.id, name=name, owner=owner, etag="")
    _apply(ev, parsed)
    db.add(ev)
    db.execute(delete(EventTombstone).where(EventTombstone.calendar_id == cal.id, EventTombstone.name == name))
    _touch(db, cal, ev)
    db.flush()
    return ev


def update_event(db: Session, cal: Calendar, ev: Event, parsed: ParsedEvent) -> Event:
    _apply(ev, parsed)
    _touch(db, cal, ev)
    db.flush()
    return ev


def delete_event(db: Session, cal: Calendar, ev: Event) -> None:
    seq = bump_seq(db, cal)
    db.execute(delete(EventTombstone).where(EventTombstone.calendar_id == cal.id, EventTombstone.name == ev.name))
    db.add(EventTombstone(calendar_id=cal.id, name=ev.name, seq=seq))
    db.delete(ev)
    db.flush()


def refresh_events(db: Session, events: Iterable[Event]) -> None:
    """Recompute ETags after something that changes how events are rendered
    (owner renamed, owner reassigned, title prefix toggled), so clients notice."""
    for ev in events:
        _touch(db, ev.calendar, ev)
    db.flush()


def events_owned_by(db: Session, user: User) -> list[Event]:
    return list(db.scalars(select(Event).where(Event.owner_id == user.id)))
