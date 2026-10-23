"""Kamaji launch-agent entry point; loads private runtime settings, then starts FastAPI."""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path


def load_env_file(path: Path) -> None:
    """Load KEY=value lines without replacing values already supplied by launchd."""
    if not path.is_file():
        raise RuntimeError(f"required environment file is missing: {path}")
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            raise RuntimeError(f"invalid environment line {number} in {path}")
        value = value.strip()
        if value.startswith(("'", '"')):
            parsed = shlex.split(value, comments=False, posix=True)
            if len(parsed) != 1:
                raise RuntimeError(f"invalid quoted value on line {number} in {path}")
            value = parsed[0]
        os.environ.setdefault(key.strip(), value)


def main() -> None:
    db_env = os.environ.get("KAMAJI_DB_ENV_FILE", "")
    secret_env = os.environ.get("OKIEDOKE_SECRETS_FILE", "")
    if not db_env or not secret_env:
        raise RuntimeError("KAMAJI_DB_ENV_FILE and OKIEDOKE_SECRETS_FILE must be set")
    load_env_file(Path(db_env))
    load_env_file(Path(secret_env))
    # Migrations run in `kamaji deploy` under the database owner, never as the app role.
    os.environ["AUTO_MIGRATE"] = "false"
    os.environ.setdefault("MEDIA_DIR", str(Path.home() / "Developer/personal/kamaji-data/okiedoke/media"))
    Path(os.environ["MEDIA_DIR"]).mkdir(parents=True, exist_ok=True)
    from uvicorn import run
    run("app.main:create_app", factory=True, host="127.0.0.1", port=int(os.environ.get("PORT", "8379")))


if __name__ == "__main__":
    main()
