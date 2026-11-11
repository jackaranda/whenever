"""ORM models."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .security import new_token


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, default=new_token)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    memberships: Mapped[list[Membership]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Calendar(Base):
    __tablename__ = "calendars"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    color: Mapped[str] = mapped_column(String(16), default="#3a87ad")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    # Prefix event titles with "[Creator]" when serving them.
    prefix_titles: Mapped[bool] = mapped_column(Boolean, default=False)
    # Secret for the shareable read-only feed at /feed/c/{feed_token}.ics; None while it's turned off.
    feed_token: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True, index=True)
    # Goes up by one on every change to the calendar's events; used for the ctag and sync-token.
    sync_seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    memberships: Mapped[list[Membership]] = relationship(back_populates="calendar", cascade="all, delete-orphan")
    events: Mapped[list[Event]] = relationship(back_populates="calendar", cascade="all, delete-orphan", passive_deletes=True)


class Membership(Base):
    __tablename__ = "memberships"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    calendar_id: Mapped[int] = mapped_column(ForeignKey("calendars.id", ondelete="CASCADE"), primary_key=True)
    can_write: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped[User] = relationship(back_populates="memberships")
    calendar: Mapped[Calendar] = relationship(back_populates="memberships")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("calendar_id", "name"),
        UniqueConstraint("calendar_id", "uid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    calendar_id: Mapped[int] = mapped_column(ForeignKey("calendars.id", ondelete="CASCADE"), index=True)
    # Resource name within the collection, e.g. "abc123.ics".
    name: Mapped[str] = mapped_column(String(255))
    uid: Mapped[str] = mapped_column(String(255))
    # VCALENDAR text as stored, with the creator annotations stripped.
    ical: Mapped[str] = mapped_column(Text)
    # ETag of the annotated representation served to clients.
    etag: Mapped[str] = mapped_column(String(64))
    dtstart_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    dtend_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    seq: Mapped[int] = mapped_column(Integer, default=0, index=True)

    calendar: Mapped[Calendar] = relationship(back_populates="events")
    owner: Mapped[User | None] = relationship()


class EventTombstone(Base):
    """Records a deleted resource so sync-collection can report it as gone."""

    __tablename__ = "event_tombstones"

    id: Mapped[int] = mapped_column(primary_key=True)
    calendar_id: Mapped[int] = mapped_column(ForeignKey("calendars.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    seq: Mapped[int] = mapped_column(Integer, index=True)
