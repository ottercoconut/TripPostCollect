"""#84 小红书指针动作：轨迹节奏与分段、框内落点、按压时长、触控板手势形状与拆分，以及小红书路径的静态检查。

计划函数全部用固定种子的 random.Random，结论不随运行变化；参照值来自操作人 2026-10-17 的 Mac 触控板样本。
"""

from __future__ import annotations

import ast
import asyncio
import math
from pathlib import Path
import random
import statistics

import pytest

from trippostcollect.runtime import human_pointer as hp

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src/trippostcollect"
VIEW = hp.Box(0.0, 0.0, 1450.0, 820.0)
SEEDS = range(200)


class RecordingMouse:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    async def move(self, x: float, y: float) -> None:
        self.events.append(("move", x, y))

    async def down(self) -> None:
        self.events.append(("down",))

    async def up(self) -> None:
        self.events.append(("up",))

    async def wheel(self, delta_x: int, delta_y: int) -> None:
        self.events.append(("wheel", delta_x, delta_y))


class RecordingPage:
    """记录每个事件及其前面的等待（毫秒）；等待不真的睡，只推进假时钟（见 clock 夹具）。"""

    viewport_size = {"width": 1450, "height": 820}
    clock = [0.0]

    def __init__(self) -> None:
        self.mouse = RecordingMouse()
        self.waits: list[float] = []

    async def wait_for_timeout(self, milliseconds: float) -> None:
        self.waits.append(milliseconds)
        self.mouse.events.append(("wait", milliseconds))
        self.clock[0] += milliseconds


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """光标按绝对时刻对齐等待；时间只随假页面的等待推进。"""
    RecordingPage.clock = [0.0]
    monkeypatch.setattr(hp, "_now_ms", lambda: RecordingPage.clock[0])


def _gaps(moves: list[hp.Move]) -> list[float]:
    return [move[2] for move in moves]


def _segments(moves: list[hp.Move]) -> list[list[hp.Move]]:
    """按段间停顿（至少 90ms 加一帧；慢尾段并入的几帧远短于此）切开：主移动与各次修正。"""
    segments: list[list[hp.Move]] = [[]]
    for move in moves:
        if move[2] > hp.SEGMENT_PAUSE_MS[0] + hp.FRAME_MS * 0.6 and segments[-1]:
            segments.append([])
        segments[-1].append(move)
    return segments


# 轨迹 -----------------------------------------------------------------------

def test_stroke_events_are_one_frame_apart_and_end_exactly_on_the_target():
    for seed in SEEDS:
        rng = random.Random(seed)
        moves = hp.plan_stroke((100.0, 700.0), (843.0, 212.0), 150.0, rng, bounds=VIEW)
        assert moves[-1][:2] == (843.0, 212.0)
        gaps = _gaps(moves)
        # 位移不到 0.1px 的帧并入下一事件，所以个别间隔是整数帧；其余在 16.7ms ±3% 内。
        assert all(hp.FRAME_MS * 0.96 <= gap <= hp.FRAME_MS * 1.04 * round(gap / hp.FRAME_MS) for gap in gaps)
        assert statistics.median(gaps) == pytest.approx(hp.FRAME_MS, rel=0.04)
        assert all(VIEW.left < x < VIEW.width and VIEW.top < y < VIEW.height for x, y, _ in moves)
        assert len({(x, y) for x, y, _ in moves}) == len(moves)


