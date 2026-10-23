"""Downloading media/avatars to MEDIA_DIR, and safe lookup of stored files."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import Settings

MAX_BYTES = 300 * 1024 * 1024
_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "video/mp4": ".mp4"}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class MediaError(Exception):
    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


class MediaStore:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.dir = settings.media_dir
        self.hosts = settings.media_hosts
        self._http = http
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path | None:
        """Absolute path for a stored file, or None for anything that isn't a plain filename."""
        if not name or not _SAFE_NAME.match(name) or name.startswith("."):
            return None
        p = self.dir / name
        return p if p.is_file() else None

    def _allowed(self, url: str) -> bool:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in self.hosts)

    def download(self, url: str, stem: str) -> str:
        """Fetch `url` into MEDIA_DIR as '<stem><ext>' and return the filename.
        Deterministic names make re-downloads idempotent; the file appears
        atomically (temp + rename) so a crash never leaves a half-written file
        that looks complete."""
        if not self._allowed(url):
            raise MediaError(f"host not allowed: {urlparse(url).hostname}", permanent=True)
        http = self._http or httpx.Client(timeout=60, follow_redirects=True)
        tmp: Path | None = None
        try:
            with http.stream("GET", url, timeout=60) as resp:
                if resp.status_code in (403, 404, 410):
                    raise MediaError(f"HTTP {resp.status_code}", permanent=True)
                if resp.status_code != 200:
                    raise MediaError(f"HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                ext = _TYPES.get(ctype) or os.path.splitext(urlparse(url).path)[1].lower() or ".bin"
                if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
                    ext = ".bin"
                name = f"{stem}{ext}"
                tmp = self.dir / f".{name}.part"
                size = 0
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_bytes(64 * 1024):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise MediaError("file too large", permanent=True)
                        f.write(chunk)
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and size != int(declared) and not resp.headers.get("content-encoding"):
                    raise MediaError(f"truncated download ({size}/{declared} bytes)")
                if size == 0:
                    raise MediaError("empty response")
                os.replace(tmp, self.dir / name)
                tmp = None
                return name
        except httpx.HTTPError as e:
            raise MediaError(f"network error: {e}") from e
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)


def stem_for(tweet_id: str, url: str) -> str:
    return f"{tweet_id}_{hashlib.sha1(url.encode()).hexdigest()[:10]}"


def avatar_stem(handle: str, url: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "", handle) or "user"
    return f"avatar_{safe}_{hashlib.sha1(url.encode()).hexdigest()[:8]}"
