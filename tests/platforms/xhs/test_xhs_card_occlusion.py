"""V16 布局下卡片落点避开吸顶遮挡并在按下前复核、扫描中途的非目标新会话从首页重新进入、不重复点选已选中的“图文”
（#102）。几何按 Mac 人工侦察 2026-10-22：搜索栏固定在 0–116，热门筛选标签排滚过原位后吸顶在 116–164，点中即带
filter_hot 重新搜索。"""

from __future__ import annotations

import asyncio
import logging
import random

import pytest

import trippostcollect.platforms.xhs.scan as xhs_scan
from support.xhs_fake_site import (
    ARROW_POINT, AUTHOR_POINT, CLOSE_POINT, HOT_ROW_STICK_TOP, TARGET_FILTERS, V16_HEADER_BOTTOM, XhsFakeSite,
    detail_card, inside, note,
)
from trippostcollect.application.visit_gate import validate_content_visits
from trippostcollect.platforms.xhs import media as xhs_media
from trippostcollect.platforms.xhs.behavior import XHS_LIST_TOP_MARGIN_PX, list_area_box, xhs_list_area
from trippostcollect.platforms.xhs.errors import PlatformRuntimeError
from trippostcollect.platforms.xhs.navigation import XhsSearchEntryFailed
from trippostcollect.platforms.xhs.page_scripts import DOM_SCRIPTS
from trippostcollect.platforms.xhs.scan import XhsCardWithheld, XhsScanMixin
from trippostcollect.runtime.human_pointer import Box

from .test_xhs_discovery_memory import events_of, last_stop, ledger_lines, prepare_crawler, write_manifest
from .test_xhs_scan import (  # noqa: F401  （_offline 为自动夹具）
    KEYWORD, Harness, _offline, _pages,
)

# 假站点 1280×900 上的 V16：标签排原位在 168–216（列表自 220 起），滚过 52px 后吸顶在 116–164。
HOT_ROW = {"left": 40.0, "top": 168.0, "right": 1240.0, "bottom": 216.0}
HOT_ROW_HEIGHT = HOT_ROW["bottom"] - HOT_ROW["top"]
STUCK_BOTTOM = HOT_ROW_STICK_TOP + HOT_ROW_HEIGHT
STUCK_FLOOR = STUCK_BOTTOM + XHS_LIST_TOP_MARGIN_PX
HEADER_FLOOR = V16_HEADER_BOTTOM + XHS_LIST_TOP_MARGIN_PX
V16_LIST_RECT = {"left": 40.0, "top": 0.0, "right": 1240.0, "bottom": 900.0}
HOT_FILTERS = [*TARGET_FILTERS, {"type": "filter_hot", "tags": ["路线规划"]}]


def _v16(site: XhsFakeSite) -> XhsFakeSite:
    site.header_bottom = V16_HEADER_BOTTOM
    site.hot_row = dict(HOT_ROW)
    site.list_top = HOT_ROW["bottom"] + 4
    site.list_rect = dict(V16_LIST_RECT)
    return site


class _FixedSteps(random.Random):
    """扫描每步都滚 417px（不超过单步上限）：第 4 行（第 2 页第 2 行）在滚动位置 1251 时封面上沿 59、中心 179，
    上沿在吸顶的标签排（116–164）下面，中心在它下方。"""

    def randint(self, a: int, b: int) -> int:
        return 417


def _hold_row_until(site: XhsFakeSite, note_ids: list[str], scroll_y: int) -> None:
    """这些卡片的封面中心在滚动位置到达 `scroll_y` 之前一直被遮住（cover_hit 为假），之后才可点。"""
    site.cover_blocked.update(dict.fromkeys(note_ids, 10**6))
    wheel = site.wheel

    def releasing(delta_y: float) -> None:
        wheel(delta_y)
        if site.scroll_y >= scroll_y:
            site.cover_blocked.update(dict.fromkeys(note_ids, 0))

    site.wheel = releasing


def _record_clicks(site: XhsFakeSite) -> list[dict]:
    """每次按下松开时的落点、标签排是否吸顶、是否点开了浮层。"""
    clicks: list[dict] = []
    click = site.click

    def recording(x: float, y: float) -> None:
        row = site.hot_row_state()
        before = site.overlay_note
        click(x, y)
        clicks.append({
            "x": x, "y": y, "stuck": bool(row and row[1]), "overlay": before is not None,
            "opened": before is None and site.overlay_note is not None,
        })

    site.click = recording
    return clicks


