# okiedoke — agent guide

Local X.com-style viewer for Twitter/X bookmarks synced by the
[Field Theory `ft` CLI](https://fieldtheory.dev/cli). Zero dependencies
beyond Python 3 stdlib; no build system, no framework, no tests.

## Architecture (read this first)

The app is ONE static HTML page with all data embedded:

1. `ft sync` (external tool) crawls X bookmarks into `~/.ft-bookmarks/`:
   `bookmarks.db` (sqlite), `bookmarks.jsonl`, `media-manifest.json`, and a
   `media/` folder with every image/video/avatar already downloaded.
2. `generate.py` reads the DB + manifest and substitutes a JSON array for the
   `/*__BOOKMARKS_DATA__*/[]` placeholder in `template.html`, writing
   `~/.ft-bookmarks/index.html`. `./index.html` here is a symlink to it.
3. `serve.py` (optional) serves that dir on localhost:8377 with a `POST /sync`
   endpoint that runs `ft sync --rebuild --yes` and regenerates.

**Never edit `index.html`** — edit `template.html`, then run
`python3 generate.py`.

## Hard-won constraints (do not regress these)

- **Output must live in `~/.ft-bookmarks/`, media referenced by RELATIVE
  paths.** Safari refuses `file://` subresources outside the page's real
  directory tree and resolves symlinks before checking — an app-dir
  `media` symlink does NOT work.
- **The media manifest is keyed by `tweetId`, not `bookmarkId`** — quoted
  tweets' media appears under the *quoted* tweet's ID (`quoted_status_id`
  column). Avatars are the entries with `profile_images` in `sourceUrl`,
  keyed by `authorHandle`.
- **"Deleted on X" detection**: `synced_at` updates on every row a crawl
  sees. `serve.py` records the start time of a successful full re-crawl in
  `last-full-sync.json`; `generate.py` tags rows with older `synced_at`.
  It refuses to write the marker if <50% of rows were touched (partial
  crawl guard). Don't infer deletion any other way — the DB has no flag.
- **User tags/edits are persisted to one file, `~/.ft-bookmarks/okiedoke-tags.json`**
  (see the schema below) — the single source of truth, browser- and URL-independent.
  Nothing may ever mutate the ft database — it's the user's *bookmark* source of
  truth. The DB is opened read-only everywhere; writes are limited to the
  generated `index.html`, `okiedoke-tags.json` (via `serve.py`'s `POST /tags`),
  and the `last-sync.json` / `last-full-sync.json` metadata.
- **Auto-tagging is OFF** (`AUTO_DERIVE_TAGS = False` in `generate.py`): posts
  arrive untagged so nothing the tool derives can fight the user's manual tags.
  `globalRemovedTags` is now largely vestigial (it existed to bulk-hide derived
  tags); `healSuppression()` guarantees an *applied* tag is never also in it.
- 32 bookmarks are replies; ft stores NO data about the parent tweet.
  `generate.py` reads optional `reply-parents.json`
  (`{parent_id: {handle, text}}`) if someone fetches those; the UI renders a
  "Replying to @handle" line when present.

## Local data & backwards compatibility (this app is used daily — do not break it)

The user's edits live in **`~/.ft-bookmarks/okiedoke-tags.json`** (one file, all
browsers/URLs share it). `localStorage` is now only an **offline mirror + a
migration source** — on load the app adopts the server file when it has data,
and seeds the file from `localStorage` only when the file is empty (see the
reconcile block at the bottom of `template.html`). Treat these keys as a stable
on-disk shape — **renaming, removing, or changing the shape of an existing key
silently discards real user data.**

| Key | Shape | Meaning |
|---|---|---|
| `hiddenBookmarks` | `string[]` | ids of locally-deleted bookmarks |
| `customTags` | `{ [id]: string[] }` | user-added tags per bookmark |
| `removedTags` | `{ [id]: string[] }` | tags removed from a single post |
| `globalRemovedTags` | `string[]` (lowercased) | tags deleted from every post (vestigial; see healSuppression) |
| `savedFilters` | `{name, expr}[]` | named boolean filter expressions |
| `filterExpr` | `string` (raw) | active filter — a per-browser VIEW pref, deliberately NOT in the shared file |
| `tagLastUsed` | `{ [tagLower]: epochMs }` | for most-recently-used ordering |

`okiedoke-tags.json` holds the same keys (minus `filterExpr`) under one JSON
object with a `version` field; `serve.py`'s `write_tags()` coerces every key to
its expected type so a bad payload can't corrupt it.

Rules for any change:

- **All tag/edit writes go through `persistTags()`** — it POSTs the full document
  to the server (atomic write) and mirrors to `localStorage`. The `save*()`
  helpers are thin wrappers over it; never write a tag key directly.
- **Read fallbacks through `readJSON(key, fallback)` / `tagSource(...)`** — they
  return the fallback on missing/malformed/wrong-shaped values, so corrupt
  storage can never crash the app at load.
- **Add features with NEW keys**, never by repurposing an existing one; extend
  `TAGS_SHAPE` in `serve.py` and `currentTagDoc()` in `template.html` together.
- The reconcile logic **never overwrites a non-empty file**, so one browser/store
  can't clobber another — preserve that invariant.
- Clearing browser site data no longer loses tags (they're on disk); only
  deleting `okiedoke-tags.json` does.

## Dev vs daily — never test a branch against real data

The daily app (`okiedoke.command`) runs on **port 8377** against `~/.ft-bookmarks`
(your REAL bookmarks + tags). **Never run a bare `python3 generate.py` or serve a
feature branch against that dir** — it overwrites the real `index.html` and a
buggy branch can corrupt the real `okiedoke-tags.json`.

Instead, test feature branches with **`./dev-serve.sh`**: it kills any stale dev
server, runs the current worktree on **port 8378** against a sandbox dir
`~/.ft-bookmarks-dev` (real DB/media as read-only symlinks, a private COPY of your
tags), and leaves 8377 + real data untouched. Both `serve.py` and `generate.py`
honor `OKIEDOKE_DATA` and `OKIEDOKE_PORT`. Reset the sandbox with
`rm -rf ~/.ft-bookmarks-dev`.

## Verifying changes (required)

Headless Chrome against the **sandbox** build (never the real one), after every
`template.html` change:

```sh
OKIEDOKE_DATA="$HOME/.ft-bookmarks-dev" python3 generate.py   # or just run ./dev-serve.sh
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --disable-gpu --enable-logging=stderr --v=1 \
  --screenshot=/tmp/ok.png --window-size=1200,2000 \
  "file://$HOME/.ft-bookmarks-dev/index.html" 2> /tmp/ok.log
grep -iE "uncaught|CONSOLE" /tmp/ok.log   # must be empty
```

Then look at the screenshot. Two real shipped-broken bugs were caught only
this way (a JS crash that blanked the table; column overflow). To test
interactions against the live server, drive `http://localhost:8378` (the dev
server) — never 8377.

## Style

- UI mimics X.com light theme precisely: colors `#0f1419`/`#536471`/
  `#1d9bf0`, borders `#eff3f4`, system font stack, 600px column.
- Vanilla JS, event delegation on `#timeline`, no libraries.
- All user text goes through `escapeHtml()` before `innerHTML`.
