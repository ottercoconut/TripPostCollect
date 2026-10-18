"""B2 全量滚动扫描：首页输入、点选“图文”、目标会话逐页登记、翻页停顿不变式与来源耗尽，并以门禁回读账本。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import random
from types import SimpleNamespace

import pytest

import trippostcollect.platforms.xhs.behavior as xhs_behavior
import trippostcollect.platforms.xhs.core as xhs_core
import trippostcollect.platforms.xhs.navigation as xhs_navigation
import trippostcollect.platforms.xhs.scan as xhs_scan
import trippostcollect.runtime.behavior as runtime_behavior
from support.xhs_fake_site import FILTER_ENTRY, FILTER_OPTION, FILTER_PANEL, LIST_TOP, XhsFakeSite, inside, note
from trippostcollect.application.candidates import AdaptiveAccumulator
from trippostcollect.application.visit_gate import validate_content_visits
from trippostcollect.artifacts.evidence import write_evidence
from trippostcollect.platforms.xhs.errors import PlatformRuntimeError
from trippostcollect.platforms.xhs.navigation import XhsSearchEntryFailed
from trippostcollect.platforms.xhs.page_scripts import DOM_SCRIPTS
from trippostcollect.runtime.visit_ledger import iter_ledger_lines, line_seq

from .support import XiaoHongShuCrawler, make_capture

KEYWORD = "青岛旅行"


async def _no_sleep(*_args, **_kwargs) -> None:
    await asyncio.sleep(0)


class Harness:
    def __init__(self, tmp_path: Path, site: XhsFakeSite, *, known: tuple[str, ...] = ()) -> None:
        self.site = site
        crawler = self.crawler = XiaoHongShuCrawler()
        crawler.context_page = site.page
        crawler.browser_context = site.context
        crawler.capture = make_capture(tmp_path, context=site.context)
        crawler.capture.install(site.page)
        crawler._scan_sleep = _no_sleep
        crawler._popup_sleep = _no_sleep
        self.pauses: list[tuple[str, int]] = []
        self.processed: list[str] = []
        self.behavior_runs = 0
        crawler._guarded_pause = self.guarded_pause
        crawler._run_human_behavior_on_primary_page = self.behavior
        crawler._process_note_card = self.process_note
        crawler._popup_checkpoint_state = self.popup_state
        self.events: list[tuple[str, dict]] = []
        self.accumulator = AdaptiveAccumulator.for_platform(
            "xhs", existing_identities=set(known), persist_retryable_skips=False,
        )
        self.accumulator.event_sink = lambda kind, details: self.events.append((kind, details))

    async def guarded_pause(self, stage: str, minimum: float, maximum: float) -> float:
        capture = self.crawler.capture
        start = capture.pause_started()
        self.pauses.append((stage, start))
        await asyncio.sleep(0)
        continuity = "completed" if stage in {"search_results", "search_page"} else "n/a"
        capture.pause_finished(stage, start, 0.0, continuity)
        return 0.0

    async def behavior(self, keyword: str) -> dict:
        assert keyword == KEYWORD
        self.behavior_runs += 1
        self.site.wheel(700)
        self.site.wheel(-300)
        return {"status": "completed"}

    async def process_note(self, item, search_page, card) -> None:
        assert search_page is self.crawler._scan.tracker.current
        self.processed.append(item.note_id)
        self.crawler._scan.accumulator.consider(item.note_id, valid=True)

    async def popup_state(self, page) -> dict:
        return {"manual_markers": [], "visible_text": "ok", "terminal": ""}

    def run(self, keyword: str = KEYWORD) -> None:
        asyncio.run(self.crawler._run_full_scan(keyword, self.accumulator))

    def ledger(self) -> list[dict]:
        return list(iter_ledger_lines(self.crawler.capture.ledger.path))

    def gate(self) -> dict:
        return validate_content_visits(
            [],
            [self.crawler.capture.ledger.path],
            platform="xhs",
            mode="discovery",
            manifests=[],
            stop_event=self.accumulator.summary(),
        )


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xhs_scan, "merge_pending_behavior_evidence", lambda pending, ports: None)
    monkeypatch.setattr(xhs_scan, "XHS_SEARCH_LOAD_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(xhs_scan, "XHS_SEARCH_CAPTURE_SECONDS", 0.2)
    monkeypatch.setattr(xhs_behavior, "XHS_VERIFICATION_SURFACE_SECONDS", 2.0)


def _pages(count: int, per_page: int, *, prefix: str = "n") -> list[list[dict]]:
    return [[note(f"{prefix}{page}-{index}") for index in range(per_page)] for page in range(1, count + 1)]


def _record_sleeps_and_clicks(harness: Harness) -> list[tuple]:
    """把扫描侧的等待与主页面点击按发生顺序记成 ("sleep", 秒) / ("click", x, y)；点击在抬起时按光标位置记。"""
    events: list[tuple] = []
    mouse = harness.site.page.mouse
    up = mouse.up

    async def sleep(seconds: float) -> None:
        events.append(("sleep", seconds))
        await asyncio.sleep(0)

    async def recording_up() -> None:
        events.append(("click", *harness.site.mouse_position))
        await up()

    harness.crawler._scan_sleep = sleep
    mouse.up = recording_up
    return events


def _clicks_inside(events: list[tuple], target: dict) -> list[int]:
    return [index for index, event in enumerate(events) if event[0] == "click" and inside(target, *event[1:])]


def _filter_diagnostics(harness: Harness) -> list[dict]:
    return [event for event in harness.crawler._navigation_diagnostics if event["stage"] == "search_filter"]


def test_two_page_keyword_is_exhausted_and_passes_entry_page_and_exhaustion_gates(tmp_path):
    site = XhsFakeSite(KEYWORD, [[note("n1"), note("v1", card_type="video"), note("n3"), note("old"), note("n4")]])
    harness = Harness(tmp_path, site, known=("old",))

    harness.run()

    assert harness.processed == ["n1", "n3", "n4"]
    assert harness.behavior_runs == 1
    summary = harness.accumulator.summary()
    assert summary["stop_reason"] == "source_exhausted"
    assert summary["stop_detail"] == "has_more_false"
    assert summary["discovery_phase"] == "full_scan"
    assert summary["source_page"] == 2 and summary["resume_page"] is None
    batches = [details for kind, details in harness.events if kind == "adaptive_batch_completed"]
    assert [(item["source_page"], item["batch_complete"]) for item in batches] == [(1, True), (2, True)]

    lines = harness.ledger()
    decisions = [line for line in lines if line["kind"] == "decision"]
    assert [(line["subject_id"], line["decision"]) for line in decisions] == [("v1", "video_skipped_from_search")]
    actions = [line for line in lines if line["kind"] == "action"]
    assert [line["action"] for line in actions[:6]] == [
        "type_search", "press_enter", "apply_filter", "apply_filter", "apply_filter", "scroll",
    ]
    assert [line["target"].get("filter") for line in actions[2:5]] == ["panel_open", "note_type", "panel_close"]
    behavior_scroll = actions[5]
    assert behavior_scroll["target"] == {"phase": "behavior_stage"}
    # 行为阶段的滚轮之前恰有一次翻页停顿，且在目标会话第 1 页之后开始。
    searches = [line for line in lines if line["kind"] == "visit" and line["stage"] == "search_results"]
    assert [(line["is_target"], line["search_page"]) for line in searches] == [(False, 1), (True, 1), (True, 2)]
    target_first = searches[1]
    assert target_first["keyword_matches_target"] is True
    assert KEYWORD not in json.dumps(lines, ensure_ascii=False)
    page_pauses = [line for line in lines if line["kind"] == "pause" and line["stage"] == "search_page"]
    assert page_pauses and target_first["seq"] < page_pauses[0]["start_seq"] < page_pauses[0]["seq"] < behavior_scroll["seq"]
    assert all(item["id"] != "hot-1" for item in target_first["items"])

    gate = harness.gate()
    assert gate["content_visit_ok"], gate
    assert gate["required_stages"] == ["search_page"]
    assert gate["continuity_ok"] is True


def test_every_scroll_after_a_has_more_page_follows_a_page_pause(tmp_path):
    site = XhsFakeSite(KEYWORD, _pages(4, 8))
    harness = Harness(tmp_path, site)

    harness.run()

    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted"
    assert len(harness.processed) == 32 and len(set(harness.processed)) == 32
    batches = [details["source_page"] for kind, details in harness.events if kind == "adaptive_batch_completed"]
    assert batches == [1, 2, 3, 4, 5]
    lines = sorted(harness.ledger()[1:], key=lambda line: line["seq"])
    owed = 0
    cleared = 0
    for line in lines:
        if line["kind"] == "visit" and line.get("is_target") and line.get("has_more") is True:
            owed = line["seq"]
        elif line["kind"] == "pause" and line["stage"] == "search_page":
            cleared = max(cleared, line["start_seq"] - 1)
        elif line["kind"] == "action" and line["action"] == "scroll":
            assert owed <= cleared, line
    gate = harness.gate()
    assert gate["content_visit_ok"], gate
    assert "search_page" in gate["required_stages"]


def test_known_pages_are_scrolled_past_without_search_results_pause(tmp_path):
    pages = _pages(2, 8, prefix="k")
    known = tuple(item["id"] for items in pages for item in items)
    site = XhsFakeSite(KEYWORD, pages)
    harness = Harness(tmp_path, site, known=known)

    harness.run()

    assert harness.processed == []
    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted"
    assert {stage for stage, _ in harness.pauses} == {"search_page"}
    assert harness.gate()["content_visit_ok"]


def test_card_without_registration_for_two_steps_stops_with_card_and_response_diagnostic(
    tmp_path, monkeypatch, caplog,
):
    evidence = tmp_path / "behavior_evidence.json"
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(evidence))
    # stray 不在任何响应里；general-1 只在回车会话（非目标）第 1 页；hot-1 是目标会话第 1 页的热搜条目（已知非笔记
    # 条目，跳过，不进诊断）。
    site = XhsFakeSite(KEYWORD, _pages(2, 4), unregistered_ids=("stray", "hot-1", "general-1"))
    harness = Harness(tmp_path, site)

    with caplog.at_level(logging.WARNING, logger="MediaCrawler"), pytest.raises(PlatformRuntimeError) as caught:
        harness.run()

    assert caught.value.code == "xhs_search_card_unregistered"
    assert harness.processed == ["n1-0"]
    assert site.card_diagnostic_reads == [["general-1", "stray"]]
    written = json.loads(evidence.with_name("behavior_evidence.navigation.json").read_text(encoding="utf-8"))
    (diagnostic,) = [event for event in written["events"] if event["stage"] == "search_scan"]
    assert diagnostic["outcome"] == "card_unregistered" and diagnostic["error"] == ""
    assert diagnostic["url"] == "https://www.xiaohongshu.com/search_result_ai" and "title" not in diagnostic
    assert diagnostic["scroll_container"] == "document" and diagnostic["view_rect"]["top"] == 0
    cards = {card["note_id"]: card for card in diagnostic["cards"]}
    assert list(cards) == ["general-1", "stray"]
    assert cards["stray"]["responses"] is None
    assert cards["stray"]["dom"] == {
        "note_id": "stray", "nodes": 1, "data_index": "0", "has_cover": True, "cover_path": "/search_result",
        "card": "section.note-item", "first_child": "a.cover.mask",
        "position": {"left": 0, "top": LIST_TOP, "width": 240, "height": 340},
    }
    target = harness.crawler._scan.target
    enter = next(session for session in harness.crawler._scan.sessions.values() if not session.is_target)
    assert target.pages[1].non_notes_seen == {"hot-1"}
    (general,) = cards["general-1"]["responses"]
    assert general == {
        "seq": enter.started_seq, "session_seq": enter.started_seq, "is_target": False, "current_target": False,
        "page": 1, "position": 1, "model_type": "note", "card_type": "normal",
    }
    assert enter.started_seq < target.started_seq
    text = json.dumps(diagnostic, ensure_ascii=False)
    assert "token" not in text and "keyword" not in text and KEYWORD not in text
    warning = next(
        record.getMessage() for record in caplog.records if "Search card stayed unregistered" in record.getMessage()
    )
    assert "hot-1" not in warning
    assert "general-1(index=2, cover=/search_result, card=section.note-item, seen=non_target:p1:note)" in warning
    assert "stray(index=0" in warning and "seen=none" in warning
    assert "xiaohongshu.com" not in warning and "token" not in warning


def test_page_item_that_never_renders_stops_with_an_items_never_rendered_diagnostic(tmp_path, monkeypatch, caplog):
    evidence = tmp_path / "behavior_evidence.json"
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(evidence))
    # 第 1 页的 n1-1 从不渲染，第 2 页的响应里又出现一次（跨页重复）；第 2 页首张卡片 n2-0 与第 1 页同在首行，
    # 一出现就把第 1 页判为越过。
    pages = _pages(2, 4)
    pages[1].append(note("n1-1"))
    site = XhsFakeSite(KEYWORD, pages)
    site.unrendered_ids = {"n1-1"}
    harness = Harness(tmp_path, site)

    with caplog.at_level(logging.WARNING, logger="MediaCrawler"), pytest.raises(PlatformRuntimeError) as caught:
        harness.run()

    assert caught.value.code == "xhs_search_items_unresolved"
    assert str(caught.value) == "XHS search items were never rendered"
    assert harness.processed == ["n1-0", "n1-2", "n1-3"]
    assert site.card_diagnostic_reads == [["n1-1"]]
    written = json.loads(evidence.with_name("behavior_evidence.navigation.json").read_text(encoding="utf-8"))
    (diagnostic,) = [event for event in written["events"] if event["stage"] == "search_scan"]
    page_fields = {
        "page": 1, "page_items": 4, "undecided": 1, "never_rendered": 1, "observed": 3,
        "observed_index_range": [0, 2], "non_note_cards_seen": 0,
        "passed_by": {"reason": "later_page", "page": 2, "note_id": "n2-0", "position": 0, "data_index": 3,
                      "top": LIST_TOP, "scroll_y": 0},
        "visible_index_range": [0, 6], "scroll_container": "document", "scroll_y": 0,
        "scroll_height": LIST_TOP + 2 * 360 + 200, "inner_height": 900,
        "view_rect": {"left": 0.0, "top": 0.0, "right": 1280.0, "bottom": 900.0},
        # 列表已在顶部：不再向上补漏，直接停止。
        "backfill": {"attempts": 0, "limit": 3, "target_index": 1, "at_top": True, "steps": []},
    }
    assert {key: diagnostic[key] for key in page_fields} == page_fields
    assert diagnostic["outcome"] == "items_never_rendered" and diagnostic["error"] == ""
    target = harness.crawler._scan.target
    (item,) = diagnostic["items"]
    assert item == {
        "note_id": "n1-1", "position": 1, "card_type": "normal", "other_pages": [2],
        "dom": {"note_id": "n1-1", "nodes": 0},
        "responses": [
            {"seq": target.pages[page].seq, "session_seq": target.started_seq, "is_target": True,
             "current_target": True, "page": page, "position": position, "model_type": "note", "card_type": "normal"}
            for page, position in ((1, 1), (2, 4))
        ],
    }
    text = json.dumps(diagnostic, ensure_ascii=False)
    assert "token" not in text and "keyword" not in text and KEYWORD not in text
    warning = next(
        record.getMessage() for record in caplog.records if "Search items were never rendered" in record.getMessage()
    )
    assert warning.endswith(
        "page=1, items=4, undecided=1, never_rendered=1, observed=3, observed_index=[0, 2], visible_index=[0, 6], "
        f"passed_by=later_page:p2:index=3, scroll_y=0/{LIST_TOP + 2 * 360 + 200}, backfill=0/3:target=1:top=True; "
        "n1-1(pos=1, type=normal, nodes=0, index=None, other_pages=[2], seen=current:p1:note+current:p2:note)"
    )
    assert "xiaohongshu.com" not in warning and "token" not in warning


def test_target_session_started_before_the_note_type_click_is_logged_with_its_trigger(tmp_path, caplog):
    site = XhsFakeSite(KEYWORD, _pages(1, 4))
    site.entry_click_applies_filter = True
    harness = Harness(tmp_path, site)

    with caplog.at_level(logging.WARNING, logger="MediaCrawler"):
        harness.run()

    assert harness.processed == [f"n1-{index}" for index in range(4)]
    actions = {line["id"]: line for line in harness.ledger() if line["kind"] == "action"}
    filters = [line["target"].get("filter") for line in actions.values() if line["action"] == "apply_filter"]
    assert filters == ["panel_open", "note_type", "panel_close"]
    scan = harness.crawler._scan
    target = scan.target
    trigger = actions[target.trigger]
    assert trigger["target"] == {"filter": "panel_open"}
    enter = next(session for session in scan.sessions.values() if not session.is_target)
    warning = next(
        record.getMessage() for record in caplog.records if "not triggered by this note-type click" in record.getMessage()
    )
    assert (
        f'trigger=apply_filter, trigger_target={{"filter": "panel_open"}}, trigger_seq={line_seq(trigger["id"])}, '
        f"target_session_seq={target.started_seq}, enter_session_seq={enter.started_seq}, "
        "enter_session_target=False, note_type_clicks=1"
    ) in warning
    assert not harness.gate()["content_visit_ok"]


def test_target_session_from_the_note_type_click_logs_no_trigger_warning(tmp_path, caplog):
    site = XhsFakeSite(KEYWORD, _pages(1, 4))
    harness = Harness(tmp_path, site)

    with caplog.at_level(logging.WARNING, logger="MediaCrawler"):
        harness.run()

    assert harness.processed == [f"n1-{index}" for index in range(4)]
    assert not any("not triggered by this note-type click" in record.getMessage() for record in caplog.records)


def test_non_note_cards_of_the_session_are_skipped_and_do_not_hold_the_page_open(tmp_path, caplog):
    # Mac 人工取证 2026-10-14：hot_query 条目在列表里占一个位置，渲染为没有封面的卡片，id 为 50 位非十六进制。
    hot = "related_query_qingdao_xiaoyushan_offline_card_0001"
    pages = _pages(2, 4)
    pages[0].insert(2, {"id": hot, "model_type": "hot_query"})
    site = XhsFakeSite(KEYWORD, pages)
    harness = Harness(tmp_path, site)

    with caplog.at_level(logging.INFO, logger="MediaCrawler"):
        harness.run()

    assert len(hot) == 50
    assert harness.processed == [f"n{page}-{index}" for page in (1, 2) for index in range(4)]
    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted"
    first = harness.crawler._scan.target.pages[1]
    assert first.passed and first.finished
    assert first.non_note_ids == {hot, "hot-1"} and first.non_notes_seen == {hot}
    assert not harness.accumulator.is_known(hot)
    assert not any(line.get("subject_id") == hot or (line.get("target") or {}).get("note_id") == hot
                   for line in harness.ledger())
    assert not any(record.getMessage().startswith("[XiaoHongShuCrawler.scan] Search card stayed")
                   for record in caplog.records)
    finished = [record.getMessage() for record in caplog.records if "finished: non_note_items" in record.getMessage()]
    assert finished[0].endswith("Search page 1 finished: non_note_items=2, non_note_cards_skipped=1")
    assert harness.gate()["content_visit_ok"]


def test_navigation_diagnostics_keep_only_paths_and_never_the_page_title(tmp_path):
    site = XhsFakeSite(KEYWORD, _pages(1, 1))
    crawler = Harness(tmp_path, site).crawler
    site.page.url = "https://www.xiaohongshu.com/explore/n1-0?xsec_token=secret-token&xsec_source=pc_search"
    crawler._install_navigation_observers(site.page)
    failed = site.page.request(
        "GET", "https://fe-static.xhscdn.com/formula-static/app.js?v=secret-token#x", resource_type="script",
    )
    site.page.fail(failed, "net::ERR_FAILED")

    diagnostic = asyncio.run(crawler._record_navigation_diagnostic(site.page, stage="search_entry", outcome="probe"))

    assert diagnostic["url"] == "https://www.xiaohongshu.com/explore/n1-0" and "title" not in diagnostic
    assert [failure["url"] for failure in diagnostic["request_failures"]] == [
        "https://fe-static.xhscdn.com/formula-static/app.js",
    ]
    assert "secret-token" not in json.dumps(diagnostic)
    assert "document.title" not in DOM_SCRIPTS["render_diagnostic"]


def test_navigation_diagnostics_keep_recent_entries_per_stage_in_time_order(tmp_path, monkeypatch):
    evidence = tmp_path / "behavior_evidence.json"
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(evidence))
    crawler = Harness(tmp_path, XhsFakeSite(KEYWORD, _pages(1, 1))).crawler
    limit = xhs_navigation.XHS_NAVIGATION_DIAGNOSTICS_PER_STAGE

    async def record() -> None:
        await crawler._record_navigation_diagnostic(crawler.context_page, stage="search_filter", outcome="attempt_1:x")
        for index in range(limit + 15):
            await crawler._record_navigation_diagnostic(
                crawler.context_page, stage="creator_profile_parse", outcome=f"ok-{index}",
            )
        await crawler._record_navigation_diagnostic(crawler.context_page, stage="search_filter", outcome="attempt_2:y")

    asyncio.run(record())

    written = json.loads(evidence.with_name("behavior_evidence.navigation.json").read_text(encoding="utf-8"))
    assert written["schema_version"] == 1 and written["events"] == crawler._navigation_diagnostics
    outcomes = [(event["stage"], event["outcome"]) for event in written["events"]]
    assert outcomes == [
        ("search_filter", "attempt_1:x"),
        *(("creator_profile_parse", f"ok-{index}") for index in range(15, limit + 15)),
        ("search_filter", "attempt_2:y"),
    ]


def test_search_that_stops_loading_is_stalled_not_exhausted(tmp_path):
    site = XhsFakeSite(KEYWORD, _pages(3, 8), stall_after=2)
    harness = Harness(tmp_path, site)

    with pytest.raises(PlatformRuntimeError) as caught:
        harness.run()

    assert caught.value.code == "xhs_search_load_stalled"
    assert harness.accumulator.summary()["stop_reason"] == "running"
    stall_scrolls = [
        line for line in harness.ledger()
        if line["kind"] == "action" and line["action"] == "scroll" and line["target"].get("phase") == "stall"
    ]
    assert len(stall_scrolls) == 2 * xhs_scan.XHS_STALL_JIGGLES


def test_target_filter_that_never_applies_stops_after_one_retry(tmp_path):
    site = XhsFakeSite(KEYWORD, _pages(1, 4), apply_filter_works=False)
    harness = Harness(tmp_path, site)

    with pytest.raises(XhsSearchEntryFailed) as caught:
        harness.run()

    assert caught.value.code == "target_filter_not_applied"
    clicks = [
        line for line in harness.ledger()
        if line["kind"] == "action" and line["target"].get("filter") == "note_type"
    ]
    assert len(clicks) == 2
    assert [event["outcome"] for event in _filter_diagnostics(harness)] == [
        "attempt_1:filter_not_confirmed", "attempt_2:filter_not_confirmed", "filter_not_confirmed",
    ]


def test_filter_panel_that_stays_open_has_its_own_code(tmp_path):
    site = XhsFakeSite(KEYWORD, _pages(1, 4), panel_closes=False)
    harness = Harness(tmp_path, site)

    with pytest.raises(XhsSearchEntryFailed) as caught:
        harness.run()

    assert caught.value.code == "filter_panel_not_closed"
    assert not any(line.get("action") == "scroll" for line in harness.ledger())
    assert [event["outcome"] for event in _filter_diagnostics(harness)] == ["filter_panel_not_closed"]


def test_filter_entry_that_renders_late_is_awaited_then_clicked_after_a_reaction_pause(tmp_path):
    site = XhsFakeSite(KEYWORD, _pages(1, 4))
    site.entry_delay_reads = 5
    harness = Harness(tmp_path, site)
    events = _record_sleeps_and_clicks(harness)

    harness.run()

    assert harness.processed == [f"n1-{index}" for index in range(4)]
    entry_click = _clicks_inside(events, FILTER_ENTRY)[0]
    before = events[:entry_click]
    # 第 1 次读状态在开面板前，入口等待从第 2 次读起，第 6 次读到入口：其间轮询等待 4 次。
    assert before.count(("sleep", xhs_scan.XHS_FILTER_POLL_SECONDS)) == 4
    kind, reaction = before[-1]
    low, high = xhs_scan.XHS_BEFORE_FILTER_SECONDS
    assert kind == "sleep" and low <= reaction <= high
    filters = [line["target"].get("filter") for line in harness.ledger() if line.get("action") == "apply_filter"]
    assert filters == ["panel_open", "note_type", "panel_close"]
    assert not _filter_diagnostics(harness) and site.diagnostic_reads == 0


def test_filter_entry_that_never_renders_waits_out_both_attempts_then_stops_with_diagnostic(
    tmp_path, monkeypatch, caplog,
):
    evidence = tmp_path / "behavior_evidence.json"
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(evidence))
    site = XhsFakeSite(KEYWORD, _pages(1, 4))
    site.entry_delay_reads = None
    harness = Harness(tmp_path, site)
    events = _record_sleeps_and_clicks(harness)

    with caplog.at_level(logging.WARNING, logger="MediaCrawler"), pytest.raises(XhsSearchEntryFailed) as caught:
        harness.run()

    assert caught.value.code == "target_filter_not_applied"
    polls = round(xhs_scan.XHS_FILTER_ENTRY_WAIT_SECONDS / xhs_scan.XHS_FILTER_POLL_SECONDS)
    assert events.count(("sleep", xhs_scan.XHS_FILTER_POLL_SECONDS)) == 2 * polls
    assert not _clicks_inside(events, FILTER_ENTRY)
    assert not any(line.get("action") == "apply_filter" for line in harness.ledger())
    written = json.loads(evidence.with_name("behavior_evidence.navigation.json").read_text(encoding="utf-8"))
    *attempts, diagnostic = [event for event in written["events"] if event["stage"] == "search_filter"]
    assert [event["outcome"] for event in attempts] == ["attempt_1:filter_entry_missing", "attempt_2:filter_entry_missing"]
    assert all(event["moments"] == {} for event in attempts) and site.diagnostic_reads == 3
    assert diagnostic["outcome"] == "filter_entry_missing" and "moments" not in diagnostic
    assert diagnostic["layout_count"] == 1 and diagnostic["entry_candidates"] == 0
    assert diagnostic["entry_visible"] is False and diagnostic["label_chains"] == []
    warning = next(record.getMessage() for record in caplog.records if "Search filter failed" in record.getMessage())
    assert "reason=filter_entry_missing" in warning and "entry_candidates=0" in warning
    assert "xiaohongshu.com" not in warning


def test_filter_option_missing_on_first_attempt_reopens_the_panel_and_records_the_attempt(
    tmp_path, monkeypatch, caplog,
):
    evidence = tmp_path / "behavior_evidence.json"
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(evidence))
    site = XhsFakeSite(KEYWORD, _pages(1, 4))
    site.option_missing_opens = 1
    harness = Harness(tmp_path, site)

    with caplog.at_level(logging.WARNING, logger="MediaCrawler"):
        harness.run()

    assert harness.processed == [f"n1-{index}" for index in range(4)]
    filters = [line["target"].get("filter") for line in harness.ledger() if line.get("action") == "apply_filter"]
    assert filters == ["panel_open", "panel_close", "panel_open", "note_type", "panel_close"]
    written = json.loads(evidence.with_name("behavior_evidence.navigation.json").read_text(encoding="utf-8"))
    (diagnostic,) = [event for event in written["events"] if event["stage"] == "search_filter"]
    assert diagnostic["outcome"] == "attempt_1:filter_option_missing"
    assert diagnostic["panel_count"] == 1 and diagnostic["panel_visible_count"] == 1
    assert diagnostic["entry_candidates"] == 1 and diagnostic["label_chains"]
    closed = {"panel_visible": False, "option": False, "entry_text": "筛选"}
    no_option = {"panel_visible": True, "option": False, "entry_text": "筛选"}
    assert diagnostic["moments"] == {
        "before_approach": closed, "before_press": closed, "first_poll": no_option, "poll_end": no_option,
    }
    (warning,) = [record.getMessage() for record in caplog.records if "Target filter attempt" in record.getMessage()]
    assert "attempt 1 failed: filter_option_missing" in warning and "panel_visible_count=1" in warning
    assert '"before_press":{"panel_visible":false,"option":false,"entry_text":"筛选"}' in warning
    assert "xiaohongshu.com" not in warning and not any("Search filter failed" in r.getMessage() for r in caplog.records)


def _record_pointer(harness: Harness) -> list[tuple]:
    """主页面的移动与抬起按发生顺序记成 ("move", x, y, 移动后面板是否开着) / ("up", x, y)，筛选账本动作记成
    ("action", filter)。"""
    events: list[tuple] = []
    site = harness.site
    mouse = site.page.mouse
    move, up = mouse.move, mouse.up
    record = harness.crawler._record_trigger_action

    def recording_action(action, target, **kwargs):
        if action == "apply_filter":
            events.append(("action", target.get("filter")))
        return record(action, target, **kwargs)

    async def recording_move(x: float, y: float, steps: int = 1) -> None:
        await move(x, y, steps)
        events.append(("move", x, y, site.panel_visible))

    async def recording_up() -> None:
        events.append(("up", *site.mouse_position))
        await up()

    mouse.move, mouse.up = recording_move, recording_up
    harness.crawler._record_trigger_action = recording_action
    return events


def _option_approach(events: list[tuple]) -> list[tuple]:
    """最后一次 note_type 动作之后、按下“图文”之前的移动（即入口到选项这一段）。"""
    start = max(index for index, event in enumerate(events) if event == ("action", "note_type"))
    pressed = next(
        index for index, event in enumerate(events)
        if index > start and event[0] == "up" and inside(FILTER_OPTION, *event[1:])
    )
    return events[start + 1:pressed]


def _hover_site(seed: int) -> XhsFakeSite:
    """悬停打开面板、光标离开入口与面板即收起的站点；主页面光标用固定种子。"""
    site = XhsFakeSite(KEYWORD, _pages(1, 4))
    site.hover_opens_panel = True
    xhs_scan.pointer_for(site.page).rng = random.Random(seed)
    return site


def _in_hover_region(x: float, y: float) -> bool:
    return inside(FILTER_ENTRY, x, y) or inside(FILTER_PANEL, x, y)


@pytest.mark.parametrize("seed", range(8))
def test_filter_panel_opened_on_hover_is_used_without_pressing_and_the_path_to_the_option_stays_inside_it(
    tmp_path, caplog, seed,
):
    site = _hover_site(seed)
    harness = Harness(tmp_path, site)
    events = _record_pointer(harness)

    with caplog.at_level(logging.INFO, logger="MediaCrawler"):
        harness.run()

    assert harness.processed == [f"n1-{index}" for index in range(4)]
    filters = [line["target"].get("filter") for line in harness.ledger() if line.get("action") == "apply_filter"]
    assert filters == ["panel_open", "note_type", "panel_close"]
    # 入口与第一次按下之间没有按下：面板由悬停打开。入口到“图文”这一段的每个轨迹点都在入口与面板的并集内，面板一直开着。
    actions = [event for event in events[events.index(("action", "panel_open")):] if event[0] != "move"]
    assert actions[:2] == [("action", "panel_open"), ("action", "note_type")]
    assert actions[2][0] == "up" and inside(FILTER_OPTION, *actions[2][1:])
    segment = _option_approach(events)
    assert len(segment) > 5
    assert all(event[0] == "move" and _in_hover_region(*event[1:3]) and event[3] for event in segment)
    assert not _filter_diagnostics(harness)
    assert any("opened on hover" in record.getMessage() for record in caplog.records)


def test_hover_panel_closed_before_the_option_press_fails_the_attempt_at_once_and_the_retry_succeeds(
    tmp_path, monkeypatch, caplog,
):
    # 第 1 次尝试不限定轨迹：越过入口左沿离开并集，面板收起；复核后不按下，立即重试（不等目标会话第 1 页）。
    site = _hover_site(0)
    harness = Harness(tmp_path, site)
    events = _record_pointer(harness)
    region = xhs_scan._hover_region
    calls = []

    def first_unconstrained(state):
        calls.append(state)
        return () if len(calls) == 1 else region(state)

    monkeypatch.setattr(xhs_scan, "_hover_region", first_unconstrained)
    waits = []
    await_page_one = harness.crawler._await_target_page_one

    async def counting_wait(*, after_seq):
        waits.append(after_seq)
        return await await_page_one(after_seq=after_seq)

    harness.crawler._await_target_page_one = counting_wait

    with caplog.at_level(logging.INFO, logger="MediaCrawler"):
        harness.run()

    assert harness.processed == [f"n1-{index}" for index in range(4)]
    filters = [line["target"].get("filter") for line in harness.ledger() if line.get("action") == "apply_filter"]
    assert filters == ["panel_open", "note_type", "panel_open", "note_type", "panel_close"]
    presses = [index for index, event in enumerate(events) if event[0] == "up" and inside(FILTER_OPTION, *event[1:])]
    assert len(presses) == 1 and len(calls) == 2 and len(waits) == 1
    # 第 1 次的入口到选项：离开并集即收起，到选项上不按下；第 2 次悬停重新打开，这一段留在并集内。
    first = events.index(("action", "note_type"))
    second = events.index(("action", "panel_open"), first)
    assert not any(event[0] == "up" for event in events[first:second])
    assert any(event[0] == "move" and not _in_hover_region(*event[1:3]) for event in events[first:second])
    assert not events[second - 1][3]
    assert all(_in_hover_region(*event[1:3]) and event[3] for event in _option_approach(events))
    (diagnostic,) = _filter_diagnostics(harness)
    assert diagnostic["outcome"] == "attempt_1:filter_panel_closed_before_option"
    assert diagnostic["moments"]["before_press"] == {"panel_visible": True, "option": True, "entry_text": "筛选"}
    assert diagnostic["moments"]["before_option_press"] == {"panel_visible": False, "option": False, "entry_text": "筛选"}
    messages = [record.getMessage() for record in caplog.records]
    assert any("attempt 1 failed: filter_panel_closed_before_option" in message for message in messages)
    assert not any("Target filter not confirmed" in message for message in messages)


def test_scroll_wheel_lands_inside_the_result_list_rectangle(tmp_path):
    site = XhsFakeSite(KEYWORD, _pages(3, 8))
    site.list_rect = {"left": 0.0, "top": 64.0, "right": 896.0, "bottom": 900.0}
    harness = Harness(tmp_path, site)

    harness.run()

    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted"
    inset = xhs_behavior.XHS_LIST_AREA_INSET
    assert site.wheel_points
    for x, y in site.wheel_points:
        assert 896 * inset <= x <= 896 * (1 - inset)
        assert 64 + 836 * inset <= y <= 900 - 836 * inset
    # 光标已在区域内就不动：整轮的滚动只在第一次移进区域，之后各步的滚轮位置相同。
    assert len(set(site.wheel_points)) == 1
    # 取不到矩形（或矩形为空）时为列表可见区内按比例取的区域；可见区也没有时不移动鼠标。
    area = XiaoHongShuCrawler._scroll_area({
        "view_rect": {"left": 0, "top": 100, "right": 1000, "bottom": 900},
        "list_rect": {"left": 0, "top": 0, "right": 0, "bottom": 0},
    })
    assert area == (300, 380, 400, 320)
    assert XiaoHongShuCrawler._scroll_area({"view_rect": None, "list_rect": None}) is None


def test_list_scrolled_inside_a_container_uses_the_container_position_band_and_step(tmp_path, caplog):
    """AI 对话布局：列表在容器内滚动（可见区 116–417，高 301），window 不动。到底判断、回到顶部、单步上限与可点击带
    都按容器的数值与可见区计算。"""
    site = XhsFakeSite(KEYWORD, _pages(3, 8))
    site.view_rect = {"left": 0.0, "top": 116.0, "right": 1280.0, "bottom": 417.0}
    harness = Harness(tmp_path, site)
    clicked: list[dict] = []
    original = harness.process_note

    async def process(item, search_page, card):
        clicked.append(card)
        await original(item, search_page, card)

    harness.crawler._process_note_card = process
    caplog.set_level(logging.INFO, logger="MediaCrawler")

    harness.run()

    summary = harness.accumulator.summary()
    assert summary["stop_reason"] == "source_exhausted" and summary["source_page"] == 4
    assert len(set(harness.processed)) == len(harness.processed) == 24
    # 可点击带自可见区顶部加 0.1 × 301 起，到底边上方 8px；封面中心都命中本卡片。
    assert all(card["cover_hit"] and 116 + 30.1 <= card["cover"]["y"] <= 409 for card in clicked)
    scrolls = [line["target"] for line in harness.ledger() if line["kind"] == "action" and line["action"] == "scroll"]
    ours = [target for target in scrolls if target["phase"] != "behavior_stage"]
    cap = int(301 * xhs_scan.XHS_SCROLL_VIEWPORT_RATIO)
    assert max(abs(target["delta_y"]) for target in ours) == cap
    # 行为阶段停在容器中部，回到顶部按容器的 scrollTop 逐步上滚到 0。
    to_top = [target for target in ours if target["phase"] == "to_top"]
    assert to_top and to_top[0]["scroll_y_before"] > 0
    assert to_top[-1]["scroll_y_before"] + to_top[-1]["delta_y"] == 0
    assert all(-cap <= target["delta_y"] < 0 for target in to_top)
    # 扫描滚到容器底部才处理末页（has_more=false），位置不超过容器的可滚范围。
    assert all(0 <= target["scroll_y_before"] <= site.scroll_height() - 301 for target in ours)
    assert "List scroll container: element, view_rect=(0, 116, 1280, 417)" in caplog.text
    assert harness.gate()["content_visit_ok"]

    # 可点击带只按 view_rect 计算：同一封面高度，可见区上移后落在带外。
    band = XiaoHongShuCrawler._click_band({"view_rect": site.view_rect, "inner_height": 900})
    assert band == (116 + 0.1 * 301, 417 - xhs_scan.XHS_CLICK_BAND_BOTTOM_MARGIN_PX)
    card = {"cover": {"x": 160.0, "y": 140.0}, "cover_hit": True}
    assert not XiaoHongShuCrawler._card_clickable(card, band)
    assert XiaoHongShuCrawler._card_clickable(card, XiaoHongShuCrawler._click_band({"view_rect": {
        "left": 0, "top": 0, "right": 1280, "bottom": 900,
    }}))
    assert XiaoHongShuCrawler._click_band({"view_rect": None}) is None


def test_card_whose_cover_center_is_covered_is_not_clicked_until_it_is_hit(tmp_path):
    """封面中心被遮住（elementFromPoint 不在本卡片内）的卡片即使在可点击带内也不点；遮挡消失后再处理。"""
    site = XhsFakeSite(KEYWORD, _pages(2, 8))
    site.cover_blocked = {"n1-1": 2}
    harness = Harness(tmp_path, site)
    clicked: list[dict] = []
    original = harness.process_note

    async def process(item, search_page, card):
        clicked.append(card)
        await original(item, search_page, card)

    harness.crawler._process_note_card = process

    harness.run()

    assert harness.processed[:4] == ["n1-0", "n1-2", "n1-1", "n1-3"]
    assert len(set(harness.processed)) == 16 and all(card["cover_hit"] for card in clicked)
    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted"


def test_keyword_mismatch_reenters_once_from_home_then_stops(tmp_path):
    site = XhsFakeSite(KEYWORD, _pages(1, 4), typed_keyword="青岛旅行攻略")
    harness = Harness(tmp_path, site)

    with pytest.raises(XhsSearchEntryFailed) as caught:
        harness.run()

    assert caught.value.code == "search_keyword_mismatch"
    actions = [line["action"] for line in harness.ledger() if line["kind"] == "action"]
    assert actions.count("press_enter") == 2 and actions.count("navigate") == 1


def test_frontend_restart_without_filter_reapplies_it_and_finishes_on_the_new_session(tmp_path):
    site = XhsFakeSite(KEYWORD, _pages(2, 4))
    harness = Harness(tmp_path, site)
    restarted = []
    original = harness.process_note

    async def process_and_restart(item, search_page, card):
        await original(item, search_page, card)
        if not restarted:
            restarted.append(item.note_id)
            site.restart_without_filter()

    harness.crawler._process_note_card = process_and_restart

    harness.run()

    summary = harness.accumulator.summary()
    assert summary["stop_reason"] == "source_exhausted"
    assert len(set(harness.processed)) == len(harness.processed) == 8
    note_type_clicks = [
        line for line in harness.ledger()
        if line["kind"] == "action" and line["target"].get("filter") == "note_type"
    ]
    assert len(note_type_clicks) == 2
    assert harness.gate()["content_visit_ok"]


def _fail_page_two_once(harness: Harness) -> list[tuple[str, str]]:
    """目标会话第 2 页的第一次请求以 net::ERR_INTERNET_DISCONNECTED 失败；网络暂停立即恢复，前端只剩第 1 页。
    返回记录 (阶段, 失败文本) 的列表。"""
    site = harness.site
    original_respond = site.respond_page
    failed = []

    def respond(number, **kwargs):
        if number == 2 and site.session_filters is not None and not failed:
            failed.append(number)
            return original_respond(number, failure="net::ERR_INTERNET_DISCONNECTED")
        return original_respond(number, **kwargs)

    site.respond_page = respond
    pauses = []

    async def pause_for_network(exc, *, stage, state, page=None):
        pauses.append((stage, exc.failure_text))
        state.setdefault("stage", stage)
        state.setdefault("started_at", 0.0)
        site.loaded = site.loaded[:1]
        return True

    harness.crawler._pause_for_network_recovery = pause_for_network
    return pauses


def test_search_request_network_failure_pauses_and_retriggers(tmp_path, monkeypatch):
    site = XhsFakeSite(KEYWORD, _pages(2, 8))
    harness = Harness(tmp_path, site)
    pauses = _fail_page_two_once(harness)

    harness.run()

    assert pauses == [("full_scan", "net::ERR_INTERNET_DISCONNECTED")]
    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted"
    retrigger = [
        line for line in harness.ledger()
        if line["kind"] == "action" and line["action"] == "scroll" and line["target"].get("phase") == "retrigger"
    ]
    assert retrigger


LIST_RECT = {"left": 40.0, "top": 0.0, "right": 1240.0, "bottom": 900.0}


def _scroll_actions(harness: Harness, phase: str) -> list[dict]:
    return [
        line["target"] for line in harness.ledger()
        if line["kind"] == "action" and line["action"] == "scroll" and line["target"].get("phase") == phase
    ]


def _assert_wheels_inside_the_list(site: XhsFakeSite) -> None:
    area = xhs_behavior.list_area_box({"list_rect": LIST_RECT})
    assert site.wheel_points
    for x, y in site.wheel_points:
        assert area.left <= x <= area.left + area.width and area.top <= y <= area.top + area.height


def test_first_row_passed_by_the_network_retrigger_is_backfilled_upward_and_the_scan_continues(
    tmp_path, monkeypatch, caplog,
):
    # #89 / #92：种子 4 下，网络恢复后的重触发在扫描第一次读卡片之前一步滚过第 1 页首行（index 0–3），第 2 页首行
    # 一出现即把第 1 页判为越过。首行从未读到：先在列表区域内向上补漏，读到后照常决定，扫描继续到来源耗尽。
    monkeypatch.setattr(xhs_scan, "SCAN_RANDOM", random.Random(4))
    site = XhsFakeSite(KEYWORD, _pages(2, 8))
    site.list_rect = dict(LIST_RECT)
    harness = Harness(tmp_path, site)
    pauses = _fail_page_two_once(harness)
    caplog.set_level(logging.INFO, logger="MediaCrawler")

    harness.run()

    assert pauses == [("full_scan", "net::ERR_INTERNET_DISCONNECTED")]
    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted"
    assert sorted(harness.processed) == sorted(f"n{page}-{index}" for page in (1, 2) for index in range(8))
    assert harness.processed[:8] == [f"n1-{index}" for index in (4, 5, 6, 7, 0, 1, 2, 3)]
    (backfill,) = _scroll_actions(harness, "backfill_unread")
    assert backfill["delta_y"] < 0 and backfill["scroll_y_before"] > 0
    assert _scroll_actions(harness, "retrigger")
    assert not [event for event in harness.crawler._navigation_diagnostics if event["stage"] == "search_scan"]
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        message.endswith(
            "Backfill reached unread search items: page=1, found=4, index=[0, 3], still_unread=0, steps=1"
        )
        for message in messages
    )
    _assert_wheels_inside_the_list(site)


def test_item_the_frontend_never_renders_is_backfilled_up_to_the_limit_then_stops(tmp_path, monkeypatch, caplog):
    evidence = tmp_path / "behavior_evidence.json"
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(evidence))
    # 第 5 页的 n5-1 照常出现在响应里，但列表从不渲染；第 6 页首张卡片出现即把第 5 页判为越过。此时列表远离顶部：
    # 向上补漏满 3 步仍没读到，以原停止码停止，诊断里有补漏记录。
    site = XhsFakeSite(KEYWORD, _pages(6, 8))
    site.unrendered_ids = {"n5-1"}
    site.list_rect = dict(LIST_RECT)
    harness = Harness(tmp_path, site)

    with caplog.at_level(logging.WARNING, logger="MediaCrawler"), pytest.raises(PlatformRuntimeError) as caught:
        harness.run()

    assert caught.value.code == "xhs_search_items_unresolved"
    assert str(caught.value) == "XHS search items were never rendered"
    assert "n5-1" not in harness.processed and {f"n5-{index}" for index in (0, 2, 3, 4, 5, 6, 7)} <= set(
        harness.processed
    )
    assert not [event for event in harness.events if event[0] == "candidate_skipped"]
    backfills = _scroll_actions(harness, "backfill_unread")
    assert len(backfills) == 3 and all(action["delta_y"] < 0 for action in backfills)
    (diagnostic,) = [event for event in harness.crawler._navigation_diagnostics if event["stage"] == "search_scan"]
    assert (diagnostic["page"], diagnostic["never_rendered"], diagnostic["undecided"]) == (5, 1, 1)
    backfill = diagnostic["backfill"]
    # n5-0（index 32）在它之前，下限为 33；n5-1 不占位置，n5-2 的 index 也是 33。
    assert {key: backfill[key] for key in ("attempts", "limit", "target_index", "at_top")} == {
        "attempts": 3, "limit": 3, "target_index": 33, "at_top": False,
    }
    steps = backfill["steps"]
    assert [step["scroll_y"] for step in steps] == [
        action["scroll_y_before"] + action["delta_y"] for action in backfills
    ]
    assert all(step["scroll_y"] > 0 and step["at_top"] is False for step in steps)
    assert [step["scroll_y"] for step in steps] == sorted((step["scroll_y"] for step in steps), reverse=True)
    for step in steps:
        low, high = step["visible_index_range"]
        assert 0 <= low <= high
    assert diagnostic["scroll_y"] == steps[-1]["scroll_y"]
    warning = next(
        record.getMessage() for record in caplog.records if "Search items were never rendered" in record.getMessage()
    )
    assert "page=5, items=8, undecided=1, never_rendered=1" in warning and "backfill=3/3:target=33:top=False;" in warning
    _assert_wheels_inside_the_list(site)


def test_core_search_maps_entry_failure_to_full_scan_runtime_stop(tmp_path, monkeypatch):
    site = XhsFakeSite(KEYWORD, _pages(1, 4), apply_filter_works=False)
    harness = Harness(tmp_path, site)
    monkeypatch.setattr(harness.crawler.settings, "KEYWORDS", KEYWORD)
    harness.crawler.ports.accumulator_factory = lambda: harness.accumulator

    asyncio.run(harness.crawler.search())

    summary = harness.accumulator.summary()
    assert summary["stop_reason"] == "runtime_failed"
    assert summary["stop_detail"] == "xhs_navigation_recovery_failed:target_filter_not_applied"
    assert summary["discovery_phase"] == "full_scan"
    assert summary["resume_page"] is None and summary["resume_cursor"] is None
    assert "source_visit_id" not in summary


def _valid_evidence() -> dict:
    return {
        "status": "completed",
        "profile": "xhs_guarded",
        "events": [
            {"event": "pause"},
            {"event": "mouse_moves"},
            {"event": "wheel_scroll", "effect_observed": True},
            {"event": "human_scroll_complete", "intent": "list", "passes": 1, "effective_passes": 1},
        ],
        "runtime_fingerprint": {
            "webdriver": False,
            "languages": ["zh-CN", "zh"],
            "platform": "MacIntel",
            "user_agent": "test-agent",
            "hardware_concurrency": 8,
            "device_memory": 8,
            "max_touch_points": 0,
            "viewport": {"width": 1440, "height": 900, "device_pixel_ratio": 2},
            "visibility_state": "visible",
        },
        "page_readiness": {"ready": True},
        "initial_visible_markers": {},
        "visible_markers": {},
        "request_pacing_events": [{"stage": "note_detail", "seconds": 1.0, "finished_at": "later"}],
    }


def test_page_pause_before_behavior_evidence_is_a_full_guarded_pause(tmp_path, monkeypatch):
    """首个关键词的翻页停顿发生在行为证据创建之前：账本照常写入含连续性结果的 pause 行，行为证据事件暂存后并入。"""
    evidence_path = tmp_path / "behavior_evidence.json"
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_ENABLED", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", str(tmp_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(evidence_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE", "xhs_guarded")
    monkeypatch.setattr(runtime_behavior.REQUEST_RANDOM, "uniform", lambda low, high: 0.0)
    moves = []

    async def visible_state(page):
        return "搜索结果", {}

    async def pause(page, bounds, *, reason, log):
        log.append({"event": "pause", "reason": reason})

    async def mouse_moves(page, profile, log, *, list_area):
        moves.append((profile.name, list_area))
        log.append({"event": "mouse_moves"})

    monkeypatch.setattr(xhs_behavior, "visible_page_state", visible_state)
    monkeypatch.setattr(xhs_behavior, "human_pause", pause)
    monkeypatch.setattr(xhs_behavior, "random_mouse_moves", mouse_moves)
    crawler = XiaoHongShuCrawler()
    crawler.context_page = SimpleNamespace(url="https://www.xiaohongshu.com/search_result_ai")
    crawler.capture = make_capture(tmp_path)
    crawler._pending_behavior_evidence = {}

    seconds = asyncio.run(crawler._guarded_pause("search_page", *xhs_scan.XHS_SEARCH_PAGE_PAUSE_SECONDS))

    assert seconds == 0.0 and moves == [("xhs_guarded", xhs_behavior.xhs_list_area)]
    assert not evidence_path.exists()
    (pause_line,) = [line for line in iter_ledger_lines(crawler.capture.ledger.path) if line["kind"] == "pause"]
    assert pause_line["stage"] == "search_page" and pause_line["continuity"] == "completed"
    pending = crawler._pending_behavior_evidence
    assert [event["stage"] for event in pending["request_pacing_events"]] == ["search_page"]
    assert [event["status"] for event in pending["continuity_events"]] == ["completed"]

    write_evidence(evidence_path, _valid_evidence())
    xhs_behavior.merge_pending_behavior_evidence(pending, ports=crawler.ports.behavior)
    merged = json.loads(evidence_path.read_text())
    assert [event["stage"] for event in merged["request_pacing_events"]] == ["search_page", "note_detail"]
    assert [event["stage"] for event in merged["continuity_events"]] == ["search_page"]

    # 行为证据存在后不再暂存：照常追加到证据文件。
    crawler._pending_behavior_evidence = None
    asyncio.run(crawler._guarded_pause("search_results", *xhs_scan.XHS_SEARCH_RESULTS_PAUSE_SECONDS))
    merged = json.loads(evidence_path.read_text())
    assert [event["stage"] for event in merged["request_pacing_events"]][-1] == "search_results"


def test_page_pause_without_evidence_or_pending_is_still_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_ENABLED", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", str(tmp_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE", "xhs_guarded")
    monkeypatch.setattr(runtime_behavior.REQUEST_RANDOM, "uniform", lambda low, high: 0.0)
    crawler = XiaoHongShuCrawler()
    crawler.capture = make_capture(tmp_path)

    with pytest.raises(RuntimeError, match="cannot update XHS request pacing evidence"):
        asyncio.run(crawler._guarded_pause("search_page", *xhs_scan.XHS_SEARCH_PAGE_PAUSE_SECONDS))


def test_full_scan_failure_mapping_keeps_unknown_errors_raised():
    assert xhs_core._full_scan_stop_detail(XhsSearchEntryFailed("search_not_rendered")) == (
        "xhs_navigation_recovery_failed:search_not_rendered"
    )
    assert xhs_core._full_scan_stop_detail(RuntimeError("xhs_login_state_unconfirmed")) is None


def test_non_rule_endpoint_461_without_verification_surface_is_only_a_signal(tmp_path):
    site = XhsFakeSite(KEYWORD, _pages(1, 4))
    harness = Harness(tmp_path, site)
    original_respond = site.respond_page

    def respond(number, **kwargs):
        if number == 2 and site.session_filters is not None:
            telemetry = site.page.request("POST", "https://edith.xiaohongshu.com/api/v2/collect", resource_type="xhr")
            site.page.respond(telemetry, status=461, headers={"verifytype": "102"})
        return original_respond(number, **kwargs)

    site.respond_page = respond

    harness.run()

    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted"
    (signal,) = [line for line in harness.ledger() if line["kind"] == "signal"]
    assert signal["status"] == 461 and signal["outcome"] == "ignored"
    assert signal["verify_headers_present"] is True
    assert harness.gate()["signals_ignored"] == 1


class _VerificationHarness:
    def __init__(self, tmp_path, monkeypatch, states):
        monkeypatch.setattr(xhs_behavior, "XHS_VERIFICATION_SURFACE_SECONDS", 3.0)
        self.crawler = crawler = XiaoHongShuCrawler()
        self.page = SimpleNamespace(url="https://www.xiaohongshu.com/search_result_ai", frames=[])
        self.page.bring_to_front = _no_sleep
        crawler.context_page = self.page
        crawler.browser_context = SimpleNamespace(pages=[self.page])
        crawler._page_is_closed = lambda page: False
        crawler._popup_sleep = _no_sleep
        self.states = list(states)
        self.recorded = []

        async def popup_state(page):
            return self.states.pop(0) if len(self.states) > 1 else self.states[0]

        crawler._popup_checkpoint_state = popup_state
        monkeypatch.setattr(
            xhs_behavior,
            "append_operator_verification_event",
            lambda event, *, ports, pending=None: self.recorded.append((event, pending)),
        )


CLEAR = {"manual_markers": [], "visible_text": "搜索结果", "terminal": ""}
CAPTCHA = {"manual_markers": ["captcha_or_verify"], "visible_text": "请完成验证", "terminal": ""}


def test_frontend_verification_waits_for_two_clear_observations(tmp_path, monkeypatch):
    harness = _VerificationHarness(tmp_path, monkeypatch, [CAPTCHA, CAPTCHA, CAPTCHA, CLEAR, CLEAR, CLEAR])
    harness.crawler._pending_behavior_evidence = {"request_pacing_events": []}

    surfaced = asyncio.run(
        harness.crawler._wait_for_frontend_verification(required=True, data_arrived=lambda: False)
    )

    assert surfaced is True
    ((event, pending),) = harness.recorded
    assert event["stage"] == "frontend_verification" and event["status"] == "completed"
    assert pending is harness.crawler._pending_behavior_evidence


def test_rule_endpoint_verification_that_never_surfaces_is_a_runtime_failure(tmp_path, monkeypatch):
    harness = _VerificationHarness(tmp_path, monkeypatch, [CLEAR])

    with pytest.raises(PlatformRuntimeError) as caught:
        asyncio.run(harness.crawler._wait_for_frontend_verification(required=True, data_arrived=lambda: False))

    assert caught.value.code == "xhs_verification_not_surfaced"
    assert asyncio.run(
        harness.crawler._wait_for_frontend_verification(required=True, data_arrived=lambda: True)
    ) is False
    assert asyncio.run(
        harness.crawler._wait_for_frontend_verification(required=False, data_arrived=lambda: False)
    ) is False
    assert harness.recorded == []


def test_terminal_state_during_frontend_verification_is_raised(tmp_path, monkeypatch):
    harness = _VerificationHarness(
        tmp_path, monkeypatch, [{"manual_markers": [], "visible_text": "访问频繁", "terminal": "rate_limited"}],
    )

    with pytest.raises(PlatformRuntimeError) as caught:
        asyncio.run(harness.crawler._wait_for_frontend_verification(required=False, data_arrived=lambda: False))

    assert caught.value.code == "xhs_rate_limited_terminal"
