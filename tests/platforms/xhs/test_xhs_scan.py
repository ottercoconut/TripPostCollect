"""B2 全量滚动扫描：首页输入、点选“图文”、目标会话逐页登记、翻页停顿不变式与来源耗尽，并以门禁回读账本。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import trippostcollect.platforms.xhs.behavior as xhs_behavior
import trippostcollect.platforms.xhs.core as xhs_core
import trippostcollect.platforms.xhs.scan as xhs_scan
import trippostcollect.runtime.behavior as runtime_behavior
from support.xhs_fake_site import FILTER_ENTRY, XhsFakeSite, note
from trippostcollect.application.candidates import AdaptiveAccumulator
from trippostcollect.application.visit_gate import validate_content_visits
from trippostcollect.artifacts.evidence import write_evidence
from trippostcollect.platforms.xhs.errors import PlatformRuntimeError
from trippostcollect.platforms.xhs.navigation import XhsSearchEntryFailed
from trippostcollect.runtime.visit_ledger import iter_ledger_lines

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
    """把扫描侧的等待与主页面点击按发生顺序记成 ("sleep", 秒) / ("click", x, y)。"""
    events: list[tuple] = []
    mouse = harness.site.page.mouse
    click = mouse.click

    async def sleep(seconds: float) -> None:
        events.append(("sleep", seconds))
        await asyncio.sleep(0)

    async def recording_click(x: float, y: float, delay: int = 0) -> None:
        events.append(("click", x, y))
        await click(x, y, delay=delay)

    harness.crawler._scan_sleep = sleep
    mouse.click = recording_click
    return events


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


def test_card_without_registration_for_two_steps_stops_the_run(tmp_path):
    site = XhsFakeSite(KEYWORD, _pages(2, 4), unregistered_ids=("stray",))
    harness = Harness(tmp_path, site)

    with pytest.raises(PlatformRuntimeError) as caught:
        harness.run()

    assert caught.value.code == "xhs_search_card_unregistered"


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
    assert [event["outcome"] for event in _filter_diagnostics(harness)] == ["filter_not_confirmed"]


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
    entry_click = events.index(("click", FILTER_ENTRY["x"], FILTER_ENTRY["y"]))
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
    assert ("click", FILTER_ENTRY["x"], FILTER_ENTRY["y"]) not in events
    assert not any(line.get("action") == "apply_filter" for line in harness.ledger())
    written = json.loads(evidence.with_name("behavior_evidence.navigation.json").read_text(encoding="utf-8"))
    (diagnostic,) = [event for event in written["events"] if event["stage"] == "search_filter"]
    assert diagnostic["outcome"] == "filter_entry_missing" and site.diagnostic_reads == 1
    assert diagnostic["layout_count"] == 1 and diagnostic["entry_candidates"] == 0
    assert diagnostic["entry_visible"] is False and diagnostic["label_chains"] == []
    warning = next(record.getMessage() for record in caplog.records if "Search filter failed" in record.getMessage())
    assert "reason=filter_entry_missing" in warning and "entry_candidates=0" in warning
    assert "xiaohongshu.com" not in warning


def test_filter_option_missing_on_first_attempt_reopens_the_panel_and_succeeds(tmp_path):
    site = XhsFakeSite(KEYWORD, _pages(1, 4))
    site.option_missing_opens = 1
    harness = Harness(tmp_path, site)

    harness.run()

    assert harness.processed == [f"n1-{index}" for index in range(4)]
    filters = [line["target"].get("filter") for line in harness.ledger() if line.get("action") == "apply_filter"]
    assert filters == ["panel_open", "panel_close", "panel_open", "note_type", "panel_close"]
    assert not _filter_diagnostics(harness)


def test_scroll_wheel_lands_inside_the_result_list_rectangle(tmp_path):
    site = XhsFakeSite(KEYWORD, _pages(3, 8))
    site.list_rect = {"left": 0.0, "top": 64.0, "right": 896.0, "bottom": 900.0}
    harness = Harness(tmp_path, site)

    harness.run()

    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted"
    inset = xhs_scan.XHS_SCROLL_POINTER_INSET
    assert site.wheel_points
    for x, y in site.wheel_points:
        assert 896 * inset <= x <= 896 * (1 - inset)
        assert 64 + 836 * inset <= y <= 900 - 836 * inset
    # 取不到矩形（或矩形为空）时按视口比例落点。
    x, y = XiaoHongShuCrawler._scroll_pointer(
        {"inner_width": 1000, "inner_height": 800, "list_rect": {"left": 0, "top": 0, "right": 0, "bottom": 0}},
    )
    assert 300 <= x <= 700 and 280 <= y <= 600


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


def test_search_request_network_failure_pauses_and_retriggers(tmp_path, monkeypatch):
    site = XhsFakeSite(KEYWORD, _pages(2, 8))
    harness = Harness(tmp_path, site)
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

    harness.run()

    assert pauses == [("full_scan", "net::ERR_INTERNET_DISCONNECTED")]
    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted"
    retrigger = [
        line for line in harness.ledger()
        if line["kind"] == "action" and line["action"] == "scroll" and line["target"].get("phase") == "retrigger"
    ]
    assert retrigger


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
            "webdriver": None,
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

    async def mouse_moves(page, profile, log):
        moves.append(profile.name)
        log.append({"event": "mouse_moves"})

    monkeypatch.setattr(xhs_behavior, "visible_page_state", visible_state)
    monkeypatch.setattr(xhs_behavior, "human_pause", pause)
    monkeypatch.setattr(xhs_behavior, "random_mouse_moves", mouse_moves)
    crawler = XiaoHongShuCrawler()
    crawler.context_page = SimpleNamespace(url="https://www.xiaohongshu.com/search_result_ai")
    crawler.capture = make_capture(tmp_path)
    crawler._pending_behavior_evidence = {}

    seconds = asyncio.run(crawler._guarded_pause("search_page", 12.0, 30.0))

    assert seconds == 0.0 and moves == ["xhs_guarded"]
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
    asyncio.run(crawler._guarded_pause("search_results", 6.0, 14.0))
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
        asyncio.run(crawler._guarded_pause("search_page", 12.0, 30.0))


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
