"""End-to-end UI behaviour, desktop and phone. Everything persisted is checked
against the server (a reload, or the API), never just against what is on screen."""
from __future__ import annotations

import json
import time
import urllib.request

import pytest


def eventually(fn, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError("condition not met in time")


def server_state(url):
    return json.load(urllib.request.urlopen(url + "/api/state"))


def shown(pg) -> int:
    return int(pg.inner_text("#shownCount").replace(",", ""))


def tags_on(pg, bid):
    return pg.eval_on_selector_all(f'article[data-id="{bid}"] .pill[data-tag]', "els => els.map(e => e.dataset.tag)")


def reload(pg):
    pg.reload()
    pg.wait_for_selector("article.tweet")


SYNC_BASE = {"configured": True, "connected": True, "handle": "harsh", "needsReauth": False, "running": False,
             "progress": None, "lastOkAt": "2099-01-01T00:00:00+00:00", "last": None}


def mock_sync(state):
    """Pretend the server reports this sync state (X can't be reached from tests)."""
    def setup(pg):
        def bootstrap(route):
            resp = route.fetch()
            body = resp.json()
            body["state"]["sync"] = {**SYNC_BASE, **state}
            route.fulfill(response=resp, body=json.dumps(body))
        pg.route("**/api/bootstrap", bootstrap)
        pg.route("**/api/sync/status", lambda r: r.fulfill(content_type="application/json",
                                                           body=json.dumps({**SYNC_BASE, **state})))
        pg.route("**/api/sync*", lambda r: r.fulfill(content_type="application/json",
                                                     body=json.dumps({"ok": True, "skipped": "cooldown"}))
                 if r.request.method == "POST" else r.fallback())
    return setup


# ------------------------------------------------------------------- desktop
def test_loads_cleanly_with_everything_from_postgres(desk):
    assert desk.inner_text("#totalCount") == "12" and shown(desk) == 11          # one post is hidden
    assert desk.inner_text("#restoreLink") == "Restore 1 deleted"
    assert tags_on(desk, "1001") == ["postgres", "buildinpublic"]                # order preserved
    assert desk.query_selector('article[data-id="1011"]') is None                # hidden stays hidden
    assert desk.query_selector('article[data-id="1010"] .pill.deleted-x') is not None   # gone from X
    assert desk.query_selector('article[data-id="1007"] .quote') is not None
    assert desk.errors == []


def test_add_tag_persists_and_first_spelling_wins(desk, app_url):
    desk.click('article[data-id="1004"] .addtag')
    desk.fill(".taginput", "Rust")
    desk.keyboard.press("Enter")
    assert tags_on(desk, "1004") == ["Rust"]
    eventually(lambda: server_state(app_url)["tags"].get("1004") == ["Rust"])

    desk.click('article[data-id="1008"] .addtag')
    desk.fill(".taginput", "rust")
    desk.keyboard.press("Enter")
    assert tags_on(desk, "1008") == ["Rust"]                                     # not "rust"
    eventually(lambda: server_state(app_url)["tags"].get("1008") == ["Rust"])
    reload(desk)
    assert tags_on(desk, "1004") == ["Rust"] and tags_on(desk, "1008") == ["Rust"]
    assert desk.errors == []


def test_autocomplete_suggests_existing_tags_most_recent_first(desk):
    desk.click('article[data-id="1004"] .addtag')
    opts = desk.eval_on_selector_all(".tagsuggest .opt .pill", "els => els.map(e => e.textContent)")
    assert opts[0] == "design"                                                   # last used in the sample
    desk.fill(".taginput", "prod")
    assert desk.eval_on_selector_all(".tagsuggest .opt .pill", "els => els.map(e => e.textContent)") == ["productivity"]
    desk.keyboard.press("Escape")
    desk.keyboard.press("Escape")
    assert tags_on(desk, "1004") == []


def test_remove_tag_persists(desk, app_url):
    desk.click('article[data-id="1001"] .pill[data-tag="postgres"] .rmtag')
    assert tags_on(desk, "1001") == ["buildinpublic"]
    eventually(lambda: server_state(app_url)["tags"]["1001"] == ["buildinpublic"])
    reload(desk)
    assert tags_on(desk, "1001") == ["buildinpublic"]


def test_hide_and_restore(desk, app_url):
    desk.click('article[data-id="1004"] .del')
    assert shown(desk) == 10 and desk.inner_text("#restoreLink") == "Restore 2 deleted"
    eventually(lambda: "1004" in server_state(app_url)["hidden"])
    reload(desk)
    assert shown(desk) == 10
    desk.click("#restoreLink")
    assert shown(desk) == 12
    eventually(lambda: server_state(app_url)["hidden"] == [])
    reload(desk)
    assert shown(desk) == 12 and desk.is_hidden("#restoreLink")


def test_search_and_boolean_filters(desk):
    desk.fill("#search", "carol")
    eventually(lambda: shown(desk) == 2)                                         # by author: 1003 and 1009
    desk.fill("#search", "")
    desk.click("#tagSummary")
    desk.fill("#filterExpr", "design and not essay")
    eventually(lambda: shown(desk) == 3)
    desk.fill("#filterExpr", "design or postgres")
    eventually(lambda: shown(desk) == 5)
    desk.fill("#filterExpr", "not (design or postgres)")
    eventually(lambda: shown(desk) == 6)
    desk.fill("#filterExpr", "(design")
    eventually(lambda: "bad" in (desk.get_attribute("#filterExpr", "class") or ""))
    assert shown(desk) == 6                                                      # bad syntax keeps the last good result
    desk.click("#filterUntagged")
    eventually(lambda: shown(desk) == 5)
    assert desk.errors == []


def test_saved_filter_roundtrip(desk, app_url):
    desk.click("#tagSummary")
    assert desk.inner_text(".sf .sfapply") == "Design inspo"                     # imported from the old tags file
    desk.fill("#filterExpr", "postgres")
    desk.fill("#filterName", "Pg")
    desk.click("#filterSaveBtn")
    eventually(lambda: [f["name"] for f in server_state(app_url)["savedFilters"]] == ["Design inspo", "Pg"])
    reload(desk)
    desk.click("#tagSummary")
    desk.click('.sf:has-text("Pg") .sfapply')
    eventually(lambda: shown(desk) == 2)
    desk.click('.sf:has-text("Pg") .sfdel')
    eventually(lambda: [f["name"] for f in server_state(app_url)["savedFilters"]] == ["Design inspo"])


def test_manage_tags_delete_restore(desk, app_url):
    desk.click("#manageTags")
    assert desk.inner_text("#tmTabActive") == "Active (7)"
    desk.click('.tm-row[data-tag="design"] .tm-act')
    assert desk.inner_text("#tmTabDeleted") == "Deleted (1)"
    desk.click("#tmClose")
    assert tags_on(desk, "1002") == []                                           # gone from every post
    eventually(lambda: sorted(server_state(app_url)["deletedTags"]) == ["design", "old-tag"])   # old-tag came from the import
    reload(desk)
    assert tags_on(desk, "1002") == []
    desk.click("#manageTags")
    desk.click("#tmTabDeleted")
    desk.click('.tm-row[data-tag="design"] .tm-act')
    desk.click("#tmClose")
    assert tags_on(desk, "1002") == ["design"]                                   # links were kept: lossless restore


def test_retagging_a_deleted_tag_revives_it(desk, app_url):
    desk.click("#manageTags")
    desk.click('.tm-row[data-tag="postgres"] .tm-act')
    desk.click("#tmClose")
    desk.click('article[data-id="1004"] .addtag')
    desk.fill(".taginput", "postgres")
    desk.keyboard.press("Enter")
    assert tags_on(desk, "1004") == ["postgres"] and tags_on(desk, "1001") == ["postgres", "buildinpublic"]
    eventually(lambda: server_state(app_url)["deletedTags"] == ["old-tag"])


def test_select_all_delete(desk, app_url):
    desk.click("#selectToggle")
    desk.click("#selAll")
    assert desk.inner_text("#selCount") == "11"
    desk.click("#selDelete")
    eventually(lambda: shown(desk) == 0)
    eventually(lambda: len(server_state(app_url)["hidden"]) == 12)
    desk.click("#restoreLink")
    assert shown(desk) == 12


def test_deck_keyboard_flow(desk, app_url):
    desk.click("#deckToggle")
    assert desk.inner_text("#deckCount") == "1 / 11"
    desk.keyboard.type("deckwork")
    desk.keyboard.press("Enter")
    assert "deckwork" in desk.inner_text("#deckPills")
    desk.keyboard.press("ArrowRight")
    assert desk.inner_text("#deckCount") == "2 / 11"
    desk.keyboard.press("ArrowLeft")
    desk.keyboard.press("Backspace")                                              # remove newest tag
    assert "deckwork" not in desk.inner_text("#deckPills")
    desk.keyboard.press("ArrowRight")
    desk.keyboard.press("Meta+Backspace")                                         # remove this post from view
    assert desk.inner_text("#deckCount") == "2 / 10"
    eventually(lambda: len(server_state(app_url)["hidden"]) == 2)
    desk.keyboard.press("Escape")                                                 # 1st Esc closes the suggestions...
    desk.keyboard.press("Escape")                                                 # ...2nd leaves the deck
    assert desk.is_hidden("#deck")
    assert desk.errors == []


def test_second_device_sees_changes_on_load(desk, new_page, app_url):
    desk.click('article[data-id="1004"] .addtag')
    desk.fill(".taginput", "shared")
    desk.keyboard.press("Enter")
    eventually(lambda: server_state(app_url)["tags"].get("1004") == ["shared"])
    other = new_page(viewport={"width": 1000, "height": 800})
    assert tags_on(other, "1004") == ["shared"]


def test_sync_buttons_hidden_without_x_credentials(desk):
    assert desk.is_hidden("#syncBtn") and desk.is_hidden("#syncInfo")


@pytest.mark.parametrize("state,button,info,bad", [
    ({"connected": False}, "Connect X", "Not connected", True),
    ({"needsReauth": True}, "Reconnect X", "X login expired", True),
    ({"last": {"status": "ok", "kind": "incremental", "fetched": 1, "inserted": 0, "markedMissing": 0,
               "mediaDownloaded": 0, "mediaFailed": 0, "error": None, "startedAt": "2099-01-01T00:00:00+00:00",
               "finishedAt": "2099-01-01T00:00:01+00:00"}}, "Sync", "Synced just now", False),
])
def test_sync_ui_states(new_page, state, button, info, bad):
    pg = new_page(setup=mock_sync(state), viewport={"width": 1000, "height": 800})
    pg.wait_for_function("!document.getElementById('syncBtn').hidden")
    assert pg.inner_text("#syncBtn") == button
    assert info in pg.inner_text("#syncInfo")
    assert ("bad" in (pg.get_attribute("#syncInfo", "class") or "")) == bad


def test_sync_progress_ring_and_error_dialog(new_page):
    running = {"running": True, "progress": 0.4,
               "last": {"status": "running", "kind": "full", "fetched": 40, "inserted": 0, "markedMissing": 0,
                        "mediaDownloaded": 0, "mediaFailed": 0, "error": None, "startedAt": "2099-01-01T00:00:00+00:00",
                        "finishedAt": None}}
    pg = new_page(setup=mock_sync(running), viewport={"width": 1000, "height": 800})
    pg.wait_for_function("document.getElementById('syncBtn').textContent.includes('40%')")
    assert pg.is_disabled("#syncBtn")

    failed = {"last": {"status": "error", "kind": "full", "fetched": 0, "inserted": 0, "markedMissing": 0,
                       "mediaDownloaded": 0, "mediaFailed": 0, "error": "X rejected the saved login (invalid_grant)",
                       "startedAt": "2099-01-01T00:00:00+00:00", "finishedAt": "2099-01-01T00:00:01+00:00"}}
    pg2 = new_page(setup=mock_sync(failed), viewport={"width": 1000, "height": 800})
    pg2.click("#syncInfo")
    assert "invalid_grant" in pg2.inner_text("#syncBody") and "Full sweep" in pg2.inner_text("#syncBody")
    pg2.keyboard.press("Escape")
    assert pg2.is_hidden("#syncDialog")


def test_old_app_look_is_preserved(desk):
    """The 600px X-style column, same palette and type as the old template."""
    shell_w = desk.eval_on_selector(".shell", "e => e.getBoundingClientRect().width")
    assert shell_w == 600
    assert desk.eval_on_selector("body", "e => getComputedStyle(e).color") == "rgb(15, 20, 25)"
    assert desk.eval_on_selector("a.time", "e => getComputedStyle(e).color") == "rgb(83, 100, 113)"


# --------------------------------------------------------------------- phone
def no_overflow(pg):
    return pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_phone_layout_has_no_horizontal_overflow_anywhere(phone):
    assert no_overflow(phone)
    phone.tap("#tagSummary")
    assert no_overflow(phone)
    phone.tap("#filterDone")
    phone.tap("#manageTags")
    assert no_overflow(phone)
    phone.tap("#tmClose")
    phone.tap("#deckToggle")
    assert no_overflow(phone)
    phone.tap("#deckClose")
    assert phone.errors == []


def test_phone_header_is_compact_and_chips_scroll_sideways(phone):
    assert phone.eval_on_selector(".topbar", "e => e.getBoundingClientRect().height") < 150
    assert phone.eval_on_selector(".chips", "e => e.scrollWidth > e.clientWidth")           # scrolls instead of wrapping


def test_phone_header_hides_on_scroll_down_and_returns(phone):
    phone.evaluate("window.scrollTo(0, 700)")
    phone.wait_for_timeout(60)
    phone.evaluate("window.scrollTo(0, 900)")
    eventually(lambda: phone.eval_on_selector(".topbar", "e => e.getBoundingClientRect().bottom") < 0)
    phone.evaluate("window.scrollTo(0, 700)")
    eventually(lambda: phone.eval_on_selector(".topbar", "e => e.getBoundingClientRect().bottom") > 100)


def test_phone_filter_sheet(phone):
    phone.tap("#tagSummary")
    eventually(lambda: phone.is_visible("#tagMenu.sheet"))
    assert phone.evaluate("document.body.classList.contains('sheet-open')")
    box = phone.eval_on_selector("#tagMenu", "e => { const r = e.getBoundingClientRect(); return [r.top, r.left, r.right]; }")
    assert box[0] == 0 and box[1] == 0 and box[2] == 390                                    # top-anchored, full width
    phone.fill("#filterExpr", "design")
    eventually(lambda: shown(phone) == 3)
    phone.tap("#filterDone")
    eventually(lambda: not phone.evaluate("document.body.classList.contains('sheet-open')"))
    assert phone.evaluate("document.getElementById('tagMenu').parentElement.id") == "tagDropdown"   # menu goes home
    phone.tap("#tagSummary")                                                                  # and reopens fine
    eventually(lambda: phone.is_visible("#tagMenu.sheet"))
    phone.tap("body", position={"x": 195, "y": 800})                                          # tap the dimmed backdrop
    eventually(lambda: not phone.evaluate("document.getElementById('tagDropdown').open"))


def test_phone_add_tag_by_tapping_a_suggestion(phone, app_url):
    phone.tap('article[data-id="1004"] .addtag')
    phone.tap(".tagsuggest .opt >> nth=0")
    eventually(lambda: tags_on(phone, "1004") == ["design"])
    eventually(lambda: server_state(app_url)["tags"].get("1004") == ["design"])


def test_phone_deck_taps_and_swipes(phone, app_url):
    phone.tap("#deckToggle")
    assert phone.inner_text("#deckCount") == "1 / 11"
    assert phone.is_visible("#deckPrev") and phone.is_hidden(".deck-hints")
    assert not phone.evaluate("document.activeElement === document.getElementById('deckInput')")   # no keyboard pop-up
    phone.tap("#deckMenu .opt >> nth=0")                                                     # tag by tapping a suggestion
    eventually(lambda: "design" in phone.inner_text("#deckPills"))
    phone.tap("#deckNext")
    assert phone.inner_text("#deckCount") == "2 / 11"

    cdp = phone.context.new_cdp_session(phone.page)

    def swipe(x0, x1, y=400):
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x0, "y": y}]})
        for i in range(1, 6):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x0 + (x1 - x0) * i / 5, "y": y}]})
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    swipe(300, 60)                                                                           # swipe left = next
    eventually(lambda: phone.inner_text("#deckCount") == "3 / 11")
    swipe(60, 300)                                                                           # swipe right = previous
    eventually(lambda: phone.inner_text("#deckCount") == "2 / 11")
    phone.tap("#deckDel")                                                                    # remove from view
    eventually(lambda: phone.inner_text("#deckCount") == "2 / 10")
    eventually(lambda: len(server_state(app_url)["hidden"]) == 2)
    phone.tap("#deckClose")
    assert phone.errors == []