def _record_wheels(site: XhsFakeSite) -> list[tuple[float, float, bool]]:
    """每个滚轮事件时的光标位置与标签排是否吸顶。"""
    wheels: list[tuple[float, float, bool]] = []
    mouse = site.page.mouse
    wheel = mouse.wheel

    async def recording(delta_x: float, delta_y: float) -> None:
        row = site.hot_row_state()
        wheels.append((*site.mouse_position, bool(row and row[1])))
        await wheel(delta_x, delta_y)

    mouse.wheel = recording
    return wheels


def _actions(lines: list[dict], kind: str | None = None) -> list[dict]:
    return [line for line in lines if line["kind"] == "action" and (kind is None or line["action"] == kind)]


def _diagnostics(crawler, stage: str) -> list[dict]:
    return [event for event in crawler._navigation_diagnostics if event["stage"] == stage]


def _log_lines(caplog, text: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if text in record.getMessage()]


def _gate(crawler, state_path, tmp_path) -> dict:
    return validate_content_visits(
        crawler.records,
        [crawler.capture.ledger.path],
        platform="xhs",
        mode="discovery",
        manifests=[write_manifest(crawler, tmp_path / "image_manifest.jsonl")],
        stop_event=last_stop(state_path),
    )


# D1 几何 ------------------------------------------------------------------------

# Mac 人工侦察 2026-10-22（内宽 1470、内高 444）：V16 整页滚动，滚动后列表从视口顶部起，搜索栏 0–116 与吸顶的标签排
# 116–164 都盖在列表上。AI 对话布局：容器可见区 116–417，搜索栏在可见区之上。
V16_STUCK = {
    "scroll_container": "document", "scroll_y": 3900,
    "view_rect": {"left": 0, "top": 0, "right": 1470, "bottom": 444},
    "list_rect": {"left": 122, "top": 0, "right": 1470, "bottom": 444},
    "top_row_rect": None, "top_occlusion_bottom": 164, "top_occluders": 2,
    "layout_present": True, "with_ai_chat": False,
}
AI_LAYOUT = {
    "scroll_container": "element", "scroll_y": 900,
    "view_rect": {"left": 0, "top": 116, "right": 1000, "bottom": 417},
    "list_rect": {"left": 0, "top": 116, "right": 980, "bottom": 417},
    "top_row_rect": None, "top_occlusion_bottom": None, "top_occluders": 0,
    "layout_present": True, "with_ai_chat": True,
}


class _MetricsPage:
    url = "https://www.xiaohongshu.com/search_result_ai"

    def __init__(self, metrics: dict) -> None:
        self.metrics = metrics

    async def evaluate(self, script: str, arg=None) -> dict:
        assert script == DOM_SCRIPTS["scroll_metrics"]
        return dict(self.metrics)


def test_occlusion_floor_raises_the_band_area_and_leave_area_and_shrinks_the_step_in_v16():
    band = XhsScanMixin._click_band(V16_STUCK)
    assert band == (164 + XHS_LIST_TOP_MARGIN_PX, 436.0)
    # 遮挡固定不动：单步上限按遮挡下沿以下的高度计，不超过带高，封面中心一步越不过整条带。
    step = XhsScanMixin._step_limit(V16_STUCK)
    assert step == int((444 - 164) * xhs_scan.XHS_SCROLL_VIEWPORT_RATIO) == 224
    assert step <= band[1] - band[0]
    unfloored = {**V16_STUCK, "top_occlusion_bottom": None}
    assert XhsScanMixin._click_band(unfloored)[0] == pytest.approx(44.4)
    assert XhsScanMixin._step_limit(unfloored) == int(444 * xhs_scan.XHS_SCROLL_VIEWPORT_RATIO) == 355
    # 行为阶段与滚轮的区域、移开光标的落点都从遮挡下沿之下 8px 起。
    area = list_area_box(V16_STUCK)
    assert area == pytest.approx(Box(391.6, 172.0, 808.8, 444 - 88.8 - 172.0))
    assert list_area_box(unfloored).top == pytest.approx(88.8)
    assert XhsScanMixin._scroll_area(V16_STUCK) == area
    assert asyncio.run(xhs_list_area(_MetricsPage(V16_STUCK))).box == area
    region = (Box(1200, 130, 84, 40), Box(900, 180, 457, 200))
    leave = xhs_scan._leave_area(XhsScanMixin._scroll_area(V16_STUCK), region)
    assert leave is not None and leave.top >= 172
    # 没有列表矩形时的比例回退同样在遮挡之下；去掉后不剩时没有区域。
    fallback = XhsScanMixin._scroll_area({**V16_STUCK, "list_rect": None})
    assert fallback == pytest.approx(Box(441.0, 172.0, 588.0, 444 * 0.75 - 172.0))
    assert XhsScanMixin._scroll_area({**V16_STUCK, "list_rect": None, "top_occlusion_bottom": 400}) is None


