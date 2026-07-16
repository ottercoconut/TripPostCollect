#!/usr/bin/env python3
"""Shared conservative browser behavior helpers for deterministic crawlers."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError


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
        list_dwell_seconds=(45, 120),
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
    "douban_group": "conservative",
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


async def random_mouse_moves(page: Page, profile: BehaviorProfile, log: list[dict[str, Any]] | None = None) -> None:
    width, height = await page_size(page)
    count = RANDOM.randint(*profile.mouse_move_count)
    for _ in range(count):
        await page.mouse.move(
            RANDOM.randint(20, max(30, width - 20)),
            RANDOM.randint(20, max(30, height - 20)),
            steps=RANDOM.randint(2, 8),
        )
        await page.wait_for_timeout(RANDOM.randint(80, 450))
    _log(log, {"event": "mouse_moves", "count": count})


async def cdp_touch_scroll(page: Page, delta_y: int, log: list[dict[str, Any]] | None = None) -> bool:
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
        _log(log, {"event": "cdp_touch_scroll", "delta_y": delta_y, "steps": steps})
        return True
    except Exception as exc:
        _log(log, {"event": "cdp_touch_scroll_failed", "error": f"{type(exc).__name__}: {exc}"})
        return False


async def wheel_scroll(page: Page, delta_y: int, log: list[dict[str, Any]] | None = None) -> None:
    await page.mouse.wheel(RANDOM.randint(-40, 40), delta_y)
    _log(log, {"event": "wheel_scroll", "delta_y": delta_y})


async def human_scroll(
    page: Page,
    profile: BehaviorProfile,
    *,
    intent: str,
    max_passes: int | None = None,
    log: list[dict[str, Any]] | None = None,
) -> None:
    low, high = profile.scroll_passes
    passes = RANDOM.randint(low, high)
    if max_passes is not None:
        passes = min(passes, max_passes)
    for index in range(max(1, passes)):
        direction = -1 if index > 0 and RANDOM.random() < 0.16 else 1
        delta = direction * RANDOM.randint(*profile.scroll_delta_px)
        use_touch = RANDOM.random() < profile.cdp_touch_probability
        if not use_touch or not await cdp_touch_scroll(page, delta, log):
            await wheel_scroll(page, delta, log)
        await page.wait_for_timeout(RANDOM.randint(450, 2600))
    _log(log, {"event": "human_scroll_complete", "intent": intent, "passes": passes})


async def dwell_on_list(page: Page, profile: BehaviorProfile, log: list[dict[str, Any]] | None = None) -> None:
    await human_pause(page, profile.list_dwell_seconds, reason="list_dwell_initial", log=log)
    await random_mouse_moves(page, profile, log)
    await human_scroll(page, profile, intent="list", max_passes=max(2, profile.scroll_passes[1] // 2), log=log)
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
