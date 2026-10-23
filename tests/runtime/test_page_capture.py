"""页面取数通道：规则匹配、来源约束、到达序号、读体时机、图片窗口、状态读取与状态哨兵。"""

from __future__ import annotations

import asyncio
from hashlib import sha256
import json

import pytest

from support.page_events import FakeContext, FakePage, FakeRequest, ServiceWorkerFrame
from trippostcollect.runtime.page_capture import (
    CaptureChannelError,
    CaptureFailure,
    CaptureRule,
    CapturedResponse,
    ImageWindow,
    PageCaptureChannel,
    SentinelRule,
    image_match_key,
    route_of,
)
from trippostcollect.runtime.visit_ledger import VisitLedger, line_seq, load_ledger


SEARCH = CaptureRule(
    name="t.search", stage="search_results", method="POST", host="so.example.com",
    path="/api/search/notes", resource_types=frozenset({"xhr", "fetch"}), body="json",
)
DETAIL = CaptureRule(
    name="t.detail", stage="note_detail", method="POST", host="edith.example.com",
    path="/api/note/<kind>/feed", resource_types=frozenset({"xhr", "fetch"}), body="json",
)
IMAGE = CaptureRule(
    name="t.image", stage="note_image", method="GET", host="*.cdn.example.com", path=None,
    resource_types=frozenset({"image"}), body="bytes", host_exclude_prefixes=("avatar",),
)
SENTINEL = SentinelRule(
    host="*.example.com", resource_types=frozenset({"xhr", "fetch"}),
    statuses=frozenset({401, 403, 429, 461, 471}), surface_check_statuses=frozenset({461, 471}),
)
SEARCH_URL = "https://so.example.com/api/search/notes"
FEED_URL = "https://edith.example.com/api/note/web/feed"
PRIMARY_URL = "https://www.example.com/search_result_ai?keyword=%25E9%259D%2592&source=web"


def clock() -> str:
    return "2026-10-11T00:00:00+00:00"


def build(tmp_path, *, sentinel=SENTINEL, state_scripts=None, max_image_handles=64):
    context = FakeContext()
    primary = FakePage(PRIMARY_URL)
    context.pages.append(primary)
    ledger = VisitLedger.create(tmp_path / "ledger.jsonl", platform="xhs", mode="discovery", clock=clock)
    channel = PageCaptureChannel(
        context, platform="xhs", rules=[SEARCH, DETAIL, IMAGE], ledger=ledger, sentinel=sentinel,
        verify_headers=("Verifytype", "Verifyuuid"), state_scripts=state_scripts or {}, clock=clock,
        max_image_handles=max_image_handles,
    )
    channel.install(primary)
    return context, primary, ledger, channel


def search_request(page, page_no=1, **kwargs):
    return page.request("POST", SEARCH_URL, post_data_json={"page": page_no, "search_id": "s1"}, **kwargs)


def body(payload) -> bytes:
    return json.dumps(payload).encode()


def ledger_lines(ledger):
    return list(load_ledger(ledger.path).lines)


def test_rule_matching_is_exact_on_method_host_path_and_resource_type():
    assert SEARCH.matches("POST", SEARCH_URL, "xhr")
    assert SEARCH.matches("post", SEARCH_URL + "?x=1", "fetch")
    assert not SEARCH.matches("GET", SEARCH_URL, "xhr")
    assert not SEARCH.matches("POST", SEARCH_URL, "document")
    assert not SEARCH.matches("POST", "https://edith.example.com/api/search/notes", "xhr")
    assert not SEARCH.matches("POST", SEARCH_URL + "/more", "xhr")
    assert DETAIL.matches("POST", FEED_URL, "xhr")
    assert not DETAIL.matches("POST", "https://edith.example.com/api/note/a/b/feed", "xhr")
    assert IMAGE.matches("GET", "https://img-1.cdn.example.com/a/b!webp", "image")
    assert not IMAGE.matches("GET", "https://cdn.example.com/a", "image")
    assert not IMAGE.matches("GET", "https://avatar-1.cdn.example.com/a", "image")
    assert not IMAGE.matches("GET", "https://img.cdn.example.com.evil.test/a", "image")
    assert SEARCH.endpoint(SEARCH_URL + "?x=1") == "POST so.example.com /api/search/notes"
    assert DETAIL.endpoint(FEED_URL) == "POST edith.example.com /api/note/<kind>/feed"


