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
- **All UI edits are localStorage-only** (see the schema below). Nothing may
  ever mutate the ft database — it's the user's source of truth. The DB is
  opened read-only everywhere; the only writes anywhere are the generated
  `index.html` and the `last-sync.json` / `last-full-sync.json` metadata.
- 32 bookmarks are replies; ft stores NO data about the parent tweet.
  `generate.py` reads optional `reply-parents.json`
  (`{parent_id: {handle, text}}`) if someone fetches those; the UI renders a
  "Replying to @handle" line when present.

## Local data & backwards compatibility (this app is used daily — do not break it)

The user's edits live only in the browser's `localStorage`. Treat these keys as
a stable on-disk format — **renaming, removing, or changing the shape of an
existing key silently discards real user data.**

| Key | Shape | Meaning |
|---|---|---|
| `hiddenBookmarks` | `string[]` | ids of locally-deleted bookmarks |
| `customTags` | `{ [id]: string[] }` | user-added tags per bookmark |
| `removedTags` | `{ [id]: string[] }` | tags removed from a single post |
| `globalRemovedTags` | `string[]` (lowercased) | tags deleted from every post |
| `savedFilters` | `{name, expr}[]` | named boolean filter expressions |
| `filterExpr` | `string` (raw) | the active filter expression |
| `tagLastUsed` | `{ [tagLower]: epochMs }` | for most-recently-used ordering |

Rules for any change:

- **Read every key through `readJSON(key, fallback)`** — it returns the fallback
  on a missing, malformed, or wrong-shaped value, so corrupt storage can never
  crash the app at load. Never `JSON.parse(localStorage.getItem(...))` directly.
- **Add features with NEW keys**, never by repurposing an existing one. A new
  key absent for existing users just reads as its fallback — that's the whole
  backwards-compatibility story, so keep it that way.
- If a format ever genuinely must change, **migrate under a new key and leave
  the old one intact**; do not overwrite it in place.
- Clearing browser site data is the only thing that resets this — code must not.

## Verifying changes (required)

Headless Chrome, after every `template.html` change:

```sh
python3 generate.py
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --disable-gpu --enable-logging=stderr --v=1 \
  --screenshot=/tmp/ok.png --window-size=1200,2000 \
  "file://$HOME/.ft-bookmarks/index.html" 2> /tmp/ok.log
grep -iE "uncaught|CONSOLE" /tmp/ok.log   # must be empty
```

Then look at the screenshot. Two real shipped-broken bugs were caught only
this way (a JS crash that blanked the table; column overflow). To test
interactions, append a script that clicks things to a copy of index.html and
screenshot that.

## Style

- UI mimics X.com light theme precisely: colors `#0f1419`/`#536471`/
  `#1d9bf0`, borders `#eff3f4`, system font stack, 600px column.
- Vanilla JS, event delegation on `#timeline`, no libraries.
- All user text goes through `escapeHtml()` before `innerHTML`.
