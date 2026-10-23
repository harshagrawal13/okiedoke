// okiedoke front end. The server (Postgres) is the single source of truth: this
// file keeps no copy of your bookmarks or tags beyond what is on screen, plus a
// small queue of edits that have not reached the server yet.
let DATA = [];
const byId = new Map();
const TOUCH = window.matchMedia("(pointer: coarse)").matches;
const MOBILE = window.matchMedia("(max-width: 640px)");
const NO_TAGS = Object.freeze([]);

// localStorage holds only per-device view prefs (the filter text) and the pending
// edit queue. Reads fall back on missing/malformed/wrong-shaped values and every
// access is guarded, so private mode or corrupt storage can never crash the app.
function storeGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function storeSet(key, value) { try { localStorage.setItem(key, value); } catch {} }
function readJSON(key, fallback) {
  try {
    const v = JSON.parse(storeGet(key));
    if (v == null || Array.isArray(v) !== Array.isArray(fallback) || typeof v !== typeof fallback) return fallback;
    return v;
  } catch {
    return fallback;
  }
}
function writeJSON(key, value) { storeSet(key, JSON.stringify(value)); }

const state = {
  search: "",
  filterExpr: storeGet("filterExpr") || "",   // a per-device view pref, not shared
  filterPred: null,           // compiled predicate for filterExpr (null = match all)
  savedFilters: [],           // [{name, expr}]
  sortKey: "date",
  sortDir: "desc",
  hidden: new Set(),          // ids removed from view (restorable)
  tags: {},                   // bookmark id -> tag names, oldest first
  deletedTags: new Set(),     // lowercased names deleted from every post (restorable)
  tagLastUsed: {},            // lowercased name -> epoch ms (most-recently-used ordering)
  selecting: false,
  selected: new Set(),
  visibleIds: [],
  sync: null,
};

// --- Edits: optimistic, queued, retried ----------------------------------------
// Every change is a tiny idempotent job. It is applied to the screen at once,
// appended to a queue persisted in localStorage, and delivered in order. If the
// phone is offline, the tunnel session expired or the server is down, the job
// stays queued and is retried with backoff — and still delivered after a reload
// or a closed tab. Jobs the server permanently refuses (e.g. the bookmark no
// longer exists) are dropped and the screen is re-synced from the server.
const QUEUE_KEY = "okiedokeQueue";
let queue = readJSON(QUEUE_KEY, []);
if (!Array.isArray(queue)) queue = [];
const saveQueue = () => writeJSON(QUEUE_KEY, queue);

// The server keeps the first spelling of a tag ("Rust" stays "Rust" when you
// later type "rust"); mirror that so the UI does not flicker when the reply lands.
function labelFor(name) {
  const low = name.toLowerCase();
  for (const id in state.tags) for (const t of state.tags[id]) if (t.toLowerCase() === low) return t;
  return name;
}

function applyLocal(job) {
  switch (job.op) {
    case "addTag": {
      const low = job.name.toLowerCase();
      state.deletedTags.delete(low);           // tagging with a deleted name brings it back
      const list = state.tags[job.id] || (state.tags[job.id] = []);
      if (!list.some(t => t.toLowerCase() === low)) list.push(labelFor(job.name));
      state.tagLastUsed[low] = Date.now();
      break;
    }
    case "rmTag": {
      const low = job.name.toLowerCase();
      const list = (state.tags[job.id] || []).filter(t => t.toLowerCase() !== low);
      if (list.length) state.tags[job.id] = list; else delete state.tags[job.id];
      break;
    }
    case "hide":
      for (const id of job.ids) job.hidden ? state.hidden.add(id) : state.hidden.delete(id);
      break;
    case "restoreAll": state.hidden.clear(); break;
    case "tagDelete": state.deletedTags.add(job.name.toLowerCase()); break;
    case "tagRestore": state.deletedTags.delete(job.name.toLowerCase()); break;
    case "saveFilter": {
      const ex = state.savedFilters.find(f => f.name.toLowerCase() === job.name.toLowerCase());
      if (ex) ex.expr = job.expr; else state.savedFilters.push({ name: job.name, expr: job.expr });
      break;
    }
    case "delFilter":
      state.savedFilters = state.savedFilters.filter(f => f.name.toLowerCase() !== job.name.toLowerCase());
      break;
  }
}

function requestFor(job) {
  const enc = encodeURIComponent;
  switch (job.op) {
    case "addTag": return ["POST", `/api/bookmarks/${enc(job.id)}/tags`, { name: job.name }];
    case "rmTag": return ["DELETE", `/api/bookmarks/${enc(job.id)}/tags?name=${enc(job.name)}`, null];
    case "hide": return ["POST", "/api/bookmarks/hide", { ids: job.ids, hidden: job.hidden }];
    case "restoreAll": return ["POST", "/api/bookmarks/restore-all", null];
    case "tagDelete": return ["POST", "/api/tags/delete", { name: job.name }];
    case "tagRestore": return ["POST", "/api/tags/restore", { name: job.name }];
    case "saveFilter": return ["PUT", "/api/saved-filters", { name: job.name, expr: job.expr }];
    case "delFilter": return ["DELETE", `/api/saved-filters?name=${enc(job.name)}`, null];
  }
  return null;
}

function send(job) {
  const [method, url, body] = requestFor(job);
  return fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
    keepalive: true,             // lets the request finish even if the tab is closed right after
    cache: "no-store",
    credentials: "same-origin",
    redirect: "error",           // an expired tunnel login redirects away: treat that as "not delivered"
  });
}

function mutate(job) {
  if (job.op === "addTag") job.name = job.name.trim().slice(0, 100);
  if (job.op === "saveFilter") { job.name = job.name.trim().slice(0, 100); job.expr = job.expr.trim().slice(0, 1000); }
  const revived = job.op === "addTag" && state.deletedTags.has(job.name.toLowerCase());
  applyLocal(job);
  if (revived) refreshAllTagRows();   // the tag reappears on every post that carries it, not just this one
  queue.push(job);
  saveQueue();
  updateRestoreLink();
  pump();
}

let pumping = false, failures = 0, retryTimer = null;
async function pump() {
  if (pumping) return;
  pumping = true;
  clearTimeout(retryTimer);
  if (queue.length) setSaveStatus("saving");
  while (queue.length) {
    const job = queue[0];
    let res = null;
    try {
      res = await send(job);
    } catch {
      res = null;                // offline, signed out, server restarting
    }
    const permanent = res && res.status >= 400 && res.status < 500 && res.status !== 408 && res.status !== 429;
    if (res && (res.ok || permanent)) {
      queue.shift();
      saveQueue();
      failures = 0;
      if (res.ok && job.op === "addTag") {
        try { relabelTag(job.id, (await res.json()).name); } catch {}
      } else if (!res.ok) {
        setSaveStatus("rejected");
        refreshState();
      }
      continue;
    }
    failures++;
    setSaveStatus(failures >= 3 ? "stuck" : "retrying");
    pumping = false;
    retryTimer = setTimeout(pump, Math.min(30000, 1000 * 2 ** failures));
    return;
  }
  pumping = false;
  if (failures === 0) setSaveStatus("saved");
}

function relabelTag(id, canonical) {
  const list = state.tags[id];
  if (!list || document.querySelector(".taginput")) return;
  const i = list.findIndex(t => t.toLowerCase() === canonical.toLowerCase());
  if (i >= 0 && list[i] !== canonical) { list[i] = canonical; refreshTagRow(id); }
}

function setSaveStatus(kind) {
  const el = document.getElementById("saveStat");
  if (!el) return;
  const map = {
    saving: ["Saving…", "var(--text-2)"],
    saved: ["✓ Saved", "var(--green)"],
    retrying: ["⚠ Offline — will retry", "#c2610a"],
    stuck: ["⚠ Not saved yet — tap to reload", "#f4212e"],
    rejected: ["⚠ A change was rejected", "#f4212e"],
  };
  const [text, color] = map[kind] || ["", ""];
  el.textContent = text;
  el.style.color = color;
  el.dataset.kind = kind || "";
  clearTimeout(el._t);
  if (kind === "saved") el._t = setTimeout(() => { el.textContent = ""; el.dataset.kind = ""; }, 1500);
  if (kind === "rejected") el._t = setTimeout(() => { el.textContent = ""; el.dataset.kind = ""; }, 4000);
}

// Replace the in-memory copy of the user's data from the server, then re-apply any
// edits still waiting in the queue so they stay visible until delivered.
function adoptState(st) {
  state.tags = st.tags || {};
  state.hidden = new Set(st.hidden || []);
  state.deletedTags = new Set((st.deletedTags || []).map(t => String(t).toLowerCase()));
  state.tagLastUsed = st.tagLastUsed || {};
  state.savedFilters = st.savedFilters || [];
  if (st.sync) state.sync = st.sync;
  for (const job of queue) applyLocal(job);
}

