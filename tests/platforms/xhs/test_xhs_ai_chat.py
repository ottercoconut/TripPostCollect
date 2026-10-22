"""进入结果页后关闭“点点AI”对话栏，按 V16 布局运行（#99）：关闭、跳过、失败按 AI 布局继续、防误点、续页归属、
重新进入再关、扫描中途重现只记诊断，以及 V16 几何下悬停面板的间隙走廊与标签行避让。"""

from __future__ import annotations

import asyncio
import logging
import random

import pytest

import trippostcollect.platforms.xhs.scan as xhs_scan
from support.xhs_fake_site import AI_CHAT_CLOSE, TOP_ROW, XhsFakeSite, box, inside
from trippostcollect.platforms.xhs.behavior import XHS_LIST_TOP_MARGIN_PX, list_area_box, xhs_list_area
from trippostcollect.platforms.xhs.page_scripts import DOM_SCRIPTS
from trippostcollect.runtime.human_pointer import Box, HumanPointer

from .test_xhs_scan import (  # noqa: F401  （_offline 为自动夹具）
    KEYWORD, LIST_RECT, Harness, _offline, _pages, _record_sleeps_and_clicks,
)

CLOSE_TARGET = {"control": "ai_chat_close"}
POINTER_KEYS = {"x_ratio", "y_ratio", "approach_ms", "moves", "settle_ms", "hold_ms"}


def _chat_site(**options) -> XhsFakeSite:
    site = XhsFakeSite(KEYWORD, _pages(2, 4))
    site.ai_chat = True
    for name, value in options.items():
        setattr(site, name, value)
    return site


def _actions(harness: Harness) -> list[dict]:
    return [line for line in harness.ledger() if line["kind"] == "action"]


def _searches(harness: Harness) -> list[dict]:
    return [line for line in harness.ledger() if line["kind"] == "visit" and line["stage"] == "search_results"]


def _diagnostics(harness: Harness, stage: str) -> list[dict]:
    return [event for event in harness.crawler._navigation_diagnostics if event["stage"] == stage]


def _button_presses(site: XhsFakeSite) -> int:
    return sum(inside(AI_CHAT_CLOSE, x, y) for x, y in site.click_points)


def _button_clicks(events: list[tuple]) -> list[int]:
    return [index for index, event in enumerate(events) if event[0] == "click" and inside(AI_CHAT_CLOSE, *event[1:])]


def _log_lines(caplog, text: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if text in record.getMessage()]


