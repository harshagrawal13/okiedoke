"""App factory.  Run:  uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from .api import api, pages
from .auth_x import XAuth
from .config import Settings
from .db import Database
from .media import MediaStore
from .scheduler import Scheduler
from .sync import SyncEngine

log = logging.getLogger("okiedoke")
if not logging.getLogger().handlers:        # uvicorn only configures its own loggers
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class _ApiGzip:
    """Compress API responses only; media must stay byte-exact for Range requests."""

    def __init__(self, app):
        self.app = app
        self.gz = GZipMiddleware(app, minimum_size=1024)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/"):
            await self.gz(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def _open_with_retry(db: Database, attempts: int = 12) -> None:
    """The database container may still be starting; wait for it instead of crash-looping."""
    for i in range(attempts):
        try:
            db.open()
            return
        except Exception as e:
            if i == attempts - 1:
                raise
            log.warning("database not ready (%s); retrying", type(e).__name__)
            db.pool.close()
            time.sleep(min(5, 1 + i))


def create_app(settings: Settings | None = None, *, http_factory=None, start_scheduler: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _open_with_retry(db)
        applied = db.migrate()
        if applied:
            log.info("applied migrations: %s", ", ".join(applied))
        app.state.scheduler = Scheduler(settings, db, app.state.engine)
        if start_scheduler:
            app.state.scheduler.start()
        yield
        app.state.scheduler.stop()
        db.close()

    app = FastAPI(title="okiedoke", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    auth = XAuth(settings, db, http=http_factory() if http_factory else None)
    app.state.settings = settings
    app.state.db = db
    app.state.auth = auth
    app.state.media = MediaStore(settings)
    app.state.engine = SyncEngine(settings, db, auth, **({"http_factory": http_factory} if http_factory else {}))

    @app.middleware("http")
    async def guard(request, call_next):
        # State-changing requests must come from this site: blocks cross-site form posts / fetches
        # that would ride on the browser's session with the tunnel's login in front of us.
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin:
                host = urlparse(origin).netloc
                allowed = {request.headers.get("host", ""), urlparse(settings.public_url).netloc}
                if host not in allowed:
                    return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        resp = await call_next(request)
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        elif not request.url.path.startswith("/media/"):
            resp.headers.setdefault("Cache-Control", "no-cache")      # revalidate (cheap 304s) so deploys show up
        return resp

    app.include_router(api)
    app.include_router(pages)
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    app.add_middleware(_ApiGzip)
    return app