def test_phone_touch_targets_and_input_sizes(phone):
    def box(sel):
        return phone.eval_on_selector(sel, "e => { const r = e.getBoundingClientRect(); return [r.width, r.height]; }")
    assert box("article .del")[1] >= 40 and box("article .del")[0] >= 40
    assert box("#selectToggle")[1] >= 40 and box("#deckToggle")[1] >= 40
    assert min(box("article .pill .rmtag")) >= 24                                            # WCAG 2.2 AA minimum
    assert box("article .addtag")[1] >= 30
    for sel in ("#search",):
        assert phone.eval_on_selector(sel, "e => parseFloat(getComputedStyle(e).fontSize)") >= 16   # iOS zooms below 16px
    phone.tap("#tagSummary")
    for sel in ("#filterExpr", "#filterName"):
        assert phone.eval_on_selector(sel, "e => parseFloat(getComputedStyle(e).fontSize)") >= 16
    phone.tap("#filterDone")
    phone.tap("#manageTags")
    assert phone.eval_on_selector("#tmSearch", "e => parseFloat(getComputedStyle(e).fontSize)") >= 16
    assert not phone.evaluate("document.activeElement === document.getElementById('tmSearch')")


def test_phone_edits_made_offline_are_delivered_when_back_online(phone, app_url):
    phone.context.set_offline(True)
    phone.tap('article[data-id="1004"] .addtag')
    phone.fill(".taginput", "offlinetag")
    phone.keyboard.press("Enter")
    assert tags_on(phone, "1004") == ["offlinetag"]                                          # shown immediately
    eventually(lambda: "Offline" in phone.inner_text("#saveStat"))
    assert "offlinetag" not in json.dumps(server_state(app_url)["tags"])
    phone.context.set_offline(False)
    phone.evaluate("window.dispatchEvent(new Event('online'))")
    eventually(lambda: server_state(app_url)["tags"].get("1004") == ["offlinetag"])
    eventually(lambda: phone.inner_text("#saveStat") in ("", "✓ Saved"))