def test_approach_is_a_fast_main_stroke_then_one_or_two_corrections_onto_the_target():
    second = 0
    for seed in SEEDS:
        rng = random.Random(seed)
        start, target = (80.0, 650.0), (810.0, 410.0)  # 约 770px，与样本里 700–760px 的主移动同级
        moves = hp.plan_approach(start, target, rng, bounds=VIEW)
        assert moves[-1][:2] == target
        main, *corrections = _segments(moves)
        assert 1 <= len(corrections) <= 2
        second += len(corrections) == 2
        # 主移动：样本 700–760px 用 140–150ms；落点偏离目标不超过 115px（加上 ±0.25rad 的方向偏差）。
        assert 100 <= sum(_gaps(main)) <= 230
        miss = math.dist(main[-1][:2], target)
        assert 0 < miss <= hp.CORRECTION_MAX_PX + 1
        # 修正前静止 90–260ms（期间没有事件），修正本身约 200ms。
        for segment in corrections:
            pause = segment[0][2] - hp.FRAME_MS
            assert hp.SEGMENT_PAUSE_MS[0] - 1 <= pause <= hp.SEGMENT_PAUSE_MS[1] + 1
            assert 100 <= sum(_gaps(segment)) - pause <= 320
    assert 0 < second < len(SEEDS)  # 有时两次修正、有时一次


def test_short_moves_go_in_a_single_stroke():
    for seed in SEEDS:
        rng = random.Random(seed)
        moves = hp.plan_approach((500.0, 500.0), (530.0, 540.0), rng, bounds=VIEW)
        assert len(_segments(moves)) == 1 and moves[-1][:2] == (530.0, 540.0)


def test_main_stroke_duration_barely_grows_with_distance():
    rng = random.Random(3)
    short = statistics.median(hp.stroke_ms(225, rng) for _ in range(400))
    long = statistics.median(hp.stroke_ms(730, rng) for _ in range(400))
    # 样本：150–300px 用 130–170ms，700–760px 用 140–150ms。
    assert 130 <= short <= 150 and 140 <= long <= 160


# 落点与按压 -----------------------------------------------------------------

def test_click_point_lands_inside_the_box_off_centre_within_the_sample_range():
    box = hp.Box(100.0, 200.0, 150.0, 110.0)  # 样本卡片 150×110
    points = [hp.click_point(box, random.Random(seed)) for seed in SEEDS]
    assert all(isinstance(x, int) and isinstance(y, int) for x, y in points)
    for x, y in points:
        assert 0.25 * 150 <= x - 100 <= 0.75 * 150
        assert 0.25 * 110 <= y - 200 <= 0.55 * 110
    center = (175, 255)
    assert sum(point == center for point in points) < len(points) * 0.05
    assert len(set(points)) > len(points) * 0.5


def test_click_point_on_small_targets_keeps_off_the_edges():
    box = hp.Box(611.5, 92.5, 26.0, 17.0)  # “筛选”文字一类的小目标
    for seed in SEEDS:
        x, y = hp.click_point(box, random.Random(seed))
        assert box.left + 2 <= x <= box.left + box.width - 2
        assert box.top + 2 <= y <= box.top + box.height - 2


def test_click_point_respects_the_vertical_limit():
    cover = hp.Box(40.0, 60.0, 240.0, 240.0)
    band = (110.0, 600.0)  # 封面上沿在可点击带外
    for seed in SEEDS:
        x, y = hp.click_point(cover, random.Random(seed), within=band)
        assert band[0] <= y <= band[1] and cover.left <= x <= cover.left + cover.width
    # 比例范围与限制不相交时，退到限制内的封面部分。
    for seed in SEEDS:
        _, y = hp.click_point(cover, random.Random(seed), within=(250.0, 600.0))
        assert 250 <= y <= cover.top + cover.height - 2


def test_click_plan_timings_and_evidence():
    page = RecordingPage()
    for seed in SEEDS:
        pointer = hp.HumanPointer(page, rng=random.Random(seed), drift_sign=1)
        pointer.position = (300.0, 500.0)
        plan = asyncio.run(pointer.plan_click(hp.Box(700.0, 300.0, 150.0, 110.0)))
        assert hp.SETTLE_MS[0] <= plan.settle_ms <= hp.SETTLE_MS[1]  # 样本 20–552ms
        assert hp.HOLD_MS[0] <= plan.hold_ms <= hp.HOLD_MS[1]  # 样本 81–132ms
        evidence = plan.evidence
        assert set(evidence) == {"x_ratio", "y_ratio", "approach_ms", "moves", "settle_ms", "hold_ms"}
        assert 0.25 <= evidence["x_ratio"] <= 0.75 and 0.25 <= evidence["y_ratio"] <= 0.55
        assert evidence["approach_ms"] == round(sum(_gaps(list(plan.moves)))) and evidence["moves"] == len(plan.moves)