def test_route_template_keeps_only_path_shape_and_key_names():
    assert route_of(PRIMARY_URL) == ("/search_result_ai", ("keyword", "source"))
    assert route_of("https://www.example.com/explore/64f1a2b3c4d5e6f7a8b9c0d1?xsec_token=abc&xsec_source=pc") == (
        "/explore/<id>", ("xsec_source", "xsec_token"),
    )
    assert route_of("https://www.example.com/search/%E9%9D%92") == ("/search/<value>", ())
    assert route_of("https://www.example.com/user/profile/1234567") == ("/user/profile/<id>", ())


@pytest.mark.asyncio
async def test_json_body_is_read_at_arrival_and_waiters_receive_parsed_payloads(tmp_path):
    _, primary, ledger, channel = build(tmp_path)
    trigger = channel.action("press_enter", page_role="primary", target={})
    response = primary.respond(search_request(primary), body=body({"data": {"has_more": True, "items": []}}))
    await asyncio.sleep(0)
    assert response.body_calls == 1  # 无人等待也立即读体，避免缓冲淘汰
    captured = await channel.wait_for("t.search", after_seq=line_seq(trigger), timeout=1)
    assert isinstance(captured, CapturedResponse)
    assert captured.seq == 2 and captured.page_role == "primary"
    assert captured.json == {"data": {"has_more": True, "items": []}}
    assert captured.request_json == {"page": 1, "search_id": "s1"}
    assert captured.route == "/search_result_ai" and captured.query_keys == ("keyword", "source")
    assert captured.endpoint == "POST so.example.com /api/search/notes"
    assert channel.latest("t.search") is captured
    assert await channel.wait_for("t.search", after_seq=captured.seq, timeout=0.01) is None


@pytest.mark.asyncio
async def test_foreign_pages_and_service_workers_are_dropped(tmp_path):
    _, primary, _, channel = build(tmp_path)
    foreign = FakePage("https://www.example.com/explore")
    primary.respond(search_request(primary, frame_page=foreign), body=body({}))
    sw_request = FakeRequest(method="POST", url=SEARCH_URL, resource_type="fetch", frame=ServiceWorkerFrame())
    primary.respond(sw_request, body=body({}))
    foreign.respond(search_request(foreign), body=body({}))
    await asyncio.sleep(0)
    assert channel.dropped_responses == 2
    assert channel.drain("t.search", after_seq=0) == []
    assert channel.ledger.last_seq == 0


@pytest.mark.asyncio
async def test_capture_seq_is_assigned_at_arrival_before_body_read(tmp_path):
    _, primary, ledger, channel = build(tmp_path)
    click = channel.action("click_card", page_role="primary", target={"note_id": "n1"})
    slow = primary.respond(primary.request("POST", FEED_URL, post_data_json={"source_note_id": "n1"}),
                           body=body({"ok": 1}), delayed=True)
    start = channel.pause_started()
    pause = channel.pause_finished("note_detail", start, 4.2)
    fast = primary.respond(primary.request("POST", FEED_URL, post_data_json={"source_note_id": "n2"}),
                           body=body({"ok": 2}))
    await asyncio.sleep(0)
    # 先到但读体慢的响应结算前，后到的响应不能越过它被返回。
    waiter = asyncio.create_task(channel.wait_for("t.detail", after_seq=line_seq(click), timeout=1))
    await asyncio.sleep(0.01)
    assert not waiter.done()
    slow.release()
    first = await waiter
    assert first.json == {"ok": 1} and first.seq == 2
    second = await channel.wait_for("t.detail", after_seq=first.seq, timeout=1)
    assert second.json == {"ok": 2} and second.seq == 5
    assert (line_seq(click), start, line_seq(pause)) == (1, 3, 4)
    assert fast.body_calls == 1
    visit = channel.record_visit(first, stage="note_detail", subject_id="n1", trigger=click)
    assert line_seq(visit) == 2
    lines = ledger_lines(ledger)
    assert [line["kind"] for line in lines] == ["action", "pause", "visit"]
    assert [line["seq"] for line in lines] == [1, 4, 2]
    assert lines[2]["trigger"] == click and lines[2]["route"] == "/search_result_ai"
    with pytest.raises(ValueError):
        channel.record_visit(first, stage="note_detail", subject_id="n1", trigger=click)


