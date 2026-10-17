#!/usr/bin/env bash
# Back up okiedoke: a Postgres dump plus the media volume. Run from the repo directory
# (cron-friendly):   0 3 * * *  cd /path/to/okiedoke && deploy/backup.sh /var/backups/okiedoke
# Bundled Postgres only; with an external Postgres, pg_dump it with your usual job and keep
# using this script for the media volume (set SKIP_DB=1).
set -euo pipefail

dest="${1:?usage: backup.sh DEST_DIR [KEEP_DAYS]}"
keep="${2:-14}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$dest"

if [ -z "${SKIP_DB:-}" ]; then
  docker compose exec -T db pg_dump -U okiedoke --no-owner okiedoke | gzip > "$dest/okiedoke-db-$stamp.sql.gz"
fi
docker compose exec -T app tar -C /data/media -cf - . | gzip > "$dest/okiedoke-media-$stamp.tar.gz"

find "$dest" -name 'okiedoke-*' -mtime +"$keep" -delete
echo "backup written to $dest ($stamp)"
