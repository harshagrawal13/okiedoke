"""The default wiring, with nothing injected: the app's own httpx clients talk over a
real socket to a fake X (X_API_BASE override), through the OAuth connect flow and
a background sync, exactly as in production minus the real X."""
from __future__ import annotations

import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from .fakex import FakeX


@pytest.fixture()
def fake_x_server():
    fx = FakeX()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            req = httpx.Request(self.command, f"https://api.x.com{self.path}", headers=dict(self.headers), content=body)
            resp = fx._handle(req)
            self.send_response(resp.status_code)
            for k, v in resp.headers.items():
                if k.lower() not in ("content-length", "transfer-encoding", "content-encoding"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp.content)))
            self.end_headers()
            self.wfile.write(resp.content)

        do_GET = do_POST = _serve

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield fx, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_connect_then_background_sync_over_real_http(db, settings, fake_x_server):
    fx, base = fake_x_server
    for i in range(1, 6):
        fx.add(str(i), f"post {i} &amp; more", author="alice" if i % 2 else "bob")
    s = replace(settings, x_api_base=base, sync_enabled=False)
    with TestClient(create_app(s, start_scheduler=False)) as c:        # note: no http_factory injected
        assert c.get("/api/sync/status").json()["connected"] is False

        loc = c.get("/auth/x/login", follow_redirects=False).headers["location"]
        assert loc.startswith(s.x_auth_url)
        state = parse_qs(urlparse(loc).query)["state"][0]
        assert c.get("/auth/x/callback", params={"code": "abc", "state": state}, follow_redirects=False).status_code == 303
        assert c.get("/api/sync/status").json()["handle"] == "me"

        assert c.post("/api/sync", params={"force": "true"}).json()["running"] is True
        for _ in range(100):
            st = c.get("/api/sync/status").json()
            if not st["running"] and st["last"] and st["last"]["status"] != "running":
                break
            time.sleep(0.1)
        assert st["last"]["status"] == "ok" and st["last"]["kind"] == "full"      # first ever sync is a full sweep
        assert st["last"]["fetched"] == 5

        books = c.get("/api/bootstrap").json()["bookmarks"]
        assert sorted(b["id"] for b in books) == ["1", "2", "3", "4", "5"]
        assert next(b for b in books if b["id"] == "1")["text"] == "post 1 & more"
        assert c.post("/api/sync").json()["skipped"] == "cooldown"                # page-open syncs are rate limited
        assert c.get("/api/health").json() == {"ok": True}