function rerenderAll() {
  buildTagOptions();
  renderSavedFilters();
  updateRestoreLink();
  refreshAllTagRows();
  applyState();
  renderSyncUI();
}

// Pull fresh state (another device may have tagged things). Never while you are
// mid-edit: the redraw would replace the input under your fingers.
async function refreshState() {
  if (deck.open || document.querySelector(".taginput") || document.getElementById("tagDropdown").open) return;
  try {
    const res = await fetch("/api/state", { cache: "no-store", redirect: "error" });
    if (!res.ok) return;
    adoptState(await res.json());
    rerenderAll();
  } catch {}
}

let hiddenAt = 0;
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") { hiddenAt = Date.now(); return; }
  if (hiddenAt && Date.now() - hiddenAt > 20000) { refreshState(); if (queue.length) pump(); }
});
window.addEventListener("online", () => { failures = 0; pump(); });
document.getElementById("saveStat").addEventListener("click", e => {
  if (e.currentTarget.dataset.kind === "stuck") location.reload();
});

// A bookmark's visible tags: those applied to it, minus any deleted from every post.
function tagsOf(b) {
  const all = state.tags[b.id];
  if (!all || !all.length) return NO_TAGS;
  if (!state.deletedTags.size) return all;
  return all.filter(t => !state.deletedTags.has(t.toLowerCase()));
}

