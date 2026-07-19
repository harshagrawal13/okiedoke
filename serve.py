#!/usr/bin/env python3
"""okiedoke: serve the bookmarks app locally and expose a /sync endpoint.

Usage: python3 serve.py  (or double-click okiedoke.command)

Serves the ft data dir (index.html + media/) on http://localhost:8377 and
opens the browser. POST /sync runs an incremental `ft sync --yes` by default
(fast, and it won't trip X's rate limits); pass ?mode=full to force a full
`--rebuild` re-crawl, which records the crawl window so generate.py can tag
bookmarks that vanished from X as "deleted on X". Either way it regenerates
index.html.
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
INDEX = FT_DIR / "index.html"                    # the built app
DB = FT_DIR / "bookmarks.db"                      # ft's synced database
LAST_FULL_SYNC = FT_DIR / "last-full-sync.json"  # last full re-crawl (for deleted-on-X)
LAST_SYNC = FT_DIR / "last-sync.json"            # last sync of any kind (for the cooldown)
TAGS_FILE = FT_DIR / "okiedoke-tags.json"        # the ONE source of truth for user tags
PORT = 8377

# The tag document's known keys and their empty shapes. POST /tags is coerced to
# exactly these so a malformed client payload can never corrupt the file.
TAGS_SHAPE = {
    "customTags": dict,
    "removedTags": dict,
    "globalRemovedTags": list,
    "hiddenBookmarks": list,
    "tagLastUsed": dict,
    "savedFilters": list,
}
tags_lock = threading.Lock()


def read_tags():
    try:
        doc = json.loads(TAGS_FILE.read_text())
        return doc if isinstance(doc, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_tags(doc):
    """Atomically replace the tag file (temp + os.replace) so a crash or a
    concurrent read never sees a half-written document."""
    clean = {"version": 1}
    for key, typ in TAGS_SHAPE.items():
        val = doc.get(key)
        clean[key] = val if isinstance(val, typ) else typ()
    tmp = TAGS_FILE.with_suffix(".json.tmp")
    with tags_lock:
        tmp.write_text(json.dumps(clean, ensure_ascii=False))
        os.replace(tmp, TAGS_FILE)
    return clean

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
    """Seconds since the last sync of any kind (drives the auto-sync cooldown)."""
    try:
        completed = json.loads(LAST_SYNC.read_text())["completedAt"]
        dt = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


def run_sync(rebuild):
    """Incremental sync skips bookmarks we already have (ft stops at known
    ones); a rebuild re-crawls everything, which is what lets generate.py
    tag rows missing from the crawl as deleted on X."""
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    sync_state["startedAt"] = started_at
    cmd = ["ft", "sync", "--yes"] + (["--rebuild"] if rebuild else [])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45 * 60)
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
    if proc.returncode != 0:
        return {"ok": False, "error": f"ft sync failed (exit {proc.returncode})", "log": tail}

    # Record the crawl window only after a successful rebuild: generate.py
    # marks rows whose synced_at predates this window as no longer bookmarked
    # on X. Sanity-check that the crawl actually touched most rows so a
    # silently-partial crawl doesn't mass-mark everything as deleted.
    import sqlite3
    conn = sqlite3.connect(FT_DIR / "bookmarks.db")
    total, = conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()
    seen, = conn.execute(
        "SELECT COUNT(*) FROM bookmarks WHERE synced_at >= ?", (started_at,)
    ).fetchone()
    conn.close()
    completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if rebuild and total and seen / total >= 0.5:
        LAST_FULL_SYNC.write_text(json.dumps({
            "startedAt": started_at, "completedAt": completed_at,
        }))
    # Record every successful sync (incremental included) so the auto-sync
    # cooldown works even when we never do a full re-crawl.
    LAST_SYNC.write_text(json.dumps({"completedAt": completed_at}))
    gen = subprocess.run(
        [sys.executable, str(APP_DIR / "generate.py")],
        capture_output=True, text=True,
    )
    if gen.returncode != 0:
        return {"ok": False, "error": "generate.py failed", "log": gen.stderr[-2000:]}
    return {"ok": True, "log": tail, "crawled": seen, "total": total, "rebuild": rebuild}


def sync_worker(rebuild):
    try:
        sync_state["result"] = run_sync(rebuild)
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
        path = self.path.split("?")[0]
        # Persist the whole tag document. The client always sends complete state,
        # so this is a full overwrite (atomic) — no server-side merge needed.
        if path == "/tags":
            try:
                length = int(self.headers.get("Content-Length", 0))
                doc = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            saved = write_tags(doc if isinstance(doc, dict) else {})
            self.send_json({"ok": True, "saved": True, "counts": {
                "customTags": len(saved["customTags"]),
                "hiddenBookmarks": len(saved["hiddenBookmarks"]),
            }})
            return
        # Sync runs in a background thread: a rebuild crawl takes minutes and
        # browsers time out long-idle fetches, so the client polls /sync-status.
        if path != "/sync":
            self.send_error(404)
            return
        query = (self.path.split("?") + [""])[1]
        forced = "force=1" in query
        if not forced and not sync_state["running"]:
            age = last_sync_age_s()
            if age is not None and age < AUTO_SYNC_COOLDOWN_S:
                self.send_json({"ok": True, "running": False, "skipped": True})
                return
        # Incremental by default (skips bookmarks we already have). A full
        # re-crawl of every bookmark reliably trips X's rate limits
        # ("fetch failed"), so we never trigger it automatically — only on an
        # explicit Shift-click (mode=full). Deleted-on-X data refreshes then.
        rebuild = "mode=full" in query
        if sync_lock.acquire(blocking=False):
            sync_state["running"] = True
            sync_state["result"] = None
            sync_state["rebuild"] = rebuild
            threading.Thread(target=sync_worker, args=(rebuild,), daemon=True).start()
        self.send_json({"ok": True, "running": True})

    def do_GET(self):
        if self.path == "/tags":
            # The freshest tag document from disk — lets any browser pull tags
            # saved by another without a regeneration.
            self.send_json(read_tags())
            return
        if self.path == "/sync-status":
            # Percent progress only makes sense for a full re-crawl; an
            # incremental sync touches few rows, so show indeterminate.
            rebuilding = sync_state["running"] and sync_state.get("rebuild")
            self.send_json({
                "running": sync_state["running"],
                "result": sync_state["result"],
                "progress": sync_progress() if rebuilding else None,
            })
            return
        super().do_GET()

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet


def ensure_built():
    """First-run bootstrap: build index.html so the server never serves a 404.
    If there's no synced data yet, point the user at the one-step setup."""
    if INDEX.exists():
        return True
    if not DB.exists():
        print(
            f"No bookmarks found in {FT_DIR}.\n"
            "Run ./setup.sh to sync your X bookmarks and launch, or:\n"
            "  1. install & sign in to ft:  https://fieldtheory.dev/cli\n"
            "  2. ft sync\n"
            "  3. python3 serve.py",
            file=sys.stderr,
        )
        return False
    print("First run: building the app from your synced bookmarks…")
    gen = subprocess.run([sys.executable, str(APP_DIR / "generate.py")])
    return gen.returncode == 0


def main():
    if not ensure_built():
        sys.exit(1)
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