def test_ai_layout_band_step_and_area_are_unchanged_by_the_header_above_the_container():
    """AI 对话布局的可见区从搜索栏下沿开始：页面脚本不报遮挡（下沿不在可见区顶部之下），报了 116 也不改变任何结果。"""
    plain = {key: value for key, value in AI_LAYOUT.items() if key not in ("top_occlusion_bottom", "top_occluders")}
    for metrics in (AI_LAYOUT, {**AI_LAYOUT, "top_occlusion_bottom": 116, "top_occluders": 1}):
        assert XhsScanMixin._click_band(metrics) == XhsScanMixin._click_band(plain) == (116 + 0.1 * 301, 409.0)
        assert XhsScanMixin._step_limit(metrics) == XhsScanMixin._step_limit(plain) == 240
        assert list_area_box(metrics) == list_area_box(plain)
        assert XhsScanMixin._scroll_area(metrics) == XhsScanMixin._scroll_area(plain)


def test_fake_site_reports_the_header_and_the_hot_row_only_once_it_sticks():
    site = _v16(XhsFakeSite(KEYWORD, _pages(2, 8)))
    site.page.url = "https://www.xiaohongshu.com/search_result_ai?keyword=x"
    assert site.top_occlusion() == (V16_HEADER_BOTTOM, 1)
    site.scroll_y = 52
    assert site.hot_row_state() == (dict(HOT_ROW, top=116.0, bottom=164.0), False)
    site.scroll_y = 53
    assert site.top_occlusion() == (STUCK_BOTTOM, 2) and site.hot_row_state()[1] is True
    site.hot_row_reported = False
    assert site.top_occlusion() == (V16_HEADER_BOTTOM, 1)
    site.view_rect = {"left": 0.0, "top": 116.0, "right": 1280.0, "bottom": 900.0}
    site.hot_row = None
    assert site.top_occlusion() == (None, 0)


# D1 + D2 端到端：吸顶后封面中心可见、上沿在标签排下面的卡片 -------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [True, False])
async def test_v16_cards_under_the_sticky_hot_row_are_clicked_below_it_and_the_tags_never(
    monkeypatch, tmp_path, caplog, reported,
):
    """第 4 行在滚到 1000px 之前不可点，之后它的封面上沿在吸顶的标签排下面、中心在排下方，此时点开。
    reported=False：页面脚本没报出标签排（以后新出现的遮挡），带不抬高，靠落点命中检测拒绝落在排上的点并重抽。"""
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    pages = [[note(f"p{page}-{index}") for index in range(8)] for page in (1, 2, 3)]
    crawler, _, state_path, site = _v16_crawler(monkeypatch, tmp_path, pages)
    site.hot_row_reported = reported
    _hold_row_until(site, [f"p2-{index}" for index in range(4, 8)], 1000)
    monkeypatch.setattr(xhs_scan, "SCAN_RANDOM", _FixedSteps(7))
    monkeypatch.setattr(xhs_media, "XHS_IMAGE_KEY_FLIP_SHARE", 0.0)  # 翻图都先点箭头：光标的随机数消耗确定
    xhs_scan.pointer_for(site.page).rng = random.Random(11)
    clicks, wheels = _record_clicks(site), _record_wheels(site)
    opened: list[tuple[dict, tuple[float, float], bool]] = []
    process = crawler._process_note_card

    async def recording(item, search_page, card):
        opened.append((card["cover"], card["click_band"], site.hot_row_state()[1]))
        await process(item, search_page, card)

    crawler._process_note_card = recording

    await crawler.search()

    assert last_stop(state_path)["stop_reason"] == "source_exhausted"
    assert len(crawler.records) == 24 and site.hot_clicks == []
    assert not [request for request in site.requests if any(f["type"] == "filter_hot" for f in request.get("filters") or [])]
    stuck = [(cover, band) for cover, band, is_stuck in opened if is_stuck]
    assert len([cover for cover, _ in stuck if cover["top"] < STUCK_BOTTOM < cover["y"]]) >= 4
    assert all(band[0] == (STUCK_FLOOR if reported else HEADER_FLOOR) for _, band in stuck)
    # 吸顶后的点击都不在标签排与搜索栏上；每次点开浮层都恰是一条 click_card。
    assert not [click for click in clicks if click["stuck"] and not click["overlay"] and click["y"] < STUCK_BOTTOM]
    lines = ledger_lines(crawler)
    assert sum(click["opened"] for click in clicks) == len(_actions(lines, "click_card")) == 24
    redraws = _log_lines(caplog, "Card click point redrawn")
    if reported:
        assert not redraws and all(y >= STUCK_FLOOR for _, y, is_stuck in wheels if is_stuck)
    else:
        assert redraws and all("button.tab" in line for line in redraws)
        assert all(y >= HEADER_FLOOR for _, y, _ in wheels)
    gate = _gate(crawler, state_path, tmp_path)
    assert gate["content_visit_ok"], gate


