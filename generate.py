#!/usr/bin/env python3
"""okiedoke: regenerate index.html from the local ft-bookmarks sqlite database."""
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

FT_DIR = Path(os.environ.get("OKIEDOKE_DATA", str(Path.home() / ".ft-bookmarks")))
DB_PATH = FT_DIR / "bookmarks.db"
MANIFEST_PATH = FT_DIR / "media-manifest.json"
# Output lives next to the real media/ directory: Safari only lets a file://
# page load files under its own real directory tree (symlinks are blocked).
REPLY_PARENTS_PATH = Path(__file__).parent / "reply-parents.json"
LAST_FULL_SYNC_PATH = FT_DIR / "last-full-sync.json"
OUT_PATH = FT_DIR / "index.html"
TEMPLATE_PATH = Path(__file__).parent / "template.html"
CONVENIENCE_LINK = Path(__file__).parent / "index.html"

HASHTAG_RE = re.compile(r"#(\w+)")
STOP_DOMAINS = {"x.com", "twitter.com", "t.co"}


def ensure_convenience_link():
    """Keep a project-dir symlink pointing at the generated file."""
    if CONVENIENCE_LINK.is_symlink():
        if CONVENIENCE_LINK.resolve() == OUT_PATH:
            return
        CONVENIENCE_LINK.unlink()
    elif CONVENIENCE_LINK.exists():
        CONVENIENCE_LINK.unlink()
    CONVENIENCE_LINK.symlink_to(OUT_PATH)


def registrable_domain(url):
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return None
    host = host.split("@")[-1].split(":")[0]
    if not host or host in STOP_DOMAINS:
        return None
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def parse_twitter_date(s):
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def load_manifest_maps():
    """Return (media_by_tweet, avatar_by_handle) using RELATIVE paths (media/<name>)."""
    if not MANIFEST_PATH.exists():
        return {}, {}
    manifest = json.loads(MANIFEST_PATH.read_text())
    by_tweet = {}
    avatars = {}
    for entry in manifest.get("entries", []):
        if entry.get("status") != "downloaded":
            continue
        local_path = entry.get("localPath")
        if not local_path:
            continue
        p = Path(local_path)
        if not p.exists():
            continue
        rel = f"media/{p.name}"
        if "profile_images" in entry.get("sourceUrl", ""):
            handle = (entry.get("authorHandle") or "").lower()
            if handle:
                avatars.setdefault(handle, rel)
            continue
        tweet_id = entry.get("tweetId")
        if not tweet_id:
            continue
        kind = "video" if p.suffix.lower() == ".mp4" else "image"
        by_tweet.setdefault(tweet_id, []).append({"type": kind, "src": rel})
    return by_tweet, avatars


def load_reply_parents():
    if not REPLY_PARENTS_PATH.exists():
        return {}
    return json.loads(REPLY_PARENTS_PATH.read_text())


def load_full_sync_start():
    """Start of the last completed full re-crawl (written by serve.py).
    Rows whose synced_at predates it were absent from that crawl, meaning
    the bookmark no longer exists on X (deleted or un-bookmarked)."""
    if not LAST_FULL_SYNC_PATH.exists():
        return None
    try:
        return json.loads(LAST_FULL_SYNC_PATH.read_text())["startedAt"]
    except (json.JSONDecodeError, KeyError):
        return None


def derive_tags(row, links, has_media, deleted_on_x):
    tags = []
    if deleted_on_x:
        tags.append("deleted on X")
    if row["tags_json"]:
        try:
            parsed = json.loads(row["tags_json"])
            if parsed:
                tags.extend(parsed)
        except json.JSONDecodeError:
            pass
    tags.extend(HASHTAG_RE.findall(row["text"] or ""))
    for link in links:
        d = registrable_domain(link)
        if d:
            tags.append(d)
    if has_media and "media" not in tags:
        tags.append("media")
    if row["primary_category"] and row["primary_category"] != "unclassified":
        tags.append(row["primary_category"])
    # de-dupe, preserve order, case-insensitive
    seen = set()
    out = []
    for t in tags:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def fmt_date(dt):
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def require_data():
    """Fail early with a friendly message if there's nothing to build from,
    instead of letting sqlite raise a cryptic 'no such table' error."""
    hint = (
        f"\nNo bookmarks found in {FT_DIR}.\n"
        "  1. Install and sign in to the ft CLI:  https://fieldtheory.dev/cli\n"
        "  2. Sync your X bookmarks:               ft sync\n"
        "  3. Build the app again:                 python3 generate.py\n"
        "     (or just run ./setup.sh, which does all of this for you)\n"
    )
    if not DB_PATH.exists():
        raise SystemExit(hint)
    conn = sqlite3.connect(DB_PATH)
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bookmarks'"
        ).fetchone()
    except sqlite3.DatabaseError:
        table = None
    finally:
        conn.close()
    if not table:
        raise SystemExit(hint)


def main():
    require_data()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM bookmarks ORDER BY posted_at DESC").fetchall()
    media_by_tweet, avatars = load_manifest_maps()
    reply_parents = load_reply_parents()
    full_sync_start = load_full_sync_start()

    records = []
    for row in rows:
        try:
            links = json.loads(row["links_json"]) if row["links_json"] else []
        except json.JSONDecodeError:
            links = []
        dt = parse_twitter_date(row["posted_at"])
        text = (row["text"] or "").strip()

        own_media = media_by_tweet.get(row["id"], [])
        handle = row["author_handle"] or ""

        quoted = None
        if row["quoted_tweet_json"]:
            try:
                q = json.loads(row["quoted_tweet_json"])
                q_handle = q.get("authorHandle") or ""
                quoted = {
                    "author": q.get("authorName") or q_handle or "",
                    "handle": q_handle,
                    "avatar": avatars.get(q_handle.lower()),
                    "text": (q.get("text") or "").strip(),
                    "url": q.get("url") or "",
                    "media": media_by_tweet.get(row["quoted_status_id"], []),
                }
            except json.JSONDecodeError:
                quoted = None

        reply_parent = None
        if row["in_reply_to_status_id"]:
            reply_parent = reply_parents.get(row["in_reply_to_status_id"])

        has_media = bool(own_media) or bool(quoted and quoted["media"])
        deleted_on_x = bool(
            full_sync_start
            and row["synced_at"]
            and row["synced_at"] < full_sync_start
        )

        records.append({
            "id": row["id"],
            "url": row["url"],
            "text": text,
            "author": row["author_name"] or handle or "",
            "handle": handle,
            "avatar": avatars.get(handle.lower()),
            "date": dt.isoformat() if dt else None,
            "dateDisplay": fmt_date(dt) if dt else "—",
            "ts": int(dt.timestamp()) if dt else 0,
            "likes": row["like_count"] or 0,
            "reposts": row["repost_count"] or 0,
            "bookmarks": row["bookmark_count"] or 0,
            "tags": derive_tags(row, links, has_media, deleted_on_x),
            "deletedOnX": deleted_on_x,
            "media": own_media,
            "quoted": quoted,
            "replyParent": reply_parent,
        })

    template = TEMPLATE_PATH.read_text()
    html = template.replace(
        "/*__BOOKMARKS_DATA__*/[]",
        json.dumps(records, ensure_ascii=False),
    )
    OUT_PATH.write_text(html)
    ensure_convenience_link()
    with_avatar = sum(1 for r in records if r["avatar"])
    print(f"Wrote {len(records)} bookmarks to {OUT_PATH} ({with_avatar} with avatars)")


if __name__ == "__main__":
    main()
