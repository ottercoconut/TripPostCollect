"""搜索结果页行为动作的列表区域（#88）：两种布局都取得到；取不到时行为阶段失败并写明原因，连续性行为跳过随机移动。"""

from __future__ import annotations

import asyncio

import pytest

import trippostcollect.platforms.xhs.behavior as xhs_behavior
from trippostcollect.platforms.xhs.page_scripts import DOM_SCRIPTS
from trippostcollect.runtime import behavior as runtime_behavior
from trippostcollect.runtime import human_flow
from trippostcollect.runtime.human_pointer import Box

# 与离线站点两种布局同构的 scroll_metrics：A 为对话栏打开、列表在顶部固定区之下的容器内滚动；B 为整页滚动。
LAYOUT_A = {
    "scroll_container": "element", "scroll_y": 720,
    "view_rect": {"left": 0, "top": 116, "right": 942, "bottom": 900},
    "list_rect": {"left": 0, "top": 116, "right": 942, "bottom": 900},
    "layout_present": True,
}
LAYOUT_B = {
    "scroll_container": "document", "scroll_y": 1500,
    "view_rect": {"left": 0, "top": 0, "right": 1280, "bottom": 900},
    "list_rect": {"left": 40, "top": 0, "right": 1240, "bottom": 900},
    "layout_present": True,
}


class MetricsPage:
    url = "https://www.xiaohongshu.com/search_result_ai"

    def __init__(self, metrics: dict) -> None:
        self.metrics = metrics

    async def evaluate(self, script: str, arg=None) -> dict:
        assert script == DOM_SCRIPTS["scroll_metrics"]
        return dict(self.metrics)


@pytest.mark.parametrize(
    ("metrics", "box", "container"),
    [
        (LAYOUT_A, Box(188.4, 272.8, 565.2, 470.4), "element"),
        (LAYOUT_B, Box(280.0, 180.0, 720.0, 540.0), "document"),
    ],
)
def test_list_area_is_the_inset_list_rectangle_in_both_layouts(metrics, box, container):
    area = asyncio.run(xhs_behavior.xhs_list_area(MetricsPage(metrics)))

    assert area.box == pytest.approx(box) and area.scroll_container == container
    assert area.scroll_y == metrics["scroll_y"]  # A 为容器的 scrollTop，B 为 window.scrollY
    # 扫描滚动落点取同一区域。
    assert xhs_behavior.list_area_box(metrics) == area.box


@pytest.mark.parametrize(
    ("metrics", "reason"),
    [
        ({**LAYOUT_A, "list_rect": None, "layout_present": False}, "search_layout_missing"),
        ({**LAYOUT_B, "list_rect": None}, "list_not_in_view"),
    ],
)
def test_missing_list_area_is_an_error_not_the_whole_viewport(metrics, reason):
    with pytest.raises(xhs_behavior.XhsListAreaUnavailable) as caught:
        asyncio.run(xhs_behavior.xhs_list_area(MetricsPage(metrics)))

    assert caught.value.reason == reason and str(caught.value) == f"xhs_list_area_unavailable:{reason}"


async def _no_pause(page, seconds_range, *, reason, log):
    log.append({"event": "pause", "reason": reason})


async def _calm_page(page):
    return "搜索结果", {}


def test_continuity_without_a_list_area_skips_the_moves_and_completes(monkeypatch):
    monkeypatch.setattr(xhs_behavior, "human_pause", _no_pause)
    monkeypatch.setattr(xhs_behavior, "visible_page_state", _calm_page)
    page = MetricsPage({**LAYOUT_A, "list_rect": None, "layout_present": False})
    pending: dict = {}

    continuity = asyncio.run(xhs_behavior.run_xhs_continuity_behavior(
        page, evidence_path="behavior_evidence.json", stage="search_page",
        write_evidence=lambda path, evidence: pytest.fail("pending 模式不写证据"), pending=pending,
    ))

    assert continuity["status"] == "completed" and pending["continuity_events"] == [continuity]
    # 不退回整个视口：没有移动光标，只记跳过及原因。
    assert continuity["events"][1:] == [{"event": "mouse_moves_skipped", "reason": "search_layout_missing"}]
    assert "status" not in pending


def test_behavior_stage_without_a_list_area_fails_with_the_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(human_flow, "human_pause", _no_pause)
    monkeypatch.setattr(runtime_behavior, "visible_page_state", _calm_page)

    class Page(MetricsPage):
        async def screenshot(self, **kwargs) -> None:
            return None

    async def ready(page, events):
        return {"ready": True}

    written = []
    with pytest.raises(RuntimeError, match="xhs_list_area_unavailable:list_not_in_view"):
        asyncio.run(runtime_behavior.run_page_behavior(
            Page({**LAYOUT_B, "list_rect": None}), platform_key="xhs", profile_name="xhs_guarded",
            evidence_path=tmp_path / "behavior_evidence.json", xhs_search_ready=ready,
            xhs_list_area=xhs_behavior.xhs_list_area,
            write_evidence=lambda path, evidence: written.append(evidence),
        ))

    (evidence,) = written
    assert evidence["status"] == "failed" and "xhs_list_area_unavailable:list_not_in_view" in evidence["error"]
    assert [event["event"] for event in evidence["events"]] == ["pause"]
