"""Application factory. Run with: uvicorn whenever.main:create_app --factory"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .admin.routes import LoginRequired, login_required_handler
from .admin.routes import router as admin_router
from .config import Settings
from .dav.routes import DAV_METHODS, dav_root
from .dav.routes import router as dav_router
from .db import make_engine, make_sessionmaker
from .feeds import router as feed_router
from .models import Base
from .web.routes import router as web_router


STATIC_DIR = Path(__file__).parent / "web" / "static"


class RootPathMiddleware:
    """Serve the app under a URL prefix (WHENEVER_ROOT_PATH).

    The reverse proxy passes paths through unchanged (/whenever/dav/...). This
    sets the ASGI root_path, so routing ignores the prefix and every URL the
    app generates includes it. Requests that arrive without the prefix (e.g.
    straight to the app port) are treated as if they had it, which is what
    uvicorn's --root-path does.
    """

    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"} and not scope.get("root_path"):
            path = scope["path"]
            if path != self.prefix and not path.startswith(self.prefix + "/"):
                path = self.prefix + path
                scope = {**scope, "raw_path": self.prefix.encode() + scope.get("raw_path", b"")}
            scope = {**scope, "root_path": self.prefix, "path": path}
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)

    app = FastAPI(title="whenever", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="whenever_session",
        max_age=8 * 3600,
        same_site="strict",
        https_only=settings.cookie_secure,
        path=f"{settings.root_path}/",
    )
    if settings.root_path:
        app.add_middleware(RootPathMiddleware, prefix=settings.root_path)
    app.add_exception_handler(LoginRequired, login_required_handler)

    app.include_router(dav_router)
    app.include_router(feed_router)
    app.include_router(admin_router)
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.api_route("/", methods=DAV_METHODS, include_in_schema=False)
    async def root(request: Request) -> Response:
        # Some clients PROPFIND the server root when given only a hostname.
        if request.method in {"GET", "HEAD"}:
            return RedirectResponse(request.url_for("users"), status_code=303)
        return await dav_root(request)

    return app
