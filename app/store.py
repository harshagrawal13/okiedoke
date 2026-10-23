"""Idempotent writes of parsed X data. Re-applying the same page is a no-op in effect."""
from __future__ import annotations

import os
import posixpath
from urllib.parse import urlparse

import psycopg
from psycopg.types.json import Jsonb

from .xparse import ParsedAuthor, ParsedMedia, ParsedPage, ParsedTweet


def _stem(url: str | None) -> str:
    """'https://pbs.twimg.com/media/AbC.jpg?name=orig' -> 'AbC' (for matching an
    imported file to the same asset reported by the API)."""
    if not url:
        return ""
    return os.path.splitext(posixpath.basename(urlparse(url).path))[0]


def upsert_author(conn: psycopg.Connection, a: ParsedAuthor) -> int:
    row = conn.execute("SELECT id FROM authors WHERE x_user_id = %s", (a.x_user_id,)).fetchone()
    if not row:
        # An author known only from the ft import (handle, no user id): adopt it.
        adopt = conn.execute(
            "SELECT id FROM authors WHERE x_user_id IS NULL AND lower(handle) = lower(%s) ORDER BY id LIMIT 1",
            (a.handle,)).fetchone()
        if adopt:
            conn.execute("UPDATE authors SET x_user_id = %s WHERE id = %s", (a.x_user_id, adopt["id"]))
    return conn.execute(
        """
        INSERT INTO authors (x_user_id, handle, name, avatar_url)
        VALUES (%(uid)s, %(handle)s, %(name)s, %(avatar)s)
        ON CONFLICT (x_user_id) DO UPDATE SET
            handle = EXCLUDED.handle,
            name = EXCLUDED.name,
            -- a changed profile picture is a different file: forget the old download
            avatar_file = CASE WHEN authors.avatar_url IS NOT NULL AND EXCLUDED.avatar_url IS NOT NULL
                                    AND authors.avatar_url <> EXCLUDED.avatar_url
                               THEN NULL ELSE authors.avatar_file END,
            avatar_attempts = CASE WHEN authors.avatar_url IS NOT NULL AND EXCLUDED.avatar_url IS NOT NULL
                                        AND authors.avatar_url <> EXCLUDED.avatar_url
                                   THEN 0 ELSE authors.avatar_attempts END,
            avatar_url = COALESCE(EXCLUDED.avatar_url, authors.avatar_url)
        RETURNING id
        """,
        {"uid": a.x_user_id, "handle": a.handle, "name": a.name, "avatar": a.avatar_url},
    ).fetchone()["id"]


def upsert_tweet(conn: psycopg.Connection, t: ParsedTweet, author_id: int | None) -> bool:
    """Returns True when the tweet row was newly inserted."""
    return conn.execute(
        """
        INSERT INTO tweets (id, author_id, text, url, posted_at, like_count, repost_count, bookmark_count,
                            reply_count, in_reply_to_tweet_id, reply_parent_handle, quoted_tweet_id, raw)
        VALUES (%(id)s, %(author)s, %(text)s, %(url)s, %(posted)s, %(likes)s, %(reposts)s, %(bookmarks)s,
                %(replies)s, %(reply_to)s, %(reply_handle)s, %(quoted)s, %(raw)s)
        ON CONFLICT (id) DO UPDATE SET
            author_id = COALESCE(EXCLUDED.author_id, tweets.author_id),
            text = EXCLUDED.text,
            url = EXCLUDED.url,
            posted_at = COALESCE(EXCLUDED.posted_at, tweets.posted_at),
            like_count = EXCLUDED.like_count,
            repost_count = EXCLUDED.repost_count,
            bookmark_count = EXCLUDED.bookmark_count,
            reply_count = EXCLUDED.reply_count,
            in_reply_to_tweet_id = COALESCE(EXCLUDED.in_reply_to_tweet_id, tweets.in_reply_to_tweet_id),
            reply_parent_handle = COALESCE(EXCLUDED.reply_parent_handle, tweets.reply_parent_handle),
            quoted_tweet_id = COALESCE(EXCLUDED.quoted_tweet_id, tweets.quoted_tweet_id),
            raw = EXCLUDED.raw,
            updated_at = now()
        RETURNING (xmax = 0) AS inserted
        """,
        {"id": t.id, "author": author_id, "text": t.text, "url": t.url, "posted": t.posted_at,
         "likes": t.likes, "reposts": t.reposts, "bookmarks": t.bookmarks, "replies": t.replies,
         "reply_to": t.in_reply_to_tweet_id, "reply_handle": t.reply_parent_handle,
         "quoted": t.quoted_tweet_id, "raw": Jsonb(t.raw)},
    ).fetchone()["inserted"]


