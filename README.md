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

- **Search** across post text, authors, and quoted posts
- **Tag filters** (multi-select) derived from hashtags, linked domains, and media
- **Sort** by date, likes, reposts, or author
- **Multi-select delete** (X-style edit mode) — hides posts locally, never
  touches your data; one-click restore
- **Sync button** that re-crawls your X bookmarks via `ft`
- **"Deleted on X" tag** on bookmarks that have disappeared from your X
  account since the last full sync

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

That's it for read-only browsing. For the in-app **Sync** button, run the
tiny local server instead (or double-click `okiedoke.command`):

```sh
python3 serve.py   # serves http://localhost:8377 and opens your browser
```

## How it works

- `generate.py` reads `~/.ft-bookmarks/bookmarks.db` and
  `media-manifest.json`, embeds all bookmark data as JSON into
  `template.html`, and writes the result to `~/.ft-bookmarks/index.html` —
  next to the `media/` folder, so everything loads via relative paths
  (Safari blocks symlinked/cross-directory `file://` loads).
- `serve.py` serves that directory and exposes `POST /sync`, which runs
  `ft sync --rebuild --yes`, records the crawl window, and regenerates.
  Bookmarks whose `synced_at` predates the last full crawl are tagged
  **Deleted on X**.
- Deleting posts in the UI only writes their IDs to `localStorage` —
  nothing is ever removed from the ft database.

Data lives wherever ft puts it (`~/.ft-bookmarks` by default); override with
the `OKIEDOKE_DATA` environment variable.

## Files

| File | Purpose |
|---|---|
| `template.html` | The whole app: HTML, CSS, and JS with a `/*__BOOKMARKS_DATA__*/` placeholder |
| `generate.py` | Build script: DB + media manifest → `index.html` |
| `serve.py` | Optional local server with the `/sync` endpoint |
| `okiedoke.command` | Double-clickable launcher for `serve.py` (macOS) |
