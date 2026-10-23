"""An in-process fake of the bits of X we talk to (OAuth token endpoint, /users/me,
bookmarks, and the twimg media host), served through httpx.MockTransport."""
from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx


class FakeX:
    def __init__(self):
        self.access_token = "access-1"
        self.refresh_token = "refresh-1"
        self._n = 1
        self.bookmarks: list[dict] = []          # API order: most recently bookmarked first
        self.quoted: dict[str, dict] = {}
        self.users: dict[str, dict] = {}
        self.media: dict[str, dict] = {}
        self.media_status: dict[str, int] = {}   # path -> forced status
        self.bookmark_failures: list[httpx.Response] = []   # served first, one per bookmarks call
        self.token_error: tuple[int, dict] | None = None
        self.calls: list[tuple[str, str]] = []

    # -- building data -----------------------------------------------------
    def user(self, handle: str) -> dict:
        if handle not in self.users:
            uid = str(1000 + len(self.users))
            self.users[handle] = {"id": uid, "username": handle, "name": handle.title(),
                                  "profile_image_url": f"https://pbs.twimg.com/profile_images/{uid}/pic_normal.jpg"}
        return self.users[handle]

    def add(self, tid: str, text: str = "hello", author: str = "alice", front: bool = False,
            photos: int = 0, video: bool = False, quote: str | None = None, reply_to: tuple[str, str] | None = None,
            created_at: str = "2024-05-01T12:00:00.000Z", entities: dict | None = None, extra: dict | None = None) -> dict:
        u = self.user(author)
        t: dict = {"id": tid, "text": text, "author_id": u["id"], "created_at": created_at,
                   "public_metrics": {"retweet_count": 2, "reply_count": 1, "like_count": 10, "quote_count": 1,
                                      "bookmark_count": 3}}
        keys = []
        for i in range(photos):
            key = f"3_{tid}_{i}"
            self.media[key] = {"media_key": key, "type": "photo", "url": f"https://pbs.twimg.com/media/{tid}x{i}.jpg",
                               "width": 100, "height": 100}
            keys.append(key)
        if video:
            key = f"7_{tid}"
            self.media[key] = {"media_key": key, "type": "video", "preview_image_url": f"https://pbs.twimg.com/p/{tid}.jpg",
                               "variants": [{"content_type": "application/x-mpegURL", "url": f"https://video.twimg.com/{tid}.m3u8"},
                                            {"bit_rate": 256000, "content_type": "video/mp4", "url": f"https://video.twimg.com/{tid}_lo.mp4"},
                                            {"bit_rate": 2176000, "content_type": "video/mp4", "url": f"https://video.twimg.com/{tid}_hi.mp4"}]}
            keys.append(key)
        if keys:
            t["attachments"] = {"media_keys": keys}
        refs = []
        if quote:
            refs.append({"type": "quoted", "id": quote})
        if reply_to:
            refs.append({"type": "replied_to", "id": reply_to[0]})
            t["in_reply_to_user_id"] = self.user(reply_to[1])["id"]
        if refs:
            t["referenced_tweets"] = refs
        if entities:
            t["entities"] = entities
        if extra:
            t.update(extra)
        if front:
            self.bookmarks.insert(0, t)
        else:
            self.bookmarks.append(t)
        return t

    def remove(self, tid: str) -> None:
        self.bookmarks = [t for t in self.bookmarks if t["id"] != tid]

    def add_quoted(self, tid: str, text: str = "quoted text", author: str = "bob", photos: int = 0) -> None:
        u = self.user(author)
        t = {"id": tid, "text": text, "author_id": u["id"], "created_at": "2024-04-01T00:00:00.000Z"}
        keys = []
        for i in range(photos):
            key = f"3_{tid}_{i}"
            self.media[key] = {"media_key": key, "type": "photo", "url": f"https://pbs.twimg.com/media/{tid}x{i}.jpg"}
            keys.append(key)
        if keys:
            t["attachments"] = {"media_keys": keys}
        self.quoted[tid] = t

    # -- transport -----------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport(), follow_redirects=True)

    def _handle(self, req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        self.calls.append((req.method, f"{host}{path}"))
        if host.endswith("twimg.com"):
            return self._media(path, req)
        if path == "/2/oauth2/token":
            return self._token(req)
        if path == "/2/users/me":
            return httpx.Response(200, json={"data": {"id": "42", "username": "me", "name": "Me"}})
        if path.startswith("/2/users/") and path.endswith("/bookmarks"):
            return self._bookmarks(req)
        return httpx.Response(404, json={"title": "not found"})

    def _token(self, req: httpx.Request) -> httpx.Response:
        if self.token_error:
            status, body = self.token_error
            return httpx.Response(status, json=body)
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        if form.get("grant_type") == "refresh_token":
            if form.get("refresh_token") != self.refresh_token:
                return httpx.Response(400, json={"error": "invalid_grant"})
        self._n += 1
        self.access_token, self.refresh_token = f"access-{self._n}", f"refresh-{self._n}"
        return httpx.Response(200, json={"access_token": self.access_token, "refresh_token": self.refresh_token,
                                         "expires_in": 7200, "scope": "tweet.read users.read bookmark.read offline.access"})

    def _bookmarks(self, req: httpx.Request) -> httpx.Response:
        if req.headers.get("authorization") != f"Bearer {self.access_token}":
            return httpx.Response(401, json={"title": "Unauthorized"})
        if self.bookmark_failures:
            return self.bookmark_failures.pop(0)
        q = req.url.params
        size = int(q.get("max_results", 100))
        start = int(q.get("pagination_token") or 0)
        chunk = self.bookmarks[start:start + size]
        end = start + len(chunk)
        user_handles = {t["author_id"] for t in chunk}
        inc_tweets, media_keys = [], set()
        for t in chunk:
            media_keys.update((t.get("attachments") or {}).get("media_keys", []))
            for r in t.get("referenced_tweets", []):
                q_t = self.quoted.get(r["id"])
                if q_t:
                    inc_tweets.append(q_t)
                    user_handles.add(q_t["author_id"])
                    media_keys.update((q_t.get("attachments") or {}).get("media_keys", []))
            if t.get("in_reply_to_user_id"):
                user_handles.add(t["in_reply_to_user_id"])
        by_id = {u["id"]: u for u in self.users.values()}
        body: dict = {"data": chunk, "meta": {"result_count": len(chunk)}}
        if end < len(self.bookmarks):
            body["meta"]["next_token"] = str(end)
        includes: dict = {"users": [by_id[i] for i in sorted(user_handles)]}
        if inc_tweets:
            includes["tweets"] = inc_tweets
        if media_keys:
            includes["media"] = [self.media[k] for k in sorted(media_keys)]
        body["includes"] = includes
        return httpx.Response(200, content=json.dumps(body), headers={"content-type": "application/json"})

    def _media(self, path: str, req: httpx.Request) -> httpx.Response:
        if path in self.media_status:
            return httpx.Response(self.media_status[path])
        ctype = "video/mp4" if path.endswith(".mp4") else "image/jpeg"
        return httpx.Response(200, content=b"BYTES:" + path.encode(), headers={"content-type": ctype})