def _v16_crawler(monkeypatch, tmp_path, pages):
    crawler, feeds, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=None, pages=pages)
    _v16(site)
    return crawler, feeds, state_path, site


def _results_site() -> XhsFakeSite:
    """结果页已在目标会话：第 1 页 8 张（两行），滚到第 2 行封面上沿 70、中心 190（标签排吸顶在 116–164）。"""
    site = _v16(XhsFakeSite(KEYWORD, _pages(2, 8)))
    site.page.url = "https://www.xiaohongshu.com/search_result_ai?keyword=x"
    site.session_filters = list(TARGET_FILTERS)
    site.loaded = [[note(f"n1-{index}") for index in range(8)]]
    site.scroll_y = int(site.list_top + 360 + 10 - 70)
    return site


@pytest.mark.parametrize("reported", [True, False])
def test_planned_points_on_the_sticky_hot_row_are_rejected_and_redrawn(tmp_path, reported):
    site = _results_site()
    site.hot_row_reported = reported
    crawler = Harness(tmp_path, site).crawler
    metrics = asyncio.run(xhs_scan.dom_scroll_metrics(site.page))
    band = XhsScanMixin._click_band(metrics)
    card = next(card for card in site.visible_cards() if card["note_id"] == "n1-4")
    assert card["cover"]["top"] == 70 and card["cover_hit"] and band[0] <= card["cover"]["y"]
    card = {**card, "click_band": band}
    outcomes = {"first": 0, "redrawn": 0, "withheld": 0}
    for seed in range(80):
        xhs_scan.pointer_for(site.page).rng = random.Random(seed)
        site.point_hits.clear()
        try:
            plan = asyncio.run(crawler._plan_card_click("n1-4", card))
        except XhsCardWithheld:
            outcomes["withheld"] += 1
            assert [(kind, hit) for kind, _, _, _, hit in site.point_hits] == [("card", False)] * 3
            assert all(HOT_ROW_STICK_TOP <= y < STUCK_BOTTOM for _, _, _, y, _ in site.point_hits)
            continue
        x, y = plan.point
        assert inside(card["cover"], x, y) and not HOT_ROW_STICK_TOP <= y < STUCK_BOTTOM
        assert site.point_hits[-1][-1] is True
        outcomes["redrawn" if len(site.point_hits) > 1 else "first"] += 1
    # 计划阶段不移动光标、不按下。
    assert site.click_points == [] and site.mouse_down is False and site.move_count == 0
    if reported:
        assert band[0] == STUCK_FLOOR and outcomes == {"first": 80, "redrawn": 0, "withheld": 0}
    else:
        assert band[0] == HEADER_FLOOR and outcomes["redrawn"] and outcomes["withheld"] and outcomes["first"]


