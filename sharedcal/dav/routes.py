"""CalDAV endpoints.

Each user's DAV tree is reachable at two prefixes:

    /dav/t/{token}/      authenticated by the token in the path (no credentials needed)
    /dav/u/{username}/   authenticated by HTTP Basic (password or token as the password)
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import format_datetime
from urllib.parse import quote, unquote, urlparse

from fastapi import APIRouter, Request, Response
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import ical, store
from ..models import Calendar, Event, EventTombstone, Membership, User
from ..security import check_user_secret, parse_basic_auth
from . import xml as dx
from .props import DavContext, calendar_props, event_props, home_props, parse_sync_token, sync_token
from .xml import c, d

router = APIRouter()

DAV_METHODS = [
    "OPTIONS", "PROPFIND", "PROPPATCH", "REPORT", "GET", "HEAD", "PUT", "DELETE",
    "MKCOL", "MKCALENDAR", "MOVE", "COPY", "LOCK", "UNLOCK",
]
ALLOW = "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, PROPPATCH, REPORT"
DAV_HEADERS = {"DAV": "1, 3, calendar-access", "Allow": ALLOW}
XML_TYPE = "application/xml; charset=utf-8"
CAL_TYPE = "text/calendar; charset=utf-8"


class DavError(Exception):
    def __init__(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None):
        self.status = status
        self.body = body
        self.headers = headers or {}

    def response(self) -> Response:
        media = XML_TYPE if self.body.startswith(b"<?xml") else "text/plain"
        return Response(self.body, status_code=self.status, headers=self.headers, media_type=media)


def _unauthorized() -> Response:
    return Response(
        "Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="sharedcal", charset="UTF-8"'},
        media_type="text/plain",
    )


def _options() -> Response:
    return Response(status_code=200, headers=DAV_HEADERS)


def _multistatus(ms: ET.Element) -> Response:
    return Response(dx.serialize(ms), status_code=207, media_type=XML_TYPE)


def _root_path(request: Request) -> str:
    return request.scope.get("root_path", "").rstrip("/")


async def _read_body(request: Request, limit: int) -> bytes:
    """Read the request body, refusing anything much larger than an event can be."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise DavError(413, b"Request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def authenticate_basic(db: Session, request: Request) -> User | None:
    creds = parse_basic_auth(request.headers.get("authorization"))
    if creds is None:
        return None
    username, secret = creds
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None or not user.active:
        return None
    if not check_user_secret(user.username, secret, user.password_hash, user.token):
        return None
    return user


# ---------------------------------------------------------------- discovery


@router.api_route("/.well-known/caldav", methods=DAV_METHODS)
async def well_known(request: Request) -> Response:
    return Response(status_code=301, headers={"Location": f"{_root_path(request)}/dav/"})


@router.api_route("/dav", methods=DAV_METHODS)
@router.api_route("/dav/", methods=DAV_METHODS)
async def dav_root(request: Request) -> Response:
    """Entry point for clients that only know the server URL.

    It tells an authenticated client where its principal is.
    """
    if request.method == "OPTIONS":
        return _options()
    if request.method not in {"PROPFIND", "GET", "HEAD"}:
        return Response(status_code=405, headers={"Allow": "OPTIONS, GET, HEAD, PROPFIND"})
    try:
        body = await _read_body(request, 64 * 1024)
    except DavError as exc:
        return exc.response()
    return await run_in_threadpool(_dav_root, request, body)


def _dav_root(request: Request, body: bytes) -> Response:
    with request.app.state.sessionmaker() as db:
        user = authenticate_basic(db, request)
        if user is None:
            return _unauthorized()
        base = f"{_root_path(request)}/dav/u/{quote(user.username)}/"
        if request.method != "PROPFIND":
            return Response(status_code=303, headers={"Location": base})
        try:
            requested = dx.requested_props(dx.parse(body))
        except dx.XMLError:
            return Response("Malformed XML", status_code=400)
        ms = dx.multistatus()
        dx.add_response(
            ms,
            request.url.path,
            {
                d("resourcetype"): dx.empty_children(d("collection")),
                d("current-user-principal"): dx.hrefs(base),
                d("principal-URL"): dx.hrefs(base),
                c("calendar-home-set"): dx.hrefs(base),
            },
            requested,
        )
        return _multistatus(ms)


