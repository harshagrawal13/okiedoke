from __future__ import annotations

import time
from dataclasses import replace

import httpx
import pytest

from app.auth_x import NeedsReauth
from app.db import SYNC_LOCK
from app.media import MediaStore

from .fakex import FakeX
from .helpers import connect_account, make_engine


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


@pytest.fixture()
def fx():
    return FakeX()


def q1(db, sql, *args):
    with db.connection() as conn:
        return conn.execute(sql, args).fetchone()


def qa(db, sql, *args):
    with db.connection() as conn:
        return conn.execute(sql, args).fetchall()


def test_full_sync_stores_bookmarks_media_and_downloads_files(db, settings, fx):
    fx.add_quoted("900", photos=1)
    fx.add("1", "first", photos=2)
    fx.add("2", "second", video=True, quote="900")
    fx.add("3", "third")
    connect_account(db, settings, fx)
    res = make_engine(db, settings, fx).run("full", "manual")

    assert res["ok"] and res["fetched"] == 3 and res["inserted"] == 3
    assert q1(db, "SELECT count(*) AS n FROM bookmarks")["n"] == 3
    media = qa(db, "SELECT tweet_id, kind, status, file FROM media ORDER BY tweet_id, position")
    assert [(m["tweet_id"], m["kind"]) for m in media] == [("1", "image"), ("1", "image"), ("2", "video"), ("900", "image")]
    assert all(m["status"] == "downloaded" and (settings.media_dir / m["file"]).read_bytes() for m in media)
    assert q1(db, "SELECT quoted_tweet_id FROM tweets WHERE id = '2'")["quoted_tweet_id"] == "900"
    assert q1(db, "SELECT count(*) AS n FROM bookmarks WHERE tweet_id = '900'")["n"] == 0   # quoted != bookmarked
    # avatars were fetched too
    assert q1(db, "SELECT count(*) AS n FROM authors WHERE avatar_file IS NOT NULL")["n"] == 2


def test_sync_is_idempotent(db, settings, fx):
    for i in range(1, 6):
        fx.add(str(i), photos=1)
    connect_account(db, settings, fx)
    eng = make_engine(db, settings, fx)
    eng.run("full", "manual")
    counts = [q1(db, f"SELECT count(*) AS n FROM {t}")["n"] for t in ("tweets", "bookmarks", "media", "authors")]
    second = eng.run("full", "manual")
    assert second["inserted"] == 0 and second["updated"] == 5
    assert counts == [q1(db, f"SELECT count(*) AS n FROM {t}")["n"] for t in ("tweets", "bookmarks", "media", "authors")]


def test_incremental_stops_after_first_page_with_nothing_new(db, settings, fx):
    s = replace(settings, incremental_page_size=2, sweep_page_size=2)
    for i in range(1, 8):
        fx.add(str(i))
    connect_account(db, s, fx)
    eng = make_engine(db, s, fx)
    eng.run("full", "manual")

    fx.calls.clear()
    res = eng.run("incremental", "auto")
    assert res["pages"] == 1 and res["inserted"] == 0          # nothing new: one request

    fx.add("100", front=True)
    fx.calls.clear()
    res = eng.run("incremental", "auto")
    assert res["inserted"] == 1 and res["pages"] == 2          # page 1 had news, page 2 had none
    assert q1(db, "SELECT count(*) AS n FROM bookmarks")["n"] == 8


def test_choose_kind_is_full_until_a_full_sweep_completes(db, settings, fx):
    fx.add("1")
    connect_account(db, settings, fx)
    eng = make_engine(db, settings, fx)
    assert eng.choose_kind() == "full"
    eng.run("incremental", "manual")
    assert eng.choose_kind() == "full"                          # incremental doesn't count
    eng.run("full", "manual")
    assert eng.choose_kind() == "incremental"


def test_deleted_on_x_is_detected_and_clears_on_return(db, settings, fx):
    for i in range(1, 6):
        fx.add(str(i))
    connect_account(db, settings, fx)
    eng = make_engine(db, settings, fx)
    eng.run("full", "manual")
    gone = fx.bookmarks[2]
    fx.remove("3")

    res = eng.run("full", "manual")
    assert res["marked_missing"] == 1
    assert [r["tweet_id"] for r in qa(db, "SELECT tweet_id FROM bookmarks WHERE missing_since IS NOT NULL")] == ["3"]

    fx.bookmarks.insert(2, gone)                                # re-bookmarked
    eng.run("full", "manual")
    assert q1(db, "SELECT count(*) AS n FROM bookmarks WHERE missing_since IS NOT NULL")["n"] == 0


