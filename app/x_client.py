"""Thin X API v2 client: auth header, 401 refresh, 429 / 5xx / network retry."""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone

import httpx

from .auth_x import XAuth
from .config import Settings

log = logging.getLogger("okiedoke.x")

EXPANSIONS = ",".join([
    "author_id", "attachments.media_keys", "referenced_tweets.id",
    "referenced_tweets.id.author_id", "in_reply_to_user_id",
])
TWEET_FIELDS = ",".join([
    "created_at", "public_metrics", "entities", "referenced_tweets", "in_reply_to_user_id",
    "attachments", "note_tweet", "author_id",
])
USER_FIELDS = "username,name,profile_image_url"
MEDIA_FIELDS = "type,url,preview_image_url,variants,width,height"


class XApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"X API {status}: {message}" if status else message)
        self.status = status


class RateLimited(XApiError):
    def __init__(self, reset_at: float | None):
        when = datetime.fromtimestamp(reset_at, timezone.utc).strftime("%H:%M UTC") if reset_at else "later"
        super().__init__(429, f"rate limited until {when}")
        self.reset_at = reset_at


class XClient:
    def __init__(self, settings: Settings, auth: XAuth, http: httpx.Client,
                 sleep=None, clock=time.time, max_attempts: int = 5, max_wait_s: float = 90):
        self.s = settings
        self.auth = auth
        self.http = http
        self.sleep = sleep or (lambda seconds: time.sleep(seconds))   # resolved at call time (patchable)
        self.clock = clock
        self.max_attempts = max_attempts
        self.max_wait_s = max_wait_s

    def _backoff(self, attempt: int) -> float:
        return min(30.0, 1.5 * (2 ** attempt)) * (0.75 + random.random() / 2)

    def get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.s.x_api_base}{path}"
        token = self.auth.access_token()
        refreshed = False
        last_error = "no response"
        attempt = 0
        while attempt < self.max_attempts:
            try:
                resp = self.http.get(url, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=30)
            except httpx.TransportError as e:
                last_error = f"network error: {e}"
                log.warning("X request failed (%s), retry %d", e, attempt + 1)
                self.sleep(self._backoff(attempt))
                attempt += 1
                continue

            code = resp.status_code
            if code == 200:
                return resp.json()
            if code == 401 and not refreshed:
                token = self.auth.access_token(stale=token)   # raises NeedsReauth if X refuses
                refreshed = True
                continue
            if code == 429:
                reset = resp.headers.get("x-rate-limit-reset")
                reset_at = float(reset) if reset and reset.isdigit() else None
                retry_after = resp.headers.get("retry-after")
                wait = (float(retry_after) if retry_after and retry_after.isdigit()
                        else (reset_at - self.clock() + 1 if reset_at else self._backoff(attempt)))
                if wait > self.max_wait_s:
                    raise RateLimited(reset_at)               # give up for now; the next sync resumes
                self.sleep(max(wait, 1))
                attempt += 1
                continue
            if code >= 500:
                last_error = f"server error {code}"
                self.sleep(self._backoff(attempt))
                attempt += 1
                continue
            raise XApiError(code, _message(resp))
        raise XApiError(0, f"giving up after {self.max_attempts} attempts ({last_error})")

    def bookmarks_page(self, user_id: str, pagination_token: str | None, max_results: int) -> dict:
        params = {
            "max_results": max(1, min(100, max_results)),
            "expansions": EXPANSIONS,
            "tweet.fields": TWEET_FIELDS,
            "user.fields": USER_FIELDS,
            "media.fields": MEDIA_FIELDS,
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        return self.get(f"/2/users/{user_id}/bookmarks", params)


def _message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    return str(body.get("detail") or body.get("title") or body)[:300]