# ---------------------------------------------------------------- user tree


@router.api_route("/dav/{mode}/{ident}", methods=DAV_METHODS)
@router.api_route("/dav/{mode}/{ident}/{path:path}", methods=DAV_METHODS)
async def dav_tree(request: Request, mode: str, ident: str, path: str = "") -> Response:
    if request.method == "OPTIONS":
        return _options()
    settings = request.app.state.settings
    try:
        body = await _read_body(request, settings.max_event_size)
    except DavError as exc:
        return exc.response()
    return await run_in_threadpool(_dispatch, request, mode, ident, path, body)


def _dispatch(request: Request, mode: str, ident: str, path: str, body: bytes) -> Response:
    settings = request.app.state.settings
    with request.app.state.sessionmaker() as db:
        if mode == "t":
            if not settings.allow_token_urls:
                return Response(status_code=404)
            user = db.scalar(select(User).where(User.token == ident))
            if user is None or not user.active:
                return Response(status_code=404)
        elif mode == "u":
            user = authenticate_basic(db, request)
            if user is None or user.username != ident:
                return _unauthorized()
        else:
            return Response(status_code=404)

        ctx = DavContext(
            db=db,
            user=user,
            base=f"{_root_path(request)}/dav/{mode}/{quote(ident)}/",
            settings=settings,
            request=request,
        )
        parts = [p for p in path.split("/") if p]
        handler = _HANDLERS.get(request.method, _forbidden)
        try:
            if len(parts) > 2:
                raise DavError(404)
            response = handler(ctx, parts, body)
            db.commit()
            return response
        except DavError as exc:
            db.rollback()
            return exc.response()
        except dx.XMLError:
            db.rollback()
            return Response("Malformed XML", status_code=400)


def _membership(ctx: DavContext, slug: str) -> tuple[Membership, Calendar]:
    found = ctx.membership(slug)
    if found is None:
        raise DavError(404)
    return found


def _event(ctx: DavContext, cal: Calendar, name: str) -> Event | None:
    return ctx.db.scalar(select(Event).where(Event.calendar_id == cal.id, Event.name == name))


def _depth(ctx: DavContext) -> str:
    return ctx.request.headers.get("depth", "1").strip().lower()


def _propfind(ctx: DavContext, parts: list[str], body: bytes) -> Response:
    requested = dx.requested_props(dx.parse(body))
    deep = _depth(ctx) != "0"
    ms = dx.multistatus()

    if not parts:
        dx.add_response(ms, ctx.base, home_props(ctx), requested)
        if deep:
            for m, cal in ctx.memberships():
                dx.add_response(ms, ctx.calendar_href(cal), calendar_props(ctx, m, cal), requested)
    elif len(parts) == 1:
        m, cal = _membership(ctx, parts[0])
        dx.add_response(ms, ctx.calendar_href(cal), calendar_props(ctx, m, cal), requested)
        if deep:
            for ev in ctx.db.scalars(select(Event).where(Event.calendar_id == cal.id)):
                dx.add_response(ms, ctx.event_href(cal, ev), event_props(ctx, m, cal, ev), requested)
    else:
        m, cal = _membership(ctx, parts[0])
        ev = _event(ctx, cal, parts[1])
        if ev is None:
            raise DavError(404)
        dx.add_response(ms, ctx.event_href(cal, ev), event_props(ctx, m, cal, ev), requested)
    return _multistatus(ms)


def _proppatch(ctx: DavContext, parts: list[str], body: bytes) -> Response:
    """Collection and event properties are managed in the admin UI, so every change is refused."""
    root = dx.parse(body)
    tags = [] if root is None else [p.tag for block in root for prop in block for p in prop]
    ms = dx.multistatus()
    resp = ET.SubElement(ms, d("response"))
    ET.SubElement(resp, d("href")).text = ctx.request.url.path
    propstat = ET.SubElement(resp, d("propstat"))
    prop = ET.SubElement(propstat, d("prop"))
    for tag in tags:
        ET.SubElement(prop, tag)
    ET.SubElement(propstat, d("status")).text = "HTTP/1.1 403 Forbidden"
    return _multistatus(ms)


# ---------------------------------------------------------------- REPORT


