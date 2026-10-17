# okiedoke — agent guide

Self-hosted X.com-style viewer for X bookmarks. FastAPI + Postgres + a vanilla-JS single page.
Runs as one container (plus Postgres) on the owner's Kamaji server behind a Cloudflare Tunnel
(hostname `bookmarks.harsh-agrawal.com`). Used daily, on a phone as well as a laptop.
The previous generation (Field Theory `ft` crawler → SQLite → generated static HTML, run on a Mac)
is gone; `app/import_ft.py` is its one-way migration path.

## Architecture

- `app/` Python 3.11+: `main.py` (factory), `api.py` (routes), `queries.py` (read models),
  `sync.py` + `store.py` + `xparse.py` + `x_client.py` + `auth_x.py` + `media.py` (X sync),
  `scheduler.py`, `import_ft.py`, `db.py` (pool + migrations), `migrations/NNN_*.sql`.
- `web/` the UI (`index.html`, `style.css`, `app.js`). No framework, no build step, no libraries.
- Postgres holds everything: `tweets`/`authors`/`media`/`bookmarks` (synced), `tags`/`bookmark_tags`/
  `saved_filters` + `bookmarks.hidden_at` (user data), `sync_runs`, `x_auth`, `legacy_imports`.
  Media files live on a volume (`MEDIA_DIR`), named `<tweetid>_<hash>.<ext>`, never rewritten.
- No auth in the app by design: Cloudflare Access gates the hostname, the container publishes to
  127.0.0.1 only. Don't add a second login; do keep the same-origin check on writes (`main.py` `guard`).

## Hard-won constraints (do not regress)

**X sync**
- X rotates the refresh token on every refresh. `XAuth.access_token` refreshes under
  `SELECT … FOR UPDATE` and stores the new pair in the same transaction. Never refresh outside it.
- One sync at a time: Postgres advisory lock `SYNC_LOCK` (also reaps rows left 'running' by a dead process).
- Each page commits on its own; a rate limit or error keeps progress and reports `partial`/`error`.
  429 waits only if the reset is within 90 s, otherwise gives up and lets the schedule retry.
- Incremental runs stop at the first page with nothing new. Only a **full** sweep may set
  `missing_since` ("Deleted on X"), and only if it reached the end of the list, saw fewer than
  `bookmark_cap - 5` bookmarks (X only exposes the latest 800), and touched ≥ 50% of known rows.
  "Seen" is `last_seen_run = this run id`, not a timestamp (clock skew between hosts). Don't infer deletion any other way.
- The first sync after an import must be a full one (`choose_kind`), and imported media (`ft:` keys) is
  *adopted* by `store.sync_media` (match by asset name, then position+kind) so nothing is re-downloaded or doubled.
- API text arrives HTML-escaped with t.co links: `xparse.expand_text` substitutes by code-point index
  first, unescapes last, and drops media/quote links. Long posts come from `note_tweet`.
- Media downloads: https + `*.twimg.com` only, atomic write, retried across runs, never fail a sync.
  Until downloaded the UI hot-links X's URL.
- The API only needs `X_CLIENT_ID` + `SECRET_KEY`; without them the app is browse-only.

**User data (this is the part that must never be lost)**
- Tags, hides and saved filters are only ever changed by granular, idempotent endpoints
  (`POST/DELETE /api/bookmarks/{id}/tags`, `/api/bookmarks/hide`, `/api/tags/delete|restore`,
  `/api/saved-filters`). No endpoint overwrites a whole document: two devices can't clobber each other.
- Deleting a tag from every post only sets `tags.deleted_at`; the links stay so Restore is lossless.
  Tagging with a deleted name revives it. First spelling of a tag wins; matching is case-insensitive
  (`name_lower`, computed in Python with `str.lower()` to agree with the JS).
- Sync code must never touch `tags`, `bookmark_tags`, `saved_filters`, `hidden_at`.
- Migrations are forward-only SQL files; never edit one that has been released. The importer is additive and
  idempotent (`ON CONFLICT DO NOTHING`), runs in one transaction, and opens ft's SQLite read-only.

**Front end**
- All user text goes through `escapeHtml()` before `innerHTML`. Event delegation on `#timeline`.
- Every edit goes through `mutate(job)`: apply locally, append to the localStorage queue, `pump()` delivers
  in order with backoff. 4xx (except 408/429) drops the job and re-syncs; network errors keep it. Jobs must stay idempotent.
  `fetch(..., {redirect: "error"})` on purpose: an expired tunnel login must look like "not delivered".
- Phone behaviour that was learned the hard way: inputs ≥ 16px on touch (iOS zooms below that); no
  auto-focus on touch (keyboard); the filter menu is moved to `<body>` as a top sheet while open (the chip row
  scrolls horizontally and `backdrop-filter` captures `position: fixed`); the deck sets `touch-action: pan-y`
  on `.deck-card` (the nearest scroller — otherwise a right-swipe becomes browser Back); the deck sizes to
  `visualViewport` (iOS keyboard); video src gets `#t=0.001` (iOS first frame); `/media` must stay byte-exact for
  Range requests (gzip is applied to `/api/` only).
- Scroll events right after the add-tag input opens are the browser scrolling it into view; don't close on them.
- Look: X light theme, `#0f1419` / `#536471` / `#1d9bf0`, borders `#eff3f4`, 600px column, system font stack.
  Desktop must stay pixel-identical to the old app below the header.

## Verifying changes (required)

```sh
export TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres    # needs CREATE DATABASE; never your real DB
pytest                       # ~90 tests: parser, sync engine vs a fake X, importer, API, real-socket e2e, browser UI
```

- `tests/fakex.py` fakes X (token endpoint, `/users/me`, bookmarks pages, twimg media) via `httpx.MockTransport`;
  `tests/test_e2e_sync.py` serves it over a real socket to exercise the default wiring.
- `tests/ui` drives headless Chromium at 1200px and as a 390px touch phone against a live server seeded
  from `tests/sample_ft.py`. Set `CHROMIUM_PATH` if needed. After any `web/` change also **look at screenshots**
  (desktop and phone): two shipped-broken bugs in the old app were only caught that way.
- Tests for X behaviour are against a fake. Anything that changes how we talk to X needs a manual first sync on
  the real server (`docker compose logs -f app`).
- Never point tests, the importer or a dev server at the production database or media volume.