def test_partial_sweep_never_mass_marks_deleted(db, settings, fx):
    for i in range(1, 11):
        fx.add(str(i))
    connect_account(db, settings, fx)
    eng = make_engine(db, settings, fx)
    eng.run("full", "manual")
    fx.bookmarks = fx.bookmarks[:2]                             # X "returns" 2 of 10: under the 50% guard
    res = eng.run("full", "manual")
    assert res["ok"] and res["marked_missing"] == 0
    assert q1(db, "SELECT count(*) AS n FROM bookmarks WHERE missing_since IS NOT NULL")["n"] == 0


def test_truncated_sweep_does_not_flag_deletions(db, settings, fx):
    s = replace(settings, bookmark_cap=10)                      # pretend X only exposes 10
    for i in range(1, 9):
        fx.add(str(i))
    connect_account(db, s, fx)
    eng = make_engine(db, s, fx)
    eng.run("full", "manual")
    fx.remove("8")
    res = eng.run("full", "manual")                             # 7 seen >= cap-5: could be cut off
    assert res["marked_missing"] == 0


def test_imported_rows_not_seen_by_a_complete_sweep_are_flagged(db, settings, fx):
    fx.add("1")
    fx.add("2")
    connect_account(db, settings, fx)
    with db.connection() as conn:                               # as the ft import would leave it
        for tid in ("1", "2", "777"):
            conn.execute("INSERT INTO tweets (id, text, url) VALUES (%s, 'x', 'https://x.com/i/status/'||%s)", (tid, tid))
            conn.execute("INSERT INTO bookmarks (tweet_id, source) VALUES (%s, 'ft-import')", (tid,))
    res = make_engine(db, settings, fx).run("full", "manual")
    assert res["marked_missing"] == 1
    assert q1(db, "SELECT tweet_id FROM bookmarks WHERE missing_since IS NOT NULL")["tweet_id"] == "777"


def test_rate_limit_keeps_progress_and_reports_partial(db, settings, fx):
    s = replace(settings, sweep_page_size=2, incremental_page_size=2)
    for i in range(1, 7):
        fx.add(str(i))
    connect_account(db, s, fx)
    eng = make_engine(db, s, fx)
    real = fx._bookmarks
    state = {"n": 0}

    def flaky(req):
        state["n"] += 1
        if state["n"] == 2:
            return httpx.Response(429, headers={"x-rate-limit-reset": str(int(time.time()) + 900)})
        return real(req)
    fx._bookmarks = flaky
    res = eng.run("full", "manual")
    assert res["status"] == "partial" and "rate limited" in res["error"]
    assert q1(db, "SELECT count(*) AS n FROM bookmarks")["n"] == 2        # first page survived
    assert q1(db, "SELECT count(*) AS n FROM bookmarks WHERE missing_since IS NOT NULL")["n"] == 0
    fx._bookmarks = real
    assert eng.run("full", "manual")["ok"]
    assert q1(db, "SELECT count(*) AS n FROM bookmarks")["n"] == 6


def test_short_rate_limit_waits_and_retries(db, settings, fx):
    fx.add("1")
    connect_account(db, settings, fx)
    fx.bookmark_failures = [httpx.Response(429, headers={"x-rate-limit-reset": str(int(time.time()) + 5)})]
    assert make_engine(db, settings, fx).run("full", "manual")["ok"]