@pytest.mark.asyncio
async def test_predicate_and_request_failures(tmp_path):
    _, primary, _, channel = build(tmp_path)
    primary.respond(primary.request("POST", FEED_URL, post_data_json={"source_note_id": "other"}), body=body({}))
    failing = primary.request("POST", FEED_URL, post_data_json={"source_note_id": "n1"})
    primary.fail(failing, "net::ERR_INTERNET_DISCONNECTED")
    primary.fail(primary.request("GET", "https://www.example.com/api/unrelated"), "net::ERR_FAILED")
    result = await channel.wait_for(
        "t.detail", after_seq=0, timeout=1,
        predicate=lambda item: (item.request_json or {}).get("source_note_id") == "n1",
    )
    assert isinstance(result, CaptureFailure)
    assert result.failure_text == "net::ERR_INTERNET_DISCONNECTED"
    assert result.endpoint == "POST edith.example.com /api/note/<kind>/feed"
    assert [type(item).__name__ for item in channel.drain("t.detail", after_seq=0)] == [
        "CapturedResponse", "CaptureFailure",
    ]


@pytest.mark.asyncio
async def test_unreadable_json_body_is_kept_as_status_only(tmp_path):
    _, primary, _, channel = build(tmp_path)
    primary.respond(search_request(primary), status=461, headers={"Verifytype": "1"}, body=b"<html>")
    captured = await channel.wait_for("t.search", after_seq=0, timeout=1)
    assert captured.status == 461 and captured.json is None and captured.body_error == "JSONDecodeError"
    assert captured.verify_headers_present is True


@pytest.mark.asyncio
async def test_image_window_holds_handles_and_reads_only_expected_urls(tmp_path):
    _, primary, ledger, channel = build(tmp_path)
    outside = primary.respond(primary.request("GET", "https://img.cdn.example.com/early", resource_type="image"),
                              body=b"early")
    window = channel.open_image_window("primary")
    click = channel.action("click_card", page_role="primary", target={"note_id": "n1"})
    first = primary.respond(primary.request("GET", "https://img.cdn.example.com/1", resource_type="image"),
                            body=b"one")
    cover = primary.respond(primary.request("GET", "https://img.cdn.example.com/cover", resource_type="image"),
                            body=b"cover")
    avatar = primary.respond(primary.request("GET", "https://avatar.cdn.example.com/a", resource_type="image"),
                             body=b"avatar")
    await asyncio.sleep(0)
    assert (outside.body_calls, first.body_calls, cover.body_calls, avatar.body_calls) == (0, 0, 0, 0)
    assert window.held_count == 2
    window.expect(
        {"https://img.cdn.example.com/1": "k:1", "https://img.cdn.example.com/2": "k:2",
         "https://img.cdn.example.com/3": "k:3"},
        subject_id="n1", trigger=click,
    )
    second = primary.respond(primary.request("GET", "https://img.cdn.example.com/2", resource_type="image"),
                             status=403, body=b"denied")
    result = await window.collect(timeout=0.05)
    assert first.body_calls == 1 and second.body_calls == 1 and cover.body_calls == 0
    one, one_visit = result["https://img.cdn.example.com/1"]
    assert one.content == b"one" and one.sha256 == sha256(b"one").hexdigest() and one.seq == 2
    assert result["https://img.cdn.example.com/2"][0].status == 403
    assert result["https://img.cdn.example.com/3"] is None
    window.close()
    late = primary.respond(primary.request("GET", "https://img.cdn.example.com/3", resource_type="image"),
                           body=b"three")
    await asyncio.sleep(0)
    assert late.body_calls == 0
    visit = next(line for line in ledger_lines(ledger) if line["id"] == one_visit)
    assert visit["stage"] == "note_image" and visit["asset_key"] == "k:1" and visit["bytes"] == 3
    assert visit["sha256"] == sha256(b"one").hexdigest() and visit["subject_id"] == "n1"
    assert visit["endpoint"] == "GET img.cdn.example.com *" and visit["attempt"] == 1