function updateRestoreLink() {
  const el = document.getElementById("restoreLink");
  el.hidden = state.hidden.size === 0;
  el.textContent = `Restore ${state.hidden.size} deleted`;
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// Runs on already-escaped text: URLs, @mentions, #hashtags become links.
function linkify(escaped) {
  return escaped
    .replace(/(https?:\/\/[^\s&]+(?:&amp;[^\s&]+)*)/g,
      '<a href="$1" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[^\w/])@(\w{1,15})\b/g,
      '$1<a href="https://x.com/$2" target="_blank" rel="noopener">@$2</a>')
    .replace(/(^|\s)#(\w+)/g,
      '$1<a href="https://x.com/hashtag/$2" target="_blank" rel="noopener">#$2</a>');
}

function fmtCount(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "K";
  return String(n);
}

function hueOf(s) {
  let hash = 0;
  const key = s.toLowerCase();
  for (let i = 0; i < key.length; i++) hash = (hash * 31 + key.charCodeAt(i)) | 0;
  return Math.abs(hash) % 360;
}

function pillStyle(tag) {
  const h = hueOf(tag);
  return `background: hsl(${h}, 80%, 94%); color: hsl(${h}, 70%, 32%);`;
}

function avatarHtml(b) {
  if (b.avatar) {
    return `<img class="avatar" src="${escapeHtml(b.avatar)}" loading="lazy" alt="">`;
  }
  const h = hueOf(b.handle || b.author || "?");
  const initial = (b.author || b.handle || "?").trim().charAt(0).toUpperCase();
  return `<div class="avatar-fallback" style="background: hsl(${h}, 55%, 55%)">${escapeHtml(initial)}</div>`;
}

function mediaGrid(media) {
  if (!media || !media.length) return "";
  const n = Math.min(media.length, 4);
  const items = media.map(m => m.type === "video"
    ? `<video src="${escapeHtml(m.src)}#t=0.001" controls preload="metadata" playsinline></video>`
    : `<img src="${escapeHtml(m.src)}" loading="lazy" alt="">`
  ).join("");
  return `<div class="media n${n}">${items}</div>`;
}

function quoteBlock(q) {
  if (!q) return "";
  const av = q.avatar
    ? `<img class="qavatar" src="${escapeHtml(q.avatar)}" loading="lazy" alt="">`
    : "";
  return `
    <div class="quote" data-url="${escapeHtml(q.url)}">
      <div class="qhead">
        ${av}
        <span class="name">${escapeHtml(q.author)}</span>
        <span class="handle">@${escapeHtml(q.handle)}</span>
      </div>
      ${q.text ? `<div class="qbody">${linkify(escapeHtml(q.text))}</div>` : ""}
      ${mediaGrid(q.media)}
    </div>`;
}

function replyLine(rp) {
  if (!rp) return "";
  return `<div class="replying">Replying to <a href="https://x.com/${encodeURIComponent(rp.handle)}" target="_blank" rel="noopener">@${escapeHtml(rp.handle)}</a></div>`;
}

const ICONS = {
  reply: '<svg viewBox="0 0 24 24"><path d="M1.751 10c0-4.42 3.584-8 8.005-8h4.366c4.49 0 8.129 3.64 8.129 8.13 0 2.96-1.607 5.68-4.196 7.11l-8.054 4.46v-3.69h-.067c-4.49.1-8.183-3.51-8.183-8.01zm8.005-6c-3.317 0-6.005 2.69-6.005 6 0 3.37 2.77 6.08 6.138 6.01l.351-.01h1.761v2.3l5.087-2.81c1.951-1.08 3.163-3.13 3.163-5.36 0-3.39-2.744-6.13-6.129-6.13H9.756z"/></svg>',
  repost: '<svg viewBox="0 0 24 24"><path d="M4.5 3.88l4.432 4.14-1.364 1.46L5.5 7.55V16c0 1.1.896 2 2 2H13v2H7.5c-2.209 0-4-1.79-4-4V7.55L1.432 9.48.068 8.02 4.5 3.88zM16.5 6H11V4h5.5c2.209 0 4 1.79 4 4v8.45l2.068-1.93 1.364 1.46-4.432 4.14-4.432-4.14 1.364-1.46 2.068 1.93V8c0-1.1-.896-2-2-2z"/></svg>',
  like: '<svg viewBox="0 0 24 24"><path d="M16.697 5.5c-1.222-.06-2.679.51-3.89 2.16l-.805 1.09-.806-1.09C9.984 6.01 8.526 5.44 7.304 5.5c-1.243.07-2.349.78-2.91 1.91-.552 1.12-.633 2.78.479 4.82 1.074 1.97 3.257 4.27 7.129 6.61 3.87-2.34 6.052-4.64 7.126-6.61 1.111-2.04 1.03-3.7.477-4.82-.561-1.13-1.666-1.84-2.908-1.91zm4.187 7.69c-1.351 2.48-4.001 5.12-8.379 7.67l-.503.3-.504-.3c-4.379-2.55-7.029-5.19-8.382-7.67-1.36-2.5-1.41-4.86-.514-6.67.887-1.79 2.647-2.91 4.601-3.01 1.651-.09 3.368.56 4.798 2.01 1.429-1.45 3.146-2.1 4.796-2.01 1.954.1 3.714 1.22 4.601 3.01.896 1.81.846 4.17-.514 6.67z"/></svg>',
  bookmark: '<svg viewBox="0 0 24 24"><path d="M4 4.5C4 3.12 5.119 2 6.5 2h11C18.881 2 20 3.12 20 4.5v18.44l-8-5.71-8 5.71V4.5zM6.5 4c-.276 0-.5.22-.5.5v14.56l6-4.29 6 4.29V4.5c0-.28-.224-.5-.5-.5h-11z"/></svg>',
  open: '<svg viewBox="0 0 24 24"><path d="M12 2.59l5.7 5.7-1.41 1.42L13 6.41V16h-2V6.41l-3.3 3.3-1.41-1.42L12 2.59zM21 15l-.02 3.51c0 1.38-1.12 2.49-2.5 2.49H5.5C4.11 21 3 19.88 3 18.5V15h2v3.5c0 .28.22.5.5.5h12.98c.28 0 .5-.22.5-.5L19 15h2z"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M16 6V4.5C16 3.12 14.88 2 13.5 2h-3C9.11 2 8 3.12 8 4.5V6H3v2h1.06l.81 11.21C4.98 20.78 6.28 22 7.86 22h8.27c1.58 0 2.88-1.22 3-2.79L19.93 8H21V6h-5zm-6-1.5c0-.28.22-.5.5-.5h3c.27 0 .5.22.5.5V6h-4V4.5zm7.13 14.57c-.04.52-.47.93-1 .93H7.86c-.53 0-.96-.41-1-.93L6.07 8h11.85l-.79 11.07z"/></svg>',
};

// X collapses long posts behind a "Show more" link; ~560 chars or 8+ lines counts as long.
function bodyHtml(text) {
  if (!text) return "";
  const long = text.length > 560 || text.split("\n").length > 8;
  const body = `<div class="body${long ? " clamped" : ""}">${linkify(escapeHtml(text))}</div>`;
  return long ? body + `<span class="showmore">Show more</span>` : body;
}

// Tag pills for one bookmark: the "deleted on X" flag (when a full sweep found it
// gone), removable tag pills, then the "+" button. Extracted so a tag change can
// re-render just this row instead of the whole timeline.
function tagRowHtml(b) {
  const rmBtn = `<span class="rmtag" title="Remove tag" role="button" aria-label="Remove tag">×</span>`;
  const gone = b.deletedOnX
    ? `<span class="pill deleted-x" title="No longer bookmarked on X">Deleted on X</span>` : "";
  const tags = tagsOf(b).map(t =>
    `<span class="pill" style="${pillStyle(t)}" data-tag="${escapeHtml(t)}">${escapeHtml(t)}${rmBtn}</span>`).join("");
  return gone + tags + `<button class="pill addtag" type="button" title="Add tag" aria-label="Add tag">+</button>`;
}

function tweetHtml(b) {
  return `
    <article class="tweet" data-id="${escapeHtml(b.id)}" data-url="${escapeHtml(b.url)}">
      <div class="selcircle"></div>
      ${avatarHtml(b)}
      <div class="content">
        <div class="head">
          <a class="name" href="https://x.com/${encodeURIComponent(b.handle)}" target="_blank" rel="noopener" style="color: var(--text);">${escapeHtml(b.author)}</a>
          <span class="handle">@${escapeHtml(b.handle)}</span>
          <span class="dot">·</span>
          <a class="time" href="${escapeHtml(b.url)}" target="_blank" rel="noopener">${escapeHtml(b.dateDisplay)}</a>
          <button class="del" type="button" title="Remove from view">${ICONS.trash}</button>
        </div>
        ${replyLine(b.replyParent)}
        ${bodyHtml(b.text)}
        ${mediaGrid(b.media)}
        ${quoteBlock(b.quoted)}
        <div class="tagrow">${tagRowHtml(b)}</div>
        <div class="actions">
          <span class="act reply">${ICONS.reply}</span>
          <span class="act repost">${ICONS.repost}<span>${fmtCount(b.reposts)}</span></span>
          <span class="act like">${ICONS.like}<span>${fmtCount(b.likes)}</span></span>
          <span class="act bm">${ICONS.bookmark}<span>${fmtCount(b.bookmarks)}</span></span>
          <span class="act open">${ICONS.open}</span>
        </div>
      </div>
    </article>`;
}

// --- Persistent keyed card cache ---------------------------------------------
// Each bookmark's <article> is built from HTML exactly once and kept forever.
// Filtering/sorting/searching only *moves* these existing nodes in and out of
// the timeline — appending an already-live node relocates it, it does not clone
// or reload it — so avatars, images and videos load once and never again.
// Combined with `content-visibility: auto`, off-screen cards cost ~nothing to
// keep around, so this beats windowing while being far simpler and bug-free.

const cardCache = new Map();   // id -> <article>
const _builder = document.createElement("template");

function getCard(b) {
  let el = cardCache.get(b.id);
  if (!el) {
    _builder.innerHTML = tweetHtml(b);            // parse this one card, once
    el = _builder.content.firstElementChild;
    cardCache.set(b.id, el);
  }
  return el;
}

let _emptyEl = null;
function emptyEl() {
  if (!_emptyEl) {
    _emptyEl = document.createElement("div");
    _emptyEl.className = "empty";
    _emptyEl.innerHTML = `<div class="big">No bookmarks found</div>Try a different search or clear your tag filters.`;
  }
  return _emptyEl;
}

function render(rows) {
  const timeline = document.getElementById("timeline");
  if (!rows.length) {
    timeline.replaceChildren(emptyEl());
  } else {
    // replaceChildren relocates cached nodes into the new order in one shot.
    timeline.replaceChildren(...rows.map(getCard));
    if (state.selecting) {
      for (const a of timeline.children) a.classList.toggle("selected", state.selected.has(a.dataset.id));
    }
  }
  setShownCount(rows.length);
  state.visibleIds = rows.map(b => b.id);
}

// Precomputed, lowercased search text per bookmark (excludes tags, which mutate).
function haystack(b) {
  if (b._hay === undefined) {
    b._hay = [b.text, b.author, "@" + b.handle, b.quoted && b.quoted.text]
      .filter(Boolean).join("\n").toLowerCase();
  }
  return b._hay;
}

// Does this bookmark pass the current hidden/tag/search filters? Shared by the
// full re-render (applyState) and the surgical updates so they never disagree.
// --- Boolean tag-filter expression: `and` / `or` / `not` / parentheses over
// tag names (bare, or "quoted" for names with spaces). Compiles the string to
// a predicate over a bookmark's lowercased tag Set. Empty string = match all. ---
function tokenizeFilter(src) {
  const toks = [];
  let i = 0;
  const space = c => c === " " || c === "\t" || c === "\n";
  const stop = c => space(c) || "()&|!\"'".includes(c);
  while (i < src.length) {
    const c = src[i];
    if (space(c)) { i++; continue; }
    if (c === "(") { toks.push({ t: "(" }); i++; continue; }
    if (c === ")") { toks.push({ t: ")" }); i++; continue; }
    if (c === "!") { toks.push({ t: "not" }); i++; continue; }
    if (c === "&") { i++; if (src[i] === "&") i++; toks.push({ t: "and" }); continue; }
    if (c === "|") { i++; if (src[i] === "|") i++; toks.push({ t: "or" }); continue; }
    if (c === '"' || c === "'") {
      const q = c; i++; let s = "";
      while (i < src.length && src[i] !== q) s += src[i++];
      if (i >= src.length) throw new Error("unclosed quote");
      i++; toks.push({ t: "tag", v: s.toLowerCase() }); continue;
    }
    let s = "";
    while (i < src.length && !stop(src[i])) s += src[i++];
    const low = s.toLowerCase();
    if (low === "and") toks.push({ t: "and" });
    else if (low === "or") toks.push({ t: "or" });
    else if (low === "not") toks.push({ t: "not" });
    else toks.push({ t: "tag", v: low });
  }
  return toks;
}

function parseFilter(src) {
  const toks = tokenizeFilter(src);
  let p = 0;
  const peek = () => toks[p];
  const atomStart = tk => tk && (tk.t === "tag" || tk.t === "(" || tk.t === "not");
  function pOr() {
    let n = pAnd();
    while (peek() && peek().t === "or") { p++; const r = pAnd(), l = n; n = s => l(s) || r(s); }
    return n;
  }
  function pAnd() {
    let n = pNot();
    // explicit `and`, or two adjacent atoms => implicit AND
    while (peek() && (peek().t === "and" || atomStart(peek()))) {
      if (peek().t === "and") p++;
      const r = pNot(), l = n; n = s => l(s) && r(s);
    }
    return n;
  }
  function pNot() {
    if (peek() && peek().t === "not") { p++; const r = pNot(); return s => !r(s); }
    return pAtom();
  }
  function pAtom() {
    const tk = peek();
    if (!tk) throw new Error("unexpected end of filter");
    if (tk.t === "(") {
      p++; const n = pOr();
      if (!peek() || peek().t !== ")") throw new Error("missing )");
      p++; return n;
    }
    if (tk.t === "tag") {
      p++;
      // Reserved keyword: "untagged" matches bookmarks with no (visible) tags.
      if (tk.v === "untagged") return s => s.size === 0;
      return s => s.has(tk.v);
    }
    throw new Error("unexpected '" + tk.t + "'");
  }
  const n = pOr();
  if (p < toks.length) throw new Error("unexpected '" + peek().t + "'");
  return n;
}

function compileFilter(expr) {
  const s = (expr || "").trim();
  if (!s) return { pred: null, ok: true };
  try { return { pred: parseFilter(s), ok: true }; }
  catch (e) { return { pred: null, ok: false, error: e.message }; }
}

function matchesFilters(b) {
  if (state.hidden.has(b.id)) return false;
  if (state.filterPred && !state.filterPred(new Set(tagsOf(b).map(t => t.toLowerCase())))) return false;
  if (state.search) {
    const q = state.search;
    return haystack(b).includes(q) || tagsOf(b).some(t => t.toLowerCase().includes(q));
  }
  return true;
}

function applyState() {
  const dir = state.sortDir === "asc" ? 1 : -1;
  const key = state.sortKey;
  const rows = DATA.filter(matchesFilters).sort((a, b) => {
    if (key === "author") return dir * (a.author || "").localeCompare(b.author || "");
    if (key === "likes") return dir * (a.likes - b.likes);
    if (key === "reposts") return dir * (a.reposts - b.reposts);
    return dir * (a.ts - b.ts);
  });
  render(rows);
}

// --- Surgical DOM updates: touch only the affected card so a delete or a tag
// change never rebuilds (and reloads the media of) all ~500 tweets. ---

function setShownCount(n) {
  document.getElementById("shownCount").textContent = n.toLocaleString();
}

// Re-render just one card's tag pills after an add/remove.
function refreshTagRow(id) {
  const b = byId.get(id);
  const art = cardCache.get(id);
  if (b && art) art.querySelector(".tagrow").innerHTML = tagRowHtml(b);
}

// Re-render every built card's tag pills in place (no node/media rebuild) —
// used when a global tag change affects many cards at once.
function refreshAllTagRows() {
  for (const [id, art] of cardCache) {
    const b = byId.get(id);
    if (b) art.querySelector(".tagrow").innerHTML = tagRowHtml(b);
  }
}

// Drop a card from the DOM without rebuilding the rest; keep visibleIds/count in sync.
function removeArticle(art) {
  const id = art.dataset.id;
  art.remove();
  const i = state.visibleIds.indexOf(id);
  if (i !== -1) state.visibleIds.splice(i, 1);
  setShownCount(state.visibleIds.length);
  if (!state.visibleIds.length) applyState();  // fall back to the empty-state view
}

// Every tag currently in use (derived + custom). Ordered by when you last
// applied the tag to a post (most recent first), then by frequency for tags
// you haven't used yet. Shared by the filter dropdown and the add-tag autocomplete.
function tagCountsSorted() {
  const counts = new Map();
  for (const b of DATA) {
    for (const t of tagsOf(b)) {
      const key = t.toLowerCase();
      const cur = counts.get(key) || { label: t, n: 0 };
      cur.n += 1;
      counts.set(key, cur);
    }
  }
  const lu = state.tagLastUsed;
  return [...counts.entries()].sort((a, b) => {
    const la = lu[a[0]] || 0, lb = lu[b[0]] || 0;
    if (la !== lb) return lb - la;   // most-recently-used tag first
    return b[1].n - a[1].n;          // then most-frequent
  });
}

// Build the whole filter dropdown: an editable boolean expression, a save row,
// the saved-filter chips, and a click-to-insert tag palette (+ include, − exclude).
function buildTagOptions() {
  const sorted = tagCountsSorted();
  const menu = document.getElementById("tagMenu");
  menu.innerHTML = `
    <div class="fhead"><span>Filter</span><button id="filterDone" class="fdone" type="button">Done</button></div>
    <div class="fbar">
      <input id="filterExpr" class="fexpr" type="text" autocomplete="off" spellcheck="false"
             placeholder="e.g. media and not archive">
      <button id="filterClear" class="fx" type="button" title="Clear filter">×</button>
    </div>
    <div id="filterErr" class="ferr" hidden></div>
    <div class="fsave">
      <input id="filterName" class="fname" type="text" autocomplete="off" placeholder="name this filter">
      <button id="filterSaveBtn" class="fsavebtn" type="button">Save</button>
    </div>
    <div id="savedRow" class="saved" hidden></div>
    <div class="fquick">
      <button id="filterUntagged" class="fquickbtn" type="button">Untagged only <span class="cnt" id="untaggedCount"></span></button>
    </div>
    <div class="fdiv">Tags — <b>+</b> include, <b>−</b> exclude</div>
    <div class="palette">
      ${sorted.map(([key, { label, n }]) => `
        <div class="prow" data-tag="${escapeHtml(key)}">
          <button class="pbtn add" type="button" title="include in filter">+</button>
          <button class="pbtn ex" type="button" title="exclude in filter">−</button>
          <span class="pname">${escapeHtml(label)}</span>
          <span class="cnt">${n}</span>
        </div>`).join("")}
    </div>`;
  wireFilterMenu();
}

function wireFilterMenu() {
  const expr = document.getElementById("filterExpr");
  expr.value = state.filterExpr;
  let t;
  expr.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(() => setFilterExpr(expr.value), 150);
  });
  expr.addEventListener("keydown", e => {
    e.stopPropagation();
    if (e.key === "Enter") { clearTimeout(t); setFilterExpr(expr.value); }
  });
  document.getElementById("filterClear").addEventListener("click", () => {
    expr.value = "";
    setFilterExpr("");
    expr.focus();
  });
  document.getElementById("filterSaveBtn").addEventListener("click", saveCurrentFilter);
  document.getElementById("filterDone").addEventListener("click", () => { document.getElementById("tagDropdown").open = false; });
  document.getElementById("filterName").addEventListener("keydown", e => {
    e.stopPropagation();
    if (e.key === "Enter") saveCurrentFilter();
  });
  // "Untagged only" quick filter: toggle the whole expression to `untagged`.
  const untaggedN = DATA.filter(b => !state.hidden.has(b.id) && tagsOf(b).length === 0).length;
  document.getElementById("untaggedCount").textContent = untaggedN;
  const untaggedBtn = document.getElementById("filterUntagged");
  untaggedBtn.classList.toggle("on", state.filterExpr.trim().toLowerCase() === "untagged");
  untaggedBtn.addEventListener("click", () => {
    const on = state.filterExpr.trim().toLowerCase() === "untagged";
    const next = on ? "" : "untagged";
    expr.value = next;
    setFilterExpr(next);
  });
  document.getElementById("tagMenu").querySelectorAll(".prow").forEach(row => {
    const tag = row.dataset.tag;
    row.querySelector(".add").addEventListener("click", () => insertTag(tag, false));
    row.querySelector(".ex").addEventListener("click", () => insertTag(tag, true));
  });
  renderSavedFilters();
}

