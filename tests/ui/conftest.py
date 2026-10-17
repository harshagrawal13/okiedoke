"""Browser tests: a live server on a throwaway database, seeded from the sample ft
directory (tests/sample_ft.py), driven by headless Chromium.

Set CHROMIUM_PATH to use a specific Chromium; otherwise Playwright's own build or
/opt/pw-browsers/chromium is used. Tests skip cleanly when no browser is available.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from app.config import Settings
from app.import_ft import Importer
from tests.conftest import USER_TABLES
from tests.sample_ft import build

playwright_sync = pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def ft_dir(tmp_path_factory) -> Path:
    return build(tmp_path_factory.mktemp("ft"))


@pytest.fixture(scope="session")
def ui_settings(database_url, tmp_path_factory) -> Settings:
    return Settings(database_url=database_url, media_dir=tmp_path_factory.mktemp("media"),
                    app_env="test", sync_enabled=False)


@pytest.fixture(scope="session")
def server(ui_settings, _db, ft_dir):
    port = _free_port()
    env = {**os.environ, "DATABASE_URL": ui_settings.database_url, "MEDIA_DIR": str(ui_settings.media_dir),
           "APP_ENV": "test", "SYNC_ENABLED": "false", "PUBLIC_URL": f"http://127.0.0.1:{port}"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory",
                             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"], env=env)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if urllib.request.urlopen(url + "/api/health", timeout=1).status == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("test server did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture()
def app_url(server, _db, ui_settings, ft_dir):
    """Fresh data for every test: wipe user + synced tables, re-import the sample."""
    with _db.connection() as conn:
        conn.execute(f"TRUNCATE {USER_TABLES} RESTART IDENTITY CASCADE")
    Importer(ft_dir, _db, ui_settings).run()
    return server


@pytest.fixture(scope="session")
def browser():
    with playwright_sync.sync_playwright() as p:
        path = os.environ.get("CHROMIUM_PATH") or ("/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
        try:
            b = p.chromium.launch(executable_path=path) if path else p.chromium.launch()
        except Exception as e:                      # no browser installed
            pytest.skip(f"Chromium not available: {e}")
        yield b
        b.close()


class Page:
    """A Playwright page plus the console errors it has produced."""

    def __init__(self, page, errors):
        self.page, self.errors = page, errors

    def __getattr__(self, name):
        return getattr(self.page, name)


def _new_page(browser, **ctx):
    context = browser.new_context(**ctx)
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    return Page(page, errors), context


@pytest.fixture()
def desk(browser, app_url):
    pg, ctx = _new_page(browser, viewport={"width": 1200, "height": 900})
    pg.goto(app_url)
    pg.wait_for_selector("article.tweet")
    yield pg
    ctx.close()


@pytest.fixture()
def phone(browser, app_url):
    pg, ctx = _new_page(browser, viewport={"width": 390, "height": 844}, device_scale_factor=2,
                        is_mobile=True, has_touch=True)
    pg.goto(app_url)
    pg.wait_for_selector("article.tweet")
    yield pg
    ctx.close()


@pytest.fixture()
def new_page(browser, app_url):
    """Factory for extra pages (a 'second device')."""
    made = []

    def make(setup=None, **ctx):
        pg, c = _new_page(browser, **ctx)
        made.append(c)
        if setup:
            setup(pg)          # e.g. route/mock before the first navigation
        pg.goto(app_url)
        pg.wait_for_selector("article.tweet")
        return pg
    yield make
    for c in made:
        c.close()
