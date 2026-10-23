"""X OAuth 2.0 (authorization code + PKCE) and token lifecycle.

X rotates the refresh token on every refresh: the old one stops working the
moment a new one is issued. Two concurrent refreshes (or a crash between "X
issued it" and "we stored it") therefore lock the account out. So refreshing
happens while holding a row lock on x_auth, and the new pair is stored in the
same transaction.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from .config import Settings
from .crypto import decrypt, encrypt
from .db import Database

log = logging.getLogger("okiedoke.auth")
REFRESH_MARGIN_S = 120


class NotConnected(Exception):
    """No X account has been connected yet."""


class NeedsReauth(Exception):
    """X rejected our refresh token; the user must reconnect."""


class TokenRefreshFailed(Exception):
    """Transient failure talking to the token endpoint; tokens are untouched."""


class BadOAuthState(Exception):
    """Callback state is unknown or expired."""


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)           # 86 chars, within the 43-128 RFC 7636 range
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class XAuth:
    def __init__(self, settings: Settings, db: Database, http: httpx.Client | None = None,
                 clock=time.time):
        self.s = settings
        self.db = db
        self._http = http
        self.clock = clock

    # -- plumbing ---------------------------------------------------------
    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=20)       # one pooled client, not one per call
        return self._http

    def _token_request(self, data: dict) -> dict:
        data = {**data, "client_id": self.s.x_client_id}
        auth = (self.s.x_client_id, self.s.x_client_secret) if self.s.x_client_secret else None
        try:
            resp = self._client().post(f"{self.s.x_api_base}/2/oauth2/token", data=data, auth=auth)
        except httpx.TransportError as e:
            raise TokenRefreshFailed(f"token endpoint unreachable: {e}") from e
        if resp.status_code == 200:
            return resp.json()
        try:
            err = resp.json()
        except ValueError:
            err = {}
        code = err.get("error", "")
        if resp.status_code in (400, 401) and code in ("invalid_grant", "invalid_request", "invalid_client", "unauthorized_client"):
            raise NeedsReauth(err.get("error_description") or code)
        raise TokenRefreshFailed(f"token endpoint returned {resp.status_code}: {resp.text[:200]}")

    # -- connect flow -----------------------------------------------------
    def begin(self) -> str:
        """Create PKCE state and return the X authorize URL to redirect to."""
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        with self.db.connection() as conn:
            conn.execute("DELETE FROM oauth_states WHERE created_at < now() - interval '1 hour'")
            conn.execute("INSERT INTO oauth_states (state, code_verifier) VALUES (%s, %s)", (state, verifier))
        return self.s.x_auth_url + "?" + urlencode({
            "response_type": "code",
            "client_id": self.s.x_client_id,
            "redirect_uri": self.s.x_redirect_uri,
            "scope": self.s.x_scopes,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })

    def complete(self, state: str, code: str) -> dict:
        with self.db.connection() as conn:
            row = conn.execute(
                "DELETE FROM oauth_states WHERE state = %s AND created_at > now() - interval '15 minutes' "
                "RETURNING code_verifier", (state,)).fetchone()
        if not row:
            raise BadOAuthState("unknown or expired state; start the connection again")
        tok = self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.s.x_redirect_uri,
            "code_verifier": row["code_verifier"],
        })
        access = tok["access_token"]
        refresh = tok.get("refresh_token")
        if not refresh:
            raise TokenRefreshFailed("X returned no refresh token; the offline.access scope is required")
        me = self._client().get(f"{self.s.x_api_base}/2/users/me",
                                headers={"Authorization": f"Bearer {access}"})
        me.raise_for_status()
        user = me.json()["data"]
        expires = datetime.fromtimestamp(self.clock() + int(tok.get("expires_in", 7200)), timezone.utc)
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO x_auth (id, x_user_id, handle, access_token_enc, refresh_token_enc, expires_at, scope, status) "
                "VALUES (1, %s, %s, %s, %s, %s, %s, 'ok') "
                "ON CONFLICT (id) DO UPDATE SET x_user_id = EXCLUDED.x_user_id, handle = EXCLUDED.handle, "
                " access_token_enc = EXCLUDED.access_token_enc, refresh_token_enc = EXCLUDED.refresh_token_enc, "
                " expires_at = EXCLUDED.expires_at, scope = EXCLUDED.scope, status = 'ok', updated_at = now()",
                (user["id"], user.get("username"), encrypt(self.s.secret_key, access),
                 encrypt(self.s.secret_key, refresh), expires, tok.get("scope")))
        return {"x_user_id": user["id"], "handle": user.get("username")}

    # -- tokens -----------------------------------------------------------
    def account(self) -> dict | None:
        with self.db.connection() as conn:
            return conn.execute("SELECT x_user_id, handle, status, expires_at FROM x_auth WHERE id = 1").fetchone()

    def access_token(self, stale: str | None = None) -> str:
        """A valid access token, refreshing under a row lock when it is about to
        expire. Pass `stale` (a token that just got a 401) to force a refresh
        unless another caller already replaced it."""
        reauth: str | None = None
        with self.db.connection() as conn:
            with conn.transaction():
                row = conn.execute("SELECT * FROM x_auth WHERE id = 1 FOR UPDATE").fetchone()
                if row is None:
                    raise NotConnected("connect your X account first")
                if row["status"] == "needs_reauth":
                    raise NeedsReauth("X access was revoked or expired; reconnect")
                current = decrypt(self.s.secret_key, row["access_token_enc"])
                fresh = row["expires_at"].timestamp() - self.clock() > REFRESH_MARGIN_S
                if stale is None and fresh:
                    return current
                if stale is not None and current != stale:
                    return current                      # someone else already refreshed
                try:
                    tok = self._token_request({
                        "grant_type": "refresh_token",
                        "refresh_token": decrypt(self.s.secret_key, row["refresh_token_enc"]),
                    })
                except NeedsReauth as e:
                    conn.execute("UPDATE x_auth SET status = 'needs_reauth', updated_at = now() WHERE id = 1")
                    reauth = str(e)
                else:
                    access = tok["access_token"]
                    refresh = tok.get("refresh_token") or decrypt(self.s.secret_key, row["refresh_token_enc"])
                    expires = datetime.fromtimestamp(self.clock() + int(tok.get("expires_in", 7200)), timezone.utc)
                    conn.execute(
                        "UPDATE x_auth SET access_token_enc = %s, refresh_token_enc = %s, expires_at = %s, "
                        " scope = COALESCE(%s, scope), updated_at = now() WHERE id = 1",
                        (encrypt(self.s.secret_key, access), encrypt(self.s.secret_key, refresh),
                         expires, tok.get("scope")))
                    return access
        raise NeedsReauth(f"X rejected the saved login ({reauth}); reconnect your X account")