// A global tag delete/restore only changes which tags are hidden; push that
// change to every card's pills, the filter palette, and the timeline. Posts and
// synced data are never modified. Reversible from the Manage-tags modal.
function applyGlobalTagChange() {
  refreshAllTagRows();   // strip/re-add the tag on every card's pills, no media reload
  buildTagOptions();     // keep the filter palette counts in sync
  applyState();          // re-filter (an active filter may reference the tag)
}

function removeTagGlobally(tagKey) {
  mutate({ op: "tagDelete", name: tagKey });
  applyGlobalTagChange();
}

function restoreTagGlobally(tagKey) {
  mutate({ op: "tagRestore", name: tagKey });
  applyGlobalTagChange();
}

// Every tag's post count, INCLUDING globally-deleted ones (so the manager can
// show what a restore brings back).
function tagInventory() {
  const counts = new Map();
  for (const b of DATA) {
    const seen = new Set();
    for (const t of state.tags[b.id] || []) {
      const k = t.toLowerCase();
      if (seen.has(k)) continue;
      seen.add(k);
      const cur = counts.get(k) || { label: t, n: 0 };
      cur.n += 1;
      counts.set(k, cur);
    }
  }
  return [...counts.entries()].sort((a, b) => b[1].n - a[1].n);
}

// --- Manage-tags modal (two tabs: Active tags to delete, Deleted tags to restore) ---
let tmTab = "active";

function openTagManager() {
  tmTab = "active";
  document.getElementById("tmSearch").value = "";
  renderTagManager();
  const bd = document.getElementById("tagManager");
  bd.hidden = false;
  if (!TOUCH) document.getElementById("tmSearch").focus();   // not on a phone: it would pop the keyboard
}

function closeTagManager() {
  document.getElementById("tagManager").hidden = true;
}

function setTagManagerTab(tab) {
  tmTab = tab;
  renderTagManager();
}

function renderTagManager() {
  const q = (document.getElementById("tmSearch").value || "").trim().toLowerCase();
  const inv = tagInventory();
  const active = inv.filter(([key]) => !state.deletedTags.has(key));
  const deleted = inv.filter(([key]) => state.deletedTags.has(key));

  document.getElementById("tmTabActive").textContent = `Active (${active.length})`;
  document.getElementById("tmTabDeleted").textContent = `Deleted (${deleted.length})`;
  document.getElementById("tmTabActive").classList.toggle("on", tmTab === "active");
  document.getElementById("tmTabDeleted").classList.toggle("on", tmTab === "deleted");

  const base = tmTab === "deleted" ? deleted : active;
  const rows = base.filter(([key, { label }]) =>
    !q || key.includes(q) || label.toLowerCase().includes(q));

  document.getElementById("tmSummary").textContent =
    q ? `${rows.length} of ${base.length} shown` : `${base.length} ${tmTab} tag${base.length === 1 ? "" : "s"}`;

  const list = document.getElementById("tmList");
  if (!rows.length) {
    const msg = q
      ? `No ${tmTab} tags match “${escapeHtml(q)}”.`
      : (tmTab === "deleted" ? "No deleted tags. Delete one from the Active tab." : "No tags.");
    list.innerHTML = `<div class="tm-empty">${msg}</div>`;
    return;
  }
  list.innerHTML = rows.map(([key, { label, n }]) => {
    const isDeleted = tmTab === "deleted";
    return `<div class="tm-row${isDeleted ? " deleted" : ""}" data-tag="${escapeHtml(key)}">
      <span class="tm-pill" style="${pillStyle(label)}">${escapeHtml(label)}</span>
      <span class="tm-cnt">${n} post${n === 1 ? "" : "s"}</span>
      ${isDeleted
        ? `<button class="tm-act restore" type="button">Restore</button>`
        : `<button class="tm-act delete" type="button">Delete</button>`}
    </div>`;
  }).join("");
  list.querySelectorAll(".tm-row").forEach(row => {
    const tag = row.dataset.tag;
    const btn = row.querySelector(".tm-act");
    btn.addEventListener("click", () => {
      if (btn.classList.contains("delete")) removeTagGlobally(tag);
      else restoreTagGlobally(tag);
      renderTagManager();  // the tag leaves this tab; stay put, modal open
    });
  });
}

