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


def run_sync():
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
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


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FT_DIR), **kwargs)

    def do_POST(self):
        if self.path != "/sync":
            self.send_error(404)
            return
        if not sync_lock.acquire(blocking=False):
            body = json.dumps({"ok": False, "error": "sync already running"}).encode()
        else:
            try:
                body = json.dumps(run_sync()).encode()
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}).encode()
            finally:
                sync_lock.release()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
