"""Turn an X API v2 bookmarks page into plain records. Pure functions, no I/O."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime

_STATUS_RE = re.compile(r"/status/(\d+)")
_MEDIA_LINK_RE = re.compile(r"/(photo|video)/\d+/?$")


@dataclass
class ParsedAuthor:
    x_user_id: str
    handle: str
    name: str
    avatar_url: str | None


@dataclass
class ParsedMedia:
    key: str
    position: int
    kind: str                 # image | video
    source_url: str
    width: int | None = None
    height: int | None = None


@dataclass
class ParsedTweet:
    id: str
    text: str
    url: str
    posted_at: datetime | None
    likes: int
    reposts: int
    bookmarks: int
    replies: int
    author: ParsedAuthor | None
    in_reply_to_tweet_id: str | None
    reply_parent_handle: str | None
    quoted_tweet_id: str | None
    media: list[ParsedMedia] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class ParsedPage:
    bookmarks: list[ParsedTweet]      # API order (most recently bookmarked first)
    quoted: list[ParsedTweet]         # tweets quoted by a bookmark (stored, but not bookmarks)
    next_token: str | None


def expand_text(text: str, entities: dict | None, drop_status_ids: frozenset[str] = frozenset()) -> str:
    """Replace t.co links with their targets and drop the trailing links X appends
    for attached media and the quoted tweet. Indices are in code points of the
    text as the API returned it (still HTML-escaped), so substitute first and
    unescape last."""
    chars = list(text)
    urls = (entities or {}).get("urls") or []
    for u in sorted(urls, key=lambda u: u.get("start", 0), reverse=True):
        s, e = u.get("start"), u.get("end")
        if not isinstance(s, int) or not isinstance(e, int) or not (0 <= s < e <= len(chars)):
            continue
        target = u.get("expanded_url") or u.get("url") or ""
        m = _STATUS_RE.search(target)
        if u.get("media_key") or _MEDIA_LINK_RE.search(target) or (m and m.group(1) in drop_status_ids):
            target = ""
        chars[s:e] = list(target)
    return html.unescape("".join(chars)).strip()


def _tweet_text(t: dict, drop_ids: frozenset[str]) -> str:
    note = t.get("note_tweet")
    if isinstance(note, dict) and note.get("text"):
        return expand_text(note["text"], note.get("entities"), drop_ids)
    return expand_text(t.get("text") or "", t.get("entities"), drop_ids)


def _best_video_url(variants: list[dict]) -> str | None:
    mp4 = [v for v in variants or [] if v.get("content_type") == "video/mp4" and v.get("url")]
    if not mp4:
        return None
    return max(mp4, key=lambda v: v.get("bit_rate") or 0)["url"]


def _media_for(t: dict, media_by_key: dict[str, dict]) -> list[ParsedMedia]:
    out: list[ParsedMedia] = []
    for key in (t.get("attachments") or {}).get("media_keys") or []:
        m = media_by_key.get(key)
        if not m:
            continue
        kind_x = m.get("type")
        if kind_x == "photo" and m.get("url"):
            kind, src = "image", m["url"] + ("?name=large" if "?" not in m["url"] else "")
        elif kind_x in ("video", "animated_gif"):
            src = _best_video_url(m.get("variants"))
            if src:
                kind = "video"
            elif m.get("preview_image_url"):
                kind, src = "image", m["preview_image_url"]
            else:
                continue
        else:
            continue
        out.append(ParsedMedia(key=key, position=len(out), kind=kind, source_url=src,
                               width=m.get("width"), height=m.get("height")))
    return out


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def avatar_url(profile_image_url: str | None) -> str | None:
    # The API returns the 48px "_normal" rendition; "_bigger" (73px) stays sharp on retina.
    return profile_image_url.replace("_normal.", "_bigger.") if profile_image_url else None


def parse_page(payload: dict) -> ParsedPage:
    inc = payload.get("includes") or {}
    users = {u["id"]: u for u in inc.get("users") or []}
    media_by_key = {m["media_key"]: m for m in inc.get("media") or [] if m.get("media_key")}
    included = {t["id"]: t for t in inc.get("tweets") or []}

    def author_of(t: dict) -> ParsedAuthor | None:
        u = users.get(t.get("author_id"))
        if not u:
            return None
        return ParsedAuthor(x_user_id=u["id"], handle=u.get("username") or "", name=u.get("name") or "",
                            avatar_url=avatar_url(u.get("profile_image_url")))

    def build(t: dict, quoted_id: str | None, drop: frozenset[str], reply_handle: str | None,
              reply_to: str | None) -> ParsedTweet:
        a = author_of(t)
        pm = t.get("public_metrics") or {}
        return ParsedTweet(
            id=t["id"],
            text=_tweet_text(t, drop),
            url=f"https://x.com/{a.handle}/status/{t['id']}" if a and a.handle else f"https://x.com/i/status/{t['id']}",
            posted_at=_parse_dt(t.get("created_at")),
            likes=int(pm.get("like_count") or 0),
            reposts=int(pm.get("retweet_count") or 0) + int(pm.get("quote_count") or 0),
            bookmarks=int(pm.get("bookmark_count") or 0),
            replies=int(pm.get("reply_count") or 0),
            author=a,
            in_reply_to_tweet_id=reply_to,
            reply_parent_handle=reply_handle,
            quoted_tweet_id=quoted_id,
            media=_media_for(t, media_by_key),
            raw=t,
        )

    bookmarks: list[ParsedTweet] = []
    quoted: dict[str, ParsedTweet] = {}
    for t in payload.get("data") or []:
        refs = {r.get("type"): r.get("id") for r in t.get("referenced_tweets") or []}
        quoted_id = refs.get("quoted")
        reply_to = refs.get("replied_to")
        reply_handle = None
        if reply_to:
            parent = included.get(reply_to)
            pu = users.get((parent or {}).get("author_id")) or users.get(t.get("in_reply_to_user_id"))
            reply_handle = (pu or {}).get("username")
        bookmarks.append(build(t, quoted_id, frozenset({quoted_id}) if quoted_id else frozenset(),
                               reply_handle, reply_to))
        if quoted_id and quoted_id in included and quoted_id not in quoted:
            quoted[quoted_id] = build(included[quoted_id], None, frozenset(), None, None)

    return ParsedPage(bookmarks=bookmarks, quoted=list(quoted.values()),
                      next_token=(payload.get("meta") or {}).get("next_token"))
