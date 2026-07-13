#!/usr/bin/env python3
"""okiedoke: serve the bookmarks app locally and expose a /sync endpoint.

Usage: python3 serve.py  (or double-click okiedoke.command)

Serves the ft data dir (index.html + media/) on http://localhost:8377 and
opens the browser. POST /sync runs `ft sync --rebuild --yes`, records the
crawl window so generate.py can tag bookmarks that vanished from X as
"deleted on X", then regenerates index.html.
"""
import json
import os
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FT_DIR = Path(os.environ.get("OKIEDOKE_DATA", str(Path.home() / ".ft-bookmarks")))
APP_DIR = Path(__file__).resolve().parent
LAST_FULL_SYNC = FT_DIR / "last-full-sync.json"
PORT = 8377

sync_lock = threading.Lock()
sync_state = {"running": False, "result": None, "startedAt": None}
AUTO_SYNC_COOLDOWN_S = 600  # skip non-forced syncs if the last one is fresher


def sync_progress():
    """Fraction of rows the in-flight crawl has re-seen (determinate-ish:
    accurate through the crawl phase, parks near 1.0 during media fetch)."""
    started = sync_state["startedAt"]
    if not started:
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(FT_DIR / "bookmarks.db", timeout=1)
        total, = conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()
        seen, = conn.execute(
            "SELECT COUNT(*) FROM bookmarks WHERE synced_at >= ?", (started,)
        ).fetchone()
        conn.close()
        return (seen / total) if total else None
    except Exception:
        return None


def last_sync_age_s():
    try:
        completed = json.loads(LAST_FULL_SYNC.read_text())["completedAt"]
        dt = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


def run_sync():
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    sync_state["startedAt"] = started_at
    proc = subprocess.run(
        ["ft", "sync", "--rebuild", "--yes"],
        capture_output=True, text=True, timeout=45 * 60,
    )
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
    if proc.returncode != 0:
        return {"ok": False, "error": f"ft sync failed (exit {proc.returncode})", "log": tail}

    # Record the crawl window only on success: generate.py marks rows whose
    # synced_at predates this window as no longer bookmarked on X. Sanity-check
    # that the crawl actually touched most rows so a silently-partial crawl
    # doesn't mass-mark everything as deleted.
    import sqlite3
    conn = sqlite3.connect(FT_DIR / "bookmarks.db")
    total, = conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()
    seen, = conn.execute(
        "SELECT COUNT(*) FROM bookmarks WHERE synced_at >= ?", (started_at,)
    ).fetchone()
    conn.close()
    if total and seen / total >= 0.5:
        LAST_FULL_SYNC.write_text(json.dumps({
            "startedAt": started_at,
            "completedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }))
    gen = subprocess.run(
        [sys.executable, str(APP_DIR / "generate.py")],
        capture_output=True, text=True,
    )
    if gen.returncode != 0:
        return {"ok": False, "error": "generate.py failed", "log": gen.stderr[-2000:]}
    return {"ok": True, "log": tail, "crawled": seen, "total": total}


def sync_worker():
    try:
        sync_state["result"] = run_sync()
    except Exception as e:
        sync_state["result"] = {"ok": False, "error": str(e)}
    finally:
        sync_state["running"] = False
        sync_state["startedAt"] = None
        sync_lock.release()


class Handler(SimpleHTTPRequestHandler):
    # Keep-alive: the page requests hundreds of images/videos; per-request
    # sockets exhaust macOS socket buffers (ENOBUFS) and spam tracebacks.
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FT_DIR), **kwargs)

    def handle(self):
        # Client disconnects and transient buffer exhaustion are routine when
        # streaming media locally — drop the connection quietly.
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as e:
            if e.errno != 55:  # ENOBUFS
                raise

    def send_json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Sync runs in a background thread: a rebuild crawl takes minutes and
        # browsers time out long-idle fetches, so the client polls /sync-status.
        if self.path.split("?")[0] != "/sync":
            self.send_error(404)
            return
        forced = "force=1" in (self.path.split("?") + [""])[1]
        if not forced and not sync_state["running"]:
            age = last_sync_age_s()
            if age is not None and age < AUTO_SYNC_COOLDOWN_S:
                self.send_json({"ok": True, "running": False, "skipped": True})
                return
        if sync_lock.acquire(blocking=False):
            sync_state["running"] = True
            sync_state["result"] = None
            threading.Thread(target=sync_worker, daemon=True).start()
        self.send_json({"ok": True, "running": True})

    def do_GET(self):
        if self.path == "/sync-status":
            self.send_json({
                "running": sync_state["running"],
                "result": sync_state["result"],
                "progress": sync_progress() if sync_state["running"] else None,
            })
            return
        super().do_GET()

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}/"
    print(f"Bookmarks app: {url}  (Ctrl-C to stop)")
    threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
