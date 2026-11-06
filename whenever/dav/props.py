"""WebDAV properties for each kind of resource in a user's DAV tree.

Each user's tree looks like this:

    {base}/                 principal and calendar home (combined, as Radicale does)
    {base}/{slug}/          a calendar the user is a member of
    {base}/{slug}/{name}    one event resource
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from email.utils import format_datetime
from urllib.parse import quote
from xml.etree.ElementTree import SubElement

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import store
from ..config import Settings
from ..models import Calendar, Event, Membership, User
from .xml import (
    PropMap,
    apple,
    c,
    cs,
    d,
    empty_children,
    hrefs,
    privileges,
    supported_reports,
)

EVENT_CONTENT_TYPE = "text/calendar; charset=utf-8; component=vevent"


@dataclass
class DavContext:
    db: Session
    user: User
    base: str  # href of the user's principal/home, ending in "/"
    settings: Settings
    request: Request

    def calendar_href(self, cal: Calendar) -> str:
        return f"{self.base}{quote(cal.slug)}/"

    def event_href(self, cal: Calendar, ev: Event) -> str:
        return f"{self.calendar_href(cal)}{quote(ev.name)}"

    def memberships(self) -> list[tuple[Membership, Calendar]]:
        rows = self.db.execute(
            select(Membership, Calendar)
            .join(Calendar, Membership.calendar_id == Calendar.id)
            .where(Membership.user_id == self.user.id)
            .order_by(Calendar.name)
        )
        return [(m, cal) for m, cal in rows]

    def membership(self, slug: str) -> tuple[Membership, Calendar] | None:
        row = self.db.execute(
            select(Membership, Calendar)
            .join(Calendar, Membership.calendar_id == Calendar.id)
            .where(Membership.user_id == self.user.id, Calendar.slug == slug)
        ).first()
        return (row[0], row[1]) if row else None

    def can_modify(self, membership: Membership, ev: Event) -> bool:
        return membership.can_write and (ev.owner_id == self.user.id or self.user.is_admin)


def sync_token(cal: Calendar) -> str:
    return f"urn:whenever:sync:{cal.id}:{cal.sync_seq}"


def parse_sync_token(token: str, cal: Calendar) -> int | None:
    prefix = f"urn:whenever:sync:{cal.id}:"
    if not token.startswith(prefix):
        return None
    try:
        seq = int(token[len(prefix):])
    except ValueError:
        return None
    return seq if 0 <= seq <= cal.sync_seq else None


def _common(ctx: DavContext) -> PropMap:
    return {
        d("current-user-principal"): hrefs(ctx.base),
        d("principal-URL"): hrefs(ctx.base),
        d("owner"): hrefs(ctx.base),
    }


def home_props(ctx: DavContext) -> PropMap:
    user = ctx.user
    address = f"mailto:{user.email}" if user.email else ctx.base
    return {
        d("resourcetype"): empty_children(d("collection"), d("principal")),
        d("displayname"): user.display_name or user.username,
        **_common(ctx),
        c("calendar-home-set"): hrefs(ctx.base),
        c("calendar-user-address-set"): hrefs(address, ctx.base),
        d("current-user-privilege-set"): privileges(d("read")),
        d("supported-report-set"): supported_reports(d("sync-collection")),
    }


def calendar_props(ctx: DavContext, membership: Membership, cal: Calendar) -> PropMap:
    privs = [d("read")]
    if membership.can_write:
        privs += [d("write-content"), d("bind"), d("unbind")]
    return {
        d("resourcetype"): empty_children(d("collection"), c("calendar")),
        d("displayname"): cal.name,
        **_common(ctx),
        c("calendar-description"): cal.description or "",
        c("calendar-timezone-id"): cal.timezone,
        apple("calendar-color"): cal.color,
        c("supported-calendar-component-set"): _components("VEVENT"),
        c("supported-calendar-data"): _calendar_data_types,
        c("max-resource-size"): str(ctx.settings.max_event_size),
        cs("getctag"): str(cal.sync_seq),
        d("sync-token"): sync_token(cal),
        d("current-user-privilege-set"): privileges(*privs),
        d("supported-report-set"): supported_reports(
            c("calendar-multiget"), c("calendar-query"), d("sync-collection")
        ),
    }


def event_props(ctx: DavContext, membership: Membership, cal: Calendar, ev: Event) -> PropMap:
    privs = [d("read")]
    if ctx.can_modify(membership, ev):
        privs += [d("write"), d("write-content"), d("write-properties")]

    def calendar_data(el) -> None:
        el.text = store.render(ev, cal).decode("utf-8")

    return {
        d("resourcetype"): "",
        d("getetag"): f'"{ev.etag}"',
        d("getcontenttype"): EVENT_CONTENT_TYPE,
        d("getlastmodified"): format_datetime(ev.updated_at.replace(tzinfo=UTC), usegmt=True),
        d("current-user-principal"): hrefs(ctx.base),
        d("current-user-privilege-set"): privileges(*privs),
        c("calendar-data"): calendar_data,
    }


def _components(*names: str):
    def fill(el) -> None:
        for name in names:
            SubElement(el, c("comp"), {"name": name})

    return fill


def _calendar_data_types(el) -> None:
    SubElement(el, c("calendar-data"), {"content-type": "text/calendar", "version": "2.0"})
