"""The web calendar: /cal/{token}/ and the JSON API it uses.

Like the ICS feeds and token CalDAV URLs, the user is identified by the
token in the path. Writes go through `store`, so CalDAV clients and feeds
see them exactly like changes made in a calendar app.
"""

from __future__ import annotations

import functools
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from icalendar import Calendar as ICalendar
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ical, store
from ..db import get_db
from ..models import Calendar, Event, Membership, User
from . import editing
from .editing import EditError, Fields
from .recurrence import Repeat, repeat_info

log = logging.getLogger(__name__)

router = APIRouter(prefix="/cal/{token}")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

MAX_RANGE = timedelta(days=400)
MAX_INSTANCES = 5000

PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self' data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    # The page URL contains the user's token: never send it to other sites.
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}


class EventIn(BaseModel):
    calendar: str | None = None  # slug; only used when creating
    title: str = Field("", max_length=500)
    location: str = Field("", max_length=500)
    description: str = Field("", max_length=10000)
    all_day: bool = False
    start: str = Field(..., max_length=40)
    end: str = Field(..., max_length=40)
    tz: str = Field("UTC", max_length=64)
    repeat: Repeat | None = None
    keep_repeat: bool = False  # keep the existing (custom) repeat rule
    scope: Literal["all", "this"] = "all"
    occurrence: str | None = Field(None, max_length=40)
    etag: str | None = None


class MoveIn(BaseModel):
    all_day: bool = False
    start: str = Field(..., max_length=40)
    end: str = Field(..., max_length=40)
    tz: str = Field("UTC", max_length=64)
    etag: str | None = None


# ---------------------------------------------------------------- helpers


class _Fail(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers={"Cache-Control": "no-store"})


def _user(request: Request, db: Session, token: str) -> User:
    if not request.app.state.settings.allow_token_urls:
        raise _Fail(404, "Not found")
    user = db.scalar(select(User).where(User.token == token))
    if user is None or not user.active:
        raise _Fail(404, "Not found")
    return user


def _memberships(db: Session, user: User) -> list[tuple[Membership, Calendar]]:
    rows = db.execute(
        select(Membership, Calendar)
        .join(Calendar, Membership.calendar_id == Calendar.id)
        .where(Membership.user_id == user.id)
        .order_by(Calendar.name)
    )
    return [(m, cal) for m, cal in rows]


def _event(db: Session, user: User, event_id: int) -> tuple[Membership, Calendar, Event]:
    ev = db.get(Event, event_id)
    membership = db.get(Membership, (user.id, ev.calendar_id)) if ev else None
    if ev is None or membership is None:
        raise _Fail(404, "That event no longer exists. Reload the calendar.")
    return membership, ev.calendar, ev


def _check_etag(ev: Event, etag: str | None) -> None:
    if etag and etag != ev.etag:
        raise _Fail(409, "Someone changed this event since you opened it. Reload the calendar and try again.")


def _require_modify(user: User, membership: Membership, ev: Event) -> None:
    if not store.can_modify(user, membership, ev):
        raise _Fail(403, "Only the person who created this event can change it.")


def _save(db: Session, settings, cal: Calendar, ev: Event | None, data: ICalendar, owner: User) -> Event:
    body = data.to_ical()
    if len(body) > settings.max_event_size:
        raise _Fail(413, "This event is too large.")
    try:
        parsed = ical.parse_event(body)
    except ical.InvalidCalendarData as exc:
        raise _Fail(400, str(exc))
    if ev is None:
        return store.create_event(db, cal, f"{parsed.uid}.ics", parsed, owner)
    return store.update_event(db, cal, ev, parsed)