// Compile the expression; on success filter + persist, on error just flag the
// input and keep the last valid results (so mid-typing never blanks the list).
function setFilterExpr(value) {
  state.filterExpr = value;
  const { pred, ok, error } = compileFilter(value);
  const input = document.getElementById("filterExpr");
  const err = document.getElementById("filterErr");
  if (ok) {
    state.filterPred = pred;
    if (input) input.classList.remove("bad");
    if (err) err.hidden = true;
    localStorage.setItem("filterExpr", value);
    updateTagSummary();
    const untaggedBtn = document.getElementById("filterUntagged");
    if (untaggedBtn) untaggedBtn.classList.toggle("on", value.trim().toLowerCase() === "untagged");
    applyState();
  } else {
    if (input) input.classList.add("bad");
    if (err) { err.hidden = false; err.textContent = error; }
  }
}

// Append a tag to the expression (AND-ed); quote names with spaces/specials.
function insertTag(tag, negate) {
  const input = document.getElementById("filterExpr");
  const token = (negate ? "not " : "") + (/[\s()&|!"']/.test(tag) ? `"${tag}"` : tag);
  const cur = (input.value || "").trim();
  input.value = cur ? `${cur} and ${token}` : token;
  setFilterExpr(input.value);
  input.focus();
}

function saveCurrentFilter() {
  const nameInput = document.getElementById("filterName");
  const name = (nameInput.value || "").trim();
  // Save what is in the box, not the debounced copy in state (typing then tapping Save quickly must work).
  const expr = (document.getElementById("filterExpr").value || "").trim();
  if (!name || !expr) return;
  mutate({ op: "saveFilter", name, expr });
  nameInput.value = "";
  renderSavedFilters();
}

function renderSavedFilters() {
  const row = document.getElementById("savedRow");
  if (!row) return;
  if (!state.savedFilters.length) { row.hidden = true; row.innerHTML = ""; return; }
  row.hidden = false;
  row.innerHTML = state.savedFilters.map((f, i) => `
    <span class="sf" data-i="${i}">
      <button class="sfapply" type="button" title="${escapeHtml(f.expr)}">${escapeHtml(f.name)}</button>
      <button class="sfdel" type="button" title="delete">×</button>
    </span>`).join("");
  row.querySelectorAll(".sf").forEach(el => {
    const i = +el.dataset.i;
    el.querySelector(".sfapply").addEventListener("click", () => {
      const input = document.getElementById("filterExpr");
      input.value = state.savedFilters[i].expr;
      setFilterExpr(input.value);
    });
    el.querySelector(".sfdel").addEventListener("click", () => {
      mutate({ op: "delFilter", name: state.savedFilters[i].name });
      renderSavedFilters();
    });
  });
}

function updateTagSummary() {
  const el = document.getElementById("tagSummary");
  const s = (state.filterExpr || "").trim();
  if (!s) { el.textContent = "Filter"; el.classList.remove("on"); return; }
  el.textContent = s.length > 22 ? s.slice(0, 21) + "…" : s;
  el.classList.add("on");
}

function updateSelBar() {
  document.getElementById("selCount").textContent = state.selected.size;
  document.getElementById("selDelete").disabled = state.selected.size === 0;
  const allSelected = state.visibleIds.length > 0 &&
    state.visibleIds.every(id => state.selected.has(id));
  document.getElementById("selAll").textContent = allSelected ? "Deselect all" : "Select all";
}

function beginAddTag(btn) {
  const id = btn.closest("article").dataset.id;
  const own = new Set(tagsOf(byId.get(id)).map(t => t.toLowerCase()));
  // Existing tags this bookmark doesn't already have, most-used first.
  const pool = tagCountsSorted()
    .map(([key, { label, n }]) => ({ key, label, n }))
    .filter(t => !own.has(t.key));

  const input = document.createElement("input");
  input.className = "taginput";
  input.placeholder = "new tag";
  input.autocomplete = "off";
  btn.replaceWith(input);
  input.focus();
  if (TOUCH) input.scrollIntoView({ block: "center" });   // keep it clear of the on-screen keyboard

  // The suggestion menu lives on <body> with position:fixed so the card's
  // `content-visibility` paint containment can never clip it.
  const menu = document.createElement("div");
  menu.className = "tagsuggest";
  document.body.appendChild(menu);

  let matches = [], active = -1, done = false;

  const positionMenu = () => {
    const r = input.getBoundingClientRect();
    menu.style.minWidth = Math.max(r.width, 170) + "px";
    menu.style.left = Math.round(Math.min(r.left, innerWidth - menu.offsetWidth - 8)) + "px";
    const h = menu.offsetHeight;
    const below = r.bottom + 4;
    // visualViewport excludes the on-screen keyboard; innerHeight does not (iOS).
    const vh = window.visualViewport ? visualViewport.height : innerHeight;
    menu.style.top = Math.round(below + h > vh ? Math.max(8, r.top - h - 4) : below) + "px";
  };

  const highlight = () =>
    [...menu.children].forEach((el, i) => el.classList.toggle("active", i === active));

  const renderMenu = () => {
    const q = input.value.trim().toLowerCase();
    matches = (q ? pool.filter(t => t.key.includes(q)) : pool).slice(0, 8);
    active = -1;
    if (!matches.length) { menu.style.display = "none"; return; }
    menu.style.display = "";
    menu.innerHTML = matches.map((t, i) =>
      `<div class="opt" data-i="${i}"><span class="pill" style="${pillStyle(t.label)}">${escapeHtml(t.label)}</span><span class="cnt">${t.n}</span></div>`
    ).join("");
    positionMenu();
  };

  const finish = (commit, valOverride) => {
    if (done) return;
    done = true;
    menu.remove();
    window.removeEventListener("scroll", onScroll, true);
    const val = (valOverride != null ? valOverride : (input.value || "").trim());
    if (commit && val && !own.has(val.toLowerCase())) {
      // A globally-deleted name comes back when you tag with it again (server + applyLocal agree).
      mutate({ op: "addTag", id, name: val });
      buildTagOptions();
    }
    refreshTagRow(id);  // redraw just this card's pills (also restores the "+" on cancel)
  };

  // Desktop: commit & close if the page scrolls. Touch: scrolling is how you reach
  // the input (and the keyboard opening fires scroll too), so follow it instead.
  // Scrolls in the first moments are the browser bringing the new input into view: follow, don't close.
  const openedAt = performance.now();
  const onScroll = () => {
    if (done) return;
    if (TOUCH || performance.now() - openedAt < 400) positionMenu();
    else finish(true);
  };

  input.addEventListener("input", renderMenu);
  input.addEventListener("keydown", e => {
    e.stopPropagation();
    if (e.key === "Enter") {
      finish(true, active >= 0 ? matches[active].label : undefined);
    } else if (e.key === "Escape") {
      if (menu.style.display !== "none") menu.style.display = "none";  // 1st Esc: close menu
      else finish(false);                                             // 2nd Esc: cancel add
    } else if (e.key === "ArrowDown" && matches.length) {
      e.preventDefault(); active = (active + 1) % matches.length; highlight();
    } else if (e.key === "ArrowUp" && matches.length) {
      e.preventDefault(); active = (active - 1 + matches.length) % matches.length; highlight();
    }
  });
  input.addEventListener("click", e => e.stopPropagation());
  // mousedown (fires before the input's blur) so picking a suggestion commits cleanly.
  menu.addEventListener("mousedown", e => {
    const opt = e.target.closest(".opt");
    if (!opt) return;
    e.preventDefault();
    finish(true, matches[+opt.dataset.i].label);
  });
  input.addEventListener("blur", () => finish(true));
  window.addEventListener("scroll", onScroll, true);

  renderMenu();  // show existing tags immediately on open
}

// --- Tag-cards deck: flip through the filtered timeline one post at a time,
// tagging with the keyboard only. The list is a snapshot taken on entry —
// tagging never removes a card mid-session (the filter re-applies on exit).
// Position survives exit/re-enter within a page session, not across reloads.
const deck = { open: false, ids: [], idx: 0 };

const deckInput = document.getElementById("deckInput");
const deckMenu = document.getElementById("deckMenu");
let deckMatches = [], deckActive = -1, deckPool = [];

function deckBookmark() { return byId.get(deck.ids[deck.idx]); }

// Existing tags this post doesn't already have, most-recently-used first.
function rebuildDeckPool() {
  const own = new Set(tagsOf(deckBookmark()).map(t => t.toLowerCase()));
  deckPool = tagCountsSorted()
    .map(([key, { label, n }]) => ({ key, label, n }))
    .filter(t => !own.has(t.key));
}

function closeDeckMenu() {
  deckMenu.hidden = true;
  deckMatches = [];
  deckActive = -1;
}

// Suggestions show as soon as you land on a card: the most-recently-used
// tags for zero-typing tagging, narrowed once you type. Esc dismisses the
// menu, handing ↑/↓ back to scrolling the card.
function renderDeckMenu() {
  const q = deckInput.value.trim().toLowerCase();
  // No cap: the menu is scrollable (max-height + overflow-y), and arrow-nav
  // scrolls the active row into view, so every tag stays reachable.
  deckMatches = q ? deckPool.filter(t => t.key.includes(q)) : deckPool;
  deckActive = -1;
  if (!deckMatches.length) { closeDeckMenu(); return; }
  deckMenu.hidden = false;
  deckMenu.innerHTML = deckMatches.map((t, i) =>
    `<div class="opt" data-i="${i}"><span class="pill" style="${pillStyle(t.label)}">${escapeHtml(t.label)}</span><span class="cnt">${t.n}</span></div>`
  ).join("");
}

function highlightDeckMenu() {
  [...deckMenu.children].forEach((el, i) => {
    const on = i === deckActive;
    el.classList.toggle("active", on);
    if (on) el.scrollIntoView({ block: "nearest" });  // keep the pick visible past the fold
  });
}

function renderDeckPills() {
  const tags = tagsOf(deckBookmark());
  document.getElementById("deckPills").innerHTML = tags.length
    ? tags.map(t => `<span class="pill" style="${pillStyle(t)}">${escapeHtml(t)}</span>`).join("")
    : `<span class="none">no tags yet</span>`;
}

function showDeckCard() {
  const b = deckBookmark();
  const card = document.getElementById("deckCard");
  card.innerHTML = tweetHtml(b);
  card.scrollTop = 0;
  document.getElementById("deckCount").textContent = `${deck.idx + 1} / ${deck.ids.length}`;
  renderDeckPills();
  rebuildDeckPool();
  deckInput.value = "";
  renderDeckMenu();
  if (!TOUCH) deckInput.focus();   // on a phone, focusing would pop the keyboard on every swipe
}

function openDeck() {
  if (!state.visibleIds.length || deck.open) return;
  if (state.selecting) setSelecting(false);
  document.getElementById("tagDropdown").open = false;
  deck.ids = state.visibleIds.slice();
  deck.idx = Math.min(deck.idx, deck.ids.length - 1);
  deck.open = true;
  document.getElementById("deck").hidden = false;
  document.body.classList.add("deck-open");
  showDeckCard();
}

function closeDeck() {
  deck.open = false;
  document.getElementById("deck").hidden = true;
  document.body.classList.remove("deck-open");
  document.getElementById("deckCard").innerHTML = "";  // stop any playing video
  closeDeckMenu();
  applyState();  // tags added in the deck may change what the filter matches
}

function navDeck(delta) {
  const next = deck.idx + delta;
  const shell = document.querySelector("#deck .deck-shell");
  if (next < 0 || next >= deck.ids.length) {
    // At the wall: rubber-band nudge in the direction attempted.
    const cls = delta < 0 ? "bounce-l" : "bounce-r";
    shell.classList.remove("bounce-l", "bounce-r");
    void shell.offsetWidth;  // restart the animation if the class was just removed
    shell.classList.add(cls);
    return;
  }
  deck.idx = next;
  showDeckCard();
}

// Same commit semantics as the timeline's add-tag input. Tagging with a name that
// was deleted from every post revives it (a post that already carries the hidden
// tag counts as untagged, so the add goes through).
function commitDeckTag(valOverride) {
  const val = (valOverride != null ? valOverride : deckInput.value).trim();
  const b = deckBookmark();
  if (!val) return;
  const low = val.toLowerCase();
  if (!tagsOf(b).some(t => t.toLowerCase() === low)) {
    mutate({ op: "addTag", id: b.id, name: val });
    buildTagOptions();
    refreshTagRow(b.id);  // keep the (hidden) timeline card in sync
    renderDeckPills();
    rebuildDeckPool();
  }
  deckInput.value = "";
  renderDeckMenu();  // straight back to MRU suggestions for the next tag
}

// Backspace on an empty input: chip-style undo of the newest visible tag.
function removeLastDeckTag() {
  const b = deckBookmark();
  const tags = tagsOf(b);
  if (!tags.length) return;
  mutate({ op: "rmTag", id: b.id, name: tags[tags.length - 1] });
  buildTagOptions();
  refreshTagRow(b.id);
  renderDeckPills();
  rebuildDeckPool();
  if (!deckMenu.hidden) renderDeckMenu();  // the removed tag is suggestible again
}

// Remove the current post from view (same as the timeline trash button — a
// hide, restorable via "Restore N deleted"; the app never touches X or the
// synced data). Drop it from the deck snapshot and advance to the next card.
function deleteCurrentDeckPost() {
  const b = deckBookmark();
  if (!b) return;
  mutate({ op: "hide", ids: [b.id], hidden: true });
  deck.ids.splice(deck.idx, 1);
  if (!deck.ids.length) { closeDeck(); return; }
  if (deck.idx >= deck.ids.length) deck.idx = deck.ids.length - 1;
  showDeckCard();
}

deckInput.addEventListener("input", renderDeckMenu);
deckInput.addEventListener("keydown", e => {
  e.stopPropagation();
  const empty = !deckInput.value;
  const menuOpen = !deckMenu.hidden && deckMatches.length > 0;
  if (e.key === "Enter") {
    if (e.metaKey || e.ctrlKey) { window.open(deckBookmark().url, "_blank", "noopener"); return; }
    commitDeckTag(menuOpen && deckActive >= 0 ? deckMatches[deckActive].label : undefined);
  } else if (e.key === "Tab" && menuOpen && (deckMatches.length === 1 || deckActive >= 0)) {
    // Tab-complete: fill the input with the sole (or highlighted) suggestion so
    // Enter then adds the full tag, not the partial text you typed.
    e.preventDefault();
    deckInput.value = (deckActive >= 0 ? deckMatches[deckActive] : deckMatches[0]).label;
    renderDeckMenu();
  } else if (e.key === "Escape") {
    if (menuOpen) closeDeckMenu();               // 1st Esc: close suggestions
    else if (!empty) { deckInput.value = ""; }   // 2nd: clear typed text
    else closeDeck();                            // 3rd: leave the deck
  } else if (e.key === "ArrowLeft" && empty) {
    e.preventDefault(); navDeck(-1);
  } else if (e.key === "ArrowRight" && empty) {
    e.preventDefault(); navDeck(1);
  } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    const dir = e.key === "ArrowDown" ? 1 : -1;
    if (menuOpen) {
      e.preventDefault();
      deckActive = (deckActive + dir + deckMatches.length) % deckMatches.length;
      highlightDeckMenu();
    } else if (empty) {
      e.preventDefault();
      document.getElementById("deckCard").scrollBy(0, dir * 160);
    }
  } else if ((e.key === "Backspace" || e.key === "Delete") && (e.metaKey || e.ctrlKey) && empty) {
    e.preventDefault(); deleteCurrentDeckPost();   // ⌘⌫: remove this post from view
  } else if (e.key === "Backspace" && empty) {
    e.preventDefault(); removeLastDeckTag();
  }
});
// The input owns the keyboard: whatever steals focus (a card click, a video),
// take it back as long as the deck is up.
deckInput.addEventListener("blur", () => {
  if (deck.open && !TOUCH) setTimeout(() => { if (deck.open) deckInput.focus(); }, 0);
});
deckMenu.addEventListener("mousedown", e => {
  const opt = e.target.closest(".opt");
  if (!opt) return;
  e.preventDefault();
  commitDeckTag(deckMatches[+opt.dataset.i].label);
});
document.getElementById("deck").addEventListener("click", e => {
  // A page of its own, not a modal: only the × (or Esc) leaves — a stray
  // click on the background shouldn't dump you back to the timeline.
  if (e.target.closest("#deckClose")) { closeDeck(); return; }
  if (e.target.closest("a") || e.target.tagName === "VIDEO" || e.target.closest(".deck-tagbar")) return;
  const quote = e.target.closest(".quote");
  if (quote && quote.dataset.url) window.open(quote.dataset.url, "_blank", "noopener");
});

function setSelecting(on) {
  state.selecting = on;
  state.selected.clear();
  document.body.classList.toggle("selecting", on);
  document.getElementById("selectToggle").textContent = on ? "Done" : "Select";
  document.querySelectorAll("article.tweet.selected").forEach(a => a.classList.remove("selected"));
  updateSelBar();
}

// One delegated click handler: selection, delete button, quote card, or whole-tweet open.
document.getElementById("timeline").addEventListener("click", e => {
  if (state.selecting) {
    const card = e.target.closest("article.tweet");
    if (!card) return;
    e.preventDefault();
    const id = card.dataset.id;
    if (state.selected.has(id)) {
      state.selected.delete(id);
      card.classList.remove("selected");
    } else {
      state.selected.add(id);
      card.classList.add("selected");
    }
    updateSelBar();
    return;
  }
  const del = e.target.closest(".del");
  if (del) {
    e.stopPropagation();
    const art = del.closest("article");
    mutate({ op: "hide", ids: [art.dataset.id], hidden: true });
    removeArticle(art);
    return;
  }
  const rm = e.target.closest(".rmtag");
  if (rm) {
    e.stopPropagation();
    const pill = rm.closest(".pill");
    const art = pill.closest("article");
    const id = art.dataset.id;
    mutate({ op: "rmTag", id, name: pill.dataset.tag });
    buildTagOptions();
    // Removing a tag can drop the card out of an active tag filter; otherwise just redraw its pills.
    if (matchesFilters(byId.get(id))) refreshTagRow(id);
    else removeArticle(art);
    return;
  }
  const add = e.target.closest(".addtag");
  if (add) {
    e.stopPropagation();
    beginAddTag(add);
    return;
  }
  if (e.target.closest(".taginput")) return;
  const more = e.target.closest(".showmore");
  if (more) {
    e.stopPropagation();
    const body = more.previousElementSibling;
    const clamped = body.classList.toggle("clamped");
    more.textContent = clamped ? "Show more" : "Show less";
    return;
  }
  if (e.target.closest("a") || e.target.tagName === "VIDEO") return;
  const quote = e.target.closest(".quote");
  if (quote && quote.dataset.url) {
    e.stopPropagation();
    window.open(quote.dataset.url, "_blank", "noopener");
    return;
  }
  const card = e.target.closest("article.tweet");
  if (card) window.open(card.dataset.url, "_blank", "noopener");
});

// Debounce so typing doesn't re-filter+re-render on every keystroke.
let searchTimer;
document.getElementById("search").addEventListener("input", e => {
  const v = e.target.value.trim().toLowerCase();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.search = v; applyState(); }, 120);
});
document.getElementById("sortKey").addEventListener("change", e => {
  state.sortKey = e.target.value;
  applyState();
});
document.getElementById("sortDir").addEventListener("click", () => {
  state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
  document.getElementById("sortDir").textContent = state.sortDir === "desc" ? "↓" : "↑";
  applyState();
});
document.getElementById("selectToggle").addEventListener("click", () => setSelecting(!state.selecting));
document.getElementById("deckToggle").addEventListener("click", openDeck);
document.getElementById("manageTags").addEventListener("click", openTagManager);
document.getElementById("tmClose").addEventListener("click", closeTagManager);
document.getElementById("tmTabActive").addEventListener("click", () => setTagManagerTab("active"));
document.getElementById("tmTabDeleted").addEventListener("click", () => setTagManagerTab("deleted"));
document.getElementById("tmSearch").addEventListener("input", renderTagManager);
document.getElementById("tmSearch").addEventListener("keydown", e => {
  if (e.key === "Escape") { closeTagManager(); return; }
  e.stopPropagation();
});
document.getElementById("tagManager").addEventListener("click", e => {
  // Close on the dim backdrop OR the × (delegated, so a click on the button's
  // text node or a mis-bound direct listener still closes reliably).
  if (e.target.id === "tagManager" || e.target.closest("#tmClose")) closeTagManager();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !document.getElementById("tagManager").hidden) closeTagManager();
});
document.getElementById("selAll").addEventListener("click", () => {
  const allSelected = state.visibleIds.length > 0 &&
    state.visibleIds.every(id => state.selected.has(id));
  if (allSelected) state.selected.clear();
  else state.visibleIds.forEach(id => state.selected.add(id));
  document.querySelectorAll("article.tweet").forEach(a =>
    a.classList.toggle("selected", state.selected.has(a.dataset.id)));
  updateSelBar();
});
document.getElementById("selCancel").addEventListener("click", () => setSelecting(false));
document.getElementById("selDelete").addEventListener("click", () => {
  const gone = new Set(state.selected);
  mutate({ op: "hide", ids: [...gone], hidden: true });
  // Remove the selected cards in place rather than rebuilding the whole timeline.
  for (const a of [...document.querySelectorAll("article.tweet.selected")]) a.remove();
  state.visibleIds = state.visibleIds.filter(id => !gone.has(id));
  setShownCount(state.visibleIds.length);
  setSelecting(false);
  if (!state.visibleIds.length) applyState();  // fall back to the empty-state view
});
document.getElementById("restoreLink").addEventListener("click", () => {
  mutate({ op: "restoreAll" });
  applyState();
});

