from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.auth_x import XAuth
from app.crypto import encrypt
from app.sync import SyncEngine

from .fakex import FakeX


def connect_account(db, settings, fx: FakeX, expires_in_s: int = 7200, status: str = "ok") -> None:
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO x_auth (id, x_user_id, handle, access_token_enc, refresh_token_enc, expires_at, status) "
            "VALUES (1, '42', 'me', %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
            "access_token_enc = EXCLUDED.access_token_enc, refresh_token_enc = EXCLUDED.refresh_token_enc, "
            "expires_at = EXCLUDED.expires_at, status = EXCLUDED.status",
            (encrypt(settings.secret_key, fx.access_token), encrypt(settings.secret_key, fx.refresh_token),
             datetime.now(timezone.utc) + timedelta(seconds=expires_in_s), status))


def make_engine(db, settings, fx: FakeX) -> SyncEngine:
    auth = XAuth(settings, db, http=fx.client())
    return SyncEngine(settings, db, auth, http_factory=fx.client)