def test_ai_chat_is_closed_after_entering_and_the_scan_runs_in_the_v16_layout(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    site = _chat_site()
    harness = Harness(tmp_path, site)
    events = _record_sleeps_and_clicks(harness)

    harness.run()

    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted"
    assert len(harness.processed) == 8 and site.ai_chat_open is False
    actions = _actions(harness)
    assert [line["action"] for line in actions[:4]] == ["type_search", "press_enter", "dismiss_ai_chat", "apply_filter"]
    dismiss = actions[2]
    assert dismiss["target"] == CLOSE_TARGET and set(dismiss["pointer"]) >= POINTER_KEYS
    assert [line["action"] for line in actions].count("dismiss_ai_chat") == 1
    # 关闭之前恰有一次反应停顿（与点选“图文”前的同一分布），之后只按下一次，第 1 次确认读数就已关闭。
    [press] = _button_clicks(events)
    kind, reaction = events[press - 1]
    low, high = xhs_scan.XHS_REACTION_SECONDS
    assert kind == "sleep" and low <= reaction <= high
    assert site.ai_chat_log[:4] == [("read", True, True), ("read", True, True), ("read", True, True), ("click",)]
    assert site.ai_chat_log[4:6] == [("closed",), ("read", False, False)]
    assert _button_presses(site) == 1

    # 关闭引发的续页：同一非目标会话（search_id、筛选签名不变）的第 2、3 页，trigger 是关闭动作；目标会话照常建立。
    searches = _searches(harness)
    assert [(line["is_target"], line["search_page"]) for line in searches[:4]] == [
        (False, 1), (False, 2), (False, 3), (True, 1),
    ]
    enter_page = searches[0]
    assert all(
        (line["search_session"], line["filter_signature"]) == (enter_page["search_session"], enter_page["filter_signature"])
        for line in searches[1:3]
    )
    assert [line["trigger"] for line in searches[1:3]] == [dismiss["id"], dismiss["id"]]
    note_type = next(line for line in actions if line["target"].get("filter") == "note_type")
    assert searches[3]["trigger"] == note_type["id"]
    assert not _log_lines(caplog, "was not triggered by this note-type click")
    assert _log_lines(caplog, "AI chat panel close:") == [
        "[XiaoHongShuCrawler.scan] AI chat panel close: clicks=1, confirmed=True, scroll_container=document, "
        "continuation_pages=2"
    ]
    assert not _diagnostics(harness, "ai_chat_close") and not _diagnostics(harness, "ai_chat_reappeared")
    gate = harness.gate()
    assert gate["content_visit_ok"], gate


def test_layout_that_is_already_v16_is_left_alone_with_one_log_line(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    site = XhsFakeSite(KEYWORD, _pages(1, 4))
    harness = Harness(tmp_path, site)

    harness.run()

    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted"
    assert "dismiss_ai_chat" not in [line["action"] for line in _actions(harness)]
    assert site.ai_chat_log == [("read", False, False)] and _button_presses(site) == 0
    assert _log_lines(caplog, "AI chat panel") == [
        "[XiaoHongShuCrawler.scan] AI chat panel absent; already V16 layout"
    ]
    assert not _diagnostics(harness, "ai_chat_close")


@pytest.mark.parametrize(
    ("mode", "fields"),
    [
        ("missing", {"button_count": 0, "button_visible": 0, "aria_label_ok": False, "center_hit": False}),
        ("covered", {"button_count": 1, "button_visible": 1, "aria_hidden_ancestor": False, "center_hit": False}),
        ("aria_hidden", {"button_count": 1, "aria_hidden_ancestor": True, "center_hit": True}),
    ],
)
def test_ai_chat_without_a_clickable_button_is_diagnosed_and_the_scan_continues_in_the_ai_layout(
    tmp_path, caplog, mode, fields,
):
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    site = _chat_site(ai_chat_button_mode=mode)
    harness = Harness(tmp_path, site)

    harness.run()

    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted" and len(harness.processed) == 8
    assert "dismiss_ai_chat" not in [line["action"] for line in _actions(harness)]
    assert site.ai_chat_open is True and _button_presses(site) == 0
    [diagnostic] = _diagnostics(harness, "ai_chat_close")
    assert diagnostic["outcome"] == "button_not_clickable"
    assert diagnostic["with_ai_chat"] is True and diagnostic["clicks"] == 0 and diagnostic["confirmed"] is False
    assert {key: diagnostic[key] for key in fields} == fields
    assert diagnostic["ai_chat_inner"] is not None
    assert (diagnostic["button"] is None) == (mode == "missing")
    assert _log_lines(caplog, "AI chat panel close:")[0].endswith(
        "clicks=0, confirmed=False, scroll_container=document, continuation_pages=0"
    )
    assert len(_log_lines(caplog, "AI chat panel not closed; continuing in the AI chat layout")) == 1
    assert [(line["is_target"], line["search_page"]) for line in _searches(harness)][:2] == [(False, 1), (True, 1)]
    # 按 AI 布局继续时不记“重新出现”。
    assert not _diagnostics(harness, "ai_chat_reappeared")
    gate = harness.gate()
    assert gate["content_visit_ok"], gate


def test_first_close_click_without_effect_is_retried_after_a_fresh_hit_test(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    site = _chat_site(ai_chat_close_ignored=1)
    harness = Harness(tmp_path, site)
    events = _record_sleeps_and_clicks(harness)

    harness.run()

    dismissals = [line for line in _actions(harness) if line["action"] == "dismiss_ai_chat"]
    assert len(dismissals) == 2 and all(line["target"] == CLOSE_TARGET for line in dismissals)
    assert _button_presses(site) == 2 and site.ai_chat_open is False
    log = site.ai_chat_log
    clicks = [index for index, entry in enumerate(log) if entry == ("click",)]
    assert len(clicks) == 2
    # 每次按下之前：反应停顿后的读数与到达静止后的复核都可点；第 2 次之前读满确认轮询。
    for index in clicks:
        assert log[index - 2:index] == [("read", True, True), ("read", True, True)]
    polls = log[clicks[0] + 1:clicks[1] - 2]
    assert polls == [("read", True, True)] * xhs_scan.XHS_AI_CHAT_CONFIRM_POLLS
    first, second = _button_clicks(events)
    between = events[first + 1:second]
    poll = ("sleep", xhs_scan.XHS_AI_CHAT_POLL_SECONDS)
    assert between[:-1] == [poll] * xhs_scan.XHS_AI_CHAT_CONFIRM_POLLS
    low, high = xhs_scan.XHS_REACTION_SECONDS
    assert between[-1][0] == "sleep" and low <= between[-1][1] <= high
    assert events[second + 1] == poll
    assert _log_lines(caplog, "AI chat panel close:")[0].endswith(
        "clicks=2, confirmed=True, scroll_container=document, continuation_pages=2"
    )
    searches = _searches(harness)
    assert [line["trigger"] for line in searches[1:3]] == [dismissals[1]["id"]] * 2
    assert not _diagnostics(harness, "ai_chat_close")
    gate = harness.gate()
    assert gate["content_visit_ok"], gate


def test_close_that_never_takes_effect_is_diagnosed_after_two_clicks_and_the_scan_continues(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    site = _chat_site(ai_chat_close_ignored=5)
    harness = Harness(tmp_path, site)

    harness.run()

    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted" and len(harness.processed) == 8
    assert [line["action"] for line in _actions(harness)].count("dismiss_ai_chat") == 2
    assert _button_presses(site) == 2 and site.ai_chat_open is True
    [diagnostic] = _diagnostics(harness, "ai_chat_close")
    assert diagnostic["outcome"] == "not_confirmed" and diagnostic["clicks"] == 2 and diagnostic["confirmed"] is False
    assert diagnostic["with_ai_chat"] is True and diagnostic["center_hit"] is True
    assert diagnostic["button"] == {key: AI_CHAT_CLOSE[key] for key in ("left", "top", "width", "height")}
    assert _log_lines(caplog, "AI chat panel not closed")[0].endswith(
        "outcome=not_confirmed, clicks=2, with_ai_chat=True, button_count=1, button_visible=1, aria_label_ok=True, "
        "aria_hidden_ancestor=False, center_hit=True"
    )
    gate = harness.gate()
    assert gate["content_visit_ok"], gate


@pytest.mark.parametrize(("delay", "dismissals"), [(20, 1), (21, 2)])
def test_panel_that_closes_late_is_never_pressed_again_at_the_old_position(tmp_path, caplog, delay, dismissals):
    """第 1 次按下生效但对话栏迟迟才关：delay=20 在确认轮询读满之后、第 2 次的命中检测时已关；delay=21 在第 2 次
    到达按钮并静止后的复核时才关。两种情况都不再按下（原位置在 V16 下是第 4 列卡片的封面）。"""
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    site = _chat_site(ai_chat_close_delay_reads=delay)
    harness = Harness(tmp_path, site)

    harness.run()

    assert [line["action"] for line in _actions(harness)].count("dismiss_ai_chat") == dismissals
    assert _button_presses(site) == 1 and site.ai_chat_open is False and site.overlay_note is None
    closed = site.ai_chat_log.index(("closed",))
    assert ("click",) not in site.ai_chat_log[closed:]
    assert _log_lines(caplog, "AI chat panel close:")[0].startswith(
        "[XiaoHongShuCrawler.scan] AI chat panel close: clicks=1, confirmed=True"
    )
    assert not _diagnostics(harness, "ai_chat_close")


def test_button_covered_by_the_time_the_pointer_arrives_is_not_pressed(tmp_path, caplog):
    """第 1 次读数（是否有对话栏）与反应停顿后的命中检测都可点，光标到达并静止后的复核时按钮已被盖住：不按下，
    第 2 次的命中检测仍被盖住就不再计划点击，写诊断后按 AI 布局继续。"""
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    site = _chat_site(ai_chat_covered_from_read=3)
    harness = Harness(tmp_path, site)

    harness.run()

    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted" and len(harness.processed) == 8
    assert [line["action"] for line in _actions(harness)].count("dismiss_ai_chat") == 1
    assert _button_presses(site) == 0 and ("click",) not in site.ai_chat_log and site.ai_chat_open is True
    [diagnostic] = _diagnostics(harness, "ai_chat_close")
    assert diagnostic["outcome"] == "button_not_clickable" and diagnostic["clicks"] == 0
    assert diagnostic["center_hit"] is False and diagnostic["button_visible"] == 1
    gate = harness.gate()
    assert gate["content_visit_ok"], gate


def test_continuation_pages_arriving_after_the_note_type_click_stay_non_target(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    site = _chat_site(ai_chat_followups_late=True)
    harness = Harness(tmp_path, site)

    harness.run()

    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted" and len(harness.processed) == 8
    searches = _searches(harness)
    assert [(line["is_target"], line["search_page"]) for line in searches[:4]] == [
        (False, 1), (True, 1), (False, 2), (False, 3),
    ]
    note_type = next(line for line in _actions(harness) if line["target"].get("filter") == "note_type")
    assert [line["trigger"] for line in searches[1:4]] == [note_type["id"]] * 3
    assert searches[2]["search_session"] == searches[0]["search_session"] != searches[1]["search_session"]
    assert harness.crawler._scan.target.search_id == searches[1]["search_session"]
    assert _log_lines(caplog, "AI chat panel close:")[0].endswith("continuation_pages=0")
    assert not _log_lines(caplog, "was not triggered by this note-type click")
    assert not _log_lines(caplog, "unseen search session")
    gate = harness.gate()
    assert gate["content_visit_ok"], gate


def test_reentering_the_search_closes_the_ai_chat_again(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    site = _chat_site()
    harness = Harness(tmp_path, site)
    process = harness.process_note

    async def leave_once(item, search_page, card) -> None:
        await process(item, search_page, card)
        if len(harness.processed) == 1:
            site.page.url = "https://www.xiaohongshu.com/explore"

    harness.crawler._process_note_card = leave_once

    harness.run()

    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted"
    actions = [line["action"] for line in _actions(harness)]
    assert actions.count("press_enter") == 2 and actions.count("dismiss_ai_chat") == 2
    enters = [index for index, action in enumerate(actions) if action == "press_enter"]
    assert all(actions[index + 1] == "dismiss_ai_chat" for index in enters)
    assert _button_presses(site) == 2 and site.ai_chat_open is False
    assert len(_log_lines(caplog, "AI chat panel close: clicks=1, confirmed=True")) == 2
    targets = [line for line in _searches(harness) if line["is_target"] and line["search_page"] == 1]
    assert len(targets) == 2
    gate = harness.gate()
    assert gate["content_visit_ok"], gate


def test_ai_chat_reappearing_during_the_scan_is_diagnosed_once_per_session_and_not_closed(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    site = _chat_site()
    harness = Harness(tmp_path, site)
    process = harness.process_note

    async def reopen_once(item, search_page, card) -> None:
        await process(item, search_page, card)
        if len(harness.processed) == 1:
            site.ai_chat_open = True

    harness.crawler._process_note_card = reopen_once

    harness.run()

    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted" and len(harness.processed) == 8
    assert [line["action"] for line in _actions(harness)].count("dismiss_ai_chat") == 1
    assert _button_presses(site) == 1 and site.ai_chat_open is True
    [diagnostic] = _diagnostics(harness, "ai_chat_reappeared")
    assert diagnostic["outcome"] == "present_during_scan" and diagnostic["with_ai_chat"] is True
    assert diagnostic["scroll_container"] == "document" and diagnostic["search_page"] == 1
    assert len(_log_lines(caplog, "AI chat panel reappeared during the scan; not closing it")) == 1
    gate = harness.gate()
    assert gate["content_visit_ok"], gate


# V16 几何（Mac 人工侦察 2026-10-22）：入口 div.filter [1354,116,84,40] 在标签行内，“筛选”文字 span 约 20px 高、在 div
# 里居中（[1376,126,40,20]）；悬停打开的面板 [992,166,457,300] 在入口正下方靠右，div 与面板之间约 10px 间隙，光标
# 垂直穿过不收起。span 到面板约 20px，超过走廊上限，所以要按 div 判定。
RECON_DIV = box(1396.0, 136.0, 84, 40)
RECON_SPAN = box(1396.0, 136.0, 40, 20)
RECON_PANEL = box(1220.5, 316.0, 457, 300)
RECON_OPTION = box(1396.0, 230.0, 48, 24)
RECON_CORRIDOR = Box(1376.0, 146.0, 40.0, 20.0)
# 同一几何横向左移 300px，放进假站点 1280 宽的视口。标签行只到入口右侧：假站点的对话栏关闭按钮在右上角，真实页面上
# 它在对话栏里、不在列表的标签行内。
V16_DIV = box(1096.0, 136.0, 84, 40)
V16_SPAN = box(1096.0, 136.0, 40, 20)
V16_PANEL = box(920.5, 316.0, 457, 300)
V16_OPTION = box(1096.0, 230.0, 48, 24)
V16_DIV_GAP = {"left": 1054.0, "top": 156.0, "width": 84.0, "height": 10.0}
V16_ROW = {"left": 40.0, "top": 116.0, "right": 1180.0, "bottom": 156.0}


def _entry_state(entry: dict, panel: dict | None, container: dict | None = None, *, with_ai_chat: bool) -> dict:
    return {"entry": entry, "panel": panel, "entry_container": container, "with_ai_chat": with_ai_chat}


def _moved(point: dict, dy: float) -> dict:
    return box(point["x"], point["y"] + dy, point["width"], point["height"])


def test_gap_corridor_is_decided_by_the_entry_container_and_spans_from_the_label_to_the_panel():
    state = _entry_state(RECON_SPAN, RECON_PANEL, RECON_DIV, with_ai_chat=False)
    assert xhs_scan._gap_corridor(state) == (RECON_CORRIDOR, 10.0)
    assert xhs_scan._hover_region(state) == (Box.from_point(RECON_SPAN), RECON_CORRIDOR, Box.from_point(RECON_PANEL))
    # div 到面板超过 16px：不加走廊。
    far = _entry_state(RECON_SPAN, _moved(RECON_PANEL, 7), RECON_DIV, with_ai_chat=False)
    assert xhs_scan._gap_corridor(far) == (None, 17.0)
    # div 碰到或盖住面板（不大于 0 也算），span 下沿仍在面板上沿之上：走廊为 span 下沿到面板上沿。
    overlapping = _moved(RECON_PANEL, -16)
    assert xhs_scan._gap_corridor(_entry_state(RECON_SPAN, overlapping, RECON_DIV, with_ai_chat=False)) == (
        Box(1376.0, 146.0, 40.0, 4.0), -6.0,
    )
    # span 下沿不在面板上沿之上、横向不重叠：都不加。
    above = _entry_state(RECON_SPAN, _moved(RECON_PANEL, -26), RECON_DIV, with_ai_chat=False)
    assert xhs_scan._gap_corridor(above)[0] is None
    aside = box(700.0, RECON_PANEL["y"], 400, RECON_PANEL["height"])
    assert xhs_scan._gap_corridor(_entry_state(RECON_SPAN, aside, RECON_DIV, with_ai_chat=False))[0] is None
    # 取不到 div 时按 span 判定：span 到面板 20px 不加，10px 加。
    assert xhs_scan._gap_corridor(_entry_state(RECON_SPAN, RECON_PANEL, with_ai_chat=False)) == (None, 20.0)
    near = _entry_state(RECON_SPAN, _moved(RECON_PANEL, -10), with_ai_chat=False)
    assert xhs_scan._gap_corridor(near)[0] == Box(1376.0, 146.0, 40.0, 10.0)
    no_panel = _entry_state(RECON_SPAN, None, RECON_DIV, with_ai_chat=False)
    assert xhs_scan._hover_region(no_panel) == (Box.from_point(RECON_SPAN),)
    assert xhs_scan._gap_corridor(no_panel) == (None, None)
    # 读数里没有 with_ai_chat 时同 V16。
    assert xhs_scan._gap_corridor({"entry": RECON_SPAN, "panel": RECON_PANEL, "entry_container": RECON_DIV}) == (
        RECON_CORRIDOR, 10.0,
    )


def test_gap_corridor_is_never_added_while_the_ai_chat_is_open():
    """AI 对话布局（关闭失败的备用路径）保持实测过的严格并集：同样的几何、div 盖住面板或按 span 判定都不加走廊，
    间隙照常算出供日志核对。"""
    state = _entry_state(RECON_SPAN, RECON_PANEL, RECON_DIV, with_ai_chat=True)
    assert xhs_scan._gap_corridor(state) == (None, 10.0)
    assert xhs_scan._hover_region(state) == (Box.from_point(RECON_SPAN), Box.from_point(RECON_PANEL))
    overlapping = _entry_state(RECON_SPAN, _moved(RECON_PANEL, -16), RECON_DIV, with_ai_chat=True)
    assert xhs_scan._gap_corridor(overlapping) == (None, -6.0)
    near = _entry_state(RECON_SPAN, _moved(RECON_PANEL, -10), with_ai_chat=True)
    assert xhs_scan._gap_corridor(near)[0] is None and len(xhs_scan._hover_region(near)) == 2


def test_vertical_path_from_the_label_down_to_the_option_passes_through_the_gap_instead_of_jumping_it():
    """60 个种子：轨迹点都在 span、走廊与面板的并集内，且绝大多数轨迹确有点落在 span 与面板之间的 20px 里（少数是
    主移动一帧本来就超过 20px）；只按 span 与面板严格取并集时，间隙里的点全被夹到边上、一个也不剩。"""
    region = xhs_scan._hover_region(_entry_state(RECON_SPAN, RECON_PANEL, RECON_DIV, with_ai_chat=False))
    strict = (Box.from_point(RECON_SPAN), Box.from_point(RECON_PANEL))
    seeds = range(60)

    def crossing(bounds: tuple[Box, ...]) -> int:
        crossed = 0
        for seed in seeds:
            pointer = HumanPointer(None, rng=random.Random(seed))
            pointer.viewport = _viewport  # type: ignore[method-assign]
            pointer.position = (1380.0 + seed % 30, 130.0 + seed % 12)
            plan = asyncio.run(pointer.plan_click(Box.from_point(RECON_OPTION), region=bounds))
            points = [(move[0], move[1]) for move in plan.moves]
            assert all(any(_within(area, *point) for area in bounds) for point in points), seed
            crossed += any(RECON_CORRIDOR.top < y < RECON_CORRIDOR.top + RECON_CORRIDOR.height for _, y in points)
        return crossed

    assert crossing(region) >= 0.75 * len(seeds)
    assert crossing(strict) == 0


async def _viewport() -> Box:
    return Box(0.0, 0.0, 1470.0, 900.0)


def _within(area: Box, x: float, y: float) -> bool:
    return area.left <= x <= area.left + area.width and area.top <= y <= area.top + area.height


@pytest.mark.parametrize("seed", range(6))
def test_v16_hover_filter_after_closing_keeps_the_panel_open_and_never_touches_the_tag_row(
    tmp_path, monkeypatch, caplog, seed,
):
    caplog.set_level(logging.INFO, logger="MediaCrawler")
    site = _chat_site(
        hover_opens_panel=True, entry_box=dict(V16_SPAN), entry_container_box=dict(V16_DIV),
        panel_box=dict(V16_PANEL), option_box=dict(V16_OPTION), hover_gaps=[dict(V16_DIV_GAP)],
        top_row=dict(V16_ROW), list_rect=dict(LIST_RECT),
    )
    xhs_scan.pointer_for(site.page).rng = random.Random(seed)
    harness = Harness(tmp_path, site)
    moves: list[tuple] = []
    mouse = site.page.mouse
    move = mouse.move

    async def recording_move(x: float, y: float, steps: int = 1) -> None:
        await move(x, y, steps)
        moves.append((x, y, site.panel_visible))

    mouse.move = recording_move
    record = harness.crawler._record_trigger_action
    marks: dict[str, int] = {}

    def recording_action(action, target, **kwargs):
        marks.setdefault(f"{action}:{target.get('filter') or target.get('phase') or ''}", len(moves))
        return record(action, target, **kwargs)

    harness.crawler._record_trigger_action = recording_action

    harness.run()

    assert harness.accumulator.summary()["stop_reason"] == "source_exhausted"
    filters = [
        (line["target"].get("filter"), line["target"].get("method"))
        for line in _actions(harness) if line["action"] == "apply_filter"
    ]
    assert filters == [("panel_open", None), ("note_type", None), ("panel_close", "pointer_leave")]
    approach = moves[marks["apply_filter:note_type"]:marks["apply_filter:panel_close"]]
    assert approach and all(visible for _, _, visible in approach)
    union = xhs_scan._hover_region(_entry_state(V16_SPAN, V16_PANEL, V16_DIV, with_ai_chat=False))
    assert len(union) == 3 and all(any(_within(area, x, y) for area in union) for x, y, _ in approach)
    assert _log_lines(caplog, "Filter option approach:") == [
        "[XiaoHongShuCrawler.scan] Filter option approach: hovered=True, with_ai_chat=False, "
        "entry=(1076, 126, 1116, 146), "
        "entry_container=(1054, 116, 1138, 156), panel=(692, 166, 1149, 466), corridor=(1076, 146, 1116, 166), "
        "container_gap=10.0"
    ]
    assert not [event for event in harness.crawler._navigation_diagnostics if event["stage"] == "search_filter"]
    # 标签行：除筛选入口外没有点击落在行内，滚轮与移开光标的落点也不在行内；“问点点 ai”从未被点。
    in_row = [point for point in site.click_points if inside(_row_box(V16_ROW), *point)]
    assert all(inside(V16_DIV, *point) for point in in_row) and site.ask_ai_clicks == 0
    assert not [point for point in site.wheel_points if inside(_row_box(V16_ROW), *point)]
    landing = moves[marks["scroll:behavior_stage"] - 1]
    assert not inside(_row_box(V16_ROW), landing[0], landing[1])
    assert site.ai_chat_open is False


def _row_box(row: dict) -> dict:
    return {"left": row["left"], "top": row["top"], "width": row["right"] - row["left"], "height": row["bottom"] - row["top"]}


# 标签行避让的几何（Mac 人工侦察 2026-10-22，内高 476）：滚动位置为 0 时标签行在列表区域内缩量之上；假定标签行吸顶
# （未经实测）时它会落进内缩后的区域，区域、移开光标的落点与卡片可点击带都从它下沿之下 8px 起。
V16_METRICS = {
    "scroll_container": "document", "scroll_y": 0,
    "view_rect": {"left": 0, "top": 0, "right": 1470, "bottom": 476},
    "list_rect": {"left": 122, "top": 116, "right": 1438, "bottom": 476},
    "top_row_rect": {"left": 122, "top": 116, "right": 1438, "bottom": 156},
    "layout_present": True, "with_ai_chat": False,
}
STICKY_METRICS = {
    **V16_METRICS, "scroll_y": 2400, "list_rect": {"left": 122, "top": 0, "right": 1438, "bottom": 476},
}


class _MetricsPage:
    url = "https://www.xiaohongshu.com/search_result_ai"

    def __init__(self, metrics: dict) -> None:
        self.metrics = metrics

    async def evaluate(self, script: str, arg=None) -> dict:
        assert script == DOM_SCRIPTS["scroll_metrics"]
        return dict(self.metrics)


def test_list_area_leave_area_and_click_band_start_below_the_tag_row():
    floor = 156 + XHS_LIST_TOP_MARGIN_PX
    region = xhs_scan._hover_region(_entry_state(RECON_SPAN, RECON_PANEL, RECON_DIV, with_ai_chat=False))
    # 滚动位置为 0：内缩已让区域离开标签行，结果不变；落点区域在入口与面板左侧。
    area = list_area_box(V16_METRICS)
    assert area == pytest.approx(Box(385.2, 188.0, 789.6, 216.0))
    leave = xhs_scan._leave_area(xhs_scan.XhsScanMixin._scroll_area(V16_METRICS), region)
    assert leave == pytest.approx(Box(385.2, 188.0, 582.8, 216.0))
    assert xhs_scan.XhsScanMixin._click_band(V16_METRICS) == (floor, 468.0)
    # 吸顶假设：内缩后的区域上沿 95.2 在标签行内，改从它下沿之下起；行为阶段的区域同此。
    sticky = list_area_box(STICKY_METRICS)
    assert sticky == pytest.approx(Box(385.2, floor, 789.6, 380.8 - floor))
    assert asyncio.run(xhs_list_area(_MetricsPage(STICKY_METRICS))).box == sticky
    leave = xhs_scan._leave_area(xhs_scan.XhsScanMixin._scroll_area(STICKY_METRICS), region)
    assert leave is not None and leave.top >= floor
    assert xhs_scan.XhsScanMixin._click_band(STICKY_METRICS)[0] == floor
    # 没有标签行读数时与原来相同；标签行占满区域时没有区域。
    plain = {**STICKY_METRICS, "top_row_rect": None}
    assert list_area_box(plain).top == pytest.approx(95.2)
    assert xhs_scan.XhsScanMixin._click_band(plain)[0] == pytest.approx(47.6)
    tall = {**STICKY_METRICS, "top_row_rect": {"left": 122, "top": 0, "right": 1438, "bottom": 400}}
    assert list_area_box(tall) is None


def test_tag_row_reported_by_the_fake_site_scrolls_with_the_list_unless_sticky():
    site = XhsFakeSite(KEYWORD, _pages(1, 4))
    site.page.url = "https://www.xiaohongshu.com/search_result_ai?keyword=x"
    site.top_row = dict(TOP_ROW)
    assert site.top_row_rect() == TOP_ROW
    site.scroll_y = 90
    assert site.top_row_rect() == {**TOP_ROW, "top": 0.0, "bottom": 26.0}
    site.scroll_y = 200
    assert site.top_row_rect() is None
    site.top_row_sticky = True
    assert site.top_row_rect() == TOP_ROW