@pytest.mark.asyncio
async def test_image_window_keeps_bytes_across_attempts_and_bounds_held_handles(tmp_path):
    _, primary, _, channel = build(tmp_path, max_image_handles=2)
    window = channel.open_image_window("primary")
    for name in ("a", "b", "c"):
        primary.respond(primary.request("GET", f"https://img.cdn.example.com/{name}", resource_type="image"),
                        body=name.encode())
    assert window.held_count == 2
    window.expect({"https://img.cdn.example.com/a": "k:a", "https://img.cdn.example.com/b": "k:b"},
                  subject_id="n1", trigger=None)
    first = await window.collect(timeout=0.05)
    assert first["https://img.cdn.example.com/a"] is None
    assert first["https://img.cdn.example.com/b"][0].content == b"b"
    # 重做时合并登记：已截获的字节保留，缺失的继续等待。
    window.expect({"https://img.cdn.example.com/a": "k:a"}, subject_id="n1", trigger=None, attempt=2)
    primary.respond(primary.request("GET", "https://img.cdn.example.com/a", resource_type="image"), body=b"a")
    second = await window.collect(timeout=1)
    assert second["https://img.cdn.example.com/b"] == first["https://img.cdn.example.com/b"]
    assert second["https://img.cdn.example.com/a"][0].content == b"a"


@pytest.mark.asyncio
async def test_image_body_read_failure_waits_for_another_arrival(tmp_path):
    _, primary, _, channel = build(tmp_path)
    window = channel.open_image_window("primary")
    window.expect({"https://img.cdn.example.com/a": "k:a"}, subject_id="n1", trigger=None)
    primary.respond(primary.request("GET", "https://img.cdn.example.com/a", resource_type="image"),
                    body_error=RuntimeError("evicted"))
    assert (await window.collect(timeout=0.02))["https://img.cdn.example.com/a"] is None
    assert window.read_errors == 1


WEBPIC = "webpic.cdn.example.com/202610181600/SIGSEG0123456789/FILEID01!nd_dft_wlteh_webp_3"


@pytest.mark.parametrize("expected_scheme, response_scheme", [("http", "https"), ("https", "http")])
@pytest.mark.parametrize("response_first", [False, True])
@pytest.mark.asyncio
async def test_image_window_treats_http_and_https_as_the_same_address(
    tmp_path, expected_scheme, response_scheme, response_first,
):
    # #93：https 页面上的 http 图片被浏览器升级为 https 请求；先到的响应进 `_held`，登记期望后同样认领。
    _, primary, ledger, channel = build(tmp_path)
    window = channel.open_image_window("primary")
    expected = f"{expected_scheme}://{WEBPIC}"

    def respond():
        return primary.respond(
            primary.request("GET", f"{response_scheme}://{WEBPIC}", resource_type="image"), body=b"img",
        )

    response = respond() if response_first else None
    window.expect({expected: "k:1"}, subject_id="n1", trigger=None)
    response = response or respond()
    result = await window.collect(timeout=1)
    assert list(result) == [expected] and response.body_calls == 1
    image, visit_id = result[expected]
    assert image.content == b"img" and image.url == f"{response_scheme}://{WEBPIC}"
    visit = next(line for line in ledger_lines(ledger) if line["id"] == visit_id)
    assert visit["endpoint"] == "GET webpic.cdn.example.com *" and visit["asset_key"] == "k:1"
    assert window.miss_report([])["unmatched_responses"] == 0


