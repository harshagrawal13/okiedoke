# okiedoke

A self-hosted, X.com-style viewer for your X bookmarks: search, boolean tag filters, saved
filters, bulk select, and a one-card-at-a-time tagging deck. Runs as one container plus
Postgres on your own server, and works in a phone browser.

```
X API (official, OAuth 2.0) ──► sync engine ──► Postgres   bookmarks, tags, filters, sync log
                                    └─────────► media volume   images, video, avatars
phone / laptop ◄──► FastAPI (this app) ◄──────► same data
```

Bookmarks, tags, hidden posts and filters all live on the server. The browser keeps no
copy beyond a small queue of edits that have not been delivered yet.

## Deploy (Docker + Cloudflare Tunnel)

```sh
git clone https://github.com/harshagrawal13/okiedoke && cd okiedoke
cp .env.example .env            # fill in the blanks (see below)
docker compose up -d --build    # app on 127.0.0.1:8379, Postgres in a volume
```

1. **Tunnel.** Add the hostname to your `cloudflared` config and route DNS:
   ```yaml
   ingress:
     - hostname: bookmarks.harsh-agrawal.com
       service: http://localhost:8379      # or http://app:8000 if cloudflared shares the compose network
   ```
   `cloudflared tunnel route dns <tunnel> bookmarks.harsh-agrawal.com`
2. **Protect it before opening it.** okiedoke has no login of its own. Put the hostname behind
   the same Cloudflare Access policy as your other apps. The compose file publishes the app on
   `127.0.0.1` only, so the tunnel is the only way in.
3. **X developer app.** In the X developer console create an app with *User authentication*:
   OAuth 2.0, permission **Read**, callback URL `https://bookmarks.harsh-agrawal.com/auth/x/callback`
   (must equal `PUBLIC_URL` + `/auth/x/callback`). Put the client id/secret in `.env`, plus
   `SECRET_KEY=$(openssl rand -base64 32)`, which encrypts the stored X tokens.
4. Open the site, tap **Connect X**, authorize. The first sync is a full sweep.

## Deploy on Kamaji

Kamaji runs OkieDoke as a native launch agent. The app gets its own database and roles
inside Kamaji's shared Postgres cluster; it does not need a Postgres container or a
PostgREST service. Its FastAPI process listens on `127.0.0.1:8379` behind Kamaji's
existing Cloudflare Tunnel and a dedicated Cloudflare Access application.

After the OkieDoke and kamaji-server changes are merged and the Access application is
created for `okiedoke.harsh-agrawal.com`:

1. On Kamaji, run `bin/kamaji add-app okiedoke` and review the dry-run plan, then run it
   with `--yes`. This creates an isolated `okiedoke` database and roles. The direct DB
   connection URL is written with mode `0600` to `~/.config/kamaji/okiedoke.env`; it
   connects as `okiedoke_anon` through the app's restricted authenticator role.
2. Create `~/.config/okiedoke/secrets.env` with `X_CLIENT_ID`, `X_CLIENT_SECRET`, and
   `SECRET_KEY`. Keep it outside the checkout and set its mode to `0600`. Generate the
   key once with `openssl rand -base64 32`; do not rotate it unless you intend to
   reconnect X, because it encrypts the saved OAuth tokens.
3. From the checkout, run `deploy/setup-kamaji.sh` once to create the virtualenv.
4. Run `bin/kamaji deploy okiedoke` and review the plan, then run it with `--yes`.
   Kamaji applies `db/migrations/` before starting/restarting the app agent.

The app stores downloaded media in
`~/Developer/personal/kamaji-data/okiedoke/media`, outside the checkout. Kamaji's
nightly backup job mirrors that directory to Google Drive. X's OAuth callback is
`https://okiedoke.harsh-agrawal.com/auth/x/callback`.

The Cloudflare Access AUD in `kamaji-server/apps.toml` is intentionally marked pending
until that dedicated Access application exists. Do not install the tunnel config until
the real AUD has replaced the placeholder.

Already have a Postgres on the server? Remove `COMPOSE_PROFILES` from `.env` and set
`DATABASE_URL` (the database and role must exist). Migrations run on start.

## Import your existing Field Theory data (once)

