#!/usr/bin/env python3
"""Shared conservative browser behavior helpers for deterministic crawlers."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

from trippostcollect.runtime.human_pointer import Box, pointer_for


RANDOM = random.SystemRandom()


@dataclass(frozen=True)
class BehaviorProfile:
    name: str
    list_dwell_seconds: tuple[float, float]
    detail_dwell_seconds: tuple[float, float]
    inter_detail_cooldown_seconds: tuple[float, float]
    comment_visit_probability: float
    max_details_per_batch: int
    scroll_passes: tuple[int, int]
    scroll_delta_px: tuple[int, int]
    cdp_touch_probability: float
    mouse_move_count: tuple[int, int] = (2, 6)


DEFAULT_PROFILES: dict[str, BehaviorProfile] = {
    "conservative": BehaviorProfile(
        name="conservative",
        list_dwell_seconds=(18, 75),
        detail_dwell_seconds=(45, 180),
        inter_detail_cooldown_seconds=(120, 600),
        comment_visit_probability=0.18,
        max_details_per_batch=2,
        scroll_passes=(3, 8),
        scroll_delta_px=(420, 1500),
        cdp_touch_probability=0.35,
    ),
    "social_high_risk": BehaviorProfile(
        name="social_high_risk",
        list_dwell_seconds=(30, 150),
        detail_dwell_seconds=(60, 240),
        inter_detail_cooldown_seconds=(240, 1200),
        comment_visit_probability=0.28,
        max_details_per_batch=1,
        scroll_passes=(4, 10),
        scroll_delta_px=(320, 1150),
        cdp_touch_probability=0.55,
    ),
    "xhs_guarded": BehaviorProfile(
        name="xhs_guarded",
        list_dwell_seconds=(8, 20),
        detail_dwell_seconds=(90, 240),
        inter_detail_cooldown_seconds=(600, 1800),
        comment_visit_probability=0.0,
        max_details_per_batch=1,
        scroll_passes=(2, 5),
        scroll_delta_px=(360, 980),
        cdp_touch_probability=0.0,
        mouse_move_count=(1, 3),
    ),
    "quick_probe": BehaviorProfile(
        name="quick_probe",
        list_dwell_seconds=(3, 12),
        detail_dwell_seconds=(6, 24),
        inter_detail_cooldown_seconds=(5, 20),
        comment_visit_probability=0.03,
        max_details_per_batch=1,
        scroll_passes=(1, 3),
        scroll_delta_px=(300, 900),
        cdp_touch_probability=0.10,
    ),
}

SITE_PROFILE_MAP = {
    "bilibili": "social_high_risk",
    "douyin": "social_high_risk",
    "weibo": "social_high_risk",
    "zhihu": "social_high_risk",
    "xhs": "xhs_guarded",
}

COMMENT_SELECTORS = (
    "[id*=comment i]",
    "[class*=comment i]",
    "[aria-label*=评论]",
    "text=评论",
)


def load_behavior_profile(
    name_or_site: str | None = None,
    overrides: dict[str, Any] | None = None,
    *,
    strict: bool = False,
) -> BehaviorProfile:
    key = (name_or_site or "conservative").strip()
    profile_name = SITE_PROFILE_MAP.get(key, key)
    profile = DEFAULT_PROFILES.get(profile_name)
    if profile is None:
        if strict:
            choices = ", ".join(sorted(set(DEFAULT_PROFILES) | set(SITE_PROFILE_MAP)))
            raise ValueError(f"Unknown behavior profile or site {key!r}. Choices: {choices}")
        profile = DEFAULT_PROFILES["conservative"]
    if not overrides:
        return profile

    values = profile.__dict__.copy()
    for key, value in overrides.items():
        if key in values and value is not None:
            if key.endswith("_seconds") or key in {"scroll_passes", "scroll_delta_px", "mouse_move_count"}:
                values[key] = tuple(value)
            else:
                values[key] = value
    return BehaviorProfile(**values)


@dataclass(frozen=True)
class ListArea:
    """调用方给出的结果列表（目前只有小红书 xhs_guarded 在搜索结果页传入，#88）：随机移动目标点与滚轮落点限定在
    `box` 内；滚动是否有效只看列表自身的滚动位置 `scroll_y`（`scroll_container` 为 element 时是滚动容器，document
    时是 window）。不传时各函数按原来的整页口径。"""

    box: Box
    scroll_y: int
    scroll_container: str

    def point(self) -> tuple[float, float]:
        box = self.box
        return (
            round(RANDOM.uniform(box.left, box.left + box.width), 1),
            round(RANDOM.uniform(box.top, box.top + box.height), 1),
        )

    @property
    def evidence(self) -> dict[str, Any]:
        """写进 mouse_moves / scroll_target 事件：所用区域的矩形与列表滚动容器的类型。"""
        return {
            "area": {key: round(value, 1) for key, value in self.box._asdict().items()},
            "scroll_container": self.scroll_container,
        }

    @property
    def position(self) -> dict[str, Any]:
        return {"scroll_y": self.scroll_y, "scroll_container": self.scroll_container}


ListAreaProbe = Callable[[Page], Awaitable[ListArea]]


async def install_runtime_hints(context: BrowserContext) -> None:
    await context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
        window.chrome = window.chrome || { runtime: {} };
        """
    )