// --- Sync with X ------------------------------------------------------------------
// The server fetches bookmarks from the official X API in the background; here we
// show its state and poll while a run is in flight. Without X credentials the
// buttons stay hidden and the app simply browses what was imported.
const syncBtn = document.getElementById("syncBtn");
const syncInfo = document.getElementById("syncInfo");
const RING_C = 50.27; // circumference matching the CSS dasharray
let syncTimer = null;
let watching = false;   // true once this page started (or saw) a run, so we report its outcome

function ago(iso) {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " d ago";
}

function syncSpinnerOn(progress) {
  syncBtn.disabled = true;
  const pct = progress == null ? null : Math.min(99, Math.round(progress * 100));
  syncBtn.innerHTML =
    `<svg class="ring${pct == null ? " indeterminate" : ""}" viewBox="0 0 20 20">` +
    `<circle class="track" cx="10" cy="10" r="8"></circle>` +
    `<circle class="arc" cx="10" cy="10" r="8" style="stroke-dashoffset:${
      pct == null ? "" : RING_C * (1 - pct / 100)
    }"></circle></svg>Syncing${pct == null ? "…" : " " + pct + "%"}`;
}

function renderSyncUI() {
  const s = state.sync;
  if (!s || !s.configured) { syncBtn.hidden = true; syncInfo.hidden = true; return; }
  syncBtn.hidden = false;
  syncInfo.hidden = false;
  const last = s.last;
  let bad = false, text;
  if (!s.connected || s.needsReauth) {
    syncBtn.disabled = false;
    syncBtn.textContent = s.needsReauth ? "Reconnect X" : "Connect X";
    text = s.needsReauth ? "⚠ X login expired" : "Not connected";
    bad = true;
  } else if (s.running) {
    syncSpinnerOn(s.progress);
    text = "Syncing…";
  } else {
    syncBtn.disabled = false;
    syncBtn.textContent = "Sync";
    if (last && last.status === "error") { text = "⚠ Sync failed"; bad = true; }
    else if (last && last.status === "partial") { text = "⚠ Partly synced"; bad = true; }
    else text = s.lastOkAt ? "Synced " + ago(s.lastOkAt) : "Never synced";
  }
  syncInfo.textContent = text;
  syncInfo.classList.toggle("bad", bad);
}