def test_first_action_enters_from_the_lower_middle_then_dispatches_one_event_per_move():
    page = RecordingPage()
    pointer = hp.HumanPointer(page, rng=random.Random(11), drift_sign=1)
    evidence = asyncio.run(pointer.click(hp.Box(1200.0, 80.0, 28.0, 20.0)))
    events = page.mouse.events
    kind, x, y = events[0]
    assert kind == "move" and evidence["entered"] is True
    assert 0.3 * 1450 <= x <= 0.7 * 1450 and 0.6 * 820 <= y <= 0.9 * 820  # 不从 (0,0) 画长线
    moves = [event for event in events if event[0] == "move"]
    assert len(moves) == evidence["moves"] + 1
    # 每个移动事件之前都有一次等待（逐个发出，不是一次连发），按下与抬起之间等待按住时长。
    for previous, current in zip(events[1:], events[2:]):
        if current[0] == "move":
            assert previous[0] == "wait"
    down = events.index(("down",))
    assert events[down - 1][0] == "wait" and events[down + 1][0] == "wait" and events[down + 2] == ("up",)
    assert hp.HOLD_MS[0] - 1 <= events[down + 1][1] <= hp.HOLD_MS[1] + 1
    final = moves[-1]
    assert isinstance(final[1], int) and isinstance(final[2], int) and pointer.position == final[1:]
    # 第二次点击从当前位置出发，不再“出现”。
    assert "entered" not in asyncio.run(pointer.click(hp.Box(600.0, 400.0, 150.0, 110.0)))


def test_click_can_stop_after_settling_and_press_does_not_settle_again():
    page = RecordingPage()
    pointer = hp.HumanPointer(page, rng=random.Random(5), drift_sign=1)
    pointer.position = (300.0, 500.0)
    plan = asyncio.run(pointer.plan_click(hp.Box(1200.0, 80.0, 28.0, 20.0)))

    async def hover_then_press() -> int:
        await pointer.approach(plan)
        await pointer.settle(plan)
        settled = len(page.mouse.events)
        await pointer.press(plan)
        return settled

    settled = asyncio.run(hover_then_press())
    hovered, pressed = page.mouse.events[:settled], page.mouse.events[settled:]
    assert hovered[-1] == ("wait", plan.settle_ms) and not any(event[0] in {"down", "up"} for event in hovered)
    assert pressed == [("down",), ("wait", plan.hold_ms), ("up",)]


# 触控板手势 -----------------------------------------------------------------

def _body(events: list[tuple[int, int]]) -> list[int]:
    """去掉末尾可能的 0 与反向 1。"""
    values = [dy for _, dy in events]
    while values and values[-1] <= 0:
        values.pop()
    return values


@pytest.mark.parametrize("distance", [8, 30, 60, 150, 250, 400, -150, -400])
def test_gesture_rises_then_decays_with_integer_deltas_summing_to_the_distance(distance):
    for seed in SEEDS:
        events = hp.plan_gesture(distance, random.Random(seed), drift_sign=-1)
        sign = 1 if distance > 0 else -1
        normalized = [(dx * sign, dy * sign) for dx, dy in events]
        assert all(isinstance(dx, int) and isinstance(dy, int) for dx, dy in events)
        assert sum(dy for _, dy in events) == distance
        assert len(events) >= hp.GESTURE_MIN_EVENTS
        assert max(abs(dy) for _, dy in events) <= 60
        body = _body(normalized)
        peak = body.index(max(body))
        assert peak < 6  # 3–6 个事件内到峰值
        assert all(later <= earlier for earlier, later in zip(body[peak:], body[peak + 1:]))
        assert body[-1] <= 3
        tail = [dy for _, dy in normalized[len(body):]]
        assert tail in ([], [0], [0, -1], [0, -1, -1])
        # deltaX 与滚动同向的小漂移，不超过 deltaY 的 0.4 倍。
        drift = sum(dx for dx, _ in normalized)
        assert drift <= 0 and abs(drift) <= 0.4 * abs(distance) + 1


