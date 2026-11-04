"""Read-only ICS subscription feeds: /feed/{token}/{slug}.ics"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ical, store
from .db import get_db
from .models import Calendar, Event, Membership, User

router = APIRouter()


@router.get("/feed/{token}/{slug}.ics")
def feed(token: str, slug: str, request: Request, db: Session = Depends(get_db)) -> Response:
    user = db.scalar(select(User).where(User.token == token))
    if user is None or not user.active:
        return Response(status_code=404)
    cal = db.scalar(
        select(Calendar)
        .join(Membership, Membership.calendar_id == Calendar.id)
        .where(Membership.user_id == user.id, Calendar.slug == slug)
    )
    if cal is None:
        return Response(status_code=404)

    # The feed changes whenever an event changes (sync_seq), or when calendar
    # settings that appear in the feed change.
    etag = hashlib.sha256(
        f"{cal.id}:{cal.sync_seq}:{cal.name}:{cal.description}:{cal.timezone}:{cal.prefix_titles}".encode()
    ).hexdigest()[:32]
    headers = {"ETag": f'"{etag}"', "Cache-Control": "private, max-age=60"}
    if request.headers.get("if-none-match", "").strip('" ') == etag:
        return Response(status_code=304, headers=headers)

    events = db.scalars(select(Event).where(Event.calendar_id == cal.id).order_by(Event.dtstart_utc))
    data = ical.build_feed(
        cal.name,
        cal.description,
        cal.timezone,
        [store.annotated(ev, cal) for ev in events],
        request.app.state.settings.feed_refresh_minutes,
    )
    headers["Content-Disposition"] = f'inline; filename="{cal.slug}.ics"'
    return Response(data, media_type="text/calendar; charset=utf-8", headers=headers)
