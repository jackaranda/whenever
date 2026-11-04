"""Command line: python -m sharedcal {serve,init-db,create-admin}"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

from .admin.routes import USERNAME_RE
from .config import Settings
from .db import make_engine, make_sessionmaker
from .models import Base, User
from .security import hash_password


def _sessionmaker():
    engine = make_engine(Settings.from_env().database_url)
    Base.metadata.create_all(engine)
    return make_sessionmaker(engine)


def init_db(_args) -> int:
    _sessionmaker()
    print("Database ready.")
    return 0


def create_admin(args) -> int:
    username = args.username.strip().lower()
    if not USERNAME_RE.match(username):
        print("Usernames may contain lowercase letters, digits, '.', '_' and '-'.", file=sys.stderr)
        return 1
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            print("Passwords don't match.", file=sys.stderr)
            return 1
    if not password:
        print("A password is required.", file=sys.stderr)
        return 1
    with _sessionmaker()() as db:
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            user = User(username=username, display_name=args.display_name or username, email=args.email or "")
            db.add(user)
            action = "Created"
        else:
            action = "Updated"
        user.password_hash = hash_password(password)
        user.is_admin = True
        user.active = True
        db.commit()
    print(f"{action} admin '{username}'.")
    return 0


def serve(args) -> int:
    import uvicorn

    uvicorn.run(
        "sharedcal.main:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips=args.forwarded_allow_ips,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sharedcal")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--forwarded-allow-ips", default="127.0.0.1", help="proxies trusted to set X-Forwarded-*")
    p.set_defaults(func=serve)

    p = sub.add_parser("init-db", help="create the database tables")
    p.set_defaults(func=init_db)

    p = sub.add_parser("create-admin", help="create an admin user, or reset an existing user's password and make them admin")
    p.add_argument("username")
    p.add_argument("--display-name")
    p.add_argument("--email")
    p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    p.set_defaults(func=create_admin)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
