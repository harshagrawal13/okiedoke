#!/bin/zsh
# Isolated dev server for testing a feature branch WITHOUT touching your real
# app or tags.
#
#   Daily app : okiedoke.command  -> port 8377, ~/.ft-bookmarks       (REAL tags)
#   This dev  : ./dev-serve.sh     -> port 8378, ~/.ft-bookmarks-dev   (SANDBOX)
#
# It reuses your real bookmarks DB + media (read-only symlinks) so posts render,
# but writes tags to a private COPY, so nothing you do while testing can corrupt
# your real okiedoke-tags.json or overwrite your real index.html.
#
# Override with env vars:  OKIEDOKE_PORT=8379 OKIEDOKE_DATA=~/somewhere ./dev-serve.sh
# Reset the sandbox (fresh copy of your real tags):  rm -rf ~/.ft-bookmarks-dev
set -e
cd "$(dirname "$0")"                              # the worktree this script lives in

PORT="${OKIEDOKE_PORT:-8378}"
REAL="${HOME}/.ft-bookmarks"
DEV="${OKIEDOKE_DATA:-${HOME}/.ft-bookmarks-dev}"

if [ ! -f "$REAL/bookmarks.db" ]; then
  echo "No real data in $REAL — run ./setup.sh once first." >&2
  exit 1
fi

# 1. Free the dev PORT only (leave the daily 8377 server running).
echo "• freeing port $PORT (killing any stale dev server there)…"
lsof -ti "tcp:${PORT}" 2>/dev/null | xargs kill 2>/dev/null || true
sleep 1

# 2. Build the sandbox data dir: real DB/media as read-only symlinks, tags as a
#    private writable copy (only created if absent, so your test edits persist
#    across restarts — delete the dir to reset).
echo "• sandbox data dir: $DEV"
mkdir -p "$DEV"
for f in bookmarks.db media-manifest.json media last-full-sync.json last-sync.json .last-version; do
  [ -e "$REAL/$f" ] && ln -sfn "$REAL/$f" "$DEV/$f"
done
if [ ! -f "$DEV/okiedoke-tags.json" ] && [ -f "$REAL/okiedoke-tags.json" ]; then
  cp "$REAL/okiedoke-tags.json" "$DEV/okiedoke-tags.json"
  echo "  copied your real tags into the sandbox (edits here won't touch the real file)"
fi

# 3. Build + serve from the sandbox on the dev port. serve.py opens the browser.
echo "• building + serving this branch on http://localhost:${PORT}  (Ctrl-C to stop)"
OKIEDOKE_DATA="$DEV" python3 generate.py
exec env OKIEDOKE_DATA="$DEV" OKIEDOKE_PORT="$PORT" python3 serve.py
