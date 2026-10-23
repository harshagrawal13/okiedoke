"""The sync engine: fetch bookmarks from X, store them, download media.

Design rules (each one answers a way the old ft crawler flaked):
- One run at a time, enforced by a Postgres advisory lock (survives restarts
  and multiple processes; released automatically if the process dies).
- Every page is committed on its own, so a crash, rate limit or network error
  mid-run keeps everything fetched so far; the next run just continues.
- Incremental runs stop at the first page with nothing new; only a *full*
  sweep may flag bookmarks as deleted on X, and only when it provably saw the
  whole list (reached the end, below X's cap, touched >= 50% of known rows).
- Media downloads are retried across runs and never fail the sync.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

import httpx

from . import store
from .auth_x import NeedsReauth, NotConnected, XAuth
from .config import Settings
from .db import SYNC_LOCK, Database
from .media import MediaError, MediaStore, avatar_stem, stem_for
from .x_client import RateLimited, XApiError, XClient
from .xparse import parse_page

log = logging.getLogger("okiedoke.sync")
MAX_MEDIA_ATTEMPTS = 5
HttpFactory = Callable[[], httpx.Client]


def _default_http() -> httpx.Client:
    return httpx.Client(timeout=30, follow_redirects=True)


class SyncEngine:
    def __init__(self, settings: Settings, db: Database, auth: XAuth, http_factory: HttpFactory = _default_http):
        self.s = settings
        self.db = db
        self.auth = auth
        self.http_factory = http_factory
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ state
    def _lock_free(self) -> bool:
        """True when no sync (in any process) holds the advisory lock."""
        with self.db.connection() as conn:
            got = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (SYNC_LOCK,)).fetchone()["ok"]
            if got:
                conn.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK,))
            return got

    def is_running(self) -> bool:
        return not self._lock_free()

    def choose_kind(self) -> str:
        """Full sweep when none has ever completed or the last is older than
        FULL_SWEEP_DAYS; otherwise incremental."""
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT max(finished_at) AS at FROM sync_runs WHERE kind = 'full' AND complete").fetchone()
            if row["at"] is None:
                return "full"
            if self.s.full_sweep_days > 0:
                due = conn.execute(
                    "SELECT %s < now() - make_interval(secs => %s) AS due",
                    (row["at"], self.s.full_sweep_days * 86400)).fetchone()["due"]
                if due:
                    return "full"
        return "incremental"

    def status(self) -> dict:
        acct = self.auth.account() if self.s.x_configured else None
        with self.db.connection() as conn:
            last = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
            last_ok = conn.execute(
                "SELECT finished_at FROM sync_runs WHERE status IN ('ok', 'partial') ORDER BY id DESC LIMIT 1").fetchone()
            live = conn.execute("SELECT count(*) AS n FROM bookmarks WHERE missing_since IS NULL").fetchone()["n"]
        running = bool(last and last["status"] == "running" and self.is_running())
        progress = None
        if running and last["kind"] == "full" and last["phase"] == "fetching":
            progress = min(0.99, last["fetched"] / max(live, last["fetched"], 1))
        out = {
            "configured": self.s.x_configured,
            "connected": bool(acct),
            "handle": acct["handle"] if acct else None,
            "needsReauth": bool(acct and acct["status"] == "needs_reauth"),
            "running": running,
            "progress": progress,
            "lastOkAt": last_ok["finished_at"].isoformat() if last_ok and last_ok["finished_at"] else None,
            "last": None,
        }
        if last:
            out["last"] = {
                "status": "running" if running else ("error" if last["status"] == "running" else last["status"]),
                "kind": last["kind"], "trigger": last["trigger"], "phase": last["phase"],
                "startedAt": last["started_at"].isoformat(),
                "finishedAt": last["finished_at"].isoformat() if last["finished_at"] else None,
                "fetched": last["fetched"], "inserted": last["inserted"], "markedMissing": last["marked_missing"],
                "mediaDownloaded": last["media_downloaded"], "mediaFailed": last["media_failed"],
                "error": last["error"],
            }
        return out

    # ------------------------------------------------------------- entrypoints
    def start_async(self, kind: str | None, trigger: str, force: bool = False) -> dict:
        """Kick off a background sync, honouring the page-open cooldown unless forced."""
        if not self.s.x_configured:
            return {"ok": True, "skipped": "not_configured"}
        acct = self.auth.account()
        if not acct:
            return {"ok": True, "skipped": "not_connected"}
        if acct["status"] == "needs_reauth":
            return {"ok": True, "skipped": "needs_reauth"}
        if self.is_running() or (self._thread and self._thread.is_alive()):
            return {"ok": True, "running": True}
        if not force and kind is None:
            with self.db.connection() as conn:
                recent = conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM sync_runs WHERE started_at > now() - %s * interval '1 minute') AS r",
                    (self.s.sync_cooldown_min,)).fetchone()["r"]
            if recent:
                return {"ok": True, "running": False, "skipped": "cooldown"}
        kind = kind or self.choose_kind()
        self._thread = threading.Thread(target=self._safe_run, args=(kind, trigger), daemon=True, name="sync")
        self._thread.start()
        return {"ok": True, "running": True, "kind": kind}

    def _safe_run(self, kind: str, trigger: str) -> None:
        try:
            self.run(kind, trigger)
        except Exception:
            log.exception("sync crashed")

    def run(self, kind: str, trigger: str) -> dict:
        with self.db.connection() as lock_conn:
            got = lock_conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (SYNC_LOCK,)).fetchone()["ok"]
            if not got:
                return {"ok": True, "running": True}
            try:
                return self._run_locked(kind, trigger)
            finally:
                lock_conn.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK,))

    # -------------------------------------------------------------------- core
    def _run_locked(self, kind: str, trigger: str) -> dict:
        with self.db.connection() as conn:
            # We hold the lock, so any row still marked running is a leftover from a dead process.
            conn.execute("UPDATE sync_runs SET status = 'error', finished_at = now(), "
                         "error = 'interrupted (process restarted)' WHERE status = 'running'")
            run_id = conn.execute(
                "INSERT INTO sync_runs (kind, trigger, phase) VALUES (%s, %s, 'fetching') RETURNING id",
                (kind, trigger)).fetchone()["id"]
        totals = {"pages": 0, "fetched": 0, "inserted": 0, "updated": 0, "marked_missing": 0,
                  "media_downloaded": 0, "media_failed": 0}
        status, error, complete = "ok", None, False
        try:
            if kind == "full":
                self.retry_failed_media()      # a full sweep gives previously failed files another chance
            complete = self._fetch(run_id, kind, totals)
            self._media(run_id, totals)
        except RateLimited as e:
            status, error = "partial", str(e)
        except (NeedsReauth, NotConnected) as e:
            status, error = "error", str(e)
        except XApiError as e:
            status, error = "error", str(e)
        except Exception as e:                                   # never leave a run 'running'
            log.exception("sync failed")
            status, error = "error", f"{type(e).__name__}: {e}"
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE sync_runs SET status = %s, phase = 'done', finished_at = now(), complete = %s, error = %s, "
                " pages = %s, fetched = %s, inserted = %s, updated = %s, marked_missing = %s, "
                " media_downloaded = %s, media_failed = %s WHERE id = %s",
                (status, complete and status == "ok", error, totals["pages"], totals["fetched"], totals["inserted"],
                 totals["updated"], totals["marked_missing"], totals["media_downloaded"], totals["media_failed"], run_id))
        log.info("sync %s %s: %s", kind, status, {**totals, "error": error})
        return {"ok": status == "ok", "status": status, "error": error, **totals}

    def _progress(self, run_id: int, totals: dict, phase: str) -> None:
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE sync_runs SET phase = %s, pages = %s, fetched = %s, inserted = %s, updated = %s, "
                " media_downloaded = %s, media_failed = %s WHERE id = %s",
                (phase, totals["pages"], totals["fetched"], totals["inserted"], totals["updated"],
                 totals["media_downloaded"], totals["media_failed"], run_id))

    def _fetch(self, run_id: int, kind: str, totals: dict) -> bool:
        """Page through the bookmarks list. Returns True when the end of the list was reached."""
        acct = self.auth.account()
        if not acct:
            raise NotConnected("connect your X account first")
        size = self.s.incremental_page_size if kind == "incremental" else self.s.sweep_page_size
        token: str | None = None
        reached_end = False
        with self.http_factory() as http:
            client = XClient(self.s, self.auth, http)
            while True:
                page = parse_page(client.bookmarks_page(acct["x_user_id"], token, size))
                with self.db.connection() as conn:
                    new, updated = store.apply_page(conn, page, run_id)
                totals["pages"] += 1
                totals["fetched"] += len(page.bookmarks)
                totals["inserted"] += new
                totals["updated"] += updated
                self._progress(run_id, totals, "fetching")
                token = page.next_token
                if not token:
                    reached_end = True
                    break
                if kind == "incremental" and new == 0:
                    break                           # nothing new here, so nothing newer follows
                if totals["fetched"] >= self.s.bookmark_cap:
                    break
                size = self.s.sweep_page_size

        if kind == "full" and reached_end:
            truncated = totals["fetched"] >= self.s.bookmark_cap - 5
            with self.db.connection() as conn:
                live = conn.execute("SELECT count(*) AS n FROM bookmarks WHERE missing_since IS NULL").fetchone()["n"]
                touched = conn.execute(
                    "SELECT count(*) AS n FROM bookmarks WHERE last_seen_run = %s", (run_id,)).fetchone()["n"]
                if not truncated and live and touched / live >= 0.5:
                    totals["marked_missing"] = store.mark_missing(conn, run_id)
                elif live:
                    log.warning("full sweep too small or truncated to detect deletions (%d/%d, truncated=%s)",
                                touched, live, truncated)
        return reached_end

    def _media(self, run_id: int, totals: dict) -> None:
        self._progress(run_id, totals, "media")
        with self.http_factory() as http:
            self._download_pending(run_id, totals, MediaStore(self.s, http))

    def _download_pending(self, run_id: int, totals: dict, store_: MediaStore) -> None:
        budget = self.s.max_media_per_run
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT id, tweet_id, source_url FROM media WHERE status = 'pending' AND attempts < %s "
                "AND source_url IS NOT NULL ORDER BY id LIMIT %s", (MAX_MEDIA_ATTEMPTS, budget)).fetchall()
            avatars = conn.execute(
                "SELECT id, handle, avatar_url FROM authors WHERE avatar_url IS NOT NULL AND avatar_file IS NULL "
                "AND avatar_attempts < %s ORDER BY id LIMIT 200", (MAX_MEDIA_ATTEMPTS,)).fetchall()
        for r in rows:
            try:
                name = store_.download(r["source_url"], stem_for(r["tweet_id"], r["source_url"]))
            except MediaError as e:
                totals["media_failed"] += 1
                with self.db.connection() as conn:
                    conn.execute(
                        "UPDATE media SET attempts = attempts + 1, last_error = %s, "
                        " status = CASE WHEN %s OR attempts + 1 >= %s THEN 'failed' ELSE status END WHERE id = %s",
                        (str(e)[:200], e.permanent, MAX_MEDIA_ATTEMPTS, r["id"]))
            else:
                totals["media_downloaded"] += 1
                with self.db.connection() as conn:
                    conn.execute("UPDATE media SET file = %s, status = 'downloaded', last_error = NULL WHERE id = %s",
                                 (name, r["id"]))
        for a in avatars:
            try:
                name = store_.download(a["avatar_url"], avatar_stem(a["handle"], a["avatar_url"]))
            except MediaError as e:
                with self.db.connection() as conn:
                    conn.execute("UPDATE authors SET avatar_attempts = avatar_attempts + %s WHERE id = %s",
                                 (MAX_MEDIA_ATTEMPTS if e.permanent else 1, a["id"]))
            else:
                with self.db.connection() as conn:
                    conn.execute("UPDATE authors SET avatar_file = %s WHERE id = %s", (name, a["id"]))
        self._progress(run_id, totals, "media")

    def retry_failed_media(self) -> int:
        with self.db.connection() as conn:
            row = conn.execute(
                "WITH r AS (UPDATE media SET status = 'pending', attempts = 0 WHERE status = 'failed' "
                " AND source_url IS NOT NULL RETURNING 1) SELECT count(*) AS n FROM r").fetchone()
            conn.execute("UPDATE authors SET avatar_attempts = 0 WHERE avatar_file IS NULL AND avatar_url IS NOT NULL")
        return row["n"]