# D2 按下前复核 --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_card_covered_when_the_pointer_arrives_is_not_pressed_and_leaves_no_click_card(
    monkeypatch, tmp_path, caplog,
):
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    pages = [[note(f"p1-{index}") for index in range(8)], [note(f"p2-{index}") for index in range(4)]]
    crawler, feeds, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=None, pages=pages)
    site.hit_overrides["p1-1"] = [None, False]  # 落点计划时命中；光标到达并静止后被新出现的遮挡盖住
    clicks = _record_clicks(site)

    await crawler.search()

    assert last_stop(state_path)["stop_reason"] == "source_exhausted"
    assert sorted(record["note_id"] for record in crawler.records) == sorted(f"p{n}-{i}" for n, i in (
        *((1, i) for i in range(8)), *((2, i) for i in range(4))
    ))
    hits = [(x, y, hit) for kind, key, x, y, hit in site.point_hits if key == "p1-1"]
    assert [hit for _, _, hit in hits] == [True, False, True, True]
    # 第一次：没有按下，光标停在计划的落点上；也没有 click_card。之后滚动一步再点开。
    assert hits[1][:2] == hits[0][:2] and not [click for click in clicks if (click["x"], click["y"]) == hits[1][:2]]
    lines = ledger_lines(crawler)
    card_clicks = _actions(lines, "click_card")
    assert [line["target"]["note_id"] for line in card_clicks].count("p1-1") == 1 and feeds.count("p1-1") == 1
    assert sum(click["opened"] for click in clicks) == len(card_clicks) == 12
    withheld_at = next(index for index, line in enumerate(card_clicks) if line["target"]["note_id"] == "p1-2")
    retried_at = next(index for index, line in enumerate(card_clicks) if line["target"]["note_id"] == "p1-1")
    assert withheld_at < retried_at
    scrolls = [line for line in _actions(lines, "scroll") if line["target"]["phase"] != "behavior_stage"]
    assert scrolls and scrolls[0]["seq"] < card_clicks[retried_at]["seq"]
    assert _log_lines(caplog, "Click not pressed") == [
        "[XiaoHongShuCrawler.scan] Click not pressed: the pointer is not on the target before the press: "
        "target=card, note_id=p1-1, target_present=True, hit=div.fake-cover"
    ]
    gate = _gate(crawler, state_path, tmp_path)
    assert gate["content_visit_ok"], gate


@pytest.mark.asyncio
async def test_overlay_controls_covered_before_the_press_are_not_pressed(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["only"])
    site.details["only"] = detail_card("only", images=3)
    monkeypatch.setattr(xhs_media, "XHS_IMAGE_KEY_FLIP_SHARE", 0.0)  # 先点箭头
    site.hit_overrides.update({"right_arrow": [False], "author": [False], "close": [False]})

    await crawler.search()

    assert [record["note_id"] for record in crawler.records] == ["only"] and len(site.author_tabs) == 1
    lines = ledger_lines(crawler)
    flips = [(line["target"]["to_index"], line["target"]["method"]) for line in _actions(lines, "flip_image")]
    assert flips == [(2, "key"), (3, "arrow")]
    assert len(_actions(lines, "click_author")) == 1 and len(_actions(lines, "close_overlay")) == 1
    # 每个控件只按下了一次（复核通过的那次），光标都曾停在控件上而没按。
    assert [sum(inside(point, x, y) for x, y in site.click_points) for point in (ARROW_POINT, AUTHOR_POINT, CLOSE_POINT)] == [1, 1, 1]
    controls = [(kind, hit) for kind, _, _, _, hit in site.point_hits if kind != "card"]
    assert controls == [("right_arrow", False), ("right_arrow", True), ("author", False), ("author", True),
                        ("close", False), ("close", True)]
    assert len(_log_lines(caplog, "Click not pressed")) == 3
    gate = _gate(crawler, state_path, tmp_path)
    assert gate["content_visit_ok"], gate


# D3 非目标的新会话 ------------------------------------------------------------------

def _hot_search(site: XhsFakeSite, tag: str = "路线规划") -> None:
    site.session_hot = tag
    site.start_session(filters=site.search_filters())


