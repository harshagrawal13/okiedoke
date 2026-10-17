"""Background sync schedule: one polling thread, no external cron."""
from __future__ import annotations

import logging
import threading

from .config import Settings
from .db import Database
from .sync import SyncEngine

log = logging.getLogger("okiedoke.scheduler")
POLL_S = 30
RETRY_AFTER_FAILURE_MIN = 15


class Scheduler:
    def __init__(self, settings: Settings, db: Database, engine: SyncEngine):
        self.s = settings
        self.db = db
        self.engine = engine
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")

    def start(self) -> None:
        if self.s.sync_enabled and self.s.x_configured:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def due(self) -> bool:
        """Is a scheduled sync owed? Measured from the last finished run; after a
        failure retry sooner than the normal cadence, but never in a tight loop."""
        acct = self.engine.auth.account()
        if not acct or acct["status"] != "ok":
            return False
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT status, finished_at FROM sync_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return True
            wait_min = self.s.sync_interval_min if row["status"] == "ok" else min(
                self.s.sync_interval_min, RETRY_AFTER_FAILURE_MIN)
            return conn.execute(
                "SELECT now() >= %s + %s * interval '1 minute' AS due", (row["finished_at"], wait_min)
            ).fetchone()["due"]

    def tick(self) -> bool:
        if self.engine.is_running() or not self.due():
            return False
        self.engine.run(self.engine.choose_kind(), "schedule")
        return True

    def _loop(self) -> None:
        while not self._stop.wait(POLL_S):
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