def test_server_errors_and_network_errors_are_retried(db, settings, fx):
    fx.add("1")
    connect_account(db, settings, fx)
    fx.bookmark_failures = [httpx.Response(503), httpx.Response(500)]
    assert make_engine(db, settings, fx).run("full", "manual")["ok"]

    calls = {"n": 0}
    real = fx._handle

    def flaky(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return real(req)
    fx._handle = flaky
    fx.add("2")
    assert make_engine(db, settings, fx).run("full", "manual")["ok"]


def test_persistent_failure_reports_error_without_hanging(db, settings, fx):
    fx.add("1")
    connect_account(db, settings, fx)
    fx.bookmark_failures = [httpx.Response(500)] * 10
    res = make_engine(db, settings, fx).run("full", "manual")
    assert res["status"] == "error" and "giving up" in res["error"]
    assert q1(db, "SELECT status FROM sync_runs")["status"] == "error"


def test_forbidden_is_a_clear_error(db, settings, fx):
    connect_account(db, settings, fx)
    fx.bookmark_failures = [httpx.Response(403, json={"detail": "client-not-enrolled"})]
    res = make_engine(db, settings, fx).run("full", "manual")
    assert res["status"] == "error" and "client-not-enrolled" in res["error"]


def test_expired_access_token_refreshes_and_rotates_refresh_token(db, settings, fx):
    fx.add("1")
    connect_account(db, settings, fx, expires_in_s=-10)         # already expired
    res = make_engine(db, settings, fx).run("full", "manual")
    assert res["ok"]
    assert fx.access_token == "access-2" and fx.refresh_token == "refresh-2"
    from app.crypto import decrypt
    row = q1(db, "SELECT * FROM x_auth")
    assert decrypt(settings.secret_key, row["refresh_token_enc"]) == "refresh-2"      # rotation persisted
    assert "refresh-2" not in row["refresh_token_enc"]                                 # and encrypted at rest


def test_401_mid_run_refreshes_once_and_continues(db, settings, fx):
    fx.add("1")
    connect_account(db, settings, fx)
    fx.access_token = "revoked-elsewhere"                       # server no longer accepts what we hold
    fx.refresh_token = "refresh-1"
    assert make_engine(db, settings, fx).run("full", "manual")["ok"]


def test_revoked_refresh_token_flags_reauth(db, settings, fx):
    connect_account(db, settings, fx, expires_in_s=-10)
    fx.token_error = (400, {"error": "invalid_grant"})
    eng = make_engine(db, settings, fx)
    res = eng.run("full", "manual")
    assert res["status"] == "error" and "reconnect" in res["error"].lower()
    assert q1(db, "SELECT status FROM x_auth")["status"] == "needs_reauth"
    assert eng.status()["needsReauth"] is True
    assert eng.start_async(None, "auto")["skipped"] == "needs_reauth"
    with pytest.raises(NeedsReauth):
        eng.auth.access_token()


def test_transient_token_failure_keeps_tokens_and_is_not_reauth(db, settings, fx):
    connect_account(db, settings, fx, expires_in_s=-10)
    fx.token_error = (503, {})
    res = make_engine(db, settings, fx).run("full", "manual")
    assert res["status"] == "error"
    assert q1(db, "SELECT status FROM x_auth")["status"] == "ok"


def test_media_failures_do_not_fail_the_sync_and_can_be_retried(db, settings, fx):
    fx.add("1", photos=2)
    fx.media_status["/media/1x0.jpg"] = 404                     # gone for good
    fx.media_status["/media/1x1.jpg"] = 503                     # flaky
    connect_account(db, settings, fx)
    eng = make_engine(db, settings, fx)
    res = eng.run("full", "manual")
    assert res["ok"] and res["media_failed"] == 2
    st = {m["key"]: m for m in qa(db, "SELECT key, status, attempts FROM media")}
    assert st["3_1_0"]["status"] == "failed"                    # 404 is permanent
    assert st["3_1_1"]["status"] == "pending" and st["3_1_1"]["attempts"] == 1

    fx.media_status.clear()
    eng.run("incremental", "auto")                              # a later run picks the flaky one up
    assert q1(db, "SELECT status FROM media WHERE key = '3_1_1'")["status"] == "downloaded"
    assert eng.retry_failed_media() == 1
    eng.run("incremental", "auto")
    assert q1(db, "SELECT status FROM media WHERE key = '3_1_0'")["status"] == "downloaded"


def test_imported_media_is_adopted_not_duplicated_or_redownloaded(db, settings, fx):
    fx.add("1", photos=2)
    connect_account(db, settings, fx)
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    with db.connection() as conn:
        conn.execute("INSERT INTO tweets (id, text, url) VALUES ('1', 'old', 'https://x.com/i/status/1')")
        conn.execute("INSERT INTO bookmarks (tweet_id, source) VALUES ('1', 'ft-import')")
        for i, name in enumerate(["old_a.jpg", "old_b.jpg"]):
            (settings.media_dir / name).write_bytes(b"ORIGINAL")
            conn.execute("INSERT INTO media (tweet_id, key, position, kind, source_url, file, status) "
                         "VALUES ('1', %s, %s, 'image', %s, %s, 'downloaded')",
                         (f"ft:{name}", i, f"https://pbs.twimg.com/media/1x{1 - i}.jpg?name=orig", name))
    fx.calls.clear()
    make_engine(db, settings, fx).run("full", "manual")
    rows = qa(db, "SELECT key, file, status, position FROM media ORDER BY position")
    assert len(rows) == 2                                       # no duplicates
    assert all(r["status"] == "downloaded" for r in rows)
    assert {r["file"] for r in rows} == {"old_a.jpg", "old_b.jpg"}   # imported files kept
    assert not any("media/1x" in c[1] for c in fx.calls if c[0] == "GET")   # nothing re-downloaded
    # matched by asset name, not just position: x0 (position 0) was imported as old_b
    assert q1(db, "SELECT file FROM media WHERE key = '3_1_0'")["file"] == "old_b.jpg"


def test_advisory_lock_blocks_concurrent_runs(db, settings, fx):
    fx.add("1")
    connect_account(db, settings, fx)
    eng = make_engine(db, settings, fx)
    with db.connection() as other:
        other.execute("SELECT pg_advisory_lock(%s)", (SYNC_LOCK,))
        assert eng.is_running()
        assert eng.run("full", "manual") == {"ok": True, "running": True}
        assert q1(db, "SELECT count(*) AS n FROM sync_runs")["n"] == 0
        other.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK,))
    assert not eng.is_running()
    assert eng.run("full", "manual")["ok"]