def test_gesture_of_the_sample_size_matches_the_sample():
    counts, peaks = [], []
    for seed in SEEDS:
        rng = random.Random(seed)
        events = hp.plan_gesture(rng.randint(135, 160), rng, drift_sign=1)
        counts.append(len(events))
        peaks.append(max(dy for _, dy in events))
    # 样本：37–52 个事件、峰值 6–11。
    assert 30 <= min(counts) and max(counts) <= 56 and 36 <= statistics.median(counts) <= 50
    assert 6 <= min(peaks) and max(peaks) <= 15 and 8 <= statistics.median(peaks) <= 12


def test_peak_grows_with_distance_up_to_fifty():
    peaks = {distance: statistics.median(
        max(dy for _, dy in hp.plan_gesture(distance, random.Random(seed), drift_sign=1)) for seed in SEEDS
    ) for distance in (60, 150, 250, 400)}
    assert peaks[60] < peaks[150] < peaks[250] < peaks[400] <= 50


@pytest.mark.parametrize("distance", [1, 150, 400, 401, 720, 980, -1200])
def test_scroll_is_split_into_gestures_of_at_most_400px(distance):
    for seed in SEEDS:
        parts = hp.split_scroll(distance, random.Random(seed))
        assert sum(parts) == distance
        assert len(parts) == math.ceil(abs(distance) / 400)
        assert all(0 < abs(part) <= 400 and (part > 0) == (distance > 0) for part in parts)


def test_scroll_plan_pauses_between_gestures_and_records_each_gesture():
    page = RecordingPage()
    pointer = hp.HumanPointer(page, rng=random.Random(5), drift_sign=-1)
    plan = pointer.plan_scroll(900)
    assert len(plan.gestures) == 3 and plan.pauses_ms[0] == 0
    assert all(hp.GESTURE_PAUSE_MS[0] <= pause <= hp.GESTURE_PAUSE_MS[1] for pause in plan.pauses_ms[1:])
    asyncio.run(pointer.perform_scroll(plan))
    wheels = [event[1:] for event in page.mouse.events if event[0] == "wheel"]
    assert wheels == [event for gesture in plan.gestures for event in gesture]
    assert plan.evidence == {"gestures": [
        {"events": len(gesture), "delta_y": sum(dy for _, dy in gesture), "delta_x": sum(dx for dx, _ in gesture)}
        for gesture in plan.gestures
    ]}
    # 手势内事件间隔一帧，手势之间停 0.4–0.95s。
    frame_waits = [wait for wait in page.waits if wait < 100]
    assert frame_waits and all(wait <= hp.FRAME_MS * 1.04 for wait in frame_waits)
    assert len([wait for wait in page.waits if wait >= hp.GESTURE_PAUSE_MS[0]]) == 2


class GeometryLocator:
    def __init__(self, page: RecordingPage, top: float, *, moves: bool = True) -> None:
        self.page = page
        self.initial = self.top = top
        self.moves = moves
        self.reads = 0

    async def evaluate(self, script: str) -> dict:
        assert script == hp.ELEMENT_VIEW_SCRIPT
        self.reads += 1
        if self.moves:
            self.top = self.initial - sum(event[2] for event in self.page.mouse.events if event[0] == "wheel")
        return {"left": 600.0, "top": self.top, "width": 60.0, "height": 30.0,
                "clip": {"left": 0.0, "top": 0.0, "right": 1450.0, "bottom": 820.0}}


