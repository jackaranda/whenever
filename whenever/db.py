"""Database engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str) -> Engine:
    if not database_url.startswith("sqlite"):
        return create_engine(database_url, pool_pre_ping=True)

    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return engine


# Columns added after the first release, which create_all() won't add to
# existing tables: (table, column, column type, statements to run afterwards).
_ADDED_COLUMNS = [
    ("calendars", "feed_token", "VARCHAR(128)",
     ["CREATE UNIQUE INDEX IF NOT EXISTS ix_calendars_feed_token ON calendars (feed_token)"]),
]


def init_db(engine: Engine) -> None:
    """Create missing tables, then add columns introduced since the database was created."""
    from .models import Base

    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, column, column_type, after in _ADDED_COLUMNS:
            if column not in {c["name"] for c in inspector.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"))
                for statement in after:
                    conn.execute(text(statement))


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as db:
        yield db