```sh
rsync -a ~/.ft-bookmarks/ kamaji:/srv/ft-import/
docker compose run --rm -v /srv/ft-import:/import:ro app python -m app.import_ft /import --dry-run
docker compose run --rm -v /srv/ft-import:/import:ro app python -m app.import_ft /import
```

Imports bookmarks, media and avatars, your tags (order and spelling), hidden posts, deleted tags,
saved filters, tag recency and the "deleted on X" flags, in one transaction. ft's database is opened
read-only. Re-running never overwrites anything already there. Your `okiedoke-tags.json` is also stored
verbatim in the `legacy_imports` table. Not imported: ft's auto-derived categories (auto-tagging was off)
and the per-browser filter text. Do this before or after connecting X; the first sweep adopts the imported
media rather than downloading it again.

## Using it

- **Phone:** open the site, then *Share → Add to Home Screen*. The header slides away as you scroll down
  and returns on scroll up; filters open as a sheet; in **Tag cards**, swipe left/right or use the buttons.
- **Sync** runs when you open the page (skipped if one ran in the last 10 minutes), hourly in the
  background, and as a full sweep about weekly. The status text in the header opens a dialog with the last
  result, errors in plain language, **Sync now**, **Full sweep** and **Reconnect X**.
- A full sweep refreshes counts and marks bookmarks that are no longer on X (red "Deleted on X" pill).
  It only flags anything when it provably saw the whole list.
- Edits (tags, hides, filters) apply instantly and are delivered in the background. Offline or signed
  out, they queue (also across reloads) and the status shows "Offline — will retry".

## Limits to know

- X exposes only your **800 most recent bookmarks** through the API. Older ones exist here only
  through the import. Because new bookmarks arrive a few at a time, syncing regularly never loses any.
- The X API bills per post read (the sources I found put owned reads at about $0.001 each; check your
  Developer Console). A quiet sync reads 25 posts; a weekly sweep reads up to 800. Tune with
  `INCREMENTAL_PAGE_SIZE`, `SYNC_INTERVAL_MIN`, `FULL_SWEEP_DAYS`.
- Endpoints and parameters follow X's published v2 API and were not exercised against live X during
  development (only against a fake of it). Watch the first sync (`docker compose logs -f app`).

## Operations

| | |
|---|---|
| Logs | `docker compose logs -f app` (every sync logs its counts; failures include the reason) |
| Health | `GET /api/health` (200 when Postgres answers); the container has a healthcheck |
| Backup | `deploy/backup.sh /var/backups/okiedoke` (Postgres dump + media), e.g. nightly from cron |
| Restore | `gunzip -c db.sql.gz \| docker compose exec -T db psql -U okiedoke okiedoke`; untar media into the volume |
| Re-sync everything | **Full sweep** in the sync dialog |
| X login expired | header shows "X login expired": tap **Reconnect X** |

## Configuration

See `.env.example`. Required: `PUBLIC_URL`, `POSTGRES_PASSWORD` (or `DATABASE_URL`). For sync:
`X_CLIENT_ID`, `SECRET_KEY` (and `X_CLIENT_SECRET` unless your X app is a public client). Without
X credentials the app still runs and browses imported data.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
export TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres   # any Postgres you can CREATE DATABASE on
pytest                      # backend, importer, API and browser tests (needs Chromium for tests/ui)
DATABASE_URL=postgresql://... uvicorn app.main:create_app --factory --reload
```

Tests create and drop their own throwaway database; they never touch `DATABASE_URL`.

| Path | Purpose |
|---|---|
| `app/xparse.py`, `x_client.py`, `auth_x.py` | X payloads, API client (retry/backoff), OAuth + rotating-token handling |
| `app/sync.py`, `store.py`, `media.py`, `scheduler.py` | sync engine, idempotent writes, downloads, schedule |
| `app/api.py`, `queries.py`, `main.py` | routes, read models, app factory |
| `app/import_ft.py` | one-time ft importer |
| `app/migrations/` | forward-only SQL migrations |
| `web/` | the UI: `index.html`, `style.css`, `app.js` (vanilla JS, no build step) |
| `tests/` | pytest suites; `tests/ui` drives headless Chromium at desktop and phone size |