def test_unexpected_hot_filter_session_reenters_from_home_and_closes_the_chat_again(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    site = XhsFakeSite(KEYWORD, _pages(3, 4))
    site.ai_chat = True
    harness = Harness(tmp_path, site)
    original = harness.process_note

    async def process(item, search_page, card):
        await original(item, search_page, card)
        if item.note_id == "n2-1":
            _hot_search(site)

    harness.crawler._process_note_card = process

    harness.run()

    summary = harness.accumulator.summary()
    assert summary["stop_reason"] == "source_exhausted"
    assert len(harness.processed) == len(set(harness.processed)) == 12  # 已处理的 ID 在新目标会话里照常滑过
    [diagnostic] = [event for event in harness.crawler._navigation_diagnostics if event["stage"] == "unexpected_search_session"]
    assert diagnostic["outcome"] == "reenter_from_home"
    assert {key: diagnostic[key] for key in ("filter_hot", "extra_keys", "missing_keys", "keyword_matches", "reentry", "limit")} == {
        "filter_hot": True, "extra_keys": ["filter_hot"], "missing_keys": [], "keyword_matches": True,
        "reentry": 1, "limit": 3,
    }
    assert diagnostic["trigger"] in {"scroll", "apply_filter"}
    lines = harness.ledger()
    actions = _actions(lines)
    hot_visit = next(line for line in lines if line["kind"] == "visit" and "filter_hot" in line.get("filter_signature", ""))
    assert hot_visit["is_target"] is False and hot_visit["search_page"] == 1
    after = [line["action"] for line in actions if line["seq"] > hot_visit["seq"]]
    # 不在原页面点选“图文”：先回首页、重新输入回车、关对话栏，再点选。
    assert after[:5] == ["navigate", "type_search", "press_enter", "dismiss_ai_chat", "apply_filter"]
    assert [line["action"] for line in actions].count("dismiss_ai_chat") == 2
    assert [line["target"].get("filter") for line in actions].count("note_type") == 2
    assert site.ai_chat_open is False and site.filter_applied is True and site.hot_clicks == []
    batches = [(details["source_page"], details["batch_complete"]) for kind, details in harness.events
               if kind == "adaptive_batch_completed"]
    assert batches == [(1, True), (2, False), (1, True), (2, True), (3, True), (4, True)]
    targets = {(line["search_session"], line["filter_signature"]) for line in lines
               if line["kind"] == "visit" and line["stage"] == "search_results" and line["is_target"]}
    assert len(targets) == 2
    gate = harness.gate()
    assert gate["content_visit_ok"], gate
    [warning] = _log_lines(caplog, "Unexpected non-target search session")
    assert "outcome=reenter_from_home" in warning and "filter_hot=True" in warning and "路线规划" not in warning


@pytest.mark.asyncio
async def test_card_click_that_starts_a_hot_filter_search_reenters_from_home_and_passes_the_gate(
    monkeypatch, tmp_path,
):
    """第 9 轮的形态：点卡片带出 filter_hot 的新搜索第 1 页（浮层不出现），列表回顶后第 2 次点击的落点已不在这张卡片上，
    交回扫描；扫描从首页重新进入，新目标会话里照常处理这张卡片，记录过真实门禁（同一账本里两个目标会话）。"""
    pages = [[note(f"p1-{index}") for index in range(8)], [note(f"p2-{index}") for index in range(4)]]
    crawler, feeds, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=None, pages=pages)
    site.feed_overrides["p2-1"] = ["hot_search"]

    await crawler.search()

    assert last_stop(state_path)["stop_reason"] == "source_exhausted"
    assert len(crawler.records) == 12 and feeds.count("p2-1") == 1
    [diagnostic] = _diagnostics(crawler, "unexpected_search_session")
    assert diagnostic["trigger"] == "click_card" and diagnostic["filter_hot"] is True
    lines = ledger_lines(crawler)
    clicks = [line for line in _actions(lines, "click_card") if line["target"]["note_id"] == "p2-1"]
    assert len(clicks) == 2  # 带出新搜索的那次与新会话里的那次；交回扫描的第 2 次尝试没有按下
    hot_visit = next(line for line in lines if line["kind"] == "visit" and "filter_hot" in line.get("filter_signature", ""))
    assert hot_visit["trigger"] == clicks[0]["id"]
    assert [line["action"] for line in _actions(lines) if line["seq"] > hot_visit["seq"]][:3] == [
        "navigate", "type_search", "press_enter",
    ]
    assert not events_of(state_path, "candidate_skipped")
    gate = _gate(crawler, state_path, tmp_path)
    assert gate["content_visit_ok"], gate