async function fetchSyncStatus() {
  const res = await fetch("/api/sync/status", { cache: "no-store", redirect: "error" });
  if (!res.ok) throw new Error("HTTP " + res.status);
  state.sync = await res.json();
  renderSyncUI();
  renderSyncDialog();
  return state.sync;
}

async function pollSync() {
  clearTimeout(syncTimer);
  try {
    const st = await fetchSyncStatus();
    if (st.running) { syncTimer = setTimeout(pollSync, 2000); return; }
    if (!watching) return;
    watching = false;
    const last = st.last || {};
    if (last.status === "error" || last.status === "partial") openSyncDialog();   // readable, not an alert()
    if (last.inserted > 0 || last.markedMissing > 0) await refreshBookmarks();
  } catch {
    syncTimer = setTimeout(pollSync, 3000);   // server briefly unreachable — keep polling
  }
}

async function startSync(force, mode) {
  const q = new URLSearchParams();
  if (force) q.set("force", "true");
  if (mode) q.set("mode", mode);
  syncSpinnerOn(null);
  try {
    const res = await fetch("/api/sync" + (q.toString() ? "?" + q : ""), { method: "POST", cache: "no-store", redirect: "error" });
    const out = await res.json();
    if (!res.ok) throw new Error(out.detail || "HTTP " + res.status);
    if (out.skipped) { await fetchSyncStatus(); return; }   // synced recently / not connected — nothing to run
    watching = true;
    syncTimer = setTimeout(pollSync, 1200);
  } catch (err) {
    try { await fetchSyncStatus(); } catch {}
    openSyncDialog(String(err.message || err));
  }
}

// Re-pull bookmarks after a sync brought something new, without a page reload.
async function refreshBookmarks() {
  try {
    const res = await fetch("/api/bootstrap", { cache: "no-store", redirect: "error" });
    if (!res.ok) return;
    const body = await res.json();
    DATA = body.bookmarks;
    byId.clear();
    for (const b of DATA) byId.set(b.id, b);
    adoptState(body.state);
    document.getElementById("totalCount").textContent = DATA.length.toLocaleString();
    rerenderAll();
  } catch {}
}

syncBtn.title = "Sync new bookmarks (Shift-click: full sweep)";
syncBtn.addEventListener("click", e => {
  const s = state.sync || {};
  if (!s.connected || s.needsReauth) { location.href = "/auth/x/login"; return; }
  startSync(true, e.shiftKey ? "full" : null);
});

