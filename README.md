# okiedoke

A local, X.com-style viewer for your Twitter/X bookmarks. Single static HTML
page, no framework, no cloud — your bookmarks, media, and avatars all render
from files on your own disk.

Built on top of [Field Theory (`ft`)](https://fieldtheory.dev/cli), which
handles the actual syncing of bookmarks from X into a local SQLite database
and downloads all media.

## What it looks like

A faithful X timeline: avatars, media grids, quoted-tweet cards, like/repost/
bookmark counts, "Show more" clamping for long posts — plus things X doesn't
give you:

- **Search** across post text, authors, tags, and quoted posts
- **Tag filters** (multi-select) derived from hashtags, linked domains, and media
- **Custom tags** — a `+` pill on every post to tag it on the go; filterable
  and searchable like any derived tag, removable via `×`
- **Sort** by date, likes, reposts, or author
- **Multi-select delete** (X-style edit mode) with **Select all** over the
  current filter — hides posts locally, never touches your data; one-click
  restore
- **Auto-sync on open** with a circular progress ring; incremental by
  default, so bookmarks you already have are skipped
- **"Deleted on X" tag** on bookmarks that have disappeared from your X
  account since the last full re-crawl

## Requirements

- macOS or Linux with Python 3.9+
- [`ft`](https://fieldtheory.dev/cli) installed and authenticated
  (`ft sync` must work)

## Setup

```sh
# 1. Sync your bookmarks (first run takes a while — it downloads all media)
ft sync

# 2. Generate the app
python3 generate.py

# 3. Open it
open ~/.ft-bookmarks/index.html
```

That's it for read-only browsing. For syncing from inside the app (auto-sync
on open + the Sync button), run the tiny local server instead — or
double-click `okiedoke.command`:

```sh
python3 serve.py   # serves http://localhost:8377 and opens your browser
```

## Syncing

When served, the app syncs automatically on open (skipped if the last sync
finished under 10 minutes ago). Syncs are **incremental**: `ft` stops
crawling as soon as it reaches bookmarks already in the database, so a
no-news sync takes seconds.

A **full re-crawl** of every bookmark — the only way to detect bookmarks
that vanished from X — runs automatically once the previous one is more than
24 hours old, or on demand with **Shift-click** on the Sync button. During a
full crawl the button shows a progress ring with a live percentage.

## How it works

- `generate.py` reads `~/.ft-bookmarks/bookmarks.db` and
  `media-manifest.json`, embeds all bookmark data as JSON into
  `template.html`, and writes the result to `~/.ft-bookmarks/index.html` —
  next to the `media/` folder, so everything loads via relative paths
  (Safari blocks symlinked/cross-directory `file://` loads).
- `serve.py` serves that directory and exposes `POST /sync`
  (`?mode=full` forces a re-crawl, `?force=1` bypasses the cooldown) plus
  `GET /sync-status` for progress polling. After a successful full crawl it
  records the crawl window; bookmarks whose `synced_at` predates it are
  tagged **Deleted on X** at the next regeneration.
- Deletions and custom tags live in the browser's `localStorage`
  (`hiddenBookmarks`, `customTags`) — nothing is ever removed from the ft
  database, and clearing site data resets them.

Data lives wherever ft puts it (`~/.ft-bookmarks` by default); override with
the `OKIEDOKE_DATA` environment variable.

## Files

| File | Purpose |
|---|---|
| `template.html` | The whole app: HTML, CSS, and JS with a `/*__BOOKMARKS_DATA__*/` placeholder |
| `generate.py` | Build script: DB + media manifest → `index.html` |
| `serve.py` | Optional local server with the `/sync` endpoint |
| `okiedoke.command` | Double-clickable launcher for `serve.py` (macOS) |
| `CLAUDE.md` / `AGENTS.md` | Onboarding notes for coding agents (symlinked) |
