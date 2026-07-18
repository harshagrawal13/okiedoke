#!/usr/bin/env bash
#
# okiedoke — one-step setup.
# Checks prerequisites, runs your first bookmark sync if needed, builds the
# app, and launches it in your browser. Safe to re-run any time.
#
set -euo pipefail
cd "$(dirname "$0")"

DATA_DIR="${OKIEDOKE_DATA:-$HOME/.ft-bookmarks}"

say()  { printf '\033[1;36m▸ %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$1" >&2; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

# 1. Python 3 --------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  warn "Python 3 is required but wasn't found."
  echo  "  macOS:  brew install python3     (or https://www.python.org/downloads/)"
  echo  "  Linux:  use your package manager (e.g. sudo apt install python3)"
  die   "Install Python 3, then re-run ./setup.sh"
fi

# 2. ft CLI (does the actual syncing from X) -------------------------------
if ! command -v ft >/dev/null 2>&1; then
  warn "The Field Theory CLI ('ft') is required — it syncs your X bookmarks and media."
  echo  "  Install and sign in:  https://fieldtheory.dev/cli"
  die   "Set up ft (make sure 'ft sync' works), then re-run ./setup.sh"
fi

# 3. First sync, only if we have no data yet -------------------------------
if [ ! -f "$DATA_DIR/bookmarks.db" ]; then
  say "No bookmarks yet — running your first sync."
  echo "  (This downloads every bookmark's images/videos, so it can take a few minutes.)"
  ft sync
else
  say "Found existing bookmarks in $DATA_DIR (the app will refresh them on open)."
fi

# 4. Build index.html ------------------------------------------------------
say "Building the app…"
python3 generate.py

# 5. Launch (opens your browser; auto-syncs on open) -----------------------
say "Launching okiedoke → http://localhost:8377   (Ctrl-C to stop)"
exec python3 serve.py
