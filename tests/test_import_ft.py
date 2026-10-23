from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app.import_ft import Importer, parse_twitter_date


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture()
def ft_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ft"
    (d / "media").mkdir(parents=True)
    for name in ("111-a.jpg", "111-b.jpg", "222.mp4", "q1.jpg", "alice.jpg", "bob.jpg"):
        (d / "media" / name).write_bytes(f"DATA-{name}".encode())

    conn = sqlite3.connect(d / "bookmarks.db")
    conn.execute("""CREATE TABLE bookmarks (
        id TEXT PRIMARY KEY, url TEXT, text TEXT, author_handle TEXT, author_name TEXT, posted_at TEXT,
        like_count INTEGER, repost_count INTEGER, bookmark_count INTEGER, tags_json TEXT, links_json TEXT,
        quoted_tweet_json TEXT, quoted_status_id TEXT, in_reply_to_status_id TEXT, synced_at TEXT,
        primary_category TEXT)""")
    rows = [
        ("111", "https://x.com/alice/status/111", "first post", "alice", "Alice", "Tue Mar 05 12:00:00 +0000 2024",
         5, 2, 1, None, "[]", json.dumps({"authorHandle": "bob", "authorName": "Bob", "text": "quoted!",
                                           "url": "https://x.com/bob/status/900"}), "900", None, "2026-01-02T00:00:00Z", None),
        ("222", "https://x.com/bob/status/222", "second post", "bob", "Bob", "Wed Mar 06 08:30:00 +0000 2024",
         0, 0, 0, None, None, None, None, "555", "2025-01-01T00:00:00Z", "unclassified"),
        ("333", "https://x.com/carol/status/333", "third", "carol", "Carol", None, 1, 1, 1, None, None, None, None, None,
         "2026-01-02T00:00:00Z", None),
    ]
    conn.executemany("INSERT INTO bookmarks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    away = "/Users/someone/.ft-bookmarks/media"   # manifest paths come from the Mac, not this machine
    entry = lambda name, **kw: {"status": "downloaded", "localPath": f"{away}/{name}", **kw}
    (d / "media-manifest.json").write_text(json.dumps({"entries": [
        entry("111-a.jpg", tweetId="111", sourceUrl="https://pbs.twimg.com/media/AAA.jpg?name=orig"),
        entry("111-b.jpg", tweetId="111", sourceUrl="https://pbs.twimg.com/media/BBB.jpg?name=orig"),
        entry("222.mp4", tweetId="222", sourceUrl="https://video.twimg.com/x/222.mp4"),
        entry("q1.jpg", tweetId="900", sourceUrl="https://pbs.twimg.com/media/QQQ.jpg"),     # keyed by the QUOTED id
        entry("alice.jpg", authorHandle="alice", sourceUrl="https://pbs.twimg.com/profile_images/1/a.jpg"),
        entry("bob.jpg", authorHandle="Bob", sourceUrl="https://pbs.twimg.com/profile_images/2/b.jpg"),
        {"status": "failed", "localPath": f"{away}/nope.jpg", "tweetId": "111"},
        entry("gone.jpg", tweetId="111", sourceUrl="https://pbs.twimg.com/media/GONE.jpg"),    # file missing on disk
    ]}))
    (d / "last-full-sync.json").write_text(json.dumps({"startedAt": "2025-06-01T00:00:00Z"}))
    (d / "okiedoke-tags.json").write_text(json.dumps({
        "version": 1,
        "customTags": {"111": ["Rust", "to-read"], "222": ["rust"], "999": ["orphan"], "333": ["Rust"]},
        "removedTags": {"111": ["derived-thing"]},
        "globalRemovedTags": ["old-tag", "RUST"],       # RUST is applied somewhere, so it must stay alive
        "hiddenBookmarks": ["333", "does-not-exist"],
        "tagLastUsed": {"rust": 1_700_000_000_000, "to-read": 1_600_000_000_000},
        "savedFilters": [{"name": "Rusty", "expr": "rust and not archive"}, {"name": "", "expr": "x"}],
    }))
    return d


def q1(db, sql, *a):
    with db.connection() as conn:
        return conn.execute(sql, a).fetchone()


def qa(db, sql, *a):
    with db.connection() as conn:
        return conn.execute(sql, a).fetchall()


def test_import_brings_everything_over(db, settings, ft_dir):
    before = _sha(ft_dir / "bookmarks.db")
    rep = Importer(ft_dir, db, settings).run()

    assert rep["bookmarks"] == 3 and rep["quoted"] == 1
    t = q1(db, "SELECT * FROM tweets WHERE id = '111'")
    assert t["text"] == "first post" and t["like_count"] == 5 and t["quoted_tweet_id"] == "900"
    assert t["posted_at"].isoformat() == "2024-03-05T12:00:00+00:00"
    assert q1(db, "SELECT text, url FROM tweets WHERE id = '900'")["text"] == "quoted!"
    assert q1(db, "SELECT in_reply_to_tweet_id FROM tweets WHERE id = '222'")["in_reply_to_tweet_id"] == "555"
    assert q1(db, "SELECT posted_at FROM tweets WHERE id = '333'")["posted_at"] is None

    # media: copied by NAME (manifest paths were from another machine); quoted media lands on the quoted tweet
    media = qa(db, "SELECT tweet_id, kind, file, status FROM media ORDER BY tweet_id, position")
    assert [(m["tweet_id"], m["kind"]) for m in media] == [("111", "image"), ("111", "image"), ("222", "video"), ("900", "image")]
    assert all(m["status"] == "downloaded" and (settings.media_dir / m["file"]).read_bytes().startswith(b"DATA-") for m in media)
    assert rep["media_missing"] == 1 and rep["media_files"] == 4
    # avatars by handle, case-insensitively
    assert (settings.media_dir / q1(db, "SELECT avatar_file FROM authors WHERE handle = 'bob'")["avatar_file"]).exists()
    assert q1(db, "SELECT avatar_file FROM authors WHERE handle = 'carol'")["avatar_file"] is None

    # "deleted on X": row 222 predates the last full re-crawl
    assert [r["tweet_id"] for r in qa(db, "SELECT tweet_id FROM bookmarks WHERE missing_since IS NOT NULL")] == ["222"]
    assert _sha(ft_dir / "bookmarks.db") == before                       # ft's database is never modified


def test_import_preserves_user_edits_losslessly(db, settings, ft_dir):
    rep = Importer(ft_dir, db, settings).run()
    tags = {r["tweet_id"]: [x["name"] for x in qa(db, "SELECT t.name FROM bookmark_tags bt JOIN tags t ON t.id = bt.tag_id "
                                                      "WHERE bt.tweet_id = %s ORDER BY bt.seq", r["tweet_id"])]
            for r in qa(db, "SELECT DISTINCT tweet_id FROM bookmark_tags")}
    assert tags == {"111": ["Rust", "to-read"], "222": ["Rust"], "333": ["Rust"]}   # order kept, case-merged, label = first seen
    assert rep["tag_links_skipped"] == 1                                              # 'orphan' on an unknown bookmark
    assert q1(db, "SELECT deleted_at FROM tags WHERE name_lower = 'old-tag'")["deleted_at"] is not None
    assert q1(db, "SELECT deleted_at FROM tags WHERE name_lower = 'rust'")["deleted_at"] is None   # applied => never deleted
    assert q1(db, "SELECT extract(epoch FROM last_used_at)::bigint AS e FROM tags WHERE name_lower = 'rust'")["e"] == 1_700_000_000
    assert [r["tweet_id"] for r in qa(db, "SELECT tweet_id FROM bookmarks WHERE hidden_at IS NOT NULL")] == ["333"]
    assert [(r["name"], r["expr"]) for r in qa(db, "SELECT name, expr FROM saved_filters")] == [("Rusty", "rust and not archive")]
    # the original document is kept verbatim, including keys we have no use for (removedTags)
    legacy = q1(db, "SELECT payload FROM legacy_imports WHERE kind = 'okiedoke-tags.json'")["payload"]
    assert legacy["removedTags"] == {"111": ["derived-thing"]} and legacy["customTags"]["999"] == ["orphan"]


def test_import_is_idempotent_and_never_overwrites(db, settings, ft_dir):
    Importer(ft_dir, db, settings).run()
    with db.connection() as conn:                                   # the app has moved on since the import
        conn.execute("UPDATE tweets SET like_count = 9999 WHERE id = '111'")
        conn.execute("UPDATE bookmarks SET hidden_at = NULL WHERE tweet_id = '333'")
        conn.execute("DELETE FROM bookmark_tags WHERE tweet_id = '222'")
    rep = Importer(ft_dir, db, settings).run()
    assert rep["bookmarks"] == 0 and rep["media_files"] == 0
    assert q1(db, "SELECT like_count FROM tweets WHERE id = '111'")["like_count"] == 9999
    assert q1(db, "SELECT count(*) AS n FROM media")["n"] == 4
    assert q1(db, "SELECT count(*) AS n FROM legacy_imports")["n"] == 1
    assert q1(db, "SELECT count(*) AS n FROM saved_filters")["n"] == 1
    assert q1(db, "SELECT count(*) AS n FROM tags WHERE name_lower = 'rust'")["n"] == 1


def test_dry_run_writes_nothing(db, settings, ft_dir):
    rep = Importer(ft_dir, db, settings, dry_run=True).run()
    assert rep["bookmarks"] == 3
    assert q1(db, "SELECT count(*) AS n FROM tweets")["n"] == 0
    assert not settings.media_dir.exists() or not any(settings.media_dir.iterdir())


def test_failed_import_leaves_database_untouched(db, settings, ft_dir, monkeypatch):
    def boom(self, conn, doc):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(Importer, "_tags", boom)
    with pytest.raises(RuntimeError):
        Importer(ft_dir, db, settings).run()
    assert q1(db, "SELECT count(*) AS n FROM bookmarks")["n"] == 0        # one transaction: all or nothing


def test_missing_database_is_a_clear_error(db, settings, tmp_path):
    with pytest.raises(SystemExit) as e:
        Importer(tmp_path, db, settings).run()
    assert "bookmarks.db" in str(e.value)


def test_parse_twitter_date():
    assert parse_twitter_date("Tue Mar 05 12:00:00 +0000 2024").isoformat() == "2024-03-05T12:00:00+00:00"
    assert parse_twitter_date("garbage") is None and parse_twitter_date(None) is None
