# okiedoke

A local, X.com-style viewer for your Twitter/X bookmarks. One static HTML
page, no framework, no cloud, no build tooling — your bookmarks, media, and
avatars all render from files on your own disk.

Syncing is handled by [Field Theory (`ft`)](https://fieldtheory.dev/cli),
which crawls your bookmarks from X into a local SQLite database and downloads
every image and video.

## Quick start

You need two things first:

1. **Python 3.9+** — already on most Macs; on Linux install via your package manager.
2. **The `ft` CLI**, installed and signed in — [get it here](https://fieldtheory.dev/cli).
   Make sure `ft sync` works once.

Then, from this folder:

```sh
./setup.sh
```

That's the whole thing. `setup.sh` checks your prerequisites, runs your first
bookmark sync if you don't have one yet, builds the app, and opens it in your
browser at **http://localhost:8377**. Re-run it any time — it only syncs when
needed.

> On macOS you can also just **double-click `okiedoke.command`**.

Prefer to do it by hand, or only want a static file to open? See
[Manual setup](#manual-setup) below.

## What you can do

A faithful X timeline — avatars, media grids, quoted-tweet cards,
like/repost/bookmark counts, "Show more" clamping — plus the things X doesn't
give you:

- **Search** across post text, authors, tags, and quoted posts.
- **Boolean tag filters** — combine tags with `and` / `or` / `not` and
  parentheses (e.g. `media and not archive`, or `not (github.com or arxiv.org)`).
  Click **+** / **−** on any tag to include/exclude it without typing.
- **Saved filters** — name a filter expression and re-apply it in one click.
- **"Untagged only"** quick filter to find bookmarks with no tags — then
  **Select → Select all** to act on them in bulk.
- **Tag anything** — a `+` on every post opens an autocomplete of your existing
  tags (most-recently-used first) or lets you type a new one. Remove a tag from
  one post with its `×`.
- **Manage tags** (button in the toolbar) — a dedicated window to **delete a
  tag from every post at once**, or **restore** it later, split into Active /
  Deleted tabs with search.
- **Sort** by date, likes, reposts, or author.
- **Multi-select delete** (X-style edit mode) with **Select all** over the
  current filter. Deletes hide posts locally and are one-click restorable.
- **Auto-sync on open** with a live progress ring; incremental, so bookmarks
  you already have are skipped.
- **"Deleted on X"** tag on bookmarks that have vanished from your X account
  since the last full re-crawl.

Everything you do here — tags, deletions, saved filters — lives only in your
browser. **Your ft database is never modified.**

## Syncing

When served, the app syncs automatically on open (skipped if the last sync
finished under 10 minutes ago). Syncs are **incremental**: `ft` stops crawling
as soon as it reaches bookmarks already in the database, so a no-news sync
takes seconds. You can also hit the **Sync** button any time.

A **full re-crawl** of every bookmark — the only way to detect bookmarks that
vanished from X — runs on demand via **Shift-click** on the Sync button. During
a full crawl the button shows a progress ring with a live percentage.

## Manual setup

If you'd rather not use `setup.sh`:

```sh
ft sync                 # 1. sync bookmarks (first run downloads all media)
python3 generate.py     # 2. build ~/.ft-bookmarks/index.html
python3 serve.py        # 3. serve http://localhost:8377 and open the browser
```

For **read-only browsing with no server** (no in-app sync button), skip step 3
and just `open ~/.ft-bookmarks/index.html`.

## How it works

- `generate.py` reads `~/.ft-bookmarks/bookmarks.db` and `media-manifest.json`,
  embeds all bookmark data as JSON into `template.html`, and writes the result
  to `~/.ft-bookmarks/index.html` — right next to the `media/` folder, so
  everything loads via relative paths (Safari blocks symlinked/cross-directory
  `file://` loads).
- `serve.py` serves that directory and exposes `POST /sync` (`?mode=full`
  forces a re-crawl, `?force=1` bypasses the cooldown) plus `GET /sync-status`
  for progress polling. On first run it builds `index.html` for you.
- Everything you change in the UI lives in the browser's `localStorage`
  (`hiddenBookmarks`, `customTags`, `removedTags`, `globalRemovedTags`,
  `savedFilters`, `filterExpr`, `tagLastUsed`). Nothing is ever removed from the
  ft database; clearing your browser's site data resets it all.

Data lives wherever ft puts it (`~/.ft-bookmarks` by default); override with the
`OKIEDOKE_DATA` environment variable.

## Files

| File | Purpose |
|---|---|
| `setup.sh` | One-step onboarding: check prereqs → first sync → build → launch |
| `template.html` | The whole app: HTML, CSS, and JS with a `/*__BOOKMARKS_DATA__*/` placeholder |
| `generate.py` | Build script: DB + media manifest → `index.html` |
| `serve.py` | Local server with the `/sync` endpoint (self-builds on first run) |
| `okiedoke.command` | Double-clickable launcher that runs `setup.sh` (macOS) |
| `CLAUDE.md` / `AGENTS.md` | Onboarding notes for coding agents (symlinked) |