def test_scroll_into_view_uses_wheel_gestures_until_the_element_is_visible():
    page = RecordingPage()
    pointer = hp.HumanPointer(page, rng=random.Random(2), drift_sign=1)
    locator = GeometryLocator(page, 2300.0)
    gestures = asyncio.run(pointer.scroll_into_view(locator))
    assert gestures and 0 <= locator.top and locator.top + 30 <= 820
    assert sum(gesture["delta_y"] for gesture in gestures) == 2300 - locator.top
    assert asyncio.run(pointer.scroll_into_view(locator)) == []  # 已可见：不滚


def test_scroll_into_view_gives_up_when_the_element_does_not_move():
    page = RecordingPage()
    pointer = hp.HumanPointer(page, rng=random.Random(2), drift_sign=1)
    locator = GeometryLocator(page, 2300.0, moves=False)
    with pytest.raises(hp.ElementNotInView):
        asyncio.run(pointer.scroll_into_view(locator))
    # 滚两轮后第三次测量仍没有位移即停（连续两轮不动），不退回程序滚动。
    assert locator.reads == 3


def test_pointer_for_keeps_one_cursor_per_page():
    first, second = RecordingPage(), RecordingPage()
    assert hp.pointer_for(first) is hp.pointer_for(first)
    assert hp.pointer_for(first) is not hp.pointer_for(second)


# 静态检查 -------------------------------------------------------------------

POINTER_RECEIVERS = {"pointer"}
FORBIDDEN_LOCATOR_ACTIONS = {"click", "dblclick", "hover", "tap", "scroll_into_view_if_needed"}
# 共享运行时中其他档位仍在用的直接鼠标与程序滚动（小红书 xhs_guarded 走 human_pointer 分支，或走不到）。
HUMAN_FLOW_EXCEPTIONS = sorted([
    ("random_mouse_moves", "mouse.move"),
    ("wheel_scroll", "mouse.wheel"),
    ("human_scroll", "mouse.move"),
    ("maybe_visit_comments", "scroll_into_view_if_needed"),  # comment_visit_probability 为 0，dwell_on_detail 无小红书调用方
])


def _receiver_is_pointer(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id in POINTER_RECEIVERS
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "pointer_for"


def _pointer_violations(tree: ast.AST) -> list[tuple[str, str]]:
    """(所在函数, 调用) ：鼠标直接调用与定位器的点击、悬停、程序滚动。"""
    hits: list[tuple[str, str]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.function = "<module>"

        def visit_FunctionDef(self, node):
            outer, self.function = self.function, node.name
            self.generic_visit(node)
            self.function = outer

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            func = node.func
            if isinstance(func, ast.Attribute):
                value = func.value
                if isinstance(value, ast.Attribute) and value.attr == "mouse":
                    hits.append((self.function, f"mouse.{func.attr}"))
                elif func.attr in FORBIDDEN_LOCATOR_ACTIONS and not _receiver_is_pointer(value):
                    hits.append((self.function, func.attr))
            self.generic_visit(node)

    Visitor().visit(tree)
    return hits


def test_xhs_paths_have_no_direct_mouse_calls_locator_clicks_or_programmatic_scrolling():
    violations = {
        str(path.relative_to(PACKAGE)): hits
        for path in sorted((PACKAGE / "platforms/xhs").glob("*.py"))
        if (hits := _pointer_violations(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert violations == {}
    shared = _pointer_violations(ast.parse((PACKAGE / "runtime/human_flow.py").read_text(encoding="utf-8")))
    assert sorted(shared) == HUMAN_FLOW_EXCEPTIONS


def test_static_check_catches_the_old_patterns():
    source = """
async def run(page, locator):
    await page.mouse.move(1, 2, steps=8)
    await page.mouse.click(1, 2)
    await locator.click(timeout=5_000)
    await locator.scroll_into_view_if_needed(timeout=5_000)
    await pointer_for(page).click(box)
    await pointer.scroll(300)
"""
    assert _pointer_violations(ast.parse(source)) == [
        ("run", "mouse.move"), ("run", "mouse.click"), ("run", "click"), ("run", "scroll_into_view_if_needed"),
    ]
