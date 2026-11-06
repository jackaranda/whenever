"""Database engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import create_engine, event
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


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as db:
        yield db
