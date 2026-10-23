"""Runtime configuration, read once from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _num(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


@dataclass(frozen=True)
class Settings:
    database_url: str
    media_dir: Path = Path("data/media")
    app_env: str = "prod"
    public_url: str = "http://localhost:8000"
    auto_migrate: bool = True     # Kamaji applies db/migrations during deploy as the database owner

    # --- X API (official). Without a client id the app runs read-only: it can
    # browse imported bookmarks but cannot sync. ---
    x_client_id: str = ""
    x_client_secret: str = ""          # empty = public client (PKCE only)
    x_api_base: str = "https://api.x.com"
    x_auth_url: str = "https://x.com/i/oauth2/authorize"
    x_scopes: str = "tweet.read users.read bookmark.read offline.access"
    secret_key: str = ""               # encrypts X tokens at rest

    # --- sync behaviour ---
    sync_enabled: bool = True
    sync_interval_min: float = 60.0    # background incremental sync cadence
    sync_cooldown_min: float = 10.0    # page-open auto-sync is skipped inside this window
    full_sweep_days: float = 7.0       # periodic full sweep (deleted-on-X detection); 0 = never
    incremental_page_size: int = 25    # first page of an incremental sync (X bills per post read)
    sweep_page_size: int = 100
    bookmark_cap: int = 800            # X only exposes the most recent N bookmarks
    media_hosts: tuple[str, ...] = ("twimg.com",)
    max_media_per_run: int = 300

    @property
    def x_redirect_uri(self) -> str:
        return self.public_url.rstrip("/") + "/auth/x/callback"

    @property
    def x_configured(self) -> bool:
        return bool(self.x_client_id and self.secret_key)

    @classmethod
    def from_env(cls) -> "Settings":
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError("DATABASE_URL is required (e.g. postgresql://user:pass@host:5432/okiedoke)")
        d = cls.__dataclass_fields__
        return cls(
            database_url=url,
            media_dir=Path(os.environ.get("MEDIA_DIR", "data/media")),
            app_env=os.environ.get("APP_ENV", "prod"),
            public_url=os.environ.get("PUBLIC_URL", "http://localhost:8000"),
            auto_migrate=_flag("AUTO_MIGRATE", True),
            x_client_id=os.environ.get("X_CLIENT_ID", "").strip(),
            x_client_secret=os.environ.get("X_CLIENT_SECRET", "").strip(),
            x_api_base=os.environ.get("X_API_BASE", d["x_api_base"].default).rstrip("/"),
            x_auth_url=os.environ.get("X_AUTH_URL", d["x_auth_url"].default),
            x_scopes=os.environ.get("X_SCOPES", d["x_scopes"].default),
            secret_key=os.environ.get("SECRET_KEY", "").strip(),
            sync_enabled=_flag("SYNC_ENABLED", True),
            sync_interval_min=_num("SYNC_INTERVAL_MIN", 60.0),
            sync_cooldown_min=_num("SYNC_COOLDOWN_MIN", 10.0),
            full_sweep_days=_num("FULL_SWEEP_DAYS", 7.0),
            incremental_page_size=int(_num("INCREMENTAL_PAGE_SIZE", 25)),
            sweep_page_size=int(_num("SWEEP_PAGE_SIZE", 100)),
            bookmark_cap=int(_num("BOOKMARK_CAP", 800)),
            max_media_per_run=int(_num("MAX_MEDIA_PER_RUN", 300)),
        )