def sync_media(conn: psycopg.Connection, tweet_id: str, items: list[ParsedMedia]) -> None:
    """Reconcile a tweet's media with what the API reports. Files imported from
    ft ('ft:<name>' keys) are adopted — matched by asset name, then by position
    and kind — so an already-downloaded file is never fetched or shown twice."""
    if not items:
        return
    existing = conn.execute(
        "SELECT id, key, kind, source_url FROM media WHERE tweet_id = %s ORDER BY position, id", (tweet_id,)
    ).fetchall()
    by_key = {r["key"]: r for r in existing}
    unadopted = [r for r in existing if r["key"].startswith("ft:")]
    for m in items:
        row = by_key.get(m.key)
        if row is None:
            match = next((r for r in unadopted if _stem(r["source_url"]) and _stem(r["source_url"]) == _stem(m.source_url)), None) \
                or next((r for r in unadopted if r["kind"] == m.kind), None)
            if match:
                unadopted.remove(match)
                row = match
        if row is not None:
            conn.execute(
                "UPDATE media SET key = %s, position = %s, source_url = %s, "
                " width = COALESCE(%s, width), height = COALESCE(%s, height) WHERE id = %s",
                (m.key, m.position, m.source_url, m.width, m.height, row["id"]))
        else:
            conn.execute(
                "INSERT INTO media (tweet_id, key, position, kind, source_url, width, height) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (tweet_id, m.key, m.position, m.kind, m.source_url, m.width, m.height))


def apply_page(conn: psycopg.Connection, page: ParsedPage, run_id: int) -> tuple[int, int]:
    """Write one page atomically. Returns (new_bookmarks, updated_bookmarks)."""
    new = updated = 0
    with conn.transaction():
        for t in page.quoted:
            aid = upsert_author(conn, t.author) if t.author else None
            upsert_tweet(conn, t, aid)
            sync_media(conn, t.id, t.media)
        for t in page.bookmarks:
            aid = upsert_author(conn, t.author) if t.author else None
            upsert_tweet(conn, t, aid)
            sync_media(conn, t.id, t.media)
            inserted = conn.execute(
                """
                INSERT INTO bookmarks (tweet_id, source, last_seen_at, last_seen_run)
                VALUES (%s, 'x-api', now(), %s)
                ON CONFLICT (tweet_id) DO UPDATE SET last_seen_at = now(), last_seen_run = EXCLUDED.last_seen_run,
                                                     missing_since = NULL
                RETURNING (xmax = 0) AS inserted
                """, (t.id, run_id)).fetchone()["inserted"]
            if inserted:
                new += 1
            else:
                updated += 1
    return new, updated


def mark_missing(conn: psycopg.Connection, run_id: int) -> int:
    """After a complete, untruncated sweep: bookmarks it did not see are gone from X."""
    row = conn.execute(
        "WITH m AS (UPDATE bookmarks SET missing_since = now() "
        " WHERE missing_since IS NULL AND last_seen_run IS DISTINCT FROM %s RETURNING 1) "
        "SELECT count(*) AS n FROM m", (run_id,)).fetchone()
    return row["n"]
