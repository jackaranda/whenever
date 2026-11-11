"""Simple HTML admin interface for users, calendars and memberships."""

from __future__ import annotations

import hmac
import re
import secrets
from pathlib import Path
from functools import cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import store
from ..db import get_db
from ..models import Calendar, Event, Membership, User
from ..security import hash_password, new_token, verify_password

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class LoginRequired(Exception):
    pass


async def login_required_handler(request: Request, exc: LoginRequired) -> Response:
    return RedirectResponse(request.url_for("login_form"), status_code=303)


# ---------------------------------------------------------------- helpers


def current_admin(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user is None or not user.active or not user.is_admin:
        request.session.pop("uid", None)
        raise LoginRequired()
    return user


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def check_csrf(request: Request) -> None:
    form = await request.form()
    sent = str(form.get("csrf", ""))
    expected = request.session.get("csrf", "")
    if not expected or not hmac.compare_digest(sent, expected):
        raise HTTPException(status_code=403, detail="Invalid CSRF token; reload the page and try again.")


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, message])


def render(request: Request, template: str, admin: User | None = None, status_code: int = 200, **context) -> HTMLResponse:
    messages = request.session.pop("flash", [])
    return templates.TemplateResponse(
        request,
        template,
        {"admin": admin, "csrf": csrf_token(request), "messages": messages, **context},
        status_code=status_code,
    )


def redirect(request: Request, route: str, **params) -> RedirectResponse:
    return RedirectResponse(request.url_for(route, **params), status_code=303)


def public_base(request: Request) -> str:
    return request.app.state.settings.base_url or str(request.base_url).rstrip("/")


# ---------------------------------------------------------------- auth


@router.get("/login", name="login_form")
def login_form(request: Request) -> HTMLResponse:
    return render(request, "login.html")


@router.post("/login", dependencies=[Depends(check_csrf)])
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None or not user.active or not user.is_admin or not verify_password(password, user.password_hash):
        flash(request, "Invalid username or password, or the account is not an admin.", "error")
        return render(request, "login.html", status_code=401)
    request.session.clear()
    request.session["uid"] = user.id
    return redirect(request, "users")


@router.post("/logout", dependencies=[Depends(check_csrf)])
def logout(request: Request) -> Response:
    request.session.clear()
    return redirect(request, "login_form")


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
def index(request: Request, admin: User = Depends(current_admin)) -> Response:
    return redirect(request, "users")


# ---------------------------------------------------------------- users


