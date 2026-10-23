from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from .fakex import FakeX
from .helpers import connect_account, make_engine


@pytest.fixture()
def fx():
    return FakeX()


@pytest.fixture()
def client(db, settings, fx):
    app = create_app(settings, http_factory=fx.client, start_scheduler=False)
    with TestClient(app, base_url="https://bookmarks.example.test") as c:
        yield c


@pytest.fixture()
def populated(db, settings, fx, client):
    fx.add_quoted("900", "quoted words", photos=1)
    fx.add("1", "first &amp; best", author="alice", photos=1, quote="900", reply_to=("77", "carol"))
    fx.add("2", "second", author="bob", video=True, created_at="2023-01-02T00:00:00.000Z")
    fx.add("3", "third", author="alice", created_at="2025-06-30T00:00:00.000Z")
    connect_account(db, settings, fx)
    assert make_engine(db, settings, fx).run("full", "manual")["ok"]
    return client


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_bootstrap_matches_the_shape_the_ui_renders(populated):
    body = populated.get("/api/bootstrap").json()
    assert body["env"] == "test"
    assert [b["id"] for b in body["bookmarks"]] == ["3", "1", "2"]            # newest post first
    b1 = next(b for b in body["bookmarks"] if b["id"] == "1")
    assert b1["text"] == "first & best"
    assert b1["author"] == "Alice" and b1["handle"] == "alice"
    assert b1["avatar"].startswith("/media/avatar_alice_")
    assert b1["dateDisplay"] == "May 1, 2024" and b1["ts"] == 1714564800
    assert (b1["likes"], b1["reposts"], b1["bookmarks"]) == (10, 3, 3)
    assert b1["media"] == [{"type": "image", "src": b1["media"][0]["src"]}] and b1["media"][0]["src"].startswith("/media/1_")
    assert b1["quoted"]["handle"] == "bob" and b1["quoted"]["text"] == "quoted words"
    assert b1["quoted"]["media"][0]["src"].startswith("/media/900_")
    assert b1["replyParent"] == {"handle": "carol"}
    assert b1["deletedOnX"] is False
    st = body["state"]
    assert st["tags"] == {} and st["hidden"] == [] and st["savedFilters"] == []
    assert st["sync"]["connected"] and st["sync"]["last"]["status"] == "ok"


def test_undownloaded_media_falls_back_to_the_remote_url(populated, db):
    with db.connection() as conn:
        conn.execute("UPDATE media SET status = 'pending', file = NULL WHERE tweet_id = '1'")
    b1 = next(b for b in populated.get("/api/bootstrap").json()["bookmarks"] if b["id"] == "1")
    assert b1["media"][0]["src"].startswith("https://pbs.twimg.com/")


def test_tags_lifecycle(populated):
    r = populated.post("/api/bookmarks/1/tags", json={"name": "  Rust "})
    assert r.json() == {"ok": True, "name": "Rust"}
    assert populated.post("/api/bookmarks/1/tags", json={"name": "ai"}).json()["name"] == "ai"
    assert populated.post("/api/bookmarks/2/tags", json={"name": "rust"}).json()["name"] == "Rust"   # first spelling wins
    assert populated.post("/api/bookmarks/1/tags", json={"name": "RUST"}).status_code == 200          # idempotent
    st = populated.get("/api/state").json()
    assert st["tags"] == {"1": ["Rust", "ai"], "2": ["Rust"]}                                        # order preserved
    assert st["tagLastUsed"]["rust"] > 0

    populated.delete("/api/bookmarks/1/tags", params={"name": "RUST"})
    populated.delete("/api/bookmarks/1/tags", params={"name": "RUST"})                               # idempotent
    assert populated.get("/api/state").json()["tags"] == {"1": ["ai"], "2": ["Rust"]}


def test_global_tag_delete_restore_and_revival_on_retag(populated):
    populated.post("/api/bookmarks/1/tags", json={"name": "rust"})
    populated.post("/api/bookmarks/2/tags", json={"name": "rust"})
    populated.post("/api/tags/delete", json={"name": "Rust"})
    st = populated.get("/api/state").json()
    assert st["deletedTags"] == ["rust"] and st["tags"]["1"] == ["rust"]       # links kept so Restore is lossless
    populated.post("/api/tags/restore", json={"name": "rust"})
    assert populated.get("/api/state").json()["deletedTags"] == []
    populated.post("/api/tags/delete", json={"name": "rust"})
    populated.post("/api/bookmarks/3/tags", json={"name": "rust"})               # tagging with it again revives it
    assert populated.get("/api/state").json()["deletedTags"] == []


def test_tag_validation_and_unknown_bookmark(populated):
    assert populated.post("/api/bookmarks/1/tags", json={"name": "   "}).status_code == 422
    assert populated.post("/api/bookmarks/1/tags", json={"name": "x" * 101}).status_code == 422
    assert populated.post("/api/bookmarks/nope/tags", json={"name": "a"}).status_code == 404
    assert populated.delete("/api/bookmarks/1/tags").status_code == 422