@pytest.mark.parametrize(
    "response_url",
    [
        "https://webpic2.cdn.example.com/202610181600/SIGSEG0123456789/FILEID01!nd_dft_wlteh_webp_3",
        "https://webpic.cdn.example.com/202610181601/SIGSEG0123456789/FILEID01!nd_dft_wlteh_webp_3",
        "https://webpic.cdn.example.com/202610181600/SIGSEG0123456789/FILEID01!nd_dft_wgth_webp_3",
        "https://webpic.cdn.example.com/202610181600/SIGSEG0123456789/FILEID01!nd_dft_wlteh_webp_3?v=2",
    ],
    ids=["host", "path", "style", "query"],
)
@pytest.mark.parametrize("response_first", [False, True])
@pytest.mark.asyncio
async def test_image_window_still_requires_the_same_host_path_and_query(tmp_path, response_url, response_first):
    _, primary, _, channel = build(tmp_path)
    window = channel.open_image_window("primary")

    def respond():
        return primary.respond(primary.request("GET", response_url, resource_type="image"), body=b"img")

    response = respond() if response_first else None
    window.expect({f"http://{WEBPIC}": "k:1"}, subject_id="n1", trigger=None)
    response = response or respond()
    assert (await window.collect(timeout=0.02)) == {f"http://{WEBPIC}": None}
    assert response.body_calls == 0
    assert image_match_key(response_url) != image_match_key(f"http://{WEBPIC}")


@pytest.mark.asyncio
async def test_image_miss_report_counts_unmatched_forms_and_compares_file_ids_without_addresses(tmp_path):
    _, primary, _, channel = build(tmp_path)
    foreign = FakePage("https://www.example.com/explore")
    channel.dropped_responses = 5  # 窗口打开前的丢弃不计入
    window = channel.open_image_window("primary")
    base = "webpic.cdn.example.com/202610181600/SIGSEG0123456789"
    expected = {
        f"http://{base}/FILEID00!nd_dft_wlteh_webp_3": "k:0",
        f"http://{base}/FILEID01!nd_dft_wlteh_webp_3": "k:1",
        f"http://{base}/FILEID02!nd_dft_wlteh_webp_3": "k:2",
        f"http://{base}/FILEID03!nd_dft_wlteh_webp_3?imageView=SECRET": "k:3",
    }
    window.expect(expected, subject_id="n1", trigger=None)
    responses = [
        f"https://{base}/FILEID00!nd_dft_wlteh_webp_3",  # 升级后的同一地址：截获
        f"https://{base}/FILEID01!nd_dft_wgth_webp_3",  # 同文件 ID、样式不同
        f"https://{base}/FILEID01!nd_dft_wgth_webp_3",
        "https://other.cdn.example.com/202610181559/SIGSEG9999999999/FILEID02!nd_dft_wlteh_webp_3",
        "https://webpic.cdn.example.com/202610171200/SIGSEGcover/COVER01!nc_n_webp_mw_1",  # 无关图片
    ]
    for url in responses:
        primary.respond(primary.request("GET", url, resource_type="image"), body=b"img")
    primary.respond(
        primary.request("GET", f"https://{base}/FILEID03!nd_dft_wlteh_webp_3?imageView=SECRET", resource_type="image"),
        body_error=RuntimeError("evicted"),
    )
    primary.respond(primary.request("GET", "https://img.cdn.example.com/x", resource_type="image",
                                    frame_page=foreign), body=b"x")
    result = await window.collect(timeout=0.05)
    missing = [url for url, captured in result.items() if captured is None]
    assert missing == list(expected)[1:]

    report = window.miss_report(missing)

    assert report["unmatched_responses"] == 4 and report["untracked_responses"] == 0
    assert report["unmatched_forms"] == [
        {"scheme": "https", "host": "webpic.cdn.example.com", "style": "nd_dft_wgth_webp_3", "count": 2},
        {"scheme": "https", "host": "other.cdn.example.com", "style": "nd_dft_wlteh_webp_3", "count": 1},
        {"scheme": "https", "host": "webpic.cdn.example.com", "style": "nc_n_webp_mw_1", "count": 1},
    ]
    form = {"scheme": "http", "host": "webpic.cdn.example.com", "style": "nd_dft_wlteh_webp_3"}
    assert report["missing"] == [
        {**form, "same_key_seen": False, "same_file_id_seen": True, "differs": [["scheme", "style"]]},
        {**form, "same_key_seen": False, "same_file_id_seen": True, "differs": [["scheme", "host", "path"]]},
        {**form, "same_key_seen": True, "same_file_id_seen": False, "differs": []},
    ]
    assert report["read_errors"] == 1 and report["dropped_responses"] == 1
    text = json.dumps(report)
    for fragment in ("://", "/", "SIGSEG", "2026101", "FILEID", "COVER", "SECRET", "imageView"):
        assert fragment not in text