def _rand_range(bounds: tuple[float, float]) -> float:
    low, high = sorted((float(bounds[0]), float(bounds[1])))
    return RANDOM.uniform(low, high)


def _log(log: list[dict[str, Any]] | None, event: dict[str, Any]) -> None:
    if log is not None:
        log.append(event)


async def page_size(page: Page) -> tuple[int, int]:
    size = await page.evaluate("() => ({ width: window.innerWidth || 1280, height: window.innerHeight || 800 })")
    return int(size.get("width") or 1280), int(size.get("height") or 800)


async def scroll_position(page: Page) -> dict[str, int]:
    try:
        async with asyncio.timeout(5):
            value = await page.evaluate(
                """() => {
                    const nodes = [document.scrollingElement, ...document.querySelectorAll('*')].filter(Boolean);
                    const seen = new Set();
                    const scrollable = nodes.filter((node) => {
                        if (seen.has(node)) return false;
                        seen.add(node);
                        return Number(node.scrollHeight || 0) - Number(node.clientHeight || 0) > 40;
                    }).slice(0, 40);
                    const positions = scrollable.map((node) => Math.round(Number(node.scrollTop || 0)));
                    return {
                        window_y: Math.round(Number(window.scrollY || 0)),
                        scrollable_count: scrollable.length,
                        scroll_top_sum: positions.reduce((total, item) => total + item, 0),
                        max_scroll_top: positions.length ? Math.max(...positions) : 0,
                    };
                }"""
            )
    except Exception:
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        key: int(value.get(key) or 0)
        for key in ("window_y", "scrollable_count", "scroll_top_sum", "max_scroll_top")
    }


def scroll_effect_observed(before: dict[str, int], after: dict[str, int]) -> bool:
    if not before or not after:
        return False
    return any(
        before.get(key) != after.get(key)
        for key in ("window_y", "scroll_top_sum", "max_scroll_top")
    )