def test_unicode_and_odd_tag_names_roundtrip(populated):
    for name in ["日本語", "c++", "a/b", "50% off", "what?&x=1", "émoji 🎉"]:
        assert populated.post("/api/bookmarks/1/tags", json={"name": name}).json()["name"] == name
        populated.delete("/api/bookmarks/1/tags", params={"name": name})
    assert populated.get("/api/state").json()["tags"] == {}


def test_hide_restore_and_restore_all(populated):
    populated.post("/api/bookmarks/hide", json={"ids": ["1", "2", "ghost"]})
    assert sorted(populated.get("/api/state").json()["hidden"]) == ["1", "2"]
    populated.post("/api/bookmarks/hide", json={"ids": ["1"], "hidden": False})
    assert populated.get("/api/state").json()["hidden"] == ["2"]
    populated.post("/api/bookmarks/restore-all")
    assert populated.get("/api/state").json()["hidden"] == []
    assert len(populated.get("/api/bootstrap").json()["bookmarks"]) == 3        # hiding never deletes data


def test_saved_filters_upsert_keep_order_and_delete(populated):
    populated.put("/api/saved-filters", json={"name": "Rusty", "expr": "rust"})
    populated.put("/api/saved-filters", json={"name": "Other", "expr": "a or b"})
    populated.put("/api/saved-filters", json={"name": "rusty", "expr": "rust and not x"})
    assert populated.get("/api/state").json()["savedFilters"] == [
        {"name": "Rusty", "expr": "rust and not x"}, {"name": "Other", "expr": "a or b"}]
    populated.delete("/api/saved-filters", params={"name": "RUSTY"})
    assert [f["name"] for f in populated.get("/api/state").json()["savedFilters"]] == ["Other"]


def test_media_serves_ranges_and_blocks_traversal(populated, settings):
    src = populated.get("/api/bootstrap").json()["bookmarks"][1]["media"][0]["src"]
    full = populated.get(src)
    assert full.status_code == 200 and "immutable" in full.headers["cache-control"]
    part = populated.get(src, headers={"Range": "bytes=0-3"})
    assert part.status_code == 206 and part.content == full.content[:4]
    assert part.headers["content-range"].startswith("bytes 0-3/")
    for bad in ("/media/..%2F..%2Fetc%2Fpasswd", "/media/.hidden", "/media/nope.jpg", "/media/a%2Fb"):
        assert populated.get(bad).status_code == 404


def test_only_api_responses_are_compressed(populated):
    big = populated.get("/api/bootstrap", headers={"Accept-Encoding": "gzip"})
    assert big.headers.get("content-encoding") == "gzip" and big.headers["cache-control"] == "no-store"
    src = big.json()["bookmarks"][1]["media"][0]["src"]
    assert "content-encoding" not in populated.get(src, headers={"Accept-Encoding": "gzip"}).headers


def test_cross_site_writes_are_refused(populated):
    evil = {"Origin": "https://evil.example"}
    assert populated.post("/api/bookmarks/1/tags", json={"name": "x"}, headers=evil).status_code == 403
    assert populated.post("/api/bookmarks/restore-all", headers=evil).status_code == 403
    assert populated.post("/api/bookmarks/1/tags", json={"name": "x"},
                          headers={"Origin": "https://bookmarks.example.test"}).status_code == 200
    assert populated.get("/api/state", headers=evil).status_code == 200            # reads are unaffected


def test_sync_endpoints(populated, db, settings, fx):
    st = populated.get("/api/sync/status").json()
    assert st["connected"] and st["running"] is False
    assert populated.post("/api/sync").json()["skipped"] == "cooldown"             # a sync just ran
    out = populated.post("/api/sync", params={"mode": "incremental", "force": "true"}).json()
    assert out["running"] is True
    populated.app.state.engine._thread.join(timeout=10)
    assert populated.post("/api/sync", params={"mode": "bogus"}).status_code == 422


def test_x_connect_flow_end_to_end(client, fx, db):
    r = client.get("/auth/x/login", follow_redirects=False)
    assert r.status_code == 302
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    cb = client.get("/auth/x/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    assert cb.status_code == 303 and cb.headers["location"] == "/?x=connected"
    assert client.get("/api/sync/status").json()["handle"] == "me"
    assert client.get("/auth/x/callback", params={"code": "abc", "state": state}).status_code == 400   # replay
    assert client.get("/auth/x/callback", params={"error": "access_denied"}).status_code == 400


def test_login_without_configuration_explains_itself(db, settings, fx):
    from dataclasses import replace
    app = create_app(replace(settings, x_client_id=""), http_factory=fx.client, start_scheduler=False)
    with TestClient(app) as c:
        r = c.get("/auth/x/login", follow_redirects=False)
        assert r.status_code == 400 and "X_CLIENT_ID" in r.text
        assert c.get("/api/sync/status").json()["configured"] is False