// --- Sync dialog ------------------------------------------------------------------
const syncDialog = document.getElementById("syncDialog");
let syncDialogNote = "";

function describeRun(l) {
  const what = l.kind === "full" ? "Full sweep" : "Sync";
  const word = { ok: "finished", partial: "stopped early", error: "failed", running: "is running" }[l.status] || l.status;
  const files = n => `${n} file${n === 1 ? "" : "s"}`;
  const bits = [`${l.fetched} fetched`, `${l.inserted} new`];
  if (l.markedMissing) bits.push(`${l.markedMissing} no longer on X`);
  if (l.mediaDownloaded) bits.push(`${files(l.mediaDownloaded)} saved`);
  if (l.mediaFailed) bits.push(`${files(l.mediaFailed)} failed`);
  return `${what} ${word} ${ago(l.finishedAt || l.startedAt)} — ${bits.join(", ")}.`;
}

function renderSyncDialog() {
  if (syncDialog.hidden) return;
  const s = state.sync || {};
  const rows = [];
  if (syncDialogNote) rows.push(`<div class="sync-err">${escapeHtml(syncDialogNote)}</div>`);
  if (!s.connected) rows.push(`<p>X is not connected yet. Connect your account so new bookmarks can sync automatically.</p>`);
  else rows.push(`<p>Connected as <b>@${escapeHtml(s.handle || "?")}</b>${s.needsReauth ? " — <b>login expired</b>, reconnect to resume syncing" : ""}.</p>`);
  if (s.last) {
    rows.push(`<p>${escapeHtml(describeRun(s.last))}</p>`);
    if (s.last.error) rows.push(`<div class="sync-err">${escapeHtml(s.last.error)}</div>`);
  }
  const busy = s.running ? " disabled" : "";
  const buttons = [];
  if (s.connected && !s.needsReauth) {
    buttons.push(`<button class="btn" data-act="sync"${busy}>Sync now</button>`);
    buttons.push(`<button class="btn" data-act="full"${busy}>Full sweep</button>`);
    buttons.push(`<button class="btn" data-act="media"${busy}>Retry failed files</button>`);
  }
  buttons.push(`<a class="btn" href="/auth/x/login">${s.connected ? "Reconnect X" : "Connect X"}</a>`);
  rows.push(`<div class="sync-actions">${buttons.join("")}</div>`);
  rows.push(`<p class="tm-note">Syncing checks only for new bookmarks. A full sweep re-reads everything X exposes (your most recent 800) — it refreshes counts and marks bookmarks that were removed from X. It runs automatically about once a week.</p>`);
  document.getElementById("syncBody").innerHTML = rows.join("");
}

function openSyncDialog(note) {
  syncDialogNote = note || "";
  syncDialog.hidden = false;
  renderSyncDialog();
}
function closeSyncDialog() { syncDialog.hidden = true; syncDialogNote = ""; }

syncInfo.addEventListener("click", () => openSyncDialog());
syncDialog.addEventListener("click", async e => {
  if (e.target.id === "syncDialog" || e.target.closest("#syncClose")) { closeSyncDialog(); return; }
  const btn = e.target.closest("[data-act]");
  if (!btn || btn.disabled) return;
  syncDialogNote = "";
  if (btn.dataset.act === "sync") startSync(true, null);
  else if (btn.dataset.act === "full") startSync(true, "full");
  else if (btn.dataset.act === "media") {
    try { await fetch("/api/sync/retry-media", { method: "POST" }); startSync(true, "incremental"); } catch {}
  }
  closeSyncDialog();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !syncDialog.hidden) closeSyncDialog();
});

// --- Filter menu: a dropdown on desktop, a top sheet on phones ------------------------
// The menu is relocated to <body> while open on a phone: the chip row scrolls
// horizontally (so it would clip an absolutely-positioned child) and the header's
// backdrop-filter would capture position:fixed. Anchored to the TOP of the screen,
// the filter input also stays clear of the on-screen keyboard.
{
  const dd = document.getElementById("tagDropdown");
  const menu = document.getElementById("tagMenu");
  dd.addEventListener("toggle", () => {
    if (MOBILE.matches && dd.open) {
      document.body.appendChild(menu);
      menu.classList.add("sheet");
      document.body.classList.add("sheet-open");
    } else {
      if (menu.parentElement !== dd) dd.appendChild(menu);
      menu.classList.remove("sheet");
      document.body.classList.remove("sheet-open");
    }
  });
}

// close tag dropdown when clicking outside
document.addEventListener("click", e => {
  const dd = document.getElementById("tagDropdown");
  if (!dd.open) return;
  // A click on a control inside the menu that re-rendered itself (e.g. delete
  // or restore a tag) leaves e.target detached; that's not an outside click, so
  // don't close the menu.
  if (!e.target.isConnected) return;
  if (!dd.contains(e.target) && !document.getElementById("tagMenu").contains(e.target)) dd.open = false;
});

// --- Phone ergonomics ------------------------------------------------------------------
// Hide the header while scrolling down, bring it back on any scroll up: the controls
// are tall, and a permanently pinned header eats a quarter of a phone screen.
{
  const bar = document.querySelector(".topbar");
  let last = window.scrollY, ticking = false;
  window.addEventListener("scroll", () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      const y = window.scrollY, dy = y - last;
      if (MOBILE.matches && !document.getElementById("tagDropdown").open && y > 0) {
        if (y > 120 && dy > 6) bar.classList.add("away");
        else if (dy < -6) bar.classList.remove("away");
      } else {
        bar.classList.remove("away");
      }
      last = y;
      ticking = false;
    });
  }, { passive: true });
}

// Size the full-screen deck to the *visible* viewport: on iOS the layout viewport
// ignores the on-screen keyboard, which would hide the tag input behind it.
if (window.visualViewport) {
  const setVvh = () => document.documentElement.style.setProperty("--vvh", visualViewport.height + "px");
  visualViewport.addEventListener("resize", setVvh);
  setVvh();
}

// Deck, by touch: swipe left/right between posts, plus on-screen buttons. The
// keyboard shortcuts above remain for desktop.
{
  const card = document.getElementById("deckCard");
  let x0 = 0, y0 = 0, t0 = 0, tracking = false;
  card.addEventListener("touchstart", e => {
    tracking = e.touches.length === 1 && e.target.tagName !== "VIDEO";
    if (tracking) { x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; t0 = Date.now(); }
  }, { passive: true });
  card.addEventListener("touchend", e => {
    if (!tracking) return;
    tracking = false;
    const t = e.changedTouches[0], dx = t.clientX - x0, dy = t.clientY - y0;
    if (Date.now() - t0 < 700 && Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.6) navDeck(dx < 0 ? 1 : -1);
  }, { passive: true });
  document.getElementById("deckPrev").addEventListener("click", () => navDeck(-1));
  document.getElementById("deckNext").addEventListener("click", () => navDeck(1));
  document.getElementById("deckDel").addEventListener("click", deleteCurrentDeckPost);
  document.getElementById("deckOpen").addEventListener("click", () => {
    const b = deckBookmark();
    if (b) window.open(b.url, "_blank", "noopener");
  });
}

// --- Boot -----------------------------------------------------------------------------
async function boot() {
  let body;
  try {
    const res = await fetch("/api/bootstrap", { cache: "no-store", redirect: "error" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    body = await res.json();
  } catch (err) {
    document.getElementById("timeline").innerHTML =
      `<div class="empty"><div class="big">Can't reach the server</div>` +
      `<p>Check your connection or sign-in, then try again.</p><button class="btn" id="retryBoot" type="button">Try again</button></div>`;
    document.getElementById("retryBoot").addEventListener("click", () => location.reload());
    return;
  }
  DATA = body.bookmarks;
  for (const b of DATA) byId.set(b.id, b);
  adoptState(body.state);
  document.getElementById("totalCount").textContent = DATA.length.toLocaleString();

  // Label the environment so the daily app and a dev build are easy to tell apart
  // when flipping browser tabs.
  const env = body.env || "prod";
  document.title = "okiedoke:" + env;
  const badge = document.getElementById("envBadge");
  badge.textContent = env;
  badge.classList.add(env === "prod" ? "prod" : "feat");
  badge.hidden = false;

  buildTagOptions();
  updateRestoreLink();
  setFilterExpr(state.filterExpr);  // compile any saved expression, then render
  renderSyncUI();
  if (queue.length) pump();         // deliver edits left over from a previous visit

  // Auto-sync on open: the server skips it inside the cooldown window, and a
  // run that is already in flight is just shown.
  const s = state.sync;
  const justConnected = new URLSearchParams(location.search).get("x") === "connected";
  if (justConnected) history.replaceState(null, "", location.pathname);
  if (s && s.configured && s.connected && !s.needsReauth) {
    if (s.running) { watching = true; pollSync(); }
    else startSync(justConnected);
  } else if (justConnected) {
    openSyncDialog("X connected, but the account status could not be read. Reload to continue.");
  }
}
boot();
