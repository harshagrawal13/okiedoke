"""HTTP routes. Every mutation is small, granular and idempotent, so a phone on a
flaky connection can safely retry it, and two devices never overwrite each other's
whole document the way the old single-file design did."""
from __future__ import annotations

import html

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, field_validator

from . import queries
from .auth_x import BadOAuthState, NeedsReauth, TokenRefreshFailed

api = APIRouter(prefix="/api")
pages = APIRouter()


def _clean(v: str, limit: int) -> str:
    v = v.strip()
    if not v or len(v) > limit:
        raise ValueError(f"must be 1-{limit} characters")
    return v


class TagBody(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _clean(v, 100)


class HideBody(BaseModel):
    ids: list[str]
    hidden: bool = True


class FilterBody(BaseModel):
    name: str
    expr: str

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _clean(v, 100)

    @field_validator("expr")
    @classmethod
    def _expr(cls, v: str) -> str:
        return _clean(v, 1000)


def _state(request: Request) -> dict:
    with request.app.state.db.connection() as conn:
        st = queries.user_state(conn)
    st["sync"] = request.app.state.engine.status()
    return st


@api.get("/health")
def health(request: Request):
    ok = request.app.state.db.ping()
    return JSONResponse({"ok": ok}, status_code=200 if ok else 503)


@api.get("/bootstrap")
def bootstrap(request: Request):
    with request.app.state.db.connection() as conn:
        records = queries.bookmarks(conn)
    return {"env": request.app.state.settings.app_env, "bookmarks": records, "state": _state(request)}


@api.get("/state")
def state(request: Request):
    return _state(request)


# ---- tags -------------------------------------------------------------------
@api.post("/bookmarks/{tweet_id}/tags")
def add_tag(tweet_id: str, body: TagBody, request: Request):
    with request.app.state.db.connection() as conn:
        with conn.transaction():
            if not conn.execute("SELECT 1 FROM bookmarks WHERE tweet_id = %s", (tweet_id,)).fetchone():
                raise HTTPException(404, "bookmark not found")
            # Re-using a name revives it if it had been deleted everywhere, and the first
            # spelling wins ("Rust" stays "Rust" when you later type "rust").
            tag = conn.execute(
                "INSERT INTO tags (name, name_lower, last_used_at) VALUES (%s, %s, now()) "
                "ON CONFLICT (name_lower) DO UPDATE SET last_used_at = now(), deleted_at = NULL "
                "RETURNING id, name", (body.name, body.name.lower())).fetchone()
            conn.execute("INSERT INTO bookmark_tags (tweet_id, tag_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                         (tweet_id, tag["id"]))
    return {"ok": True, "name": tag["name"]}


@api.delete("/bookmarks/{tweet_id}/tags")
def remove_tag(tweet_id: str, request: Request, name: str = Query(min_length=1, max_length=100)):
    with request.app.state.db.connection() as conn:
        conn.execute("DELETE FROM bookmark_tags WHERE tweet_id = %s AND tag_id IN "
                     "(SELECT id FROM tags WHERE name_lower = %s)", (tweet_id, name.strip().lower()))
    return {"ok": True}


@api.post("/tags/delete")
def delete_tag(body: TagBody, request: Request):
    """Delete a tag from every post. Links are kept so Restore brings it all back."""
    with request.app.state.db.connection() as conn:
        conn.execute("UPDATE tags SET deleted_at = COALESCE(deleted_at, now()) WHERE name_lower = %s",
                     (body.name.lower(),))
    return {"ok": True}


@api.post("/tags/restore")
def restore_tag(body: TagBody, request: Request):
    with request.app.state.db.connection() as conn:
        conn.execute("UPDATE tags SET deleted_at = NULL WHERE name_lower = %s", (body.name.lower(),))
    return {"ok": True}


# ---- hide / restore -----------------------------------------------------------
@api.post("/bookmarks/hide")
def hide(body: HideBody, request: Request):
    """Remove from view (or bring back). Never touches X or the synced data."""
    with request.app.state.db.connection() as conn:
        conn.execute(
            "UPDATE bookmarks SET hidden_at = CASE WHEN %s THEN COALESCE(hidden_at, now()) ELSE NULL END "
            "WHERE tweet_id = ANY(%s)", (body.hidden, body.ids))
    return {"ok": True}


@api.post("/bookmarks/restore-all")
def restore_all(request: Request):
    with request.app.state.db.connection() as conn:
        conn.execute("UPDATE bookmarks SET hidden_at = NULL WHERE hidden_at IS NOT NULL")
    return {"ok": True}


# ---- saved filters ------------------------------------------------------------
@api.put("/saved-filters")
def save_filter(body: FilterBody, request: Request):
    with request.app.state.db.connection() as conn:
        conn.execute(
            "INSERT INTO saved_filters (name, name_lower, expr) VALUES (%s, %s, %s) "
            "ON CONFLICT (name_lower) DO UPDATE SET expr = EXCLUDED.expr", (body.name, body.name.lower(), body.expr))
    return {"ok": True}


@api.delete("/saved-filters")
def delete_filter(request: Request, name: str = Query(min_length=1, max_length=100)):
    with request.app.state.db.connection() as conn:
        conn.execute("DELETE FROM saved_filters WHERE name_lower = %s", (name.strip().lower(),))
    return {"ok": True}


# ---- sync ---------------------------------------------------------------------
@api.get("/sync/status")
def sync_status(request: Request):
    return request.app.state.engine.status()


@api.post("/sync")
def sync(request: Request, mode: str | None = Query(None, pattern="^(full|incremental)$"), force: bool = False):
    """Start a background sync. Without `force` it is skipped inside the cooldown
    window, which is what lets the page trigger one every time it opens."""
    return request.app.state.engine.start_async(mode, "manual" if force else "auto", force=force)


@api.post("/sync/retry-media")
def retry_media(request: Request):
    return {"ok": True, "requeued": request.app.state.engine.retry_failed_media()}


# ---- media + X connect -------------------------------------------------------
@pages.get("/media/{name}")
def media(name: str, request: Request):
    path = request.app.state.media.path(name)
    if path is None:
        raise HTTPException(404)
    # Names are content-addressed and never rewritten, so browsers can keep them forever.
    # FileResponse also answers Range requests, which iOS Safari needs to play video.
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


def _message_page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>okiedoke</title><body style='font:16px system-ui;max-width:32rem;margin:15vh auto;padding:0 1rem'>"
        f"<h2>{html.escape(title)}</h2><p>{html.escape(body)}</p><p><a href='/'>Back to bookmarks</a></p>", status_code=status)


@pages.get("/auth/x/login")
def x_login(request: Request):
    st = request.app.state
    if not st.settings.x_configured:
        return _message_page("X is not configured", "Set X_CLIENT_ID and SECRET_KEY, then restart.", 400)
    return RedirectResponse(st.auth.begin(), status_code=302)


@pages.get("/auth/x/callback")
def x_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error or not code or not state:
        return _message_page("X did not connect", error or "missing code", 400)
    try:
        request.app.state.auth.complete(state, code)
    except BadOAuthState as e:
        return _message_page("Connection expired", str(e), 400)
    except (TokenRefreshFailed, NeedsReauth) as e:
        return _message_page("X rejected the connection", str(e), 502)
    return RedirectResponse("/?x=connected", status_code=303)