def list_scroll_moved(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """列表口径（见 ListArea）：同一种滚动容器且滚动位置变了；对话栏等其他节点的滚动不算。"""
    return before["scroll_container"] == after["scroll_container"] and before["scroll_y"] != after["scroll_y"]


async def human_pause(
    page: Page,
    seconds_range: tuple[float, float],
    *,
    reason: str,
    log: list[dict[str, Any]] | None = None,
) -> float:
    seconds = _rand_range(seconds_range)
    _log(log, {"event": "pause", "reason": reason, "seconds": round(seconds, 3)})
    await page.wait_for_timeout(int(seconds * 1000))
    return seconds


async def random_mouse_moves(
    page: Page,
    profile: BehaviorProfile,
    log: list[dict[str, Any]] | None = None,
    *,
    list_area: ListAreaProbe | None = None,
) -> None:
    """给出 `list_area` 时目标点都在列表区域内（开始时取一次区域，事件里记下它），否则在整个视口内。"""
    area = await list_area(page) if list_area is not None else None
    if area is None:
        width, height = await page_size(page)
    count = RANDOM.randint(*profile.mouse_move_count)
    for _ in range(count):
        if area is not None:
            x, y = area.point()
        else:
            x = RANDOM.randint(20, max(30, width - 20))
            y = RANDOM.randint(20, max(30, height - 20))
        if profile.name == "xhs_guarded":
            # 小红书：分段轨迹，事件按 60Hz 逐个发出（#84）。
            await pointer_for(page).move_to(x, y)
        else:
            await page.mouse.move(x, y, steps=RANDOM.randint(2, 8))
        await page.wait_for_timeout(RANDOM.randint(80, 450))
    _log(log, {"event": "mouse_moves", "count": count, **(area.evidence if area is not None else {})})


async def cdp_touch_scroll(page: Page, delta_y: int, log: list[dict[str, Any]] | None = None) -> bool:
    before = await scroll_position(page)
    try:
        session = await page.context.new_cdp_session(page)
        width, height = await page_size(page)
        start_x = RANDOM.randint(max(20, width // 4), max(30, width * 3 // 4))
        start_y = RANDOM.randint(max(40, height // 2), max(50, height * 4 // 5))
        steps = RANDOM.randint(5, 10)
        timestamp = time.time()
        await session.send(
            "Input.dispatchTouchEvent",
            {"type": "touchStart", "touchPoints": [{"x": start_x, "y": start_y, "id": 1}], "timestamp": timestamp},
        )
        for step in range(1, steps + 1):
            y = start_y - (delta_y * step / steps)
            await session.send(
                "Input.dispatchTouchEvent",
                {
                    "type": "touchMove",
                    "touchPoints": [{"x": start_x + RANDOM.randint(-8, 8), "y": y, "id": 1}],
                    "timestamp": timestamp + step * 0.035,
                },
            )
            await asyncio.sleep(RANDOM.uniform(0.018, 0.055))
        await session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": [], "timestamp": time.time()})
        await page.wait_for_timeout(120)
        after = await scroll_position(page)
        effect_observed = scroll_effect_observed(before, after)
        _log(
            log,
            {
                "event": "cdp_touch_scroll",
                "delta_y": delta_y,
                "steps": steps,
                "effect_observed": effect_observed,
                "before": before,
                "after": after,
            },
        )
        return effect_observed
    except Exception as exc:
        _log(log, {"event": "cdp_touch_scroll_failed", "error": f"{type(exc).__name__}: {exc}"})
        return False


async def wheel_scroll(
    page: Page,
    delta_y: int,
    log: list[dict[str, Any]] | None = None,
    *,
    gestures: bool = False,
    list_area: ListAreaProbe | None = None,
) -> bool:
    """`gestures` 为真（小红书 xhs_guarded 档位）时按触控板手势序列滚动，并在事件里记各手势的事件数与位移。
    给出 `list_area` 时前后位置与是否有效按列表口径（`list_scroll_moved`），否则按整页 `scroll_position`。"""
    before = (await list_area(page)).position if list_area is not None else await scroll_position(page)
    scroll: dict[str, Any] = {}
    if gestures:
        scroll = await pointer_for(page).scroll(delta_y)
    else:
        await page.mouse.wheel(RANDOM.randint(-40, 40), delta_y)
    await page.wait_for_timeout(120)
    if list_area is not None:
        after = (await list_area(page)).position
        effect_observed = list_scroll_moved(before, after)
    else:
        after = await scroll_position(page)
        effect_observed = scroll_effect_observed(before, after)
    _log(
        log,
        {
            "event": "wheel_scroll",
            "delta_y": delta_y,
            "effect_observed": effect_observed,
            "before": before,
            "after": after,
            **scroll,
        },
    )
    return effect_observed


async def human_scroll(
    page: Page,
    profile: BehaviorProfile,
    *,
    intent: str,
    max_passes: int | None = None,
    log: list[dict[str, Any]] | None = None,
    list_area: ListAreaProbe | None = None,
) -> None:
    """给出 `list_area` 时每一轮的滚轮落点都在当时的列表区域内（事件里记下它），不用触摸滚动，有效与否按列表
    口径；否则落点在视口横 1/3–4/5、纵 1/3–4/5，有效与否按整页口径。"""
    low, high = profile.scroll_passes
    passes = RANDOM.randint(low, high)
    if max_passes is not None:
        passes = min(passes, max_passes)
    effective_passes = 0
    for index in range(max(1, passes)):
        area = await list_area(page) if list_area is not None else None
        if area is not None:
            target_x, target_y = area.point()
        else:
            width, height = await page_size(page)
            target_x = RANDOM.randint(max(30, width // 3), max(31, width * 4 // 5))
            target_y = RANDOM.randint(max(40, height // 3), max(41, height * 4 // 5))
        gestures = profile.name == "xhs_guarded"
        if gestures:
            await pointer_for(page).move_to(target_x, target_y)
        else:
            await page.mouse.move(target_x, target_y, steps=RANDOM.randint(3, 8))
        _log(log, {
            "event": "scroll_target", "x": target_x, "y": target_y, **(area.evidence if area is not None else {}),
        })
        direction = -1 if index > 0 and RANDOM.random() < 0.16 else 1
        delta = direction * RANDOM.randint(*profile.scroll_delta_px)
        use_touch = area is None and RANDOM.random() < profile.cdp_touch_probability
        effect_observed = use_touch and await cdp_touch_scroll(page, delta, log)
        if not effect_observed:
            effect_observed = await wheel_scroll(page, delta, log, gestures=gestures, list_area=list_area)
        if not effect_observed:
            effect_observed = await wheel_scroll(page, -delta, log, gestures=gestures, list_area=list_area)
        if effect_observed:
            effective_passes += 1
        await page.wait_for_timeout(RANDOM.randint(450, 2600))
    _log(
        log,
        {
            "event": "human_scroll_complete",
            "intent": intent,
            "passes": passes,
            "effective_passes": effective_passes,
        },
    )


async def dwell_on_list(
    page: Page,
    profile: BehaviorProfile,
    log: list[dict[str, Any]] | None = None,
    *,
    list_area: ListAreaProbe | None = None,
) -> None:
    await human_pause(page, profile.list_dwell_seconds, reason="list_dwell_initial", log=log)
    await random_mouse_moves(page, profile, log, list_area=list_area)
    await human_scroll(
        page, profile, intent="list", max_passes=max(2, profile.scroll_passes[1] // 2), log=log, list_area=list_area,
    )
    if RANDOM.random() < 0.35:
        await human_pause(page, (2.0, 12.0), reason="list_after_scroll", log=log)


def _detail_bounds(profile: BehaviorProfile, content_hint: dict[str, Any] | None) -> tuple[float, float]:
    low, high = profile.detail_dwell_seconds
    hint = content_hint or {}
    content_length = int(hint.get("content_length") or hint.get("body_text_length") or 0)
    image_count = int(hint.get("image_count") or hint.get("loaded_image_count") or 0)
    scale = min(1.8, 1.0 + min(content_length, 8000) / 12000 + min(image_count, 40) / 120)
    return low * scale, high * scale


async def maybe_visit_comments(page: Page, profile: BehaviorProfile, log: list[dict[str, Any]] | None = None) -> bool:
    if RANDOM.random() >= profile.comment_visit_probability:
        _log(log, {"event": "comments_skipped_by_probability"})
        return False
    for selector in COMMENT_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.count() <= 0:
                continue
            await locator.scroll_into_view_if_needed(timeout=2500)
            await page.wait_for_timeout(RANDOM.randint(1500, 6500))
            await human_scroll(page, profile, intent="comments", max_passes=2, log=log)
            _log(log, {"event": "comments_visited", "selector": selector})
            return True
        except PlaywrightTimeoutError:
            continue
        except Exception as exc:
            _log(log, {"event": "comments_visit_error", "selector": selector, "error": f"{type(exc).__name__}: {exc}"})
    _log(log, {"event": "comments_not_found"})
    return False


async def dwell_on_detail(
    page: Page,
    profile: BehaviorProfile,
    *,
    content_hint: dict[str, Any] | None = None,
    max_scroll_passes: int | None = None,
    log: list[dict[str, Any]] | None = None,
) -> None:
    await human_pause(page, _detail_bounds(profile, content_hint), reason="detail_dwell_initial", log=log)
    await random_mouse_moves(page, profile, log)
    await human_scroll(page, profile, intent="detail", max_passes=max_scroll_passes, log=log)
    await maybe_visit_comments(page, profile, log)


async def inter_detail_cooldown(profile: BehaviorProfile, log: list[dict[str, Any]] | None = None) -> float:
    seconds = _rand_range(profile.inter_detail_cooldown_seconds)
    _log(log, {"event": "inter_detail_cooldown", "seconds": round(seconds, 3)})
    await asyncio.sleep(seconds)
    return seconds


async def return_to_list(
    page: Page,
    list_url: str,
    profile: BehaviorProfile,
    *,
    log: list[dict[str, Any]] | None = None,
) -> None:
    try:
        response = await page.go_back(wait_until="domcontentloaded", timeout=20_000)
        _log(log, {"event": "go_back_to_list", "ok": bool(response)})
    except Exception as exc:
        _log(log, {"event": "go_back_failed", "error": f"{type(exc).__name__}: {exc}", "fallback_url": list_url})
        await page.goto(list_url, wait_until="domcontentloaded", timeout=45_000)
    await dwell_on_list(page, profile, log)