@pytest.mark.asyncio
async def test_image_miss_report_bounds_tracked_addresses(tmp_path):
    _, primary, _, channel = build(tmp_path)
    window = ImageWindow(channel, "primary", 64, max_seen=2)
    channel._windows.add(window)
    for name in ("a", "b", "c", "c"):
        primary.respond(primary.request("GET", f"https://img.cdn.example.com/{name}!s", resource_type="image"),
                        body=b"x")
    report = window.miss_report([])
    assert report["untracked_responses"] == 1 and report["unmatched_responses"] == 3
    assert report["unmatched_forms"] == [{"scheme": "https", "host": "img.cdn.example.com", "style": "s", "count": 3}]


@pytest.mark.asyncio
async def test_read_state_writes_state_visit_only_when_accepted(tmp_path):
    scripts = {"creator_page_data": "() => window.__STATE__"}
    context, primary, ledger, channel = build(tmp_path, state_scripts=scripts)
    tab = context.new_page("https://www.example.com/user/profile/5f3e2d1c0b9a887766554433?xsec_token=t")
    assert channel.role_of(tab) == "new_tab"
    channel.attach(tab, "author_tab")
    assert channel.role_of(tab) == "author_tab" and tab.handler_count("response") == 1
    tab.state["() => window.__STATE__"] = {"followers": 3}
    click = channel.action("click_author", page_role="primary", target={"user_id": "u1"})
    value, visit = await channel.read_state(tab, "creator_page_data", None, stage="creator_profile",
                                            subject_id="u1", trigger=click, url_user_id="u1")
    assert value == {"followers": 3}
    line = next(item for item in ledger_lines(ledger) if item["id"] == visit)
    assert line["source"] == "page_state" and line["rule"] == "creator_page_data"
    assert line["page_role"] == "author_tab" and line["route"] == "/user/profile/<id>"
    assert line["query_keys"] == ["xsec_token"] and line["url_user_id"] == "u1"
    assert line_seq(visit) > line_seq(click)
    value, skipped = await channel.read_state(tab, "creator_page_data", None, stage="creator_profile",
                                              subject_id="u1", trigger=click, accept=lambda value: False)
    assert skipped is None and value == {"followers": 3}
    with pytest.raises(ValueError):
        await channel.read_state(tab, "unknown", None, stage="x", subject_id=None, trigger=None)
    with pytest.raises(ValueError):
        await channel.read_state(FakePage(), "creator_page_data", None, stage="x", subject_id=None, trigger=None)


@pytest.mark.asyncio
async def test_sentinel_statuses_only_write_signals_without_reading_bodies(tmp_path):
    _, primary, ledger, channel = build(tmp_path)
    collect = "https://t2.example.com/api/v2/collect"
    responses = [
        primary.respond(primary.request("POST", collect), status=status, body=b"x")
        for status in (401, 403, 429)
    ]
    verify = primary.respond(primary.request("POST", "https://as.example.com/api/sec/v1/scripting"),
                             status=461, headers={"verifyuuid": "u"}, body=b"x")
    ok = primary.respond(primary.request("POST", collect), status=200, body=b"x")
    await asyncio.sleep(0)
    assert all(item.body_calls == 0 for item in [*responses, verify, ok])
    assert all(not channel.drain(name, after_seq=0) for name in ("t.search", "t.detail"))
    signals = [line for line in ledger_lines(ledger) if line["kind"] == "signal"]
    assert [(line["status"], line["outcome"]) for line in signals] == [
        (401, "ignored"), (403, "ignored"), (429, "ignored"),
    ]
    assert signals[0]["endpoint"] == "POST t2.example.com /api/v2/collect"
    hits = channel.take_sentinel_hits()
    assert [(hit.status, hit.verify_headers_present) for hit in hits] == [(461, True)]
    signal = channel.signal(hits[0], outcome="verification_surfaced")
    assert line_seq(signal) == hits[0].seq == 4
    assert channel.take_sentinel_hits() == []


