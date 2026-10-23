"""行为动作的列表区域（#88）：给出区域时随机移动目标点与滚轮落点都在区域内、滚动有效只看列表；不给时口径不变。"""

from __future__ import annotations

import asyncio
import random

import pytest

from trippostcollect.runtime import human_flow
from trippostcollect.runtime import human_pointer as hp

# 布局 A 的几何：列表容器可见区在顶部固定区（116px）之下，右侧 942px 起是对话栏；区域为它四周内缩 20%。
LIST = hp.Box(0.0, 116.0, 942.0, 784.0)
AREA = hp.Box(188.4, 272.8, 565.2, 470.4)
SEEDS = range(20)


def _inside(box: hp.Box, x: float, y: float) -> bool:
    return box.left <= x <= box.left + box.width and box.top <= y <= box.top + box.height


class Mouse:
    def __init__(self, page: "Page") -> None:
        self.page = page
        self.position = (0.0, 0.0)
        self.moves: list[tuple[float, float]] = []
        self.wheels: list[tuple[float, float]] = []

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        self.position = (x, y)
        self.moves.append((x, y))

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        self.wheels.append(self.position)
        # 落点在列表上滚列表，否则滚对话栏；`list_frozen` 时列表不动（只有其他节点滚动）。
        if _inside(LIST, *self.position) and not self.page.list_frozen:
            self.page.list_y += delta_y
        else:
            self.page.chat_y += abs(delta_y)


class Page:
    viewport_size = {"width": 1450, "height": 900}

    def __init__(self, *, list_frozen: bool = False) -> None:
        self.mouse = Mouse(self)
        self.list_frozen = list_frozen
        self.list_y = 0
        self.chat_y = 0
        self.probes = 0

    async def wait_for_timeout(self, milliseconds: float) -> None:
        return None

    async def evaluate(self, script: str) -> dict:
        if "innerWidth" in script:
            return {"width": 1450, "height": 900}
        # scroll_position：整页口径把所有可滚动节点（含对话栏）都算进去。
        return {
            "window_y": 0, "scrollable_count": 2,
            "scroll_top_sum": self.list_y + self.chat_y, "max_scroll_top": max(self.list_y, self.chat_y),
        }

    async def list_area(self, page: "Page") -> human_flow.ListArea:
        assert page is self
        self.probes += 1
        return human_flow.ListArea(box=AREA, scroll_y=self.list_y, scroll_container="element")


@pytest.fixture
def seeded(monkeypatch):
    """目标点与光标轨迹按种子取随机数。光标按页面缓存（与 pointer_for 相同），并且已在列表上：正式轮到行为阶段
    与连续性行为时，光标已因搜索框与筛选点击有了位置，不会再从视口中下部“出现”。"""
    def seed(value: int) -> None:
        pointers: dict[int, hp.HumanPointer] = {}

        def pointer_for(page):
            if id(page) not in pointers:
                pointer = pointers[id(page)] = hp.HumanPointer(page, rng=random.Random(value), drift_sign=1)
                pointer.position = (470.0, 500.0)
            return pointers[id(page)]

        monkeypatch.setattr(human_flow, "RANDOM", random.Random(value))
        monkeypatch.setattr(human_flow, "pointer_for", pointer_for)
    return seed


def test_with_a_list_area_targets_and_wheels_stay_inside_and_paths_stay_on_the_list(seeded):
    profile = human_flow.load_behavior_profile("xhs_guarded", strict=True)
    for seed in SEEDS:
        seeded(seed)
        page, events = Page(), []

        async def run() -> None:
            await human_flow.random_mouse_moves(page, profile, events, list_area=page.list_area)
            await human_flow.human_scroll(page, profile, intent="list", max_passes=3, log=events, list_area=page.list_area)

        asyncio.run(run())

        area = {"left": 188.4, "top": 272.8, "width": 565.2, "height": 470.4}
        (moves,) = [event for event in events if event["event"] == "mouse_moves"]
        assert moves["area"] == area and moves["scroll_container"] == "element"
        targets = [event for event in events if event["event"] == "scroll_target"]
        assert targets and all(_inside(AREA, event["x"], event["y"]) for event in targets)
        assert all(event["area"] == area and event["scroll_container"] == "element" for event in targets)
        # 滚轮都落在区域内（轨迹终点即目标点），轨迹本身不离开列表，对话栏从未滚动。
        assert page.mouse.wheels and all(_inside(AREA, *point) for point in page.mouse.wheels)
        assert page.mouse.moves and all(_inside(LIST, *point) for point in page.mouse.moves), seed
        assert page.chat_y == 0
        wheels = [event for event in events if event["event"] == "wheel_scroll"]
        assert all(set(event["before"]) == {"scroll_y", "scroll_container"} for event in wheels)
        (complete,) = [event for event in events if event["event"] == "human_scroll_complete"]
        assert complete["effective_passes"] == complete["passes"]


def test_scrolling_only_other_nodes_is_not_an_effective_list_scroll(seeded):
    profile = human_flow.load_behavior_profile("xhs_guarded", strict=True)
    seeded(1)
    page, events = Page(list_frozen=True), []

    asyncio.run(human_flow.human_scroll(
        page, profile, intent="list", max_passes=2, log=events, list_area=page.list_area,
    ))

    assert page.chat_y > 0 and page.list_y == 0
    wheels = [event for event in events if event["event"] == "wheel_scroll"]
    assert wheels and not any(event["effect_observed"] for event in wheels)
    assert events[-1]["effective_passes"] == 0
    # 同一页面按整页口径（不给区域）会把其他节点的滚动算作有效。
    seeded(1)
    page, events = Page(list_frozen=True), []
    asyncio.run(human_flow.human_scroll(page, profile, intent="list", max_passes=2, log=events))
    assert events[-1]["effective_passes"] > 0


def test_list_scroll_counts_only_within_the_same_container():
    before = {"scroll_y": 0, "scroll_container": "element"}
    assert human_flow.list_scroll_moved(before, {"scroll_y": 300, "scroll_container": "element"})
    assert not human_flow.list_scroll_moved(before, {"scroll_y": 0, "scroll_container": "element"})
    assert not human_flow.list_scroll_moved(before, {"scroll_y": 300, "scroll_container": "document"})


def test_other_profiles_keep_viewport_targets_and_page_scroll_measure(seeded):
    profile = human_flow.load_behavior_profile("social_high_risk", strict=True)
    seeded(3)
    page, events = Page(), []

    async def run() -> None:
        await human_flow.random_mouse_moves(page, profile, events)
        await human_flow.human_scroll(page, profile, intent="list", max_passes=2, log=events)

    asyncio.run(run())

    assert page.probes == 0
    (moves,) = [event for event in events if event["event"] == "mouse_moves"]
    assert set(moves) == {"event", "count"}
    for event in (event for event in events if event["event"] == "scroll_target"):
        assert set(event) == {"event", "x", "y"}
        assert 1450 // 3 <= event["x"] <= 1450 * 4 // 5 and 900 // 3 <= event["y"] <= 900 * 4 // 5
    wheels = [event for event in events if event["event"] == "wheel_scroll"]
    assert wheels and all(set(event["before"]) == {"window_y", "scrollable_count", "scroll_top_sum", "max_scroll_top"}
                          for event in wheels)
