"""A synthetic ft data directory (sqlite + manifest + media + tags) used by the UI tests.

    python -m tests.sample_ft /tmp/ft_sample      # then: python -m app.import_ft /tmp/ft_sample
"""
import json
import random
import shutil
import sqlite3
import sys
from pathlib import Path


def build(out: Path) -> Path:
    shutil.rmtree(out, ignore_errors=True)
    (out / "media").mkdir(parents=True)
    random.seed(7)
    _build(out)
    return out


def _build(out: Path) -> None:
    def svg(name, w, h, hue, label):
        (out / "media" / name).write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<rect width="100%" height="100%" fill="hsl({hue},60%,70%)"/>'
            f'<circle cx="{w*0.3}" cy="{h*0.4}" r="{min(w,h)*0.18}" fill="hsl({(hue+40)%360},70%,45%)"/>'
            f'<text x="50%" y="88%" font-size="{h*0.09}" text-anchor="middle" fill="#fff" font-family="sans-serif">{label}</text></svg>')

    authors = {"alice": "Alice Chen", "bob": "Bob Martin", "carol": "Carol Díaz", "dave": "Dave", "erin": "Erin Okafor",
               "frank_the_very_long_handle": "Frank The Extremely Long Display Name Person"}
    for i, h in enumerate(["alice", "bob", "carol"]):
        svg(f"av_{h}.svg", 80, 80, i * 110, h[0].upper())

    LOREM = ("Shipping a small thing every day compounds. The trick is making the unit of work small enough that "
             "you never need motivation to start. ")
    long_text = (LOREM * 6).strip()
    posts = [
        ("1001", "alice", "Just shipped a tiny Postgres-backed app. #buildinpublic https://example.com/post @bob thoughts?", 0, None, None),
        ("1002", "bob", "One image post.\nWith a second line.", 1, None, None),
        ("1003", "carol", "Two images side by side", 2, None, None),
        ("1004", "dave", "Three images: one tall, two stacked", 3, None, None),
        ("1005", "erin", "Four images in a grid. " + LOREM, 4, None, None),
        ("1006", "alice", long_text, 0, None, None),
        ("1007", "frank_the_very_long_handle", "Quote tweeting something with a picture", 0, "2001", None),
        ("1008", "bob", "A reply to somebody else", 0, None, "7001"),
        ("1009", "carol", "Plain short post, no media.", 0, None, None),
        ("1010", "dave", "Old one that vanished from X", 1, None, None),
        ("1011", "erin", "Lots of likes\n\n\n\nand blank lines", 0, None, None),
        ("1012", "alice", "Another one for the pile #tags", 2, None, None),
    ]
    conn = sqlite3.connect(out / "bookmarks.db")
    conn.execute("""CREATE TABLE bookmarks (
        id TEXT PRIMARY KEY, url TEXT, text TEXT, author_handle TEXT, author_name TEXT, posted_at TEXT,
        like_count INTEGER, repost_count INTEGER, bookmark_count INTEGER, tags_json TEXT, links_json TEXT,
        quoted_tweet_json TEXT, quoted_status_id TEXT, in_reply_to_status_id TEXT, synced_at TEXT, primary_category TEXT)""")
    entries = []
    for i, (pid, h, text, nimg, quote, reply) in enumerate(posts):
        day = 28 - i
        quoted_json = None
        if quote:
            quoted_json = json.dumps({"authorHandle": "carol", "authorName": "Carol Díaz", "text": "The original, quoted post text.",
                                      "url": f"https://x.com/carol/status/{quote}"})
        synced = "2020-01-01T00:00:00Z" if pid == "1010" else "2030-01-01T00:00:00Z"
        conn.execute("INSERT INTO bookmarks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            pid, f"https://x.com/{h}/status/{pid}", text, h, authors[h], f"Mon Sep {day:02d} 10:{i:02d}:00 +0000 2025",
            random.choice([0, 7, 120, 4300, 15200, 2_300_000]), random.choice([0, 3, 88, 1500]), random.choice([0, 2, 40]),
            None, None, quoted_json, quote, reply, synced, None))
        sizes = {1: [(900, 500)], 2: [(600, 600)] * 2, 3: [(600, 900), (600, 400), (600, 400)], 4: [(600, 400)] * 4}
        for k, (w, hgt) in enumerate(sizes.get(nimg, [])):
            name = f"{pid}_{k}.svg"
            svg(name, w, hgt, (i * 37 + k * 20) % 360, f"{pid} #{k+1}")
            entries.append({"status": "downloaded", "localPath": f"{out}/media/{name}", "tweetId": pid,
                            "sourceUrl": f"https://pbs.twimg.com/media/{pid}{k}.jpg"})
    svg("q_2001.svg", 600, 340, 200, "quoted media")
    entries.append({"status": "downloaded", "localPath": f"{out}/media/q_2001.svg", "tweetId": "2001",
                    "sourceUrl": "https://pbs.twimg.com/media/q.jpg"})
    for h in ["alice", "bob", "carol"]:
        entries.append({"status": "downloaded", "localPath": f"{out}/media/av_{h}.svg", "authorHandle": h,
                        "sourceUrl": f"https://pbs.twimg.com/profile_images/1/{h}.jpg"})
    conn.commit()
    (out / "media-manifest.json").write_text(json.dumps({"entries": entries}))
    (out / "last-full-sync.json").write_text(json.dumps({"startedAt": "2025-01-01T00:00:00Z"}))
    (out / "okiedoke-tags.json").write_text(json.dumps({
        "version": 1,
        "customTags": {"1001": ["postgres", "buildinpublic"], "1002": ["design"], "1003": ["design", "inspo"],
                       "1005": ["design"], "1006": ["essay", "productivity", "a-rather-long-tag-name-that-wraps-around"],
                       "1009": ["postgres"]},
        "removedTags": {}, "globalRemovedTags": ["old-tag"], "hiddenBookmarks": ["1011"],
        "tagLastUsed": {"design": 1_759_000_000_000, "postgres": 1_758_000_000_000},
        "savedFilters": [{"name": "Design inspo", "expr": "design and not essay"}]}))


if __name__ == "__main__":
    print("sample at", build(Path(sys.argv[1])))
