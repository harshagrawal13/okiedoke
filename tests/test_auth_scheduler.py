from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest

from app.auth_x import BadOAuthState, XAuth, pkce_pair
from app.crypto import decrypt
from app.scheduler import Scheduler

from .fakex import FakeX
from .helpers import connect_account, make_engine


def q1(db, sql, *args):
    with db.connection() as conn:
        return conn.execute(sql, args).fetchone()


def test_pkce_challenge_matches_verifier():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def test_connect_flow_stores_encrypted_tokens(db, settings):
    fx = FakeX()
    auth = XAuth(settings, db, http=fx.client())
    url = auth.begin()
    parsed = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == settings.x_auth_url
    assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "https://bookmarks.example.test/auth/x/callback"
    assert "offline.access" in q["scope"] and "bookmark.read" in q["scope"]

    out = auth.complete(q["state"], "the-code")
    assert out == {"x_user_id": "42", "handle": "me"}
    row = q1(db, "SELECT * FROM x_auth")
    assert decrypt(settings.secret_key, row["access_token_enc"]) == fx.access_token
    assert fx.access_token not in row["access_token_enc"]
    assert auth.account()["status"] == "ok"
    with pytest.raises(BadOAuthState):                     # state is single-use
        auth.complete(q["state"], "the-code")


def test_callback_with_unknown_state_is_rejected(db, settings):
    with pytest.raises(BadOAuthState):
        XAuth(settings, db, http=FakeX().client()).complete("nope", "code")


def test_reconnect_clears_needs_reauth(db, settings):
    fx = FakeX()
    connect_account(db, settings, fx, status="needs_reauth")
    auth = XAuth(settings, db, http=fx.client())
    state = parse_qs(urlparse(auth.begin()).query)["state"][0]
    auth.complete(state, "code")
    assert auth.account()["status"] == "ok"


def test_scheduler_runs_when_due_and_backs_off_after_failure(db, settings):
    fx = FakeX()
    fx.add("1")
    s = replace(settings, sync_interval_min=60)
    connect_account(db, s, fx)
    eng = make_engine(db, s, fx)
    sch = Scheduler(s, db, eng)

    assert sch.due()                                       # never ran
    assert sch.tick() is True
    assert not sch.due() and sch.tick() is False           # just finished OK: wait out the interval

    with db.connection() as conn:                          # pretend the last run ended 20 min ago in error
        conn.execute("UPDATE sync_runs SET status = 'error', finished_at = now() - interval '20 minutes'")
    assert sch.due()                                       # retry after 15 min, not the full 60

    with db.connection() as conn:
        conn.execute("UPDATE sync_runs SET finished_at = now() - interval '5 minutes'")
    assert not sch.due()                                   # but never in a tight loop


def test_scheduler_idle_without_account_or_after_reauth(db, settings):
    fx = FakeX()
    sch = Scheduler(settings, db, make_engine(db, settings, fx))
    assert not sch.due()                                   # no X account connected
    connect_account(db, settings, fx, status="needs_reauth")
    assert not sch.due()