def _handle(func):
    """Turn _Fail and EditError into JSON error responses, rolling back the transaction."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        db: Session = kwargs["db"]
        try:
            return func(*args, **kwargs)
        except _Fail as exc:
            db.rollback()
            return _error(exc.status, exc.message)
        except EditError as exc:
            db.rollback()
            return _error(exc.status, str(exc))

    return wrapper


# ---------------------------------------------------------------- page


@router.get("/", name="web_calendar", response_class=HTMLResponse)
def page(request: Request, token: str, db: Session = Depends(get_db)) -> Response:
    try:
        user = _user(request, db, token)
    except _Fail:
        return HTMLResponse("Not found", status_code=404, headers=PAGE_HEADERS)
    calendars = [
        {"slug": cal.slug, "name": cal.name, "color": cal.color, "can_write": m.can_write}
        for m, cal in _memberships(db, user)
    ]
    config = {
        "api": request.url_for("web_events", token=token).path,
        "user": user.display_name or user.username,
        "calendars": calendars,
    }
    return templates.TemplateResponse(
        request, "calendar.html", {"config": config, "user": user}, headers=PAGE_HEADERS
    )


# ---------------------------------------------------------------- API


def _parse_bound(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if "T" in value else datetime.fromisoformat(value + "T00:00:00")
    except ValueError:
        raise _Fail(400, "Invalid date range.")
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None)


@router.get("/api/events", name="web_events")
@_handle
def list_events(
    request: Request,
    token: str,
    start: str = Query(..., max_length=40),
    end: str = Query(..., max_length=40),
    db: Session = Depends(get_db),
):
    """Occurrences of all the user's calendars between `start` and `end`, in FullCalendar's event format."""
    user = _user(request, db, token)
    lo, hi = _parse_bound(start), _parse_bound(end)
    if hi <= lo:
        raise _Fail(400, "Invalid date range.")
    hi = min(hi, lo + MAX_RANGE)

    out: list[dict] = []
    for membership, cal in _memberships(db, user):
        stmt = select(Event).where(Event.calendar_id == cal.id)
        condition = store.time_range_condition(_naive_utc(lo), _naive_utc(hi))
        if condition is not None:
            stmt = stmt.where(condition)
        for ev in db.scalars(stmt):
            try:
                data = ICalendar.from_ical(ev.ical)
                found = editing.occurrences(data, lo, hi)
            except Exception:  # one unreadable event shouldn't break the whole calendar
                log.warning("Could not expand event %s in calendar %s", ev.id, cal.slug, exc_info=True)
                continue
            owner = store.owner_labels(ev.owner)[1]
            can_edit = store.can_modify(user, membership, ev)
            for occ in found:
                item = editing.describe_component(occ)
                rid = ""
                if ev.is_recurring and "RECURRENCE-ID" in occ:
                    rid = editing.iso(occ.decoded("RECURRENCE-ID"))
                out.append({
                    "id": f"{ev.id}/{rid}",
                    "title": item["title"],
                    "start": item["start"],
                    "end": item["end"],
                    "allDay": item["all_day"],
                    "color": cal.color,
                    "editable": can_edit and not ev.is_recurring,
                    "extendedProps": {
                        "event_id": ev.id,
                        "calendar": cal.slug,
                        "owner": owner,
                        "location": item["location"],
                        "recurring": ev.is_recurring,
                        "occurrence": rid,
                        "can_edit": can_edit,
                        "etag": ev.etag,
                    },
                })
                if len(out) >= MAX_INSTANCES:
                    return JSONResponse(out, headers={"Cache-Control": "no-store"})
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


@router.get("/api/events/{event_id}", name="web_event")
@_handle
def get_event(
    request: Request,
    token: str,
    event_id: int,
    occurrence: str | None = Query(None, max_length=40),
    db: Session = Depends(get_db),
):
    """Everything the details and edit dialogs need for one event (and optionally one of its occurrences)."""
    user = _user(request, db, token)
    membership, cal, ev = _event(db, user, event_id)
    data = ICalendar.from_ical(ev.ical)
    master = editing.master_of(data)
    info = repeat_info(master)
    result = {
        "id": ev.id,
        "etag": ev.etag,
        "calendar": cal.slug,
        "calendar_name": cal.name,
        "color": cal.color,
        "owner": store.owner_labels(ev.owner)[1],
        "can_edit": store.can_modify(user, membership, ev),
        "recurring": ev.is_recurring,
        "series": editing.describe_component(master),
        "repeat_kind": info.kind,
        "repeat": info.repeat.model_dump(mode="json") if info.repeat else None,
        "repeat_text": info.text,
        "has_exceptions": editing.has_exceptions(data),
        "occurrence": None,
    }
    if occurrence and ev.is_recurring:
        _, occ = editing.find_occurrence(data, editing.parse_occurrence(occurrence))
        result["occurrence"] = {"id": occurrence, **editing.describe_component(occ)}
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


def _fields(body: EventIn) -> Fields:
    return Fields(body.title, body.location, body.description)


@router.post("/api/events", status_code=201)
@_handle
def create_event(request: Request, token: str, body: EventIn, db: Session = Depends(get_db)):
    user = _user(request, db, token)
    found = next(((m, c) for m, c in _memberships(db, user) if c.slug == body.calendar), None)
    if found is None:
        raise _Fail(404, "Choose a calendar.")
    membership, cal = found
    if not membership.can_write:
        raise _Fail(403, "You can only view this calendar.")
    times = editing.parse_times(body.start, body.end, body.all_day, body.tz)
    data = editing.new_calendar(_fields(body), times, body.repeat)
    ev = _save(db, request.app.state.settings, cal, None, data, user)
    db.commit()
    return JSONResponse({"id": ev.id}, status_code=201)


@router.put("/api/events/{event_id}")
@_handle
def update_event(request: Request, token: str, event_id: int, body: EventIn, db: Session = Depends(get_db)):
    user = _user(request, db, token)
    membership, cal, ev = _event(db, user, event_id)
    _require_modify(user, membership, ev)
    _check_etag(ev, body.etag)
    data = ICalendar.from_ical(ev.ical)
    times = editing.parse_times(body.start, body.end, body.all_day, body.tz)
    cleared = False
    if body.scope == "this" and ev.is_recurring:
        if not body.occurrence:
            raise _Fail(400, "Choose the occurrence to change.")
        editing.update_occurrence(data, editing.parse_occurrence(body.occurrence), _fields(body), times)
    else:
        cleared = editing.update_series(data, _fields(body), times, body.repeat, body.keep_repeat)
    _save(db, request.app.state.settings, cal, ev, data, user)
    db.commit()
    return JSONResponse({"id": ev.id, "exceptions_cleared": cleared})


@router.post("/api/events/{event_id}/move")
@_handle
def move_event(request: Request, token: str, event_id: int, body: MoveIn, db: Session = Depends(get_db)):
    """Drag and drop / resize of a non-repeating event."""
    user = _user(request, db, token)
    membership, cal, ev = _event(db, user, event_id)
    _require_modify(user, membership, ev)
    _check_etag(ev, body.etag)
    data = ICalendar.from_ical(ev.ical)
    editing.move(data, editing.parse_times(body.start, body.end, body.all_day, body.tz))
    _save(db, request.app.state.settings, cal, ev, data, user)
    db.commit()
    return JSONResponse({"id": ev.id})


@router.delete("/api/events/{event_id}")
@_handle
def delete_event(
    request: Request,
    token: str,
    event_id: int,
    scope: Literal["all", "this"] = "all",
    occurrence: str | None = Query(None, max_length=40),
    etag: str | None = None,
    db: Session = Depends(get_db),
):
    user = _user(request, db, token)
    membership, cal, ev = _event(db, user, event_id)
    _require_modify(user, membership, ev)
    _check_etag(ev, etag)
    if scope == "this" and ev.is_recurring:
        if not occurrence:
            raise _Fail(400, "Choose the occurrence to delete.")
        data = ICalendar.from_ical(ev.ical)
        editing.delete_occurrence(data, editing.parse_occurrence(occurrence))
        _save(db, request.app.state.settings, cal, ev, data, user)
    else:
        store.delete_event(db, cal, ev)
    db.commit()
    return Response(status_code=204)