def test_unexpected_sessions_beyond_the_limit_stop_the_run(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    site = XhsFakeSite(KEYWORD, _pages(1, 8))
    harness = Harness(tmp_path, site)
    original = harness.process_note

    async def process(item, search_page, card):
        await original(item, search_page, card)
        _hot_search(site)

    harness.crawler._process_note_card = process

    with pytest.raises(PlatformRuntimeError) as caught:
        harness.run()

    assert caught.value.code == "xhs_unexpected_search_session_limit"
    assert len(harness.processed) == 4
    diagnostics = [event for event in harness.crawler._navigation_diagnostics if event["stage"] == "unexpected_search_session"]
    assert [(event["reentry"], event["outcome"]) for event in diagnostics] == [
        (1, "reenter_from_home"), (2, "reenter_from_home"), (3, "reenter_from_home"), (4, "limit_exceeded"),
    ]
    actions = [line["action"] for line in _actions(harness.ledger())]
    assert actions.count("press_enter") == 4 and actions.count("navigate") == 3


# D4 已选中的“图文” -------------------------------------------------------------------

def test_note_type_already_selected_on_a_late_target_session_is_not_pressed_again(tmp_path, monkeypatch, caplog):
    """第 1 次点选后目标会话第 1 页晚到（等待超时）：第 2 次打开面板时“图文”已选中，当前会话已是目标会话，按成功处理；
    再点会取消“图文”（假站点 option_deselects）。"""
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    site = XhsFakeSite(KEYWORD, _pages(1, 4))
    site.option_deselects = True
    harness = Harness(tmp_path, site)
    crawler = harness.crawler
    waited = crawler._await_target_page_one
    late = []

    async def await_target_page_one(*, after_seq):
        if not late:
            late.append(after_seq)
            return None  # 第 1 页已到但还没消费：等待“超时”
        return await waited(after_seq=after_seq)

    crawler._await_target_page_one = await_target_page_one

    harness.run()

    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted" and len(harness.processed) == 4
    # 第 2 次尝试时面板仍开着、“图文”已选中：既不重开面板也不再点选，之后照常收起。
    filters = [line["target"].get("filter") for line in _actions(harness.ledger(), "apply_filter")]
    assert filters == ["panel_open", "note_type", "panel_close", "panel_close"]
    assert site.filter_applied is True and len([request for request in site.requests if request.get("filters")]) == 2
    assert _log_lines(caplog, "already selected") == [
        "[XiaoHongShuCrawler.scan] Target filter option already selected and the current search session is the "
        "target; not pressing it"
    ]
    outcomes = [event["outcome"] for event in _diagnostics(crawler, "search_filter")]
    assert outcomes == ["attempt_1:filter_not_confirmed"]
    gate = harness.gate()
    assert gate["content_visit_ok"], gate


def test_note_type_preselected_with_another_filter_reenters_from_home(tmp_path, caplog):
    """回车会话已带“图文”与热门标签（非目标）：面板里“图文”已选中，原地无法修复，不点；回首页重新输入后照常点选。"""
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    site = XhsFakeSite(KEYWORD, _pages(1, 4))
    site.option_deselects = True
    site.enter_filters = [list(HOT_FILTERS)]
    harness = Harness(tmp_path, site)

    harness.run()

    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted" and len(harness.processed) == 4
    actions = _actions(harness.ledger())
    navigate = next(line for line in actions if line["action"] == "navigate")
    before = [line["target"].get("filter") for line in actions if line["seq"] < navigate["seq"]]
    assert "note_type" not in before and "panel_open" in before
    assert [line["action"] for line in actions].count("press_enter") == 2
    assert [line["target"].get("filter") for line in actions].count("note_type") == 1
    outcomes = [event["outcome"] for event in _diagnostics(harness.crawler, "search_filter")]
    assert outcomes == ["attempt_1:filter_option_preselected"]
    assert _log_lines(caplog, "re-entering from the home page once: target_filter_preselected")
    gate = harness.gate()
    assert gate["content_visit_ok"], gate


def test_note_type_preselected_on_both_entries_stops_the_run(tmp_path):
    site = XhsFakeSite(KEYWORD, _pages(1, 4))
    site.enter_filters = [list(HOT_FILTERS), list(HOT_FILTERS)]
    harness = Harness(tmp_path, site)

    with pytest.raises(XhsSearchEntryFailed) as caught:
        harness.run()

    assert caught.value.code == "target_filter_preselected"
    filters = [line["target"].get("filter") for line in _actions(harness.ledger(), "apply_filter")]
    assert "note_type" not in filters