def _report(ctx: DavContext, parts: list[str], body: bytes) -> Response:
    root = dx.parse(body)
    if root is None:
        raise DavError(400, b"REPORT requires a body")
    if len(parts) != 1:
        raise DavError(403, dx.error(d("supported-report")))
    m, cal = _membership(ctx, parts[0])
    requested = dx.requested_props(root)
    ms = dx.multistatus()

    if root.tag == c("calendar-multiget"):
        for href_el in root.iter(d("href")):
            href = (href_el.text or "").strip()
            name = _name_from_href(href, ctx.calendar_href(cal))
            ev = _event(ctx, cal, name) if name else None
            if ev is None:
                dx.add_status(ms, href, "HTTP/1.1 404 Not Found")
            else:
                dx.add_response(ms, ctx.event_href(cal, ev), event_props(ctx, m, cal, ev), requested)

    elif root.tag == c("calendar-query"):
        for ev in _query_events(ctx, cal, root):
            dx.add_response(ms, ctx.event_href(cal, ev), event_props(ctx, m, cal, ev), requested)

    elif root.tag == d("sync-collection"):
        token_el = root.find(d("sync-token"))
        token = (token_el.text or "").strip() if token_el is not None else ""
        since = 0
        if token:
            parsed = parse_sync_token(token, cal)
            if parsed is None:
                raise DavError(403, dx.error(d("valid-sync-token")))
            since = parsed
        events = list(ctx.db.scalars(select(Event).where(Event.calendar_id == cal.id, Event.seq > since)))
        for ev in events:
            dx.add_response(ms, ctx.event_href(cal, ev), event_props(ctx, m, cal, ev), requested)
        if since:
            live = {ev.name for ev in events}
            for tomb in ctx.db.scalars(
                select(EventTombstone).where(EventTombstone.calendar_id == cal.id, EventTombstone.seq > since)
            ):
                if tomb.name not in live:
                    dx.add_status(ms, f"{ctx.calendar_href(cal)}{quote(tomb.name)}", "HTTP/1.1 404 Not Found")
        ET.SubElement(ms, d("sync-token")).text = sync_token(cal)

    else:
        raise DavError(403, dx.error(d("supported-report")))

    return _multistatus(ms)


def _name_from_href(href: str, collection_href: str) -> str | None:
    path = unquote(urlparse(href).path)
    prefix = unquote(collection_href)
    if not path.startswith(prefix):
        return None
    name = path[len(prefix):]
    return name if name and "/" not in name else None


def _parse_ical_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y%m%dT%H%M%SZ")
    except ValueError:
        raise DavError(403, dx.error(c("valid-filter")))


def _query_events(ctx: DavContext, cal: Calendar, root: ET.Element) -> list[Event]:
    """Handle the calendar-query filters clients actually use: a VEVENT comp-filter
    with an optional time-range and an optional UID prop-filter.

    Other filters are ignored, so the result may include events that don't match them (a superset).
    """
    stmt = select(Event).where(Event.calendar_id == cal.id)
    vcal = root.find(f"{c('filter')}/{c('comp-filter')}")
    comp = vcal.find(c("comp-filter")) if vcal is not None else None
    if comp is not None:
        if comp.get("name", "").upper() != "VEVENT":
            return []
        time_range = comp.find(c("time-range"))
        if time_range is not None:
            start = _parse_ical_utc(time_range.get("start"))
            end = _parse_ical_utc(time_range.get("end"))
            conditions = []
            if end is not None:
                conditions.append(Event.dtstart_utc < end)
            if start is not None:
                conditions.append(or_(Event.dtend_utc > start, Event.dtstart_utc >= start))
            if conditions:
                stmt = stmt.where(or_(Event.is_recurring, Event.dtstart_utc.is_(None), and_(*conditions)))
        for prop_filter in comp.findall(c("prop-filter")):
            text_match = prop_filter.find(c("text-match"))
            if prop_filter.get("name", "").upper() != "UID" or text_match is None or not text_match.text:
                continue
            value = text_match.text.strip()
            matches = Event.uid == value if text_match.get("match-type") == "equals" else Event.uid.contains(value)
            stmt = stmt.where(~matches if text_match.get("negate-condition") == "yes" else matches)
    return list(ctx.db.scalars(stmt))