def test_phone_queued_edits_survive_a_reload(phone, app_url):
    phone.route("**/api/bookmarks/*/tags", lambda r: r.abort())                              # server unreachable for writes
    phone.tap('article[data-id="1004"] .addtag')
    phone.fill(".taginput", "survivor")
    phone.keyboard.press("Enter")
    eventually(lambda: "Offline" in phone.inner_text("#saveStat"))
    reload(phone)
    assert tags_on(phone, "1004") == ["survivor"]                                            # replayed on top of server state
    assert server_state(app_url)["tags"].get("1004") is None
    phone.unroute("**/api/bookmarks/*/tags")
    phone.evaluate("window.dispatchEvent(new Event('online'))")
    eventually(lambda: server_state(app_url)["tags"].get("1004") == ["survivor"])


def test_phone_permanent_rejections_do_not_loop_forever(phone, app_url):
    phone.route("**/api/bookmarks/1004/tags", lambda r: r.fulfill(status=404, content_type="application/json",
                                                                  body='{"detail":"bookmark not found"}'))
    phone.tap('article[data-id="1004"] .addtag')
    phone.fill(".taginput", "ghost")
    phone.keyboard.press("Enter")
    eventually(lambda: phone.evaluate("JSON.parse(localStorage.getItem('okiedokeQueue') || '[]').length === 0"))
    eventually(lambda: tags_on(phone, "1004") == [])                                          # screen re-synced to the server
