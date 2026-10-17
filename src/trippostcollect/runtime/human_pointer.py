"""小红书正式轮的指针动作：分段移动轨迹、元素框内落点、按压时长与触控板滚轮手势（#84）。

轨迹（三次 Bezier、带加减速的进度曲线、低频侧向摆动）、Fitts 定律加对数正态的移动时长、框内落点和用
连续滚轮事件把元素滚进可视区的做法移植自 CloakBrowser 包装层 cloakbrowser/human/ 的 mouse.py 与
engine.py（https://github.com/CloakHQ/CloakBrowser ，提交 fbf005bc199920b592df17ac4d489324e29d56d8），
MIT 许可，版权声明与许可全文见 resources/licenses/CloakBrowser-LICENSE。只移植算法，不依赖 cloakbrowser 包。

参数按操作人 2026-10-17 在 Mac 上用触控板手工录制的样本校准（探针页 6 次卡片点击与 4 个滚动手势），各取值
旁注明对应的样本项。每次 `mouse.move` / `mouse.wheel` 只发一个事件，事件之间按 60Hz 节奏真实等待；等待走
`page.wait_for_timeout`，按绝对时刻对齐，CDP 往返不累积。
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import random
import time
from typing import Any, Callable, NamedTuple, Optional
import weakref

RANDOM = random.SystemRandom()

FRAME_MS = 1000 / 60  # 样本：移动与滚轮事件都约每 16–17ms 一个
FRAME_JITTER = 0.03
MIN_STEP_PX = 0.1  # 坐标带一位小数；两次事件间位移不到它时不发（真实指针不报告没有位移的移动）

# 主移动：样本 700–760px 用 140–150ms、150–300px 用 130–170ms，时长几乎不随距离增长。
MAIN_TIME_BASE_MS = 110.0
MAIN_TIME_PER_BIT_MS = 6.0
MAIN_TIME_SPREAD = 0.1
# 修正：样本 1–2 次、共 40–115px、约 200ms。主移动落点偏离目标的距离取全程的 6%–22%，上限 115px。
CORRECTION_TIME_BASE_MS = 150.0
CORRECTION_TIME_PER_BIT_MS = 12.0
CORRECTION_TIME_SPREAD = 0.15
CORRECTION_SHARE = (0.06, 0.22)
CORRECTION_MAX_PX = 115.0
CORRECTION_MIN_DISTANCE_PX = 60.0  # 更短的移动一笔到位
OVERSHOOT_CHANCE = 0.7  # 其余为没够到
SECOND_CORRECTION_CHANCE = 0.35
SECOND_CORRECTION_PX = (3.0, 12.0)
SEGMENT_PAUSE_MS = (90.0, 260.0)  # 样本：段间静止 120–200ms，期间没有移动事件
# 以下三项沿用参考实现。
CURVATURE = 0.12
PEAK = (0.22, 0.38)
WOBBLE_MAX_PX = 1.5

SETTLE_MS = (20.0, 550.0)  # 样本：修正结束到按下 20、34、67、284、318、552ms
HOLD_MS = (80.0, 135.0)  # 样本：按住 81、84、100、116、117、132ms
# 落点：样本 6 张 150×110 卡片横向 27–81%、纵向 26–48%；截断正态，横向以 50% 为中心、纵向以 40% 为中心。
CLICK_X = (0.25, 0.75, 0.5, 0.15)  # (下限, 上限, 中心, 标准差)，均为元素宽度的比例
CLICK_Y = (0.25, 0.55, 0.40, 0.09)
CLICK_EDGE_PX = 2.0  # 小目标（标签文字、作者名）落点距元素边至少 2px
# 本页第一个指针动作前，光标先出现在视口中下部（像从窗口外移进来），停一下再走轨迹。
ENTRY_X = (0.3, 0.7)
ENTRY_Y = (0.6, 0.9)
ENTRY_PAUSE_MS = (150.0, 400.0)

# 触控板手势：样本每个手势 37–52 个事件、0.6–0.9s、共 135–160px、3–6 个事件内升到峰值 6–11 后长时间衰减到
# 1–3，可能以 deltaY=0 结束，偶尔末尾 1–2 个 -1；全部事件 |deltaY| 最大 54；手势间停 0.4–0.95s。
GESTURE_MAX_PX = 400
GESTURE_PEAK_MAX = 50.0
GESTURE_MIN_EVENTS = 8
GESTURE_RISE_EVENTS = (3, 6)
GESTURE_TAIL = (0.7, 1.4)  # 衰减段末尾的浮点值，量化后形成样本里的一串 1
GESTURE_PAUSE_MS = (400.0, 950.0)
GESTURE_END_ZERO_CHANCE = 0.5
GESTURE_REVERSE_CHANCE = 0.4
# 样本 deltaX 同向小漂移，累计约为 deltaY 的 -0.35 到 -0.43 倍：每个手势取 0–0.4 倍，方向整轮固定。
GESTURE_DRIFT_MAX = 0.4
ROUND_DRIFT_SIGN = RANDOM.choice((-1, 1))

SCROLL_INTO_VIEW_ROUNDS = 8
SCROLL_SETTLE_MS = (300.0, 600.0)  # 参考实现 scroll_settle_delay
SCROLL_AREA = (0.3, 0.7)  # 光标不在滚动区域内时移到区域内这个比例范围的随机点

VIEWPORT_SCRIPT = "() => ({ width: window.innerWidth || 0, height: window.innerHeight || 0 })"
# 元素框与它最近的可纵向滚动祖先的可见区（与视口相交；没有时为视口）；只读几何。
ELEMENT_VIEW_SCRIPT = """(node) => {
    const rect = node.getBoundingClientRect();
    const viewport = { left: 0, top: 0, right: window.innerWidth || 0, bottom: window.innerHeight || 0 };
    let clip = viewport;
    for (let parent = node.parentElement; parent && parent !== document.body
        && parent !== document.documentElement; parent = parent.parentElement) {
        const style = window.getComputedStyle(parent);
        if (['auto', 'scroll', 'overlay'].includes(style.overflowY) && parent.scrollHeight > parent.clientHeight + 1) {
            const box = parent.getBoundingClientRect();
            const left = Math.max(box.left + parent.clientLeft, viewport.left);
            const top = Math.max(box.top + parent.clientTop, viewport.top);
            const right = Math.min(box.left + parent.clientLeft + parent.clientWidth, viewport.right);
            const bottom = Math.min(box.top + parent.clientTop + parent.clientHeight, viewport.bottom);
            if (right > left && bottom > top) clip = { left, top, right, bottom };
            break;
        }
    }
    return { left: rect.left, top: rect.top, width: rect.width, height: rect.height, clip };
}"""

Move = tuple[float, float, float]  # (x, y, 距上一事件的毫秒)


class Box(NamedTuple):
    left: float
    top: float
    width: float
    height: float

    @classmethod
    def from_point(cls, point: dict[str, Any]) -> "Box":
        """页面脚本给出的元素框 {left, top, width, height}（另带的 x、y 是中心点，这里不用）。"""
        return cls(float(point["left"]), float(point["top"]), float(point["width"]), float(point["height"]))

    @classmethod
    def from_bounding_box(cls, value: Optional[dict[str, float]]) -> "Box":
        """Playwright `bounding_box()` 的结果（x、y 为左上角）；元素不可见时为 None。"""
        if not value or value["width"] <= 0 or value["height"] <= 0:
            raise ElementNotInView("element has no visible box")
        return cls(float(value["x"]), float(value["y"]), float(value["width"]), float(value["height"]))


class ElementNotInView(RuntimeError):
    """元素没有可见框，或滚轮手势在上限内没能把它滚进可视区；调用方按原有失败路径处理。"""


# ---------------------------------------------------------------------------
# 随机取值
# ---------------------------------------------------------------------------

def _bounded_lognormal(rng: random.Random, low: float, high: float) -> float:
    """对数正态，以 low、high 为约 5%、95% 分位，落在区间外时重抽（人的时长右偏，不是均匀分布）。"""
    mu = (math.log(low) + math.log(high)) / 2
    sigma = (math.log(high) - math.log(low)) / (2 * 1.6449)
    for _ in range(32):
        value = math.exp(rng.gauss(mu, sigma))
        if low <= value <= high:
            return value
    return rng.uniform(low, high)


def _truncated_normal(rng: random.Random, low: float, high: float, center: float, sd: float) -> float:
    if high <= low:
        return (low + high) / 2
    for _ in range(32):
        value = rng.gauss(center, sd)
        if low <= value <= high:
            return value
    return rng.uniform(low, high)


def _lognormal(rng: random.Random, median: float, sigma: float) -> float:
    return median * math.exp(rng.gauss(0.0, sigma))


# ---------------------------------------------------------------------------
# 轨迹（移植自 mouse.py：_bezier、_random_control_points、_progress_curve、plan_stroke、plan_move）
# ---------------------------------------------------------------------------

def _bezier(points: tuple[tuple[float, float], ...], t: float) -> tuple[float, float]:
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = points
    u = 1 - t
    a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return a * x0 + b * x1 + c * x2 + d * x3, a * y0 + b * y1 + c * y2 + d * y3


def _control_points(
    start: tuple[float, float], end: tuple[float, float], rng: random.Random,
) -> tuple[tuple[float, float], tuple[float, float]]:
    dx, dy = end[0] - start[0], end[1] - start[1]
    dist = math.hypot(dx, dy) or 1.0
    px, py = -dy / dist, dx / dist
    # 两个控制点多数时候弯向同一侧：手划的是弧，很少是 S 形。
    bias1 = rng.uniform(-CURVATURE, CURVATURE) * dist
    bias2 = bias1 * rng.uniform(0.2, 1.2) if rng.random() < 0.8 else rng.uniform(-CURVATURE, CURVATURE) * dist
    return (
        (start[0] + dx * 0.25 + px * bias1, start[1] + dy * 0.25 + py * bias1),
        (start[0] + dx * 0.75 + px * bias2, start[1] + dy * 0.75 + py * bias2),
    )


def _progress_curve(peak: float) -> Callable[[float], float]:
    """0→1 的平滑距离曲线，速度在 peak 处最大（时间扭曲的最小加加速度曲线：前段快、尾段长而慢）。"""
    k = math.log(0.5) / math.log(peak)

    def curve(t: float) -> float:
        u = t ** k
        return u * u * u * (10 - 15 * u + 6 * u * u)

    return curve


def _clamp_point(point: tuple[float, float], bounds: Optional[Box]) -> tuple[float, float]:
    if bounds is None:
        return point
    return (
        min(max(point[0], bounds.left + 1), bounds.left + bounds.width - 1),
        min(max(point[1], bounds.top + 1), bounds.top + bounds.height - 1),
    )


def stroke_ms(dist: float, rng: random.Random, *, correction: bool = False) -> float:
    """一笔移动的毫秒数：Fitts 定律给中位数，再按对数正态展开；至少两帧。"""
    if correction:
        median = CORRECTION_TIME_BASE_MS + CORRECTION_TIME_PER_BIT_MS * math.log2(1 + dist / 8)
        spread = CORRECTION_TIME_SPREAD
    else:
        median = MAIN_TIME_BASE_MS + MAIN_TIME_PER_BIT_MS * math.log2(1 + dist / 8)
        spread = MAIN_TIME_SPREAD
    return max(FRAME_MS * 2, _lognormal(rng, median, spread))


def plan_stroke(
    start: tuple[float, float], end: tuple[float, float], duration_ms: float, rng: random.Random,
    *, bounds: Optional[Box] = None,
) -> list[Move]:
    """一笔移动的事件；末事件恰在 end，事件间隔为一帧（带 ±3% 抖动），太小的位移并入下一事件。"""
    sx, sy = start
    ex, ey = end
    dist = math.hypot(ex - sx, ey - sy)
    if dist < MIN_STEP_PX:
        return []
    gaps = [FRAME_MS * rng.uniform(1 - FRAME_JITTER, 1 + FRAME_JITTER)
            for _ in range(max(2, round(duration_ms / FRAME_MS)))]
    span = sum(gaps)
    points = (start, *_control_points(start, end, rng), end)
    curve = _progress_curve(rng.uniform(*PEAK))
    # 低频侧向摆动（手不是尺子），两端为零。
    ux, uy = (ex - sx) / dist, (ey - sy) / dist
    amp = min(WOBBLE_MAX_PX, dist * 0.02) * rng.uniform(-1, 1)
    waves = rng.uniform(0.5, 1.5)
    phase = rng.uniform(0, math.pi)
    moves: list[Move] = []
    last = (sx, sy)
    elapsed = pending = 0.0
    for index, gap in enumerate(gaps, 1):
        elapsed += gap
        pending += gap
        if index == len(gaps):
            if moves and abs(moves[-1][0] - ex) < MIN_STEP_PX and abs(moves[-1][1] - ey) < MIN_STEP_PX:
                pending += moves.pop()[2]  # 上一帧已经到了：把它换成恰好落在终点的事件
            moves.append((ex, ey, pending))
            break
        t = elapsed / span
        x, y = _bezier(points, curve(t))
        wobble = amp * math.sin(math.pi * t) * math.sin(phase + 2 * math.pi * waves * t)
        x, y = _clamp_point((round(x - uy * wobble, 1), round(y + ux * wobble, 1)), bounds)
        if abs(x - last[0]) < MIN_STEP_PX and abs(y - last[1]) < MIN_STEP_PX:
            continue
        moves.append((x, y, pending))
        pending = 0.0
        last = (x, y)
    return moves


def plan_approach(
    start: tuple[float, float], end: tuple[float, float], rng: random.Random, *, bounds: Optional[Box] = None,
) -> list[Move]:
    """接近目标：一段快速主移动落在目标附近（多数越过、少数没够到），停一下，再 1–2 次小幅修正到目标；
    段间停顿并入下一段首个事件的等待，停顿期间没有事件。60px 以内一笔到位。"""
    dist = math.hypot(end[0] - start[0], end[1] - start[1])
    if dist < MIN_STEP_PX:
        return []
    if dist < CORRECTION_MIN_DISTANCE_PX:
        return plan_stroke(start, end, stroke_ms(dist, rng), rng, bounds=bounds)
    miss = min(CORRECTION_MAX_PX, dist * rng.uniform(*CORRECTION_SHARE))
    if rng.random() >= OVERSHOOT_CHANCE:
        miss = -miss
    angle = math.atan2(end[1] - start[1], end[0] - start[0]) + rng.uniform(-0.25, 0.25)
    landing = _clamp_point(
        (round(end[0] + math.cos(angle) * miss, 1), round(end[1] + math.sin(angle) * miss, 1)), bounds,
    )
    moves = plan_stroke(start, landing, stroke_ms(math.hypot(landing[0] - start[0], landing[1] - start[1]), rng),
                        rng, bounds=bounds)
    waypoints = [end]
    if rng.random() < SECOND_CORRECTION_CHANCE:
        offset, turn = rng.uniform(*SECOND_CORRECTION_PX), rng.uniform(0, 2 * math.pi)
        waypoints.insert(0, _clamp_point(
            (round(end[0] + math.cos(turn) * offset, 1), round(end[1] + math.sin(turn) * offset, 1)), bounds,
        ))
    current = landing
    for point in waypoints:
        stroke = plan_stroke(
            current, point, stroke_ms(math.hypot(point[0] - current[0], point[1] - current[1]), rng, correction=True),
            rng, bounds=bounds,
        )
        if stroke:
            x, y, wait = stroke[0]
            stroke[0] = (x, y, wait + rng.uniform(*SEGMENT_PAUSE_MS))
        moves.extend(stroke)
        current = point
    return moves


# ---------------------------------------------------------------------------
# 落点（移植自 mouse.py:click_target 与 engine.py:_point，范围与分布按样本改写）
# ---------------------------------------------------------------------------

def _landing_span(
    start: float, size: float, shape: tuple[float, float, float, float], within: Optional[tuple[float, float]],
) -> tuple[float, float]:
    low, high = start + size * shape[0], start + size * shape[1]
    edge = min(CLICK_EDGE_PX, size / 2)
    inner = (start + edge, start + size - edge)
    for limit in (within, None):
        bounds = (max(low, inner[0]), min(high, inner[1]))
        if limit is not None:
            bounds = (max(bounds[0], limit[0]), min(bounds[1], limit[1]))
            if bounds[0] > bounds[1]:  # 比例范围与限制不相交时，只在限制内、离边 2px 取点
                bounds = (max(inner[0], limit[0]), min(inner[1], limit[1]))
        if bounds[0] <= bounds[1]:
            return bounds
    center = start + size / 2
    return center, center


def _integer_in(rng: random.Random, low: float, high: float, center: float, sd: float) -> int:
    value = round(_truncated_normal(rng, low, high, min(max(center, low), high), sd))
    first, last = math.ceil(low), math.floor(high)
    if first <= last:
        return min(max(value, first), last)
    return round((low + high) / 2)


def click_point(
    box: Box, rng: random.Random, *, within: Optional[tuple[float, float]] = None,
) -> tuple[int, int]:
    """元素框内的整数落点（点击事件的 clientX/clientY 是整数）。`within` 为纵向限制 (上沿, 下沿)，例如卡片的
    可点击带；与比例范围不相交时退到限制内的任意位置。"""
    x_low, x_high = _landing_span(box.left, box.width, CLICK_X, None)
    y_low, y_high = _landing_span(box.top, box.height, CLICK_Y, within)
    x = _integer_in(rng, x_low, x_high, box.left + box.width * CLICK_X[2], box.width * CLICK_X[3])
    y = _integer_in(rng, y_low, y_high, box.top + box.height * CLICK_Y[2], box.height * CLICK_Y[3])
    return x, y


# ---------------------------------------------------------------------------
# 触控板滚轮手势（替代参考实现 engine.py:_wheel_burst 的鼠标滚轮刻度，形状按触控板样本改写）
# ---------------------------------------------------------------------------

def gesture_peak(distance: float, rng: random.Random) -> float:
    """峰值随距离增长：150px 约 10、400px 约 39（样本 135–160px 的手势峰值 6–11），上限 50；
    小距离按 distance/8 收紧，保证事件数。"""
    median = distance * (0.045 + 0.00013 * distance)
    floor = max(2.0, min(6.0, distance / 8))
    return min(GESTURE_PEAK_MAX, max(floor, median * rng.uniform(0.8, 1.25)))


def _gesture_profile(total: int, peak: float, rise: int, power: float, tail: float) -> list[int]:
    rising = [peak * ((index + 1) / rise) ** power for index in range(rise)]
    if sum(rising) > total * 0.5:
        rising = [value * total * 0.5 / sum(rising) for value in rising]
        peak = rising[-1]
    rest = total - sum(rising)
    if peak > tail:
        # 几何衰减 peak·q^k 到 tail 为止，总和约等于 rest：(peak - tail)·q / (1 - q) = rest。
        ratio = rest / (rest + peak - tail)
        count = max(1, math.ceil(math.log(tail / peak) / math.log(ratio)))
        falling = [peak * ratio ** step for step in range(1, count + 1)]
    else:
        falling = [tail] * max(1, round(rest / tail))
    values = rising + falling
    scale = total / sum(values)
    steps: list[int] = []
    carry = 0.0
    for index, value in enumerate(values):
        wanted = value * scale + carry
        step = max(1, round(wanted))
        if index >= rise:
            step = min(step, steps[-1])  # 峰值之后不再上升
        carry = wanted - step
        steps.append(step)
    excess = sum(steps) - total
    while excess > 0:  # 最小取 1 让尾部多出的量从末尾扣掉
        if steps[-1] == 1:
            steps.pop()
        else:
            steps[-1] -= 1
        excess -= 1
    steps.extend([1] * -excess)
    return steps


def plan_gesture(distance: int, rng: random.Random, *, drift_sign: int) -> list[tuple[int, int]]:
    """一个触控板手势的逐事件整数位移 (deltaX, deltaY)：3–6 个事件内升到峰值后衰减到 1，deltaY 之和恰为
    distance；可能以一个 0 结束，偶尔再跟 1–2 个反向的 1。deltaX 为 deltaY 的同向小比例漂移。"""
    total = abs(int(distance))
    if total == 0:
        return []
    sign = 1 if distance > 0 else -1
    peak = gesture_peak(total, rng)
    rise = rng.randint(*GESTURE_RISE_EVENTS)
    power = rng.uniform(1.0, 1.6)
    tail = rng.uniform(*GESTURE_TAIL)
    while True:
        steps = _gesture_profile(total, peak, rise, power, tail)
        if len(steps) >= GESTURE_MIN_EVENTS or peak <= 1:
            break
        peak = max(1.0, peak - 1)
    if rng.random() < GESTURE_END_ZERO_CHANCE:
        steps.append(0)
        if rng.random() < GESTURE_REVERSE_CHANCE:
            back = rng.randint(1, 2)
            steps[steps.index(max(steps))] += back
            steps.extend([-1] * back)
    ratio = rng.uniform(0, GESTURE_DRIFT_MAX) * drift_sign
    events: list[tuple[int, int]] = []
    carry = 0.0
    for step in steps:
        dx = 0
        if step > 0:
            wanted = step * ratio + carry
            dx = round(wanted)
            carry = wanted - dx
        events.append((dx * sign, step * sign))
    return events


def split_scroll(distance: int, rng: random.Random) -> list[int]:
    """把一次滚动拆成多个手势：个数按每个最多 400px 取，各手势距离在均分值上下浮动，和恰为 distance。"""
    total = abs(int(distance))
    if total == 0:
        return []
    sign = 1 if distance > 0 else -1
    count = math.ceil(total / GESTURE_MAX_PX)
    base = total / count
    spread = min(base * 0.15, GESTURE_MAX_PX - base)
    weights = [base + rng.uniform(-spread, spread) for _ in range(count)]
    scaled = [weight * total / sum(weights) for weight in weights]
    parts = [math.floor(value) for value in scaled]
    for index in sorted(range(count), key=lambda item: scaled[item] - parts[item], reverse=True)[:total - sum(parts)]:
        parts[index] += 1
    if max(parts) > GESTURE_MAX_PX:
        parts = [total // count + (1 if index < total % count else 0) for index in range(count)]
    return [part * sign for part in parts]


# ---------------------------------------------------------------------------
# 计划与执行
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClickPlan:
    entry: Optional[tuple[float, float]]
    entry_pause_ms: float
    moves: tuple[Move, ...]
    point: tuple[int, int]
    box: Box
    settle_ms: float
    hold_ms: float

    @property
    def approach_ms(self) -> int:
        return round(sum(move[2] for move in self.moves))

    @property
    def evidence(self) -> dict[str, Any]:
        """账本与行为证据里的点击记录：落点在元素框中的比例、接近时长、移动事件数、按下前静止与按住时长。"""
        evidence: dict[str, Any] = {
            "x_ratio": round((self.point[0] - self.box.left) / self.box.width, 3) if self.box.width else 0.5,
            "y_ratio": round((self.point[1] - self.box.top) / self.box.height, 3) if self.box.height else 0.5,
            "approach_ms": self.approach_ms,
            "moves": len(self.moves),
            "settle_ms": round(self.settle_ms),
            "hold_ms": round(self.hold_ms),
        }
        if self.entry is not None:
            evidence["entered"] = True
        return evidence


@dataclass(frozen=True)
class ScrollPlan:
    gestures: tuple[tuple[tuple[int, int], ...], ...]
    pauses_ms: tuple[float, ...]  # 第 i 个手势之前的停顿（第一个为 0）

    @property
    def evidence(self) -> dict[str, Any]:
        """每个手势的事件数与总位移。"""
        return {"gestures": [
            {"events": len(events), "delta_y": sum(dy for _, dy in events), "delta_x": sum(dx for dx, _ in events)}
            for events in self.gestures
        ]}


class HumanPointer:
    """一个页面的虚拟光标：记住位置，所有移动、点击与滚轮都从当前位置出发。Playwright 的虚拟鼠标从 (0,0)
    开始；位置未知时，第一个动作先让光标出现在视口中下部。"""

    def __init__(self, page: Any, *, rng: Optional[random.Random] = None, drift_sign: Optional[int] = None) -> None:
        self.page = page
        self.rng = rng or RANDOM
        self.drift_sign = drift_sign or ROUND_DRIFT_SIGN
        self.position: Optional[tuple[float, float]] = None

    async def viewport(self) -> Box:
        size = self.page.viewport_size or await self.page.evaluate(VIEWPORT_SCRIPT)
        return Box(0.0, 0.0, float(size["width"]), float(size["height"]))

    def inside(self, area: Box) -> bool:
        if self.position is None:
            return False
        x, y = self.position
        return area.left <= x <= area.left + area.width and area.top <= y <= area.top + area.height

    async def _start(self) -> tuple[Optional[tuple[float, float]], float, tuple[float, float], Box]:
        view = await self.viewport()
        if self.position is not None:
            return None, 0.0, self.position, view
        entry = (
            round(view.width * self.rng.uniform(*ENTRY_X), 1),
            round(view.height * self.rng.uniform(*ENTRY_Y), 1),
        )
        return entry, self.rng.uniform(*ENTRY_PAUSE_MS), entry, view

    async def plan_click(self, box: Box, *, within: Optional[tuple[float, float]] = None) -> ClickPlan:
        entry, entry_pause, start, view = await self._start()
        point = click_point(box, self.rng, within=within)
        return ClickPlan(
            entry=entry,
            entry_pause_ms=entry_pause,
            moves=tuple(plan_approach(start, point, self.rng, bounds=view)),
            point=point,
            box=box,
            settle_ms=_bounded_lognormal(self.rng, *SETTLE_MS),
            hold_ms=_bounded_lognormal(self.rng, *HOLD_MS),
        )

    async def approach(self, plan: ClickPlan) -> None:
        await self._travel(plan.entry, plan.entry_pause_ms, plan.moves)

    async def settle(self, plan: ClickPlan) -> None:
        """按下前的静止；调用方可在静止后读页面再决定是否 `press`。"""
        await self._wait(plan.settle_ms)

    async def press(self, plan: ClickPlan) -> None:
        """原地按下、按住、松开（不含按下前的静止）。"""
        await self.page.mouse.down()
        await self._wait(plan.hold_ms)
        await self.page.mouse.up()

    async def perform_click(self, plan: ClickPlan) -> None:
        await self.approach(plan)
        await self.settle(plan)
        await self.press(plan)

    async def click(self, box: Box, *, within: Optional[tuple[float, float]] = None) -> dict[str, Any]:
        plan = await self.plan_click(box, within=within)
        await self.perform_click(plan)
        return plan.evidence

    async def move_to(self, x: float, y: float) -> dict[str, Any]:
        entry, entry_pause, start, view = await self._start()
        moves = plan_approach(start, _clamp_point((x, y), view), self.rng, bounds=view)
        await self._travel(entry, entry_pause, moves)
        return {"approach_ms": round(sum(move[2] for move in moves)), "moves": len(moves)}

    async def move_into(self, area: Box) -> None:
        """光标不在区域内时移到区域中部的随机点；已在区域内则不动。"""
        if not self.inside(area):
            await self.move_to(
                area.left + area.width * self.rng.uniform(*SCROLL_AREA),
                area.top + area.height * self.rng.uniform(*SCROLL_AREA),
            )

    def plan_scroll(self, distance: int) -> ScrollPlan:
        parts = split_scroll(distance, self.rng)
        gestures = tuple(tuple(plan_gesture(part, self.rng, drift_sign=self.drift_sign)) for part in parts)
        pauses = tuple(0.0 if index == 0 else self.rng.uniform(*GESTURE_PAUSE_MS) for index in range(len(gestures)))
        return ScrollPlan(gestures=gestures, pauses_ms=pauses)

    async def perform_scroll(self, plan: ScrollPlan) -> None:
        for events, pause in zip(plan.gestures, plan.pauses_ms):
            await self._wait(pause)
            deadline = _now_ms()
            for index, (dx, dy) in enumerate(events):
                if index:
                    deadline += FRAME_MS * self.rng.uniform(1 - FRAME_JITTER, 1 + FRAME_JITTER)
                    await self._wait_until(deadline)
                await self.page.mouse.wheel(dx, dy)

    async def scroll(self, distance: int) -> dict[str, Any]:
        plan = self.plan_scroll(distance)
        await self.perform_scroll(plan)
        return plan.evidence

    async def scroll_into_view(self, locator: Any, *, rounds: int = SCROLL_INTO_VIEW_ROUNDS) -> list[dict[str, Any]]:
        """用滚轮手势把元素滚进它所在滚动区域的可视范围（移植自 engine.py:scroll_into_view 与
        _needed_scroll 的纵向部分）：光标先移进该区域，每轮按需要的位移滚动后重新测量；连续两轮位置不变或轮数
        用尽仍不可见时抛出 ElementNotInView，不退回程序滚动。返回各手势的记录。"""
        gestures: list[dict[str, Any]] = []
        stalled = 0
        previous: Optional[float] = None
        for index in range(rounds + 1):
            geometry = await locator.evaluate(ELEMENT_VIEW_SCRIPT)
            if float(geometry["width"]) <= 0 or float(geometry["height"]) <= 0:
                raise ElementNotInView("element has no visible box")
            delta = self._needed_scroll(geometry)
            if delta == 0:
                return gestures
            if previous is not None:
                stalled = stalled + 1 if abs(float(geometry["top"]) - previous) < 1 else 0
            if index == rounds or stalled >= 2:
                break
            clip = geometry["clip"]
            await self.move_into(Box(clip["left"], clip["top"], clip["right"] - clip["left"], clip["bottom"] - clip["top"]))
            plan = self.plan_scroll(delta)
            await self.perform_scroll(plan)
            gestures.extend(plan.evidence["gestures"])
            await self._wait(self.rng.uniform(*SCROLL_SETTLE_MS))
            previous = float(geometry["top"])
        raise ElementNotInView("element did not scroll into view within the wheel bound")

    def _needed_scroll(self, geometry: dict[str, Any]) -> int:
        top, height = float(geometry["top"]), float(geometry["height"])
        clip_top, clip_bottom = float(geometry["clip"]["top"]), float(geometry["clip"]["bottom"])
        clip_height = clip_bottom - clip_top
        if height <= clip_height:
            if top >= clip_top - 1 and top + height <= clip_bottom + 1:
                return 0
            return round(top + height / 2 - (clip_top + clip_height * self.rng.uniform(0.4, 0.6)))
        if min(top + height, clip_bottom) - max(top, clip_top) >= clip_height * 0.5:
            return 0
        return round(top - clip_top - clip_height * 0.1)

    async def _travel(self, entry: Optional[tuple[float, float]], entry_pause: float, moves: Any) -> None:
        if entry is not None:
            await self.page.mouse.move(*entry)
            self.position = entry
            await self._wait(entry_pause)
        deadline = _now_ms()
        for x, y, wait in moves:
            deadline += wait
            await self._wait_until(deadline)
            await self.page.mouse.move(x, y)
            self.position = (x, y)

    async def _wait(self, milliseconds: float) -> None:
        if milliseconds > 0:
            await self.page.wait_for_timeout(milliseconds)

    async def _wait_until(self, deadline_ms: float) -> None:
        await self._wait(deadline_ms - _now_ms())


def _now_ms() -> float:
    return time.monotonic() * 1000


_POINTERS: "weakref.WeakKeyDictionary[Any, HumanPointer]" = weakref.WeakKeyDictionary()


def pointer_for(page: Any) -> HumanPointer:
    """该页面的虚拟光标（按页面保存位置；新标签页各有一个，从未知位置开始）。"""
    pointer = _POINTERS.get(page)
    if pointer is None:
        pointer = _POINTERS[page] = HumanPointer(page)
    return pointer