# ---------------------------------------------------------------- GET / PUT / DELETE


def _get(ctx: DavContext, parts: list[str], body: bytes) -> Response:
    head = ctx.request.method == "HEAD"
    if not parts:
        return Response(b"" if head else b"sharedcal calendar home\n", media_type="text/plain")
    m, cal = _membership(ctx, parts[0])
    if len(parts) == 1:
        events = list(ctx.db.scalars(select(Event).where(Event.calendar_id == cal.id)))
        data = ical.build_feed(
            cal.name, cal.description, cal.timezone,
            [store.annotated(ev, cal) for ev in events],
            ctx.settings.feed_refresh_minutes,
        )
        return Response(b"" if head else data, media_type=CAL_TYPE)
    ev = _event(ctx, cal, parts[1])
    if ev is None:
        raise DavError(404)
    data = store.render(ev, cal)
    headers = {
        "ETag": f'"{ev.etag}"',
        "Last-Modified": format_datetime(ev.updated_at.replace(tzinfo=UTC), usegmt=True),
    }
    return Response(b"" if head else data, media_type=CAL_TYPE, headers=headers)


def _etags(header: str) -> set[str]:
    return {tag.strip().removeprefix("W/").strip('"') for tag in header.split(",") if tag.strip()}


def _check_preconditions(ctx: DavContext, existing: Event | None) -> None:
    if_match = ctx.request.headers.get("if-match")
    if if_match is not None:
        tags = _etags(if_match)
        if existing is None or ("*" not in tags and existing.etag not in tags):
            raise DavError(412, b"ETag mismatch")
    if_none_match = ctx.request.headers.get("if-none-match")
    if if_none_match is not None and existing is not None:
        tags = _etags(if_none_match)
        if "*" in tags or existing.etag in tags:
            raise DavError(412, b"Resource exists")


def _put(ctx: DavContext, parts: list[str], body: bytes) -> Response:
    if len(parts) != 2:
        raise DavError(405, headers={"Allow": ALLOW})
    m, cal = _membership(ctx, parts[0])
    if not m.can_write:
        raise DavError(403, dx.error(d("need-privileges")))
    name = parts[1]
    existing = _event(ctx, cal, name)
    _check_preconditions(ctx, existing)
    try:
        parsed = ical.parse_event(body)
    except ical.InvalidCalendarData as exc:
        raise DavError(403, dx.error(c(exc.precondition)))

    if existing is not None:
        if not ctx.can_modify(m, existing):
            raise DavError(403, dx.error(d("need-privileges")))
        if parsed.uid != existing.uid:
            raise DavError(403, dx.error(c("no-uid-conflict"), ctx.event_href(cal, existing)))
        store.update_event(ctx.db, cal, existing, parsed)
        status = 204
    else:
        clash = ctx.db.scalar(select(Event).where(Event.calendar_id == cal.id, Event.uid == parsed.uid))
        if clash is not None:
            raise DavError(403, dx.error(c("no-uid-conflict"), ctx.event_href(cal, clash)))
        store.create_event(ctx.db, cal, name, parsed, ctx.user)
        status = 201
    # No ETag is returned: the stored event is served with creator annotations
    # added, so it differs from what the client sent (RFC 4791 §5.3.4). The
    # client fetches it again to get the real ETag.
    return Response(status_code=status)


def _delete(ctx: DavContext, parts: list[str], body: bytes) -> Response:
    if len(parts) != 2:
        raise DavError(403, b"Calendars are managed in the admin interface")
    m, cal = _membership(ctx, parts[0])
    ev = _event(ctx, cal, parts[1])
    if ev is None:
        raise DavError(404)
    _check_preconditions(ctx, ev)
    if not ctx.can_modify(m, ev):
        raise DavError(403, dx.error(d("need-privileges")))
    store.delete_event(ctx.db, cal, ev)
    return Response(status_code=204)


def _forbidden(ctx: DavContext, parts: list[str], body: bytes) -> Response:
    raise DavError(403, b"Not supported")


_HANDLERS = {
    "PROPFIND": _propfind,
    "PROPPATCH": _proppatch,
    "REPORT": _report,
    "GET": _get,
    "HEAD": _get,
    "PUT": _put,
    "DELETE": _delete,
}
