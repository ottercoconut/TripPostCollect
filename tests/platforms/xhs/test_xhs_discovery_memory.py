"""全量滚动扫描的抓取记忆与停止口径：已知候选滑过、候选失败、运行级失败集中写停止事件（full_scan，无恢复位置）。

搜索与单篇处理都经离线站点驱动：点封面打开浮层、截获 feed 与图片、点击作者名读作者页投影、关闭浮层。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from hashlib import sha256
from unittest.mock import ANY, AsyncMock, Mock

import pytest

from playwright.async_api import Error as PlaywrightError
from support.xhs_fake_site import (
    AUTHOR_POINT,
    AVATAR_POINT,
    COMMENT_POINT,
    inside,
    AuthorTab,
    XhsFakeSite,
    detail_card,
    image_bytes,
    image_url,
    note,
)
from trippostcollect.application import events as app_events
from trippostcollect.application.visit_gate import validate_content_visits
from trippostcollect.artifacts.image_manifest import ImageManifestEntry, write_manifest_atomic
from trippostcollect.platforms.xhs import author as xhs_author
from trippostcollect.platforms.xhs import behavior as xhs_behavior
from trippostcollect.platforms.xhs import core as xhs_core
from trippostcollect.platforms.xhs import media as xhs_media
from trippostcollect.platforms.xhs import overlay as xhs_overlay
from trippostcollect.platforms.xhs import scan as xhs_scan
from trippostcollect.platforms.xhs.errors import (
    IPBlockError,
    PlatformRuntimeError,
    XHSMainPageClosedUnexpected,
    XHSNetworkRecoveryTimeout,
    xhs_cdp_lifecycle_stop_detail,
)
from trippostcollect.platforms.xhs.manual_wait import XHSManualWaitBudgetExhausted
from trippostcollect.platforms.xhs.session import XHS_NEW_PAGE_MIN_HOLD_SECONDS
from trippostcollect.runtime.browser import CDPBrowserLifecycleError
from trippostcollect.runtime.visit_ledger import iter_ledger_lines

from .support import XiaoHongShuCrawler, assert_no_avatar_log_lines, config, crawler_log_lines, make_capture

KEYWORD = "青岛旅游"
LOGIN_EXPIRED = {"code": -101, "success": False, "msg": "登录已过期"}


def lifecycle_error(code: str, *, stage: str = "search") -> CDPBrowserLifecycleError:
    return CDPBrowserLifecycleError(
        {
            "code": code,
            "kind": code,
            "pid": 4321,
            "returncode": 23 if code == "xhs_browser_process_exited" else None,
        },
        stage=stage,
    )


async def _no_sleep(*_args, **_kwargs) -> None:
    return None


def prepare_crawler(monkeypatch, tmp_path, *, items, pages=None):
    """目标会话第 1 页为 `items`（或多页 `pages`），其后是 has_more=false 的空页。

    返回 (crawler, 发出 feed 请求的笔记 ID 列表, 状态文件, 站点)。暂存与写出替换为内存记录：
    `crawler.staged[note_id]` 为交给暂存的图片，`crawler.records` 为经真实 `store_xhs_note` 写出的 JSONL 记录；
    `crawler.pause_ranges` 为各节奏停顿收到的 (档位, 下限, 上限)。
    """
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"events": []}), encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(tmp_path / "behavior_evidence.json"))
    monkeypatch.setattr(config, "KEYWORDS", KEYWORD)
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(xhs_scan, "merge_pending_behavior_evidence", lambda pending, ports: None)
    monkeypatch.setattr(xhs_scan, "XHS_SEARCH_LOAD_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(xhs_overlay, "XHS_DETAIL_CAPTURE_SECONDS", 0.05)
    monkeypatch.setattr(xhs_media, "XHS_IMAGE_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(xhs_author, "XHS_AUTHOR_TAB_OPEN_SECONDS", 0.05)
    monkeypatch.setattr(xhs_author, "XHS_CREATOR_RUNTIME_READY_SECONDS", 0.05)
    monkeypatch.setattr(xhs_author, "inspect_visible_page_state", AsyncMock(return_value=("", {})))

    site = XhsFakeSite(KEYWORD, pages if pages is not None else [[note(item) for item in items]])
    crawler = XiaoHongShuCrawler()
    crawler.context_page = site.page
    crawler.browser_context = site.context
    crawler.capture = make_capture(tmp_path, context=site.context)
    crawler.capture.install(site.page)
    crawler._scan_sleep = _no_sleep
    crawler._popup_sleep = _no_sleep

    crawler.pause_ranges = []

    async def guarded_pause(stage, minimum, maximum):
        crawler.pause_ranges.append((stage, minimum, maximum))
        start = crawler.capture.pause_started()
        continuity = "completed" if stage in {"search_results", "search_page"} else "n/a"
        crawler.capture.pause_finished(stage, start, 0.0, continuity)
        return 0.0

    async def behavior(keyword):
        site.wheel(600)
        site.wheel(-600)
        return {"status": "completed"}

    crawler._guarded_pause = guarded_pause
    crawler._run_human_behavior_on_primary_page = behavior
    crawler._popup_checkpoint_state = AsyncMock(
        return_value={"closed": False, "visible_text": "ok", "manual_markers": [], "terminal": ""}
    )
    crawler.staged = {}
    crawler.records = []

    async def stage_images(note_id, items):
        crawler.staged[note_id] = items

    class MemorySink:
        async def store_content(self, item):
            crawler.records.append(item)

    async def store(note_item):
        await xhs_core.store_xhs_note(
            note_item, source_keyword=KEYWORD, crawler_type="search", save_data_option="jsonl",
            current_timestamp=lambda: 0, content_sink_factory=lambda _crawler_type: MemorySink(),
        )

    crawler.update_xhs_note_images = stage_images
    crawler.record_xhs_note_image_failure = AsyncMock()
    monkeypatch.setattr(crawler, "update_xhs_note", AsyncMock(side_effect=store))
    return crawler, site.feed_requests, state_path, site


def events_of(state_path, kind: str) -> list[dict]:
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    return [event["details"] for event in events if event["type"] == kind]


def last_stop(state_path) -> dict:
    return events_of(state_path, "adaptive_search_stopped")[-1]


def assert_runtime_stop(stopped: dict, detail: str, *, source_page=1) -> None:
    assert stopped["stop_reason"] == "runtime_failed"
    assert stopped["stop_detail"] == detail
    assert stopped["discovery_phase"] == "full_scan"
    assert stopped["source_page"] == source_page
    assert stopped["resume_page"] is None and stopped["resume_cursor"] is None
    assert stopped["batch_complete"] is False
    assert "source_visit_id" not in stopped


def ledger_lines(crawler) -> list[dict]:
    return list(iter_ledger_lines(crawler.capture.ledger.path))


def network_sleeps(sleeps: list[float]) -> list[float]:
    """网络退避的等待；作者标签页的保留也经同一时钟（`attach_network_clock` 把它固定为通用最短保留），单独剔除。"""
    return [seconds for seconds in sleeps if seconds != XHS_NEW_PAGE_MIN_HOLD_SECONDS]


def write_manifest(crawler, path) -> str:
    """按交给暂存的字节写出与正式暂存同构的 manifest，供门禁核对 sha256。"""
    entries = [
        ImageManifestEntry(
            schema_version=1, platform_key="xhs", platform_post_id=note_id, image_role="content",
            source_index=item["source_index"], source_key="image_list", source_asset_key=item["source_asset_key"],
            source_url=item["url"], fetch_status="downloaded", attempts=1, http_status=200,
            staging_path=f"images/{note_id}/{item['source_index']}.webp", size_bytes=len(item["content"]),
            mime_type="image/webp", width=1, height=1, sha256=sha256(item["content"]).hexdigest(), error_code=None,
        )
        for note_id, items in crawler.staged.items()
        for item in items
    ]
    write_manifest_atomic(path, entries)
    return str(path)


@pytest.mark.asyncio
async def test_known_note_is_skipped_before_detail_request(monkeypatch, tmp_path):
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
        conn.execute("INSERT INTO web_posts VALUES ('xhs', 'known-note', NULL)")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DB_PATH", str(db_path))
    crawler, detail_ids, state_path, site = prepare_crawler(
        monkeypatch, tmp_path, items=["known-note", "new-note"],
    )

    await crawler.search()

    assert detail_ids == ["new-note"]
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted"
    assert stopped["resume_page"] is None
    assert stopped["batch_complete"] is True


@pytest.mark.asyncio
async def test_all_unknown_candidates_are_processed_before_exhaustion(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path, site = prepare_crawler(
        monkeypatch, tmp_path, items=["first", "second"],
    )

    await crawler.search()

    assert detail_ids == ["first", "second"]
    assert [record["note_id"] for record in crawler.records] == ["first", "second"]
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted"
    assert stopped["stop_detail"] == "has_more_false"
    assert stopped["discovery_phase"] == "full_scan"
    assert stopped["source_page"] == 2 and stopped["source_has_more"] is False
    assert stopped["source_cursor"] == site.search_id
    assert stopped["resume_page"] is None and stopped["resume_cursor"] is None
    assert stopped["batch_complete"] is True
    assert stopped["candidate_identities"] == ["first", "second"]
    assert stopped["source_visit_id"].startswith(crawler.capture.ledger.ledger_id)


@pytest.mark.asyncio
async def test_overlay_records_pass_the_real_visit_gate(monkeypatch, tmp_path):
    """两页关键词：浮层详情、翻图截获、作者标签页与关闭浮层写出的账本与记录，经真实门禁回读全部通过。"""
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path, site = prepare_crawler(
        monkeypatch, tmp_path, items=None, pages=[[note("p1-a"), note("p1-b")], [note("p2-a")]],
    )
    site.details["p1-a"] = detail_card("p1-a", images=3, user_id="shared-author")
    site.details["p2-a"] = detail_card("p2-a", images=1, user_id="shared-author")

    with crawler_log_lines() as log_lines:
        await crawler.search()

    # 整轮日志（含写出记录的日志）不命中头像正则，子进程日志不会被整段替换为审计标记（#80）。
    assert log_lines and any(line.startswith("[store.xhs.update_xhs_note]") for line in log_lines)
    assert_no_avatar_log_lines(log_lines)

    assert detail_ids == ["p1-a", "p1-b", "p2-a"]
    assert len(site.author_tabs) == 2  # 同一作者只点一次
    assert all(tab.closed for tab in site.author_tabs)
    records = crawler.records
    assert [record["note_id"] for record in records] == ["p1-a", "p1-b", "p2-a"]
    refs = {record["note_id"]: record["content_visit_refs"] for record in records}
    assert refs["p2-a"]["creator_profile"] == refs["p1-a"]["creator_profile"]
    assert len(refs["p1-a"]["images"]) == 3
    assert all(ref["ledger_id"] == crawler.capture.ledger.ledger_id for ref in refs.values())
    actions = [line["action"] for line in ledger_lines(crawler) if line["kind"] == "action"]
    assert actions.count("flip_image") == 3 and actions.count("click_author") == 2
    assert actions.count("close_tab") == 2 and actions.count("close_overlay") == 3
    # #84：每次点击带落点在元素框中的比例与接近时长，每次列表滚动带各手势的事件数与位移；真实门禁照常通过。
    clicks = {"click_card", "close_overlay", "click_author", "apply_filter", "type_search"}
    for line in (line for line in ledger_lines(crawler) if line["kind"] == "action"):
        if line["target"].get("method") == "pointer_leave":
            # #81：移开光标收起面板只接近、不静止也不按下，指针记录没有 settle_ms 与 hold_ms。
            pointer = line["pointer"]
            assert 0 < pointer["x_ratio"] < 1 and 0 < pointer["y_ratio"] < 1
            assert pointer["approach_ms"] > 0 and pointer["moves"] > 0
            assert "settle_ms" not in pointer and "hold_ms" not in pointer
        elif line["action"] in clicks or (line["action"] == "flip_image" and line["target"]["method"] == "arrow"):
            pointer = line["pointer"]
            assert 0 < pointer["x_ratio"] < 1 and 0 < pointer["y_ratio"] < 1
            assert pointer["approach_ms"] > 0 and pointer["moves"] > 0 and 80 <= pointer["hold_ms"] <= 135
        elif line["action"] == "scroll" and "delta_y" in line["target"]:
            gestures = line["pointer"]["gestures"]
            assert sum(gesture["delta_y"] for gesture in gestures) == line["target"]["delta_y"]
            assert all(gesture["events"] >= 8 for gesture in gestures)
        else:
            assert "pointer" not in line, line["action"]
    # 操作人 2026-10-14 要求全面提速（#79）后的各档取值。
    assert set(crawler.pause_ranges) == {
        ("search_page", 4.0, 10.0), ("search_results", 2.0, 5.0), ("note_detail", 2.0, 4.0), ("creator_profile", 2.0, 5.0),
    }
    flips = [line["seconds"] for line in ledger_lines(crawler) if line["kind"] == "pause" and line["stage"] == "note_image"]
    assert len(flips) == 3 and all(0.8 <= seconds <= 1.8 for seconds in flips)

    result = validate_content_visits(
        records,
        [crawler.capture.ledger.path],
        platform="xhs",
        mode="discovery",
        manifests=[write_manifest(crawler, tmp_path / "image_manifest.jsonl")],
        stop_event=last_stop(state_path),
    )

    assert result["problem_counts"] == {}
    assert result["content_visit_ok"] is True and result["checked_records"] == 3
    assert result["required_stages"] == ["creator_profile", "note_detail", "search_page", "search_results"]
    assert result["continuity_ok"] is True and result["request_pacing_ok"] is True


@pytest.mark.asyncio
async def test_next_page_arriving_before_a_cross_page_duplicate_is_clicked_passes_the_real_gate(monkeypatch, tmp_path):
    """#97 瀑布流：第 1 页的搜索结果停顿之后、点开跨页重复笔记 dup 之前，第 2 页已经送达。dup 登记在第 1 页，作为
    第 1 页的结果点开；第 2 页在它自己第一张登记卡片之前停顿，真实门禁通过。"""
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path, site = prepare_crawler(
        monkeypatch, tmp_path, items=None, pages=[[note("p1-a"), note("dup")], [note("dup"), note("p2-a")]],
    )
    # 行为阶段不滚动，第 2 页只在第 1 页的搜索结果停顿之后送达。
    crawler._run_human_behavior_on_primary_page = AsyncMock(return_value={"status": "completed"})
    pause = crawler._guarded_pause

    async def deliver_next_page_after_results_pause(stage, minimum, maximum):
        seconds = await pause(stage, minimum, maximum)
        if stage == "search_results" and len(site.loaded) == 1:
            site.respond_page(2)
        return seconds

    crawler._guarded_pause = deliver_next_page_after_results_pause

    await crawler.search()

    assert detail_ids == ["p1-a", "dup", "p2-a"]
    lines = ledger_lines(crawler)
    pages = {
        line["search_page"]: line for line in lines
        if line["kind"] == "visit" and line["stage"] == "search_results" and line["is_target"]
    }
    clicks = {line["target"]["note_id"]: line["seq"] for line in lines if line.get("action") == "click_card"}
    results_pauses = [line for line in lines if line["kind"] == "pause" and line["stage"] == "search_results"]
    assert len(results_pauses) == 2
    assert results_pauses[0]["seq"] < pages[2]["seq"] < clicks["dup"] < results_pauses[1]["start_seq"]
    assert results_pauses[1]["seq"] < clicks["p2-a"]
    refs = {record["note_id"]: record["content_visit_refs"] for record in crawler.records}
    assert refs["dup"]["search_results"] == pages[1]["id"] and refs["p2-a"]["search_results"] == pages[2]["id"]

    result = validate_content_visits(
        crawler.records,
        [crawler.capture.ledger.path],
        platform="xhs",
        mode="discovery",
        manifests=[write_manifest(crawler, tmp_path / "image_manifest.jsonl")],
        stop_event=last_stop(state_path),
    )

    assert result["problem_counts"] == {}
    assert result["content_visit_ok"] is True and result["checked_records"] == 3


@pytest.mark.asyncio
async def test_author_tab_hold_is_drawn_per_open_and_tab_stays_until_projection(monkeypatch, tmp_path):
    """作者标签页每次打开抽一次 5–9 秒保留，从出现起计，记在 close_tab 的 target.hold_seconds；读到投影之前不关。

    第一位作者 1 秒读完投影，按抽到的保留关闭；第二位作者读投影耗时 12 秒，长于任何抽样，读完即关、不额外停留。
    """
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["quick", "slow"])
    clock = {"now": 100.0}

    async def sleep(seconds: float) -> None:
        clock["now"] += seconds

    crawler._popup_monotonic = lambda: clock["now"]
    crawler._popup_sleep = sleep
    draws: list[tuple[float, float, float]] = []

    class RecordingRandom:
        def uniform(self, low: float, high: float) -> float:
            value = xhs_author.random.uniform(low, high)
            draws.append((low, high, value))
            return value

    monkeypatch.setattr(xhs_author, "AUTHOR_RANDOM", RecordingRandom())
    read_done: dict[str, float] = {}

    def slow_read(user_id: str, seconds: float):
        def hook() -> None:
            clock["now"] += seconds
            read_done[user_id] = clock["now"]
        return hook

    site.projection_hooks["author-quick"] = slow_read("author-quick", 1.0)
    site.projection_hooks["author-slow"] = slow_read("author-slow", 12.0)
    closed_at: dict[str, float] = {}

    async def close(tab) -> None:
        closed_at[tab.user_id] = clock["now"]
        tab.closed = True

    monkeypatch.setattr(AuthorTab, "close", close)

    await crawler.search()

    assert last_stop(state_path)["stop_reason"] == "source_exhausted"
    assert [tab.user_id for tab in site.author_tabs] == ["author-quick", "author-slow"]
    assert [(low, high) for low, high, _ in draws] == [(5.0, 9.0), (5.0, 9.0)]
    holds = [value for _, _, value in draws]
    assert all(5.0 <= value <= 9.0 for value in holds)
    lines = ledger_lines(crawler)
    closes = [line for line in lines if line.get("action") == "close_tab"]
    assert [line["target"]["hold_seconds"] for line in closes] == [round(value, 3) for value in holds]
    creator_visits = [line for line in lines if line["kind"] == "visit" and line["stage"] == "creator_profile"]
    assert [line["seq"] < close["seq"] for line, close in zip(creator_visits, closes, strict=True)] == [True, True]
    for tab, hold in zip(site.author_tabs, holds, strict=True):
        opened_at = crawler._new_pages[id(tab)][1]
        assert crawler._new_pages[id(tab)][3] == hold
        assert closed_at[tab.user_id] >= read_done[tab.user_id]
        assert closed_at[tab.user_id] == pytest.approx(max(opened_at + hold, read_done[tab.user_id]))
    quick, slow = site.author_tabs
    assert closed_at["author-quick"] == pytest.approx(crawler._new_pages[id(quick)][1] + holds[0])
    assert closed_at["author-slow"] == pytest.approx(read_done["author-slow"])


@pytest.mark.asyncio
async def test_browser_context_close_is_recorded_as_runtime_failure(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["new-note"])
    crawler._enrich_creator_via_author_tab = AsyncMock(
        side_effect=PlaywrightError(
            "BrowserContext.new_page: Target page, context or browser has been closed"
        )
    )

    await crawler.search()

    stopped = last_stop(state_path)
    assert_runtime_stop(stopped, "browser_context_closed")
    assert stopped["source_cursor"] == site.search_id


@pytest.mark.asyncio
async def test_creator_security_limit_is_recorded_as_terminal_runtime_failure(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["security-limited-note"])
    crawler._enrich_creator_via_author_tab = AsyncMock(
        side_effect=PlatformRuntimeError(
            "XHS creator profile is blocked by a platform security limit",
            code="platform_security_limit_300011",
        )
    )

    await crawler.search()

    assert not events_of(state_path, "candidate_skipped")
    stopped = last_stop(state_path)
    assert_runtime_stop(stopped, "platform_security_limit_300011")
    assert stopped["source_cursor"] == site.search_id
    assert stopped["candidate_identities"] == []


@pytest.mark.asyncio
async def test_feed_missing_twice_is_retryable_run_only_and_search_continues(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path, site = prepare_crawler(
        monkeypatch, tmp_path, items=None, pages=[[note("retry-detail")], [note("later")]],
    )
    site.feed_overrides["retry-detail"] = ["no_feed", "no_feed"]

    await crawler.search()

    assert [record["note_id"] for record in crawler.records] == ["later"]
    (skipped,) = events_of(state_path, "candidate_skipped")
    assert skipped["identity"] == "retry-detail"
    assert skipped["failure_scope"] == "post"
    assert skipped["error_code"] == "note_detail_not_observed"
    assert skipped["attempts"] == 2
    assert skipped["retryable"] is True
    # 浮层出现但没有 feed：先点关闭按钮，再开一次；两次都没有 feed 后关闭浮层。
    actions = [(line["action"], line["target"].get("note_id")) for line in ledger_lines(crawler) if line["kind"] == "action"]
    assert actions[actions.index(("click_card", "retry-detail")):][:4] == [
        ("click_card", "retry-detail"), ("close_overlay", "retry-detail"),
        ("click_card", "retry-detail"), ("close_overlay", "retry-detail"),
    ]
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted"
    # 可重试的候选失败只算本轮已决定，不进入跨轮记忆。
    assert stopped["candidate_identities"] == ["later"]


@pytest.mark.asyncio
async def test_overlay_shown_without_feed_is_reopened_once_and_then_processed(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["slow"])
    site.feed_overrides["slow"] = ["no_feed"]

    await crawler.search()

    (record,) = crawler.records
    detail = next(line for line in ledger_lines(crawler) if line.get("id") == record["content_visit_refs"]["note_detail"])
    clicks = [line for line in ledger_lines(crawler) if line.get("action") == "click_card"]
    assert len(clicks) == 2 and detail["trigger"] == clicks[1]["id"]
    assert not events_of(state_path, "candidate_skipped")


@pytest.mark.asyncio
async def test_three_notes_whose_overlay_never_opens_stop_the_run(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["a", "b", "c", "d"])
    for note_id in ("a", "b", "c"):
        site.feed_overrides[note_id] = ["no_overlay", "no_overlay"]

    await crawler.search()

    assert detail_ids == []
    assert [event["identity"] for event in events_of(state_path, "candidate_skipped")] == ["a", "b"]
    assert_runtime_stop(last_stop(state_path), "xhs_overlay_not_opening")


@pytest.mark.asyncio
async def test_overlay_that_cannot_be_closed_stops_the_run(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["stuck"])
    site.overlay_closes = False

    await crawler.search()

    assert crawler.records == []
    assert_runtime_stop(last_stop(state_path), "xhs_overlay_close_failed")


@pytest.mark.asyncio
async def test_ip_block_stops_run_without_skipping_or_marking_seen(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["blocked-detail"])
    site.feed_overrides["blocked-detail"] = [{"code": 300012, "success": False, "msg": "网络连接异常"}]

    await crawler.search()

    crawler.update_xhs_note.assert_not_awaited()
    assert not events_of(state_path, "candidate_skipped")
    stopped = last_stop(state_path)
    assert_runtime_stop(stopped, "ip_blocked_300012")
    assert stopped["candidate_identities"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "error_code"),
    [
        ({"code": -510000, "success": False, "msg": "笔记不存在"}, "note_not_found"),
        ({"code": 0, "success": True, "data": {"items": [{"id": "other", "note_card": detail_card("other")}]}},
         "note_id_mismatch"),
        ({"code": 0, "success": True, "data": {"items": []}}, "detail_parse_failed"),
    ],
    ids=["note_missing", "id_mismatch", "parse_failed"],
)
async def test_deterministic_feed_failures_are_final_candidate_skips(monkeypatch, tmp_path, override, error_code):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["broken", "fine"])
    site.feed_overrides["broken"] = [override]

    await crawler.search()

    (skipped,) = events_of(state_path, "candidate_skipped")
    assert skipped["identity"] == "broken" and skipped["error_code"] == error_code
    assert skipped["retryable"] is False and skipped["attempts"] == 1
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted"
    assert stopped["candidate_identities"] == ["broken", "fine"]


@pytest.mark.asyncio
async def test_video_feed_is_closed_and_decided_without_a_record(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["clip"])
    site.details["clip"] = detail_card("clip", note_type="video")

    await crawler.search()

    assert crawler.records == [] and site.overlay_note is None
    lines = ledger_lines(crawler)
    assert not [line for line in lines if line.get("stage") == "note_detail" and line["kind"] == "visit"]
    assert [line["decision"] for line in lines if line["kind"] == "decision"] == ["video_skipped_from_detail"]
    assert last_stop(state_path)["candidate_identities"] == ["clip"]


@pytest.mark.asyncio
async def test_image_not_captured_is_retryable_and_later_candidate_continues(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(
        monkeypatch, tmp_path, items=["retry-image", "success-image"],
    )
    site.image_overrides[image_url("retry-image", 1)] = "missing"

    await crawler.search()

    assert [record["note_id"] for record in crawler.records] == ["success-image"]
    (skipped,) = events_of(state_path, "candidate_skipped")
    assert skipped["identity"] == "retry-image"
    assert skipped["failure_scope"] == "image"
    assert skipped["error_code"] == "image_response_not_observed"
    assert skipped["source_index"] == 1
    assert skipped["retryable"] is True
    failure = crawler.record_xhs_note_image_failure.await_args.args[1]
    assert failure["error_code"] == "image_response_not_observed" and failure["http_status"] is None
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted"
    assert stopped["candidate_identities"] == ["success-image"]


@pytest.mark.asyncio
async def test_upgraded_http_images_are_captured_and_a_restyled_image_writes_a_miss_diagnostic(monkeypatch, tmp_path):
    # #93：feed 给 http 地址，前端实际请求 https（混合内容自动升级）仍能认领；换了样式的图片未截获时，
    # 抛出前写 note_images 诊断，只有计数、序号、协议、主机、样式与布尔值。
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["upgraded", "restyled"])
    for note_id in ("upgraded", "restyled"):
        card = detail_card(note_id, images=3)
        for index, image in enumerate(card["image_list"]):
            image["url_default"] = image_url(note_id, index).replace("https://", "http://")
            site.image_overrides[image["url_default"]] = image_url(note_id, index)
        site.details[note_id] = card
    restyled = image_url("restyled", 0).replace("https://", "http://")
    site.image_overrides[restyled] = image_url("restyled", 0).replace("nd_dft_wlteh_webp_3", "nd_dft_wgth_webp_3")

    with crawler_log_lines() as log_lines:
        await crawler.search()

    assert [record["note_id"] for record in crawler.records] == ["upgraded"]
    assert [item["content"] for item in crawler.staged["upgraded"]] == [image_bytes("upgraded", i) for i in range(3)]
    assert [item["url"] for item in crawler.staged["upgraded"]] == [
        image_url("upgraded", i).replace("https://", "http://") for i in range(3)
    ]
    (skipped,) = events_of(state_path, "candidate_skipped")
    assert (skipped["identity"], skipped["error_code"], skipped["source_index"], skipped["retryable"]) == (
        "restyled", "image_response_not_observed", 0, True,
    )
    assert crawler.record_xhs_note_image_failure.await_count == 1
    (diagnostic,) = [event for event in crawler._navigation_diagnostics if event["stage"] == "note_images"]
    assert diagnostic["outcome"] == "image_response_not_observed" and diagnostic["note_id"] == "restyled"
    assert (diagnostic["expected_count"], diagnostic["captured_count"], diagnostic["missing_source_indexes"]) == (
        3, 2, [0],
    )
    assert diagnostic["unmatched_responses"] == 1 and diagnostic["untracked_responses"] == 0
    assert diagnostic["unmatched_forms"] == [
        {"scheme": "https", "host": "sns-webpic-qc.xhscdn.com", "style": "nd_dft_wgth_webp_3", "count": 1},
    ]
    assert diagnostic["missing"] == [
        {
            "source_index": 0, "scheme": "http", "host": "sns-webpic-qc.xhscdn.com", "style": "nd_dft_wlteh_webp_3",
            "same_key_seen": False, "same_file_id_seen": True, "differs": [["scheme", "style"]],
        },
    ]
    assert diagnostic["read_errors"] == 0 and diagnostic["dropped_responses"] == 0
    # 页面地址是渲染诊断的公共字段（只到路径）；其余字段不含任何地址。
    assert diagnostic["url"] == "https://www.xiaohongshu.com/search_result_ai"
    text = json.dumps({key: value for key, value in diagnostic.items() if key != "url"}, ensure_ascii=False)
    assert "://" not in text and "notes_pre_post" not in text and "202601" not in text
    (line,) = [line for line in log_lines if "Note images not captured" in line]
    assert "restyled expected=3, captured=2, missing=[0]" in line
    assert "https sns-webpic-qc.xhscdn.com !nd_dft_wgth_webp_3×1" in line and "same_file_id=1/1" in line
    assert "://" not in line and "notes_pre_post" not in line


@pytest.mark.asyncio
async def test_terminal_image_status_is_final_and_later_candidate_continues(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(
        monkeypatch, tmp_path, items=["terminal-image", "must-store"],
    )
    site.image_overrides[image_url("terminal-image", 0)] = 404

    await crawler.search()

    assert [record["note_id"] for record in crawler.records] == ["must-store"]
    (skipped,) = events_of(state_path, "candidate_skipped")
    assert skipped["identity"] == "terminal-image"
    assert skipped["failure_scope"] == "image"
    assert skipped["error_code"] == "image_source_unavailable"
    assert skipped["retryable"] is False
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted"
    assert stopped["candidate_identities"] == ["must-store", "terminal-image"]


@pytest.mark.asyncio
async def test_blocking_image_status_stops_the_run(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["limited"])
    site.image_overrides[image_url("limited", 0)] = 429

    await crawler.search()

    assert not events_of(state_path, "candidate_skipped")
    assert_runtime_stop(last_stop(state_path), "image_rate_limited")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "reason", "retryable", "attempts"),
    [
        (lambda site: setattr(site, "author_tab_failures", 2), "author_tab_not_opened", True, 2),
        (lambda site: site.author_url_ids.update({"author-retry-creator": "someone-else"}), "creator_mismatch", False, 1),
        (lambda site: site.creator_projections.update({"author-retry-creator": {"status": "missing"}}),
         "runtime_projection_empty", True, 1),
        (lambda site: site.creator_projections.update(
            {"author-retry-creator": {"status": "rejected", "reason": "projection_avatar_check_incomplete"}}),
         "projection_avatar_check_incomplete", False, 1),
    ],
    ids=["tab_not_opened", "creator_mismatch", "projection_empty", "avatar_check_incomplete"],
)
async def test_creator_failure_is_a_candidate_skip_and_later_candidate_continues(
    monkeypatch, tmp_path, setup, reason, retryable, attempts,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(
        monkeypatch, tmp_path, items=["retry-creator", "success-creator"],
    )
    setup(site)

    await crawler.search()

    assert [record["note_id"] for record in crawler.records] == ["success-creator"]
    assert "retry-creator" not in crawler.staged
    (skipped,) = events_of(state_path, "candidate_skipped")
    assert skipped["identity"] == "retry-creator"
    assert skipped["failure_scope"] == "post"
    assert skipped["error_code"] == "creator_profile_unavailable"
    assert skipped["detail"] == f"creator_profile_failed:{reason}"
    assert skipped["attempts"] == attempts and skipped["retryable"] is retryable
    assert site.overlay_note is None and all(tab.closed for tab in site.author_tabs)
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted"
    expected = ["success-creator"] if retryable else ["retry-creator", "success-creator"]
    assert stopped["candidate_identities"] == expected


HEX_NOTE = "681097f0000000000d009ccf"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "outcome", "attempts", "visible", "clicked_path", "clicks"),
    [
        (lambda site: site.hidden_author_notes.add(HEX_NOTE), "author_link_missing",
         ["link_missing", "link_missing"], 0, None, 0),
        (lambda site: setattr(site, "author_tab_failures", 2), "author_tab_timeout",
         ["tab_timeout", "tab_timeout"], 1, "/explore/<id>", 2),
    ],
    ids=["link_missing", "tab_timeout"],
)
async def test_author_tab_not_opened_records_a_link_diagnostic_without_query_values(
    monkeypatch, tmp_path, caplog, setup, outcome, attempts, visible, clicked_path, clicks,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=[HEX_NOTE, "success-creator"])
    setup(site)

    with caplog.at_level(logging.WARNING, logger="MediaCrawler"):
        await crawler.search()

    (skipped,) = events_of(state_path, "candidate_skipped")
    assert skipped["identity"] == HEX_NOTE and skipped["detail"] == "creator_profile_failed:author_tab_not_opened"
    assert [record["note_id"] for record in crawler.records] == ["success-creator"]
    author_clicks = [
        line for line in ledger_lines(crawler)
        if line.get("action") == "click_author" and line["target"]["note_id"] == HEX_NOTE
    ]
    assert len(author_clicks) == clicks and site.author_diagnostic_reads == 1
    written = json.loads((tmp_path / "behavior_evidence.navigation.json").read_text(encoding="utf-8"))
    (diagnostic,) = [event for event in written["events"] if event["stage"] == "creator_profile"]
    assert diagnostic["outcome"] == outcome and diagnostic["attempts"] == attempts and diagnostic["error"] == ""
    assert diagnostic["url"] == f"https://www.xiaohongshu.com/explore/{HEX_NOTE}" and "title" not in diagnostic
    assert diagnostic["view_visible"] is True
    assert (diagnostic["author_names"], diagnostic["author_names_visible"]) == (1, visible)
    assert (diagnostic["profile_links"], diagnostic["profile_links_visible"]) == (1, visible)
    assert (diagnostic["author_id_links"], diagnostic["author_id_links_visible"]) == (1, visible)
    (link,) = diagnostic["profile_link_nodes"]
    assert link["visible"] is bool(visible) and link["target"] == "_blank"
    assert link["chain"][:2] == ["a.name", "div.author"]
    assert diagnostic.get("clicked_path") == clicked_path
    assert "token" not in json.dumps(diagnostic)
    warning = next(record.getMessage() for record in caplog.records if "Author tab not opened" in record.getMessage())
    assert f"outcome={outcome}, attempts={attempts}, view_visible=True, author_names=1, " in warning
    assert f"author_names_visible={visible}" in warning and f"clicked_path={clicked_path}" in warning
    assert "xiaohongshu.com" not in warning and "token" not in warning


def _link(user_id: str, point: dict | None, *, comment: bool = False, named: bool = True) -> dict:
    return {"user_id": user_id, "point": dict(point) if point else None, "comment": comment, "named": named}


AUTHOR = f"author-{HEX_NOTE}"
# Mac 人工取证 2026-10-14 的浮层：旧位置 div.author 下的作者名隐藏在前，评论者的 a.name 也在 div.author 下，
# 作者本人的评论同样指向作者主页；可见的作者名在 interaction-container 的 author-container 里，旁边是头像链接。
AUTHOR_LINK_CASES = {
    "hidden_old_location": ([_link(AUTHOR, None), _link(AUTHOR, AUTHOR_POINT)], AUTHOR_POINT),
    "commenter": ([_link("commenter-1", COMMENT_POINT, comment=True), _link(AUTHOR, AUTHOR_POINT)], AUTHOR_POINT),
    "own_comment": (
        [_link(AUTHOR, None), _link(AUTHOR, COMMENT_POINT, comment=True), _link(AUTHOR, AUTHOR_POINT)], AUTHOR_POINT,
    ),
    "avatar_only": ([_link(AUTHOR, None), _link(AUTHOR, AVATAR_POINT, named=False)], AVATAR_POINT),
}


def test_author_click_point_prefers_a_visible_named_link_outside_the_comments():
    def links(case: str) -> list[dict]:
        return [
            {key: link[key] for key in ("point", "comment", "named")}
            for link in AUTHOR_LINK_CASES[case][0]
            if link["user_id"] == AUTHOR
        ]

    for case, (_, expected) in AUTHOR_LINK_CASES.items():
        assert xhs_author.author_click_point(links(case)) == expected, case
    named_comment_first = [
        {"point": dict(COMMENT_POINT), "comment": True, "named": True},
        {"point": dict(AVATAR_POINT), "comment": False, "named": False},
        {"point": dict(AUTHOR_POINT), "comment": False, "named": True},
    ]
    assert xhs_author.author_click_point(named_comment_first) == AUTHOR_POINT
    assert xhs_author.author_click_point([{"point": None, "comment": False, "named": True}]) is None
    assert xhs_author.author_click_point([{"point": dict(COMMENT_POINT), "comment": True, "named": True}]) is None
    assert xhs_author.author_click_point(None) is None and xhs_author.author_click_point([]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(AUTHOR_LINK_CASES))
async def test_author_link_is_located_by_the_author_id_outside_the_comments(monkeypatch, tmp_path, case):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=[HEX_NOTE])
    links, expected = AUTHOR_LINK_CASES[case]
    site.profile_links[HEX_NOTE] = links

    await crawler.search()

    assert [record["note_id"] for record in crawler.records] == [HEX_NOTE]
    assert not events_of(state_path, "candidate_skipped")
    assert len(site.author_tabs) == 1 and site.author_tabs[0].user_id == AUTHOR
    assert any(inside(expected, x, y) for x, y in site.click_points)
    assert not any(inside(COMMENT_POINT, x, y) for x, y in site.click_points) and site.comment_clicks == 0
    assert len([line for line in ledger_lines(crawler) if line.get("action") == "click_author"]) == 1
    assert last_stop(state_path)["stop_reason"] == "source_exhausted"


@pytest.mark.asyncio
async def test_login_expired_feed_recovers_and_redoes_the_whole_note(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["expired"])
    site.feed_overrides["expired"] = [dict(LOGIN_EXPIRED)]
    crawler._wait_for_midrun_login_recovery = AsyncMock(return_value=True)

    await crawler.search()

    crawler._wait_for_midrun_login_recovery.assert_awaited_once_with()
    assert detail_ids == ["expired", "expired"]
    (record,) = crawler.records
    detail = next(line for line in ledger_lines(crawler) if line.get("id") == record["content_visit_refs"]["note_detail"])
    clicks = [line for line in ledger_lines(crawler) if line.get("action") == "click_card"]
    assert len(clicks) == 2 and detail["trigger"] == clicks[1]["id"]
    assert last_stop(state_path)["stop_reason"] == "source_exhausted"


@pytest.mark.asyncio
async def test_detail_network_outage_pauses_and_redoes_the_note(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["network-detail"])
    site.feed_overrides["network-detail"] = ["net::ERR_INTERNET_DISCONNECTED"]
    sleeps = attach_network_clock(monkeypatch, crawler, wait_seconds=10.0)

    await crawler.search()

    assert detail_ids == ["network-detail", "network-detail"]
    assert network_sleeps(sleeps) == [2.0]
    outcomes = [call.kwargs["outcome"] for call in crawler._record_network_recovery_event.await_args_list]
    assert outcomes == ["network_paused", "network_recovered"]
    assert [record["note_id"] for record in crawler.records] == ["network-detail"]
    assert last_stop(state_path)["stop_reason"] == "source_exhausted"


@pytest.mark.asyncio
async def test_frontend_verification_on_feed_waits_and_redoes_the_note(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["verify"])
    site.feed_overrides["verify"] = [461]
    crawler._wait_for_frontend_verification = AsyncMock(return_value=True)

    await crawler.search()

    assert crawler._wait_for_frontend_verification.await_args.kwargs["required"] is True
    assert detail_ids == ["verify", "verify"]
    assert [record["note_id"] for record in crawler.records] == ["verify"]


def expire_login_on_page_two(site: XhsFakeSite, *, times: int = 1) -> None:
    site.search_overrides[2] = [dict(LOGIN_EXPIRED) for _ in range(times)]


@pytest.mark.asyncio
async def test_search_login_expiry_waits_and_retriggers_the_same_session(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    expire_login_on_page_two(site)
    crawler._wait_for_midrun_login_recovery = AsyncMock(return_value=True)

    await crawler.search()

    crawler._wait_for_midrun_login_recovery.assert_awaited_once_with()
    assert [request["page"] for request in site.requests if "filters" in request] == [1, 2, 2]
    assert len({request["search_id"] for request in site.requests if "filters" in request}) == 1
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted"
    assert stopped["stop_detail"] == "has_more_false"


@pytest.mark.asyncio
async def test_search_login_expiry_without_recovery_stops_as_login_required(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    expire_login_on_page_two(site)
    crawler._wait_for_midrun_login_recovery = AsyncMock(return_value=False)

    await crawler.search()

    assert_runtime_stop(last_stop(state_path), "login_required", source_page=ANY)


@pytest.mark.asyncio
async def test_main_page_closed_in_search_login_recovery_stops_at_current_page(monkeypatch, tmp_path):
    """第 1 页完成并发布；第 2 页登录过期，同时主页面关闭（浏览器仍存活）。"""
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path, site = prepare_crawler(
        monkeypatch, tmp_path, items=None, pages=[[note("note-confirmed")], [note("never")]],
    )
    closed = {"value": False}
    site.page.is_closed = lambda: closed["value"]

    async def still_behavior(keyword):
        return {"status": "completed"}

    # 行为阶段不滚动：第 2 页只在第 1 页处理完、扫描向下滚动时才请求。
    crawler._run_human_behavior_on_primary_page = still_behavior

    def close_on_page_two(number, request_json):
        if number == 2 and "filters" in request_json:
            closed["value"] = True

    site.before_respond = close_on_page_two
    expire_login_on_page_two(site)
    crawler.cdp_manager = Mock(assert_alive=Mock())
    published = []
    monkeypatch.setattr(app_events, "_batch_publisher", published.append)

    await crawler.search()

    crawler.cdp_manager.assert_alive.assert_any_call("midrun_login_recovery")
    assert detail_ids == ["note-confirmed"]
    assert not events_of(state_path, "candidate_skipped")
    completed = events_of(state_path, "adaptive_batch_completed")
    assert [(batch["source_page"], batch["resume_page"]) for batch in completed] == [(1, None)]
    assert published == completed
    assert_runtime_stop(last_stop(state_path), "main_page_closed", source_page=2)


class TargetClosedError(PlaywrightError):
    """与 Playwright 同名的关闭异常；分类按类名识别。"""


LOGIN_RECOVERY_RUN_FAILURES = [
    (
        PlatformRuntimeError("xhs_rate_limited_terminal:midrun_login_recovery", code="xhs_rate_limited_terminal"),
        "xhs_rate_limited_terminal",
    ),
    (XHSManualWaitBudgetExhausted(), "xhs_manual_checkpoint_budget_exhausted"),
    (XHSNetworkRecoveryTimeout("midrun_login_confirmation", 600.0), "network_recovery_timeout"),
    (IPBlockError("Network connection error"), "ip_blocked_300012"),
    (PlaywrightError("Protocol error (Runtime.evaluate): boom"), "browser_runtime_failed"),
    (TargetClosedError("Target page, context or browser has been closed"), "browser_context_closed"),
    (XHSMainPageClosedUnexpected(stage="midrun_login_recovery"), "main_page_closed"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "stop_detail"),
    LOGIN_RECOVERY_RUN_FAILURES,
    ids=[
        "terminal_popup", "manual_budget", "network_timeout", "ip_block",
        "playwright", "target_closed", "main_page_closed",
    ],
)
async def test_search_login_recovery_run_failure_writes_structured_stop(
    monkeypatch, tmp_path, failure, stop_detail,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=[])
    site.search_overrides[1] = [dict(LOGIN_EXPIRED)]
    crawler._wait_for_midrun_login_recovery = AsyncMock(side_effect=failure)

    await crawler.search()

    crawler._wait_for_midrun_login_recovery.assert_awaited_once_with()
    assert detail_ids == []
    assert not events_of(state_path, "candidate_skipped")
    assert not events_of(state_path, "adaptive_batch_completed")
    (stopped,) = events_of(state_path, "adaptive_search_stopped")
    assert stopped["stop_reason"] == "runtime_failed"
    assert stopped["stop_detail"] == stop_detail
    assert stopped["resume_page"] is None and stopped["batch_complete"] is False


@pytest.mark.asyncio
async def test_search_login_recovery_terminal_popup_stops_through_real_recovery(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    expire_login_on_page_two(site)
    crawler.cdp_manager = Mock(assert_alive=Mock())
    crawler._popup_checkpoint_state = AsyncMock(
        return_value={"closed": False, "visible_text": "访问频繁", "manual_markers": [], "terminal": "rate_limited"}
    )
    crawler._profile_ui_visible = AsyncMock(return_value=False)

    await crawler.search()

    crawler._popup_checkpoint_state.assert_awaited_once()
    crawler._profile_ui_visible.assert_not_awaited()
    assert_runtime_stop(last_stop(state_path), "xhs_rate_limited_terminal", source_page=ANY)


@pytest.mark.asyncio
async def test_search_login_recovery_unclassified_error_still_escapes(monkeypatch, tmp_path):
    # 未识别异常照常上抛，不伪造停止事件或来源耗尽。
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    expire_login_on_page_two(site)
    failure = ValueError("unexpected recovery bug")
    crawler._wait_for_midrun_login_recovery = AsyncMock(side_effect=failure)

    with pytest.raises(ValueError) as exc_info:
        await crawler.search()

    assert exc_info.value is failure
    assert not events_of(state_path, "adaptive_search_stopped")




POST_INTERACTION_RUN_FAILURES = [
    (lifecycle_error("xhs_browser_context_closed_unexpected", stage="post_interaction"), "browser_context_closed"),
    (lifecycle_error("xhs_browser_process_exited", stage="post_interaction"), "browser_process_exited"),
    (lifecycle_error("xhs_cdp_disconnected_unexpected", stage="post_interaction"), "cdp_disconnected"),
    (XHSNetworkRecoveryTimeout("post_interaction", 600.0), "network_recovery_timeout"),
]


def prepare_post_interaction(monkeypatch, tmp_path, failure):
    """真实帖子互动入口：在当前浮层内执行，不新开标签页；互动本身按用例抛错。"""
    crawler, detail_ids, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["note-interacted"])
    crawler.post_interaction_mode = "like-one"
    crawler.post_interaction_attempted = False
    interaction = AsyncMock(side_effect=failure)
    monkeypatch.setattr(xhs_behavior, "run_requested_post_interaction", interaction)
    crawler._new_guarded_page = AsyncMock()
    return crawler, state_path, site, interaction


@pytest.mark.asyncio
@pytest.mark.parametrize(("failure", "stop_detail"), POST_INTERACTION_RUN_FAILURES)
async def test_post_interaction_lifecycle_or_network_timeout_fails_the_run(
    monkeypatch, tmp_path, failure, stop_detail
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, state_path, site, interaction = prepare_post_interaction(monkeypatch, tmp_path, failure)

    await crawler.search()

    assert interaction.await_args.args[0] is site.page
    crawler._new_guarded_page.assert_not_awaited()
    crawler.update_xhs_note.assert_not_awaited()
    assert not events_of(state_path, "candidate_skipped")
    assert not events_of(state_path, "adaptive_batch_completed")
    assert_runtime_stop(last_stop(state_path), stop_detail)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [PlaywrightError("Target page, context or browser has been closed"), RuntimeError("xhs_post_interaction_button_missing")],
    ids=["page_closed", "plain_runtime_error"],
)
async def test_post_interaction_own_failure_does_not_stop_crawl(monkeypatch, tmp_path, failure):
    # 互动自身失败只记告警，照常关闭浮层、入库并继续到来源耗尽。
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, state_path, site, interaction = prepare_post_interaction(monkeypatch, tmp_path, failure)

    await crawler.search()

    interaction.assert_awaited_once()
    assert crawler.post_interaction_attempted is True
    crawler._new_guarded_page.assert_not_awaited()
    assert [record["note_id"] for record in crawler.records] == ["note-interacted"]
    assert not events_of(state_path, "candidate_skipped")
    assert last_stop(state_path)["stop_reason"] == "source_exhausted"


@pytest.mark.asyncio
async def test_post_interaction_runs_inside_the_overlay_before_it_closes(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, state_path, site, interaction = prepare_post_interaction(monkeypatch, tmp_path, None)
    seen = []
    interaction.side_effect = lambda page, **kwargs: seen.append((site.overlay_note, kwargs["note_id"])) or {
        "status": "completed"
    }

    await crawler.search()

    assert seen == [("note-interacted", "note-interacted")]
    assert interaction.await_args.kwargs["requested_mode"] == "like-one"


def attach_network_clock(monkeypatch, crawler, *, wait_seconds: float) -> list[float]:
    crawler._record_network_recovery_event = AsyncMock()
    clock = {"now": 50.0}
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock["now"]

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    crawler._popup_monotonic = monotonic
    crawler._popup_sleep = sleep
    monkeypatch.setattr(xhs_author, "XHS_AUTHOR_TAB_HOLD_SECONDS", (XHS_NEW_PAGE_MIN_HOLD_SECONDS,) * 2)
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_NETWORK_WAIT_SECONDS", str(wait_seconds))
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_NETWORK_RETRY_MIN_SECONDS", "2")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_NETWORK_RETRY_MAX_SECONDS", "4")
    return sleeps


@pytest.mark.asyncio
async def test_search_disconnect_pauses_and_rescrolls_in_the_same_browser_session(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    site.search_overrides[2] = ["net::ERR_INTERNET_DISCONNECTED"]
    sleeps = attach_network_clock(monkeypatch, crawler, wait_seconds=10.0)
    context, page = crawler.browser_context, crawler.context_page

    await crawler.search()

    assert [request["page"] for request in site.requests if "filters" in request] == [1, 2, 2]
    assert network_sleeps(sleeps) == [2.0]
    assert crawler.browser_context is context and crawler.context_page is page
    outcomes = [call.kwargs["outcome"] for call in crawler._record_network_recovery_event.await_args_list]
    assert outcomes == ["network_paused", "network_recovered"]
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted"
    assert stopped["stop_detail"] == "has_more_false"


@pytest.mark.asyncio
async def test_search_disconnect_timeout_is_a_runtime_stop(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    site.search_overrides[2] = ["net::ERR_INTERNET_DISCONNECTED"] * 5
    sleeps = attach_network_clock(monkeypatch, crawler, wait_seconds=5.0)

    await crawler.search()

    assert network_sleeps(sleeps) == [2.0, 3.0]
    assert_runtime_stop(last_stop(state_path), "network_recovery_timeout", source_page=ANY)


@pytest.mark.asyncio
async def test_unrecoverable_search_request_failure_is_a_runtime_stop(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    site.search_overrides[2] = ["net::ERR_ABORTED"]

    await crawler.search()

    assert_runtime_stop(last_stop(state_path), "search_request_failed", source_page=ANY)


@pytest.mark.parametrize(
    ("code", "stop_detail"),
    [
        ("xhs_browser_process_exited", "browser_process_exited"),
        ("xhs_browser_context_closed_unexpected", "browser_context_closed"),
        ("xhs_cdp_disconnected_unexpected", "cdp_disconnected"),
        ("xhs_main_page_closed_unexpected", "main_page_closed"),
    ],
)
@pytest.mark.asyncio
async def test_cdp_lifecycle_failure_stops_without_retry_or_relaunch(code, stop_detail, monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    crawler._assert_primary_page_alive = Mock(side_effect=lifecycle_error(code, stage="full_scan"))
    crawler.launch_browser_with_cdp = AsyncMock()

    await crawler.search()

    assert detail_ids == []
    crawler.launch_browser_with_cdp.assert_not_awaited()
    assert not events_of(state_path, "adaptive_batch_completed")
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "runtime_failed"
    assert stopped["stop_detail"] == stop_detail
    assert stopped["resume_page"] is None and stopped["batch_complete"] is False
    assert stopped.get("candidate_identities") in (None, [])


def test_main_page_closed_is_lifecycle_failure_with_unchanged_message() -> None:
    exc = XHSMainPageClosedUnexpected(stage="behavior")

    assert isinstance(exc, CDPBrowserLifecycleError)
    assert str(exc) == "xhs_main_page_closed_unexpected:stage=behavior"
    assert xhs_cdp_lifecycle_stop_detail(exc) == "main_page_closed"
    assert xhs_cdp_lifecycle_stop_detail(lifecycle_error("xhs_unknown_lifecycle")) == "browser_runtime_failed"


@pytest.mark.asyncio
async def test_cdp_disconnect_during_network_pause_is_a_lifecycle_stop(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    site.search_overrides[2] = ["net::ERR_INTERNET_DISCONNECTED"]
    crawler._pause_for_network_recovery = AsyncMock(
        side_effect=lifecycle_error("xhs_cdp_disconnected_unexpected", stage="full_scan")
    )

    await crawler.search()

    assert crawler._pause_for_network_recovery.await_count == 1
    assert_runtime_stop(last_stop(state_path), "cdp_disconnected", source_page=ANY)




@pytest.mark.asyncio
async def test_cdp_process_exit_while_opening_the_overlay_is_not_candidate_failure(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["new-note"])
    crawler._open_note_overlay = AsyncMock(
        side_effect=lifecycle_error("xhs_browser_process_exited", stage="detail:new-note")
    )

    await crawler.search()

    assert crawler._open_note_overlay.await_count == 1
    assert not events_of(state_path, "candidate_skipped")
    assert not events_of(state_path, "adaptive_batch_completed")
    assert_runtime_stop(last_stop(state_path), "browser_process_exited")


@pytest.mark.asyncio
async def test_cdp_context_close_from_creator_is_not_candidate_failure(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["new-note"])
    crawler._enrich_creator_via_author_tab = AsyncMock(
        side_effect=lifecycle_error("xhs_browser_context_closed_unexpected", stage="creator:new-note")
    )

    await crawler.search()

    assert crawler._enrich_creator_via_author_tab.await_count == 1
    assert not events_of(state_path, "candidate_skipped")
    assert not events_of(state_path, "adaptive_batch_completed")
    assert_runtime_stop(last_stop(state_path), "browser_context_closed")
