"""Read side: turn Postgres rows into the record shape the UI renders."""
from __future__ import annotations

from datetime import datetime

import psycopg


def _media_src(row: dict) -> str | None:
    if row["status"] == "downloaded" and row["file"]:
        return f"/media/{row['file']}"
    return row["source_url"]                    # not downloaded (yet): hot-link X's copy so the post still renders


def _avatar(row: dict, prefix: str = "") -> str | None:
    f, u = row.get(prefix + "avatar_file"), row.get(prefix + "avatar_url")
    return f"/media/{f}" if f else u


def _date_display(dt: datetime | None) -> str:
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}" if dt else "—"


def bookmarks(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT b.tweet_id, b.missing_since,
               t.url, t.text, t.posted_at, t.like_count, t.repost_count, t.bookmark_count,
               t.reply_parent_handle, t.quoted_tweet_id,
               a.handle, a.name AS author_name, a.avatar_file, a.avatar_url,
               q.url AS q_url, q.text AS q_text,
               qa.handle AS q_handle, qa.name AS q_author_name, qa.avatar_file AS q_avatar_file, qa.avatar_url AS q_avatar_url
        FROM bookmarks b
        JOIN tweets t ON t.id = b.tweet_id
        LEFT JOIN authors a ON a.id = t.author_id
        LEFT JOIN tweets q ON q.id = t.quoted_tweet_id
        LEFT JOIN authors qa ON qa.id = q.author_id
        ORDER BY t.posted_at DESC NULLS LAST, b.tweet_id DESC
        """
    ).fetchall()
    ids = {r["tweet_id"] for r in rows} | {r["quoted_tweet_id"] for r in rows if r["quoted_tweet_id"]}
    media: dict[str, list[dict]] = {}
    for m in conn.execute(
        "SELECT tweet_id, kind, file, status, source_url FROM media WHERE tweet_id = ANY(%s) ORDER BY tweet_id, position, id",
        (list(ids),),
    ):
        src = _media_src(m)
        if src:
            media.setdefault(m["tweet_id"], []).append({"type": m["kind"], "src": src})

    out = []
    for r in rows:
        dt = r["posted_at"]
        handle = r["handle"] or ""
        quoted = None
        if r["quoted_tweet_id"] and r["q_url"] is not None:
            qh = r["q_handle"] or ""
            quoted = {
                "author": r["q_author_name"] or qh, "handle": qh, "avatar": _avatar(r, "q_"),
                "text": r["q_text"] or "", "url": r["q_url"], "media": media.get(r["quoted_tweet_id"], []),
            }
        out.append({
            "id": r["tweet_id"], "url": r["url"], "text": r["text"],
            "author": r["author_name"] or handle, "handle": handle, "avatar": _avatar(r),
            "date": dt.isoformat() if dt else None, "dateDisplay": _date_display(dt),
            "ts": int(dt.timestamp()) if dt else 0,
            "likes": r["like_count"], "reposts": r["repost_count"], "bookmarks": r["bookmark_count"],
            "deletedOnX": r["missing_since"] is not None,
            "media": media.get(r["tweet_id"], []),
            "quoted": quoted,
            "replyParent": {"handle": r["reply_parent_handle"]} if r["reply_parent_handle"] else None,
        })
    return out


def user_state(conn: psycopg.Connection) -> dict:
    """Everything the user has done: tags, hidden posts, saved filters. Small enough
    to refetch whenever the page regains focus, so a phone and a laptop stay in step."""
    tags: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT bt.tweet_id, t.name FROM bookmark_tags bt JOIN tags t ON t.id = bt.tag_id ORDER BY bt.tweet_id, bt.seq"
    ):
        tags.setdefault(r["tweet_id"], []).append(r["name"])
    return {
        "tags": tags,
        "hidden": [r["tweet_id"] for r in conn.execute("SELECT tweet_id FROM bookmarks WHERE hidden_at IS NOT NULL")],
        "deletedTags": [r["name_lower"] for r in conn.execute("SELECT name_lower FROM tags WHERE deleted_at IS NOT NULL")],
        "tagLastUsed": {r["name_lower"]: int(r["ms"]) for r in conn.execute(
            "SELECT name_lower, extract(epoch FROM last_used_at) * 1000 AS ms FROM tags WHERE last_used_at IS NOT NULL")},
        "savedFilters": [{"name": r["name"], "expr": r["expr"]} for r in conn.execute(
            "SELECT name, expr FROM saved_filters ORDER BY seq")],
    }
