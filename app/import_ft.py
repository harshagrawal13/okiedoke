"""One-time import of an existing Field Theory (`ft`) data directory.

    python -m app.import_ft /path/to/.ft-bookmarks [--dry-run]

Reads (never writes) <dir>/bookmarks.db, media-manifest.json, media/*, and the
old app's okiedoke-tags.json; writes to Postgres and MEDIA_DIR. Re-running is
safe: bookmarks and tags that already exist are left exactly as they are, so a
second import (or an import after the first X sync) never overwrites newer data.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from .config import Settings
from .db import Database

log = logging.getLogger("okiedoke.import")
TWITTER_DATE = "%a %b %d %H:%M:%S %z %Y"


def parse_twitter_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, TWITTER_DATE).astimezone(timezone.utc)
    except ValueError:
        return None


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _status_id(url: str | None) -> str | None:
    m = re.search(r"/status/(\d+)", url or "")
    return m.group(1) if m else None


class Importer:
    def __init__(self, src: Path, db: Database, settings: Settings, dry_run: bool = False):
        self.src = src
        self.db = db
        self.s = settings
        self.dry = dry_run
        self._avatar_handles: set[str] = set()
        self.report: dict[str, int] = {k: 0 for k in (
            "bookmarks", "quoted", "media_files", "media_missing", "avatars", "tags", "tag_links",
            "hidden", "saved_filters", "deleted_on_x", "tag_links_skipped")}

    # -- source readers ----------------------------------------------------
    def _open_sqlite(self) -> sqlite3.Connection:
        path = self.src / "bookmarks.db"
        if not path.exists():
            raise SystemExit(f"No bookmarks.db in {self.src}. Is this an ft data directory?")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)   # read-only: ft's data is never modified
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'bookmarks'").fetchone():
            raise SystemExit(f"{path} has no 'bookmarks' table.")
        return conn

    def _manifest(self) -> tuple[dict[str, list[dict]], dict[str, dict]]:
        """(media entries by tweet id, avatar entry by lowercased handle), skipping
        entries whose file is not actually on disk. Files are looked up by name in
        <src>/media/ because manifest paths are absolute paths from the original machine."""
        by_tweet: dict[str, list[dict]] = {}
        avatars: dict[str, dict] = {}
        manifest = _read_json(self.src / "media-manifest.json") or {}
        for e in manifest.get("entries", []):
            if e.get("status") != "downloaded" or not e.get("localPath"):
                continue
            name = Path(e["localPath"]).name
            f = self.src / "media" / name
            if not f.is_file():
                f = Path(e["localPath"])
            if not f.is_file():
                self.report["media_missing"] += 1
                continue
            entry = {"name": name, "path": f, "source_url": e.get("sourceUrl") or ""}
            if "profile_images" in entry["source_url"]:
                handle = (e.get("authorHandle") or "").lower()
                if handle:
                    avatars.setdefault(handle, entry)
            elif e.get("tweetId"):
                by_tweet.setdefault(str(e["tweetId"]), []).append(entry)
        return by_tweet, avatars

    # -- writers ------------------------------------------------------------
    def _copy(self, entry: dict) -> str:
        name = f"ft_{entry['name']}"
        dest = self.s.media_dir / name
        if not self.dry and not dest.exists():
            self.s.media_dir.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f".{name}.part")
            shutil.copyfile(entry["path"], tmp)
            tmp.replace(dest)
        return name

    @staticmethod
    def _author(conn: psycopg.Connection, handle: str, name: str, avatar_file: str | None) -> int | None:
        if not handle:
            return None
        row = conn.execute("SELECT id FROM authors WHERE lower(handle) = lower(%s) ORDER BY (x_user_id IS NULL), id LIMIT 1",
                           (handle,)).fetchone()
        if row:
            if avatar_file:
                conn.execute("UPDATE authors SET avatar_file = COALESCE(avatar_file, %s) WHERE id = %s",
                             (avatar_file, row["id"]))
            return row["id"]
        return conn.execute("INSERT INTO authors (handle, name, avatar_file) VALUES (%s, %s, %s) RETURNING id",
                            (handle, name or handle, avatar_file)).fetchone()["id"]

    def _media(self, conn: psycopg.Connection, tweet_id: str, entries: list[dict]) -> None:
        for pos, e in enumerate(entries):
            name = self._copy(e)
            kind = "video" if Path(e["name"]).suffix.lower() == ".mp4" else "image"
            inserted = conn.execute(
                "INSERT INTO media (tweet_id, key, position, kind, source_url, file, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'downloaded') ON CONFLICT (tweet_id, key) DO NOTHING RETURNING id",
                (tweet_id, f"ft:{name}", pos, kind, e["source_url"] or None, name)).fetchone()
            self.report["media_files"] += 1 if inserted else 0

    def run(self) -> dict[str, int]:
        src = self._open_sqlite()
        try:
            rows = [dict(r) for r in src.execute("SELECT * FROM bookmarks ORDER BY posted_at DESC")]
        finally:
            src.close()
        by_tweet, avatars = self._manifest()
        full_sync = _read_json(self.src / "last-full-sync.json") or {}
        reply_parents = _read_json(self.src / "reply-parents.json") or {}
        tags_doc = _read_json(self.src / "okiedoke-tags.json")

        with self.db.connection() as conn:
            try:
                with conn.transaction():          # all-or-nothing: a failure leaves the database untouched
                    for r in rows:
                        self._bookmark(conn, r, by_tweet, avatars, full_sync.get("startedAt"), reply_parents)
                    if isinstance(tags_doc, dict):
                        self._tags(conn, tags_doc)
                    if self.dry:
                        raise _DryRun()           # roll everything back, keep the report
            except _DryRun:
                pass
        self.report["avatars"] = len(self._avatar_handles)
        return self.report

    def _bookmark(self, conn, r: dict, by_tweet, avatars, full_sync_start, reply_parents) -> None:
        tid = str(r["id"])
        handle = r.get("author_handle") or ""
        av = avatars.get(handle.lower())
        author_id = self._author(conn, handle, r.get("author_name") or "", self._copy(av) if av else None)
        if av:
            self._avatar_handles.add(handle.lower())
        quoted_id = str(r["quoted_status_id"]) if r.get("quoted_status_id") else None
        q = None
        if r.get("quoted_tweet_json"):
            try:
                q = json.loads(r["quoted_tweet_json"])
            except json.JSONDecodeError:
                q = None
        if q:
            quoted_id = quoted_id or _status_id(q.get("url")) or f"q-{tid}"
            q_handle = q.get("authorHandle") or ""
            q_av = avatars.get(q_handle.lower())
            q_author = self._author(conn, q_handle, q.get("authorName") or "", self._copy(q_av) if q_av else None)
            conn.execute(
                "INSERT INTO tweets (id, author_id, text, url) VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (quoted_id, q_author, (q.get("text") or "").strip(),
                 q.get("url") or (f"https://x.com/{q_handle}/status/{quoted_id}" if q_handle else f"https://x.com/i/status/{quoted_id}")))
            self._media(conn, quoted_id, by_tweet.get(quoted_id, []))
            self.report["quoted"] += 1
        else:
            quoted_id = None
        reply_to = str(r["in_reply_to_status_id"]) if r.get("in_reply_to_status_id") else None
        parent = reply_parents.get(reply_to) if reply_to else None
        conn.execute(
            """
            INSERT INTO tweets (id, author_id, text, url, posted_at, like_count, repost_count, bookmark_count,
                                in_reply_to_tweet_id, reply_parent_handle, quoted_tweet_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING
            """,
            (tid, author_id, (r.get("text") or "").strip(),
             r.get("url") or (f"https://x.com/{handle}/status/{tid}" if handle else f"https://x.com/i/status/{tid}"),
             parse_twitter_date(r.get("posted_at")), r.get("like_count") or 0, r.get("repost_count") or 0,
             r.get("bookmark_count") or 0, reply_to, (parent or {}).get("handle"), quoted_id))
        self._media(conn, tid, by_tweet.get(tid, []))
        missing = bool(full_sync_start and r.get("synced_at") and r["synced_at"] < full_sync_start)
        new = conn.execute(
            "INSERT INTO bookmarks (tweet_id, source, missing_since) VALUES (%s, 'ft-import', %s) "
            "ON CONFLICT (tweet_id) DO NOTHING RETURNING 1", (tid, datetime.now(timezone.utc) if missing else None)).fetchone()
        if new:
            self.report["bookmarks"] += 1
            self.report["deleted_on_x"] += 1 if missing else 0

    def _tags(self, conn, doc: dict) -> None:
        conn.execute(
            "INSERT INTO legacy_imports (kind, source_path, payload) SELECT 'okiedoke-tags.json', %s, %s "
            "WHERE NOT EXISTS (SELECT 1 FROM legacy_imports WHERE kind = 'okiedoke-tags.json' AND payload = %s)",
            (str(self.src / "okiedoke-tags.json"), Jsonb(doc), Jsonb(doc)))

        def tag_id(name: str) -> int:
            name = name.strip()[:100]
            return conn.execute(
                "INSERT INTO tags (name, name_lower) VALUES (%s, %s) "
                "ON CONFLICT (name_lower) DO UPDATE SET name_lower = EXCLUDED.name_lower RETURNING id",
                (name, name.lower())).fetchone()["id"]

        known = {r["tweet_id"] for r in conn.execute("SELECT tweet_id FROM bookmarks")}
        applied: set[str] = set()
        for bid, names in (doc.get("customTags") or {}).items():
            for name in names if isinstance(names, list) else []:
                if not isinstance(name, str) or not name.strip():
                    continue
                if str(bid) not in known:
                    self.report["tag_links_skipped"] += 1
                    continue
                inserted = conn.execute(
                    "INSERT INTO bookmark_tags (tweet_id, tag_id) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING 1",
                    (str(bid), tag_id(name))).fetchone()
                applied.add(name.strip().lower())
                self.report["tag_links"] += 1 if inserted else 0

        # An applied tag is never globally deleted (the old app's healSuppression invariant).
        for name in doc.get("globalRemovedTags") or []:
            if isinstance(name, str) and name.strip() and name.strip().lower() not in applied:
                conn.execute("UPDATE tags SET deleted_at = COALESCE(deleted_at, now()) WHERE id = %s", (tag_id(name),))

        for name, ms in (doc.get("tagLastUsed") or {}).items():
            if isinstance(name, str) and isinstance(ms, (int, float)):
                conn.execute("UPDATE tags SET last_used_at = to_timestamp(%s / 1000.0) WHERE name_lower = %s "
                             "AND last_used_at IS NULL", (ms, name.lower()))

        for bid in doc.get("hiddenBookmarks") or []:
            n = conn.execute("UPDATE bookmarks SET hidden_at = COALESCE(hidden_at, now()) WHERE tweet_id = %s "
                             "RETURNING 1", (str(bid),)).fetchone()
            self.report["hidden"] += 1 if n else 0

        for f in doc.get("savedFilters") or []:
            if isinstance(f, dict) and str(f.get("name", "")).strip() and str(f.get("expr", "")).strip():
                name = str(f["name"]).strip()
                n = conn.execute(
                    "INSERT INTO saved_filters (name, name_lower, expr) VALUES (%s, %s, %s) "
                    "ON CONFLICT (name_lower) DO NOTHING RETURNING 1", (name, name.lower(), str(f["expr"]).strip())).fetchone()
                self.report["saved_filters"] += 1 if n else 0
        self.report["tags"] = conn.execute("SELECT count(*) AS n FROM tags").fetchone()["n"]


class _DryRun(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path, help="the ft data directory (contains bookmarks.db)")
    ap.add_argument("--dry-run", action="store_true", help="parse and report; write nothing")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings.from_env()
    db = Database(settings.database_url)
    db.open()
    try:
        db.migrate()
        report = Importer(args.source, db, settings, dry_run=args.dry_run).run()
    finally:
        db.close()
    print(("DRY RUN (nothing written) " if args.dry_run else "Imported ") + json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
