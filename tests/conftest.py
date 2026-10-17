"""Test fixtures: a throwaway Postgres database per test session.

Point TEST_DATABASE_URL at any Postgres you can CREATE DATABASE on, e.g.
  TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres pytest
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest

from app.config import Settings
from app.db import Database

ADMIN_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:54329/postgres")

USER_TABLES = (
    "bookmark_tags, bookmarks, media, tweets, authors, tags, saved_filters, "
    "sync_runs, x_auth, oauth_states, legacy_imports"
)


@pytest.fixture(scope="session")
def database_url():
    name = f"okiedoke_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    base = ADMIN_URL.rsplit("/", 1)[0]
    yield f"{base}/{name}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def _db(database_url):
    db = Database(database_url)
    db.open()
    db.migrate()
    yield db
    db.close()


@pytest.fixture()
def db(_db):
    with _db.connection() as conn:
        conn.execute(f"TRUNCATE {USER_TABLES} RESTART IDENTITY CASCADE")
    return _db


@pytest.fixture()
def settings(database_url, tmp_path: Path) -> Settings:
    return Settings(
        database_url=database_url,
        media_dir=tmp_path / "media",
        app_env="test",
        public_url="https://bookmarks.example.test",
        x_client_id="client-id",
        x_client_secret="client-secret",
        secret_key="test-secret-key",
        sync_enabled=False,
    )