@pytest.mark.asyncio
async def test_one_counter_for_actions_captures_pauses_and_decisions(tmp_path):
    _, primary, _, channel = build(tmp_path)
    action = channel.action("scroll", page_role="primary", target={"delta_y": 400})
    primary.respond(search_request(primary, 2), body=body({}))
    start = channel.pause_started()
    pause = channel.pause_finished("search_page", start, 12.0, "completed")
    decision = channel.decision("n9", "video_skipped_from_search")
    captured = await channel.wait_for("t.search", after_seq=0, timeout=1)
    assert [line_seq(action), captured.seq, start, line_seq(pause), line_seq(decision)] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_callback_failure_faults_the_channel(tmp_path):
    _, primary, ledger, channel = build(tmp_path)
    ledger.close()
    primary.respond(primary.request("POST", "https://t2.example.com/api/v2/collect"), status=403, body=b"")
    with pytest.raises(CaptureChannelError):
        channel.action("scroll", page_role="primary", target={})
    with pytest.raises(CaptureChannelError):
        await channel.wait_for("t.search", after_seq=0, timeout=0.01)


@pytest.mark.asyncio
async def test_ledger_never_contains_urls_tokens_or_query_values(tmp_path):
    _, primary, ledger, channel = build(tmp_path)
    primary.url = "https://www.example.com/explore/64f1a2b3c4d5e6f7a8b9c0d1?xsec_token=SECRET&xsec_source=pc"
    click = channel.action("click_card", page_role="primary", target={"note_id": "64f1a2b3c4d5e6f7a8b9c0d1"})
    window = channel.open_image_window("primary")
    window.expect({"https://img.cdn.example.com/p/abc?imageView=SECRET2": "k:1"}, subject_id="n1", trigger=click)
    primary.respond(primary.request("GET", "https://img.cdn.example.com/p/abc?imageView=SECRET2",
                                    resource_type="image"), body=b"img")
    feed = primary.respond(
        primary.request("POST", FEED_URL + "?a=SECRET3", post_data_json={"xsec_token": "SECRET4"}),
        body=body({"avatar": "https://avatar/x", "desc": "正文"}),
    )
    captured = await channel.wait_for("t.detail", after_seq=0, timeout=1)
    assert feed.body_calls == 1
    channel.record_visit(captured, stage="note_detail", subject_id="n1", trigger=click)
    await window.collect(timeout=1)
    text = ledger.path.read_text(encoding="utf-8")
    # 查询键名可以出现（如 xsec_token），值不得出现。
    for secret in ("SECRET", "://", "avatar", "正文", "imageView"):
        assert secret not in text


@pytest.mark.asyncio
async def test_close_stops_capture_and_closes_the_ledger(tmp_path):
    _, primary, ledger, channel = build(tmp_path)
    slow = primary.respond(search_request(primary), body=body({}), delayed=True)
    await asyncio.sleep(0)
    await channel.aclose()
    late = primary.respond(search_request(primary, 2), body=body({}))
    await asyncio.sleep(0)
    assert channel.closed and late.body_calls == 0 and slow.body_calls == 1
    assert channel.drain("t.search", after_seq=0) == []
    with pytest.raises(RuntimeError):
        ledger.append("decision", seq=ledger.next_seq(), page_role=None, subject_id="n", decision="x",
                      from_visit=None)


@pytest.mark.asyncio
async def test_latest_pending_seq_reports_responses_still_reading_their_body(tmp_path):
    _, primary, _, channel = build(tmp_path)
    assert channel.latest_pending_seq("t.search") == 0
    first = primary.respond(search_request(primary), body=body({}), delayed=True)
    second = primary.respond(search_request(primary, 2), body=body({}), delayed=True)
    await asyncio.sleep(0)
    pending = channel.latest_pending_seq("t.search")
    assert pending > 0 and channel.drain("t.search", after_seq=0) == []
    first.release()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert channel.latest_pending_seq("t.search") == pending
    second.release()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert channel.latest_pending_seq("t.search") == 0
    assert [item.seq for item in channel.drain("t.search", after_seq=0)] == [pending - 1, pending]
    with pytest.raises(ValueError):
        channel.latest_pending_seq("unknown.rule")
