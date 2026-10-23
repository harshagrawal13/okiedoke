from app.xparse import expand_text, parse_page


def test_expand_text_replaces_tco_and_drops_media_and_quote_links():
    text = "Look at this https://t.co/aaa and https://t.co/bbb https://t.co/ccc https://t.co/ddd"
    entities = {"urls": [
        {"start": 13, "end": 27, "url": "https://t.co/aaa", "expanded_url": "https://example.com/post"},
        {"start": 32, "end": 46, "url": "https://t.co/bbb", "expanded_url": "https://x.com/alice/status/1/photo/1"},
        {"start": 47, "end": 61, "url": "https://t.co/ccc", "expanded_url": "https://x.com/bob/status/99"},
        {"start": 62, "end": 76, "url": "https://t.co/ddd", "expanded_url": "https://x.com/z/status/5", "media_key": "3_1"},
    ]}
    # recompute offsets from the real string so the test can't drift from the fixture
    for u in entities["urls"]:
        i = text.index(u["url"])
        u["start"], u["end"] = i, i + len(u["url"])
    out = expand_text(text, entities, frozenset({"99"}))
    assert out == "Look at this https://example.com/post and"


def test_expand_text_unescapes_html_after_substitution():
    assert expand_text("Tom &amp; Jerry &lt;3", None) == "Tom & Jerry <3"


def test_expand_text_ignores_bad_indices():
    entities = {"urls": [{"start": 50, "end": 60, "url": "x", "expanded_url": "y"}]}
    assert expand_text("short", entities) == "short"


def _payload():
    return {
        "data": [{
            "id": "10", "text": "hi https://t.co/q", "author_id": "u1", "created_at": "2024-05-01T12:34:56.000Z",
            "public_metrics": {"like_count": 5, "retweet_count": 2, "quote_count": 3, "bookmark_count": 1, "reply_count": 4},
            "referenced_tweets": [{"type": "quoted", "id": "20"}, {"type": "replied_to", "id": "30"}],
            "in_reply_to_user_id": "u3",
            "entities": {"urls": [{"start": 3, "end": 17, "url": "https://t.co/q", "expanded_url": "https://x.com/bob/status/20"}]},
            "attachments": {"media_keys": ["m1", "m2", "m3"]},
        }],
        "includes": {
            "users": [
                {"id": "u1", "username": "alice", "name": "Alice", "profile_image_url": "https://pbs.twimg.com/profile_images/1/a_normal.jpg"},
                {"id": "u2", "username": "bob", "name": "Bob"},
                {"id": "u3", "username": "carol", "name": "Carol"},
            ],
            "tweets": [{"id": "20", "text": "quoted", "author_id": "u2", "created_at": "2024-04-01T00:00:00.000Z"}],
            "media": [
                {"media_key": "m1", "type": "photo", "url": "https://pbs.twimg.com/media/A.jpg"},
                {"media_key": "m2", "type": "video", "preview_image_url": "https://pbs.twimg.com/p.jpg", "variants": [
                    {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/a.m3u8"},
                    {"bit_rate": 1, "content_type": "video/mp4", "url": "https://video.twimg.com/lo.mp4"},
                    {"bit_rate": 9, "content_type": "video/mp4", "url": "https://video.twimg.com/hi.mp4"}]},
                {"media_key": "m3", "type": "animated_gif", "variants": [
                    {"bit_rate": 0, "content_type": "video/mp4", "url": "https://video.twimg.com/gif.mp4"}]},
            ],
        },
        "meta": {"result_count": 1, "next_token": "abc"},
    }


def test_parse_page_full_record():
    page = parse_page(_payload())
    assert page.next_token == "abc"
    (t,) = page.bookmarks
    assert (t.id, t.url) == ("10", "https://x.com/alice/status/10")
    assert t.text == "hi"                                   # quote link dropped
    assert (t.likes, t.reposts, t.bookmarks, t.replies) == (5, 5, 1, 4)   # reposts = RT + quotes
    assert t.author.avatar_url == "https://pbs.twimg.com/profile_images/1/a_bigger.jpg"
    assert t.quoted_tweet_id == "20" and t.in_reply_to_tweet_id == "30"
    assert t.reply_parent_handle == "carol"
    assert [(m.kind, m.source_url) for m in t.media] == [
        ("image", "https://pbs.twimg.com/media/A.jpg?name=large"),
        ("video", "https://video.twimg.com/hi.mp4"),       # highest bitrate mp4, never the m3u8
        ("video", "https://video.twimg.com/gif.mp4"),
    ]
    assert [m.position for m in t.media] == [0, 1, 2]
    (q,) = page.quoted
    assert q.id == "20" and q.author.handle == "bob" and q.text == "quoted"


def test_parse_page_prefers_note_tweet_for_long_posts():
    p = {"data": [{"id": "1", "text": "truncated…", "author_id": "u1",
                   "note_tweet": {"text": "the full long text &amp; more"}}],
         "includes": {"users": [{"id": "u1", "username": "a", "name": "A"}]}}
    assert parse_page(p).bookmarks[0].text == "the full long text & more"


def test_parse_page_tolerates_missing_author_and_empty_page():
    p = {"data": [{"id": "1", "text": "orphan", "author_id": "gone"}], "meta": {"result_count": 1}}
    t = parse_page(p).bookmarks[0]
    assert t.author is None and t.url == "https://x.com/i/status/1"
    assert parse_page({"meta": {"result_count": 0}}).bookmarks == []