def test_stale_running_row_from_a_dead_process_is_reaped(db, settings, fx):
    fx.add("1")
    connect_account(db, settings, fx)
    with db.connection() as conn:
        conn.execute("INSERT INTO sync_runs (kind, trigger, phase) VALUES ('full', 'manual', 'fetching')")
    eng = make_engine(db, settings, fx)
    assert eng.status()["running"] is False                      # lock is free, so it isn't really running
    assert eng.status()["last"]["status"] == "error"
    eng.run("incremental", "auto")
    assert q1(db, "SELECT count(*) AS n FROM sync_runs WHERE status = 'running'")["n"] == 0
    assert "interrupted" in q1(db, "SELECT error FROM sync_runs ORDER BY id LIMIT 1")["error"]


def test_start_async_honours_cooldown_unless_forced(db, settings, fx):
    fx.add("1")
    connect_account(db, settings, fx)
    eng = make_engine(db, settings, fx)
    eng.run("full", "manual")
    assert eng.start_async(None, "auto")["skipped"] == "cooldown"
    out = eng.start_async("incremental", "manual", force=True)
    assert out["running"] is True
    eng._thread.join(timeout=10)
    assert q1(db, "SELECT count(*) AS n FROM sync_runs")["n"] == 2


def test_not_configured_and_not_connected_are_reported_not_raised(db, settings, fx):
    assert make_engine(db, replace(settings, x_client_id=""), fx).start_async(None, "auto")["skipped"] == "not_configured"
    assert make_engine(db, settings, fx).start_async(None, "auto")["skipped"] == "not_connected"
    st = make_engine(db, settings, fx).status()
    assert st["configured"] and not st["connected"]


def test_media_store_rejects_foreign_hosts_and_path_tricks(settings):
    ms = MediaStore(settings, httpx.Client(transport=FakeX().transport()))
    from app.media import MediaError
    with pytest.raises(MediaError) as e:
        ms.download("https://evil.example.com/a.jpg", "x")
    assert e.value.permanent
    with pytest.raises(MediaError):
        ms.download("http://pbs.twimg.com/a.jpg", "x")           # not https
    for bad in ("../etc/passwd", "a/b.jpg", ".hidden", "", "a b.jpg"):
        assert ms.path(bad) is None