@router.get("/users", name="users")
def users(request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> HTMLResponse:
    rows = db.execute(
        select(User, func.count(Membership.calendar_id))
        .outerjoin(Membership, Membership.user_id == User.id)
        .group_by(User.id)
        .order_by(User.username)
    ).all()
    return render(request, "users.html", admin, rows=rows)


@router.post("/users", dependencies=[Depends(check_csrf)])
def create_user(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    is_admin: bool = Form(False),
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> Response:
    username = username.strip().lower()
    if not USERNAME_RE.match(username):
        flash(request, "Usernames may contain lowercase letters, digits, '.', '_' and '-'.", "error")
        return redirect(request, "users")
    if db.scalar(select(User).where(User.username == username)):
        flash(request, f"User '{username}' already exists.", "error")
        return redirect(request, "users")
    if is_admin and not password:
        flash(request, "Admin accounts need a password.", "error")
        return redirect(request, "users")
    user = User(
        username=username,
        display_name=display_name.strip() or username,
        email=email.strip(),
        password_hash=hash_password(password) if password else None,
        is_admin=is_admin,
    )
    db.add(user)
    db.commit()
    flash(request, f"Created user '{username}'.")
    return redirect(request, "user_detail", user_id=user.id)


def _get_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such user")
    return user


@router.get("/users/{user_id}", name="user_detail")
def user_detail(
    request: Request, user_id: int, admin: User = Depends(current_admin), db: Session = Depends(get_db)
) -> HTMLResponse:
    user = _get_user(db, user_id)
    memberships = db.execute(
        select(Membership, Calendar)
        .join(Calendar, Membership.calendar_id == Calendar.id)
        .where(Membership.user_id == user.id)
        .order_by(Calendar.name)
    ).all()
    event_count = db.scalar(select(func.count(Event.id)).where(Event.owner_id == user.id))
    others = db.scalars(select(User).where(User.id != user.id).order_by(User.username)).all()
    return render(
        request,
        "user_detail.html",
        admin,
        user=user,
        memberships=memberships,
        event_count=event_count,
        others=others,
        base=public_base(request),
        allow_token_urls=request.app.state.settings.allow_token_urls,
    )


@router.post("/users/{user_id}", dependencies=[Depends(check_csrf)])
def update_user(
    request: Request,
    user_id: int,
    display_name: str = Form(""),
    email: str = Form(""),
    is_admin: bool = Form(False),
    active: bool = Form(False),
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> Response:
    user = _get_user(db, user_id)
    if user.id == admin.id and (not is_admin or not active):
        flash(request, "You can't remove your own admin rights or deactivate yourself.", "error")
        return redirect(request, "user_detail", user_id=user.id)
    if is_admin and not user.password_hash:
        flash(request, "Set a password before making this user an admin.", "error")
        return redirect(request, "user_detail", user_id=user.id)
    new_name = display_name.strip() or user.username
    renamed = new_name != user.display_name
    user.display_name = new_name
    user.email = email.strip()
    user.is_admin = is_admin
    user.active = active
    if renamed:
        # The creator's name is part of every event they created, so those events change too.
        store.refresh_events(db, store.events_owned_by(db, user))
    db.commit()
    flash(request, "Saved.")
    return redirect(request, "user_detail", user_id=user.id)


@router.post("/users/{user_id}/password", dependencies=[Depends(check_csrf)])
def set_password(
    request: Request,
    user_id: int,
    password: str = Form(""),
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> Response:
    user = _get_user(db, user_id)
    if not password:
        if user.is_admin:
            flash(request, "Admins must have a password.", "error")
            return redirect(request, "user_detail", user_id=user.id)
        user.password_hash = None
        flash(request, "Password removed; this user can now only sign in with their token.")
    else:
        user.password_hash = hash_password(password)
        flash(request, "Password updated.")
    db.commit()
    return redirect(request, "user_detail", user_id=user.id)


@router.post("/users/{user_id}/token", dependencies=[Depends(check_csrf)])
def rotate_token(
    request: Request, user_id: int, admin: User = Depends(current_admin), db: Session = Depends(get_db)
) -> Response:
    user = _get_user(db, user_id)
    user.token = new_token()
    db.commit()
    flash(request, "New token issued. The user's old URLs no longer work.")
    return redirect(request, "user_detail", user_id=user.id)


@router.post("/users/{user_id}/delete", dependencies=[Depends(check_csrf)])
def delete_user(
    request: Request,
    user_id: int,
    events: str = Form("reassign"),
    reassign_to: int | None = Form(None),
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> Response:
    user = _get_user(db, user_id)
    if user.id == admin.id:
        flash(request, "You can't delete your own account.", "error")
        return redirect(request, "user_detail", user_id=user.id)
    owned = store.events_owned_by(db, user)
    if events == "delete":
        for ev in owned:
            store.delete_event(db, ev.calendar, ev)
    elif events == "reassign":
        target = db.get(User, reassign_to) if reassign_to else None
        if target is None or target.id == user.id:
            flash(request, "Choose a user to take over the events.", "error")
            return redirect(request, "user_detail", user_id=user.id)
        for ev in owned:
            ev.owner = target
        store.refresh_events(db, owned)
    else:
        for ev in owned:
            ev.owner = None
        store.refresh_events(db, owned)
    name = user.username
    db.delete(user)
    db.commit()
    flash(request, f"Deleted user '{name}'.")
    return redirect(request, "users")


# ---------------------------------------------------------------- calendars


@router.get("/calendars", name="calendars")
def calendars(request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> HTMLResponse:
    member_counts = dict(
        db.execute(select(Membership.calendar_id, func.count()).group_by(Membership.calendar_id)).all()
    )
    event_counts = dict(db.execute(select(Event.calendar_id, func.count()).group_by(Event.calendar_id)).all())
    cals = db.scalars(select(Calendar).order_by(Calendar.name)).all()
    return render(
        request, "calendars.html", admin, calendars=cals, member_counts=member_counts,
        event_counts=event_counts, timezones=timezone_names(),
    )


@cache
def timezone_names() -> list[str]:
    return sorted(available_timezones())


def _valid_timezone(tz: str) -> bool:
    try:
        ZoneInfo(tz)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def _calendar_form_errors(slug: str | None, color: str, timezone: str) -> str | None:
    if slug is not None and not SLUG_RE.match(slug):
        return "Slugs may contain lowercase letters, digits and '-'."
    if not COLOR_RE.match(color):
        return "Colour must look like #3a87ad."
    if not _valid_timezone(timezone):
        return f"Unknown timezone '{timezone}'."
    return None


@router.post("/calendars", dependencies=[Depends(check_csrf)])
def create_calendar(
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    description: str = Form(""),
    color: str = Form("#3a87ad"),
    timezone: str = Form("UTC"),
    prefix_titles: bool = Form(False),
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> Response:
    name = name.strip()
    slug = (slug.strip() or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")).lower()
    timezone = timezone.strip() or "UTC"
    err = None if name else "A name is required."
    err = err or _calendar_form_errors(slug, color, timezone)
    if not err and db.scalar(select(Calendar).where(Calendar.slug == slug)):
        err = f"A calendar with slug '{slug}' already exists."
    if err:
        flash(request, err, "error")
        return redirect(request, "calendars")
    cal = Calendar(
        name=name, slug=slug, description=description.strip(), color=color,
        timezone=timezone, prefix_titles=prefix_titles,
    )
    db.add(cal)
    db.commit()
    flash(request, f"Created calendar '{name}'.")
    return redirect(request, "calendar_detail", calendar_id=cal.id)


def _get_calendar(db: Session, calendar_id: int) -> Calendar:
    cal = db.get(Calendar, calendar_id)
    if cal is None:
        raise HTTPException(status_code=404, detail="No such calendar")
    return cal


@router.get("/calendars/{calendar_id}", name="calendar_detail")
def calendar_detail(
    request: Request, calendar_id: int, admin: User = Depends(current_admin), db: Session = Depends(get_db)
) -> HTMLResponse:
    cal = _get_calendar(db, calendar_id)
    members = db.execute(
        select(Membership, User)
        .join(User, Membership.user_id == User.id)
        .where(Membership.calendar_id == cal.id)
        .order_by(User.username)
    ).all()
    event_count = db.scalar(select(func.count(Event.id)).where(Event.calendar_id == cal.id))
    return render(
        request, "calendar_detail.html", admin, cal=cal, members=members,
        event_count=event_count, timezones=timezone_names(), base=public_base(request),
    )


@router.post("/calendars/{calendar_id}", dependencies=[Depends(check_csrf)])
def update_calendar(
    request: Request,
    calendar_id: int,
    name: str = Form(...),
    description: str = Form(""),
    color: str = Form("#3a87ad"),
    timezone: str = Form("UTC"),
    prefix_titles: bool = Form(False),
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> Response:
    cal = _get_calendar(db, calendar_id)
    timezone = timezone.strip() or "UTC"
    err = None if name.strip() else "A name is required."
    err = err or _calendar_form_errors(None, color, timezone)
    if err:
        flash(request, err, "error")
        return redirect(request, "calendar_detail", calendar_id=cal.id)
    prefix_changed = prefix_titles != cal.prefix_titles
    cal.name = name.strip()
    cal.description = description.strip()
    cal.color = color
    cal.timezone = timezone
    cal.prefix_titles = prefix_titles
    if prefix_changed:
        store.refresh_events(db, list(cal.events))
    db.commit()
    flash(request, "Saved.")
    return redirect(request, "calendar_detail", calendar_id=cal.id)


@router.post("/calendars/{calendar_id}/feed", dependencies=[Depends(check_csrf)])
def calendar_feed_link(
    request: Request,
    calendar_id: int,
    action: str = Form(...),
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> Response:
    """Turn the calendar's shareable read-only feed on or off, or replace its link."""
    cal = _get_calendar(db, calendar_id)
    if action == "enable":
        cal.feed_token = cal.feed_token or new_token()
        flash(request, "Shareable feed turned on. Anyone with the link can see this calendar's events.")
    elif action == "rotate":
        cal.feed_token = new_token()
        flash(request, "New feed link issued. The old link no longer works.")
    elif action == "disable":
        cal.feed_token = None
        flash(request, "Shareable feed turned off. The link no longer works.")
    else:
        raise HTTPException(status_code=400, detail="Unknown action")
    db.commit()
    return redirect(request, "calendar_detail", calendar_id=cal.id)


@router.post("/calendars/{calendar_id}/delete", dependencies=[Depends(check_csrf)])
def delete_calendar(
    request: Request,
    calendar_id: int,
    confirm: str = Form(""),
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> Response:
    cal = _get_calendar(db, calendar_id)
    if confirm.strip() != cal.slug:
        flash(request, f"Type '{cal.slug}' to confirm deleting this calendar.", "error")
        return redirect(request, "calendar_detail", calendar_id=cal.id)
    name = cal.name
    db.delete(cal)
    db.commit()
    flash(request, f"Deleted calendar '{name}' and all its events.")
    return redirect(request, "calendars")


# ---------------------------------------------------------------- memberships


@router.get("/memberships", name="memberships")
def memberships(request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> HTMLResponse:
    users_ = db.scalars(select(User).order_by(User.username)).all()
    cals = db.scalars(select(Calendar).order_by(Calendar.name)).all()
    current = {(m.user_id, m.calendar_id): m for m in db.scalars(select(Membership))}
    return render(request, "memberships.html", admin, users=users_, calendars=cals, current=current)


def _membership_key(key: str, prefix: str) -> tuple[int, int] | None:
    if not key.startswith(prefix):
        return None
    try:
        uid, cid = (int(x) for x in key[len(prefix):].split("_"))
    except ValueError:
        return None
    return uid, cid


@router.post("/memberships", dependencies=[Depends(check_csrf)])
async def save_memberships(
    request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    # Checkbox names: m_{user}_{calendar} for membership, w_{user}_{calendar} for write access.
    wanted: dict[tuple[int, int], bool] = {}
    for key in form.keys():
        ids = _membership_key(key, "m_")
        if ids:
            wanted[ids] = f"w_{key[2:]}" in form

    valid_users = set(db.scalars(select(User.id)))
    valid_cals = set(db.scalars(select(Calendar.id)))
    existing = {(m.user_id, m.calendar_id): m for m in db.scalars(select(Membership))}
    added = removed = changed = 0
    for key, membership in existing.items():
        if key not in wanted:
            db.delete(membership)
            removed += 1
        elif membership.can_write != wanted[key]:
            membership.can_write = wanted[key]
            changed += 1
    for (uid, cid), can_write in wanted.items():
        if (uid, cid) not in existing and uid in valid_users and cid in valid_cals:
            db.add(Membership(user_id=uid, calendar_id=cid, can_write=can_write))
            added += 1
    db.commit()
    flash(request, f"Memberships saved: {added} added, {removed} removed, {changed} changed.")
    return redirect(request, "memberships")
