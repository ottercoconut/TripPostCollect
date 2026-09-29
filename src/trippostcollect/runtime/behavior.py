"""通用行为执行；平台等待与证据写出由入口注入。"""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Page

from trippostcollect.records.text_signals import is_xhs_sms_terminal_text
from trippostcollect.runtime.human_flow import dwell_on_list, load_behavior_profile
from trippostcollect.runtime.helpers import utc_now


HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS = 240
XHS_VISIBLE_CHECK_INTERVAL_SECONDS = 5.0
REQUEST_RANDOM = random.SystemRandom()
REQUIRED_BEHAVIOR_EVENTS = frozenset({"pause", "mouse_moves", "human_scroll_complete"})
CAPTCHA_VISIBLE_RE = re.compile(
    r"人机验证|安全验证|请完成验证|请通过验证|图形验证码|滑块验证码|拖动滑块|security verification|captcha|geetest",
    re.I,
)
RATE_LIMIT_VISIBLE_RE = re.compile(
    r"访问(?:过于)?频繁|请求(?:过于)?频繁|操作频繁|"
    r"requests?\s+(?:are\s+)?too\s+frequent|too many requests|rate limit|"
    r"try again after\s+\d+\s+minutes?",
    re.I,
)
BLOCKED_VISIBLE_RE = re.compile(r"拒绝访问|access denied|forbidden|访问受限", re.I)
XHS_PLATFORM_SECURITY_LIMIT_RE = re.compile(
    r"安全限制|账号异常|account exception(?:\s*,?\s*please retry later)?|\b300011\b",
    re.I,
)
XHS_PLATFORM_SECURITY_LIMIT_URL_RE = re.compile(r"/website-login/error(?:[?#]|$)", re.I)
XHS_CAPTCHA_URL_RE = re.compile(r"/website-login/captcha(?:[?#]|$)", re.I)
XHS_LOGIN_URL_RE = re.compile(r"/(?:login|website-login)(?:[/?#]|$)", re.I)
LOGIN_VISIBLE_RE = re.compile(r"请先登录|登录后查看|需要登录|login_required", re.I)


async def runtime_fingerprint(page: Page) -> dict[str, Any]:
    async with asyncio.timeout(10):
        return await page.evaluate(
            """() => ({
                webdriver: navigator.webdriver,
                languages: Array.from(navigator.languages || []),
                platform: navigator.platform || '',
                user_agent: navigator.userAgent || '',
                hardware_concurrency: navigator.hardwareConcurrency || null,
                device_memory: navigator.deviceMemory || null,
                max_touch_points: navigator.maxTouchPoints || 0,
                viewport: {
                    width: window.innerWidth || 0,
                    height: window.innerHeight || 0,
                    device_pixel_ratio: window.devicePixelRatio || 1,
                },
                visibility_state: document.visibilityState || '',
            })"""
        )


async def visible_page_state(page: Page) -> tuple[str, dict[str, bool]]:
    text_parts: list[str] = []
    try:
        text = await page.locator("body").inner_text(timeout=5_000)
    except Exception:
        text = ""
    normalized_main = " ".join(text.split())
    if normalized_main:
        text_parts.append(normalized_main)

    main_frame = getattr(page, "main_frame", None)
    for frame in list(getattr(page, "frames", ()) or ())[:8]:
        if frame is main_frame:
            continue
        try:
            frame_text = await frame.locator("body").inner_text(timeout=1_500)
        except Exception:
            continue
        normalized_frame = " ".join(frame_text.split())
        if normalized_frame and normalized_frame not in text_parts:
            text_parts.append(normalized_frame)

    normalized = " ".join(text_parts)
    page_url = str(getattr(page, "url", "") or "")
    hostname = (urlparse(page_url).hostname or "").lower()
    is_xhs_page = hostname == "xiaohongshu.com" or hostname.endswith(".xiaohongshu.com")
    xhs_login_control_visible = False
    if is_xhs_page:
        try:
            xhs_login_control_visible = bool(
                await page.evaluate(
                    """() => Array.from(
                        document.querySelectorAll("a, button, [role='button']")
                    ).some((element) => {
                        const text = (element.innerText || element.textContent || "").trim();
                        const style = window.getComputedStyle(element);
                        return text === "登录"
                            && style.display !== "none"
                            && style.visibility !== "hidden"
                            && element.getClientRects().length > 0;
                    })"""
                )
            )
        except Exception:
            xhs_login_control_visible = False
    markers = {
        "platform_security_limit": bool(
            is_xhs_page
            and (
                XHS_PLATFORM_SECURITY_LIMIT_RE.search(normalized)
                or XHS_PLATFORM_SECURITY_LIMIT_URL_RE.search(page_url)
            )
        ),
        "captcha_or_verify": bool(
            CAPTCHA_VISIBLE_RE.search(normalized)
            or (is_xhs_page and XHS_CAPTCHA_URL_RE.search(page_url))
        ),
        "sms_verification_terminal": bool(
            is_xhs_page and is_xhs_sms_terminal_text(normalized)
        ),
        "rate_limited": bool(RATE_LIMIT_VISIBLE_RE.search(normalized)),
        "blocked": bool(BLOCKED_VISIBLE_RE.search(normalized)),
        "login_required": bool(
            LOGIN_VISIBLE_RE.search(normalized)
            or (is_xhs_page and XHS_LOGIN_URL_RE.search(page_url))
            or xhs_login_control_visible
        ),
    }
    return normalized[:360], markers


def visible_challenge(markers: dict[str, bool]) -> str:
    return next(
        (
            key
            for key in (
                "platform_security_limit",
                "sms_verification_terminal",
                "rate_limited",
                "blocked",
                "captcha_or_verify",
                "login_required",
            )
            if markers.get(key)
        ),
        "",
    )


def runtime_fingerprint_valid(fingerprint: dict[str, Any] | None) -> bool:
    if not isinstance(fingerprint, dict):
        return False
    viewport = fingerprint.get("viewport") or {}
    return bool(
        "webdriver" in fingerprint
        and fingerprint.get("webdriver") is None
        and fingerprint.get("languages")
        and fingerprint.get("platform")
        and fingerprint.get("user_agent")
        and fingerprint.get("visibility_state") == "visible"
        and int(viewport.get("width") or 0) > 0
        and int(viewport.get("height") or 0) > 0
    )


def effective_scroll_recorded(events: list[dict[str, Any]]) -> bool:
    return any(
        item.get("event") == "human_scroll_complete"
        and int(item.get("effective_passes") or 0) > 0
        for item in events
        if isinstance(item, dict)
    )


async def dwell_on_list_with_checks(
    page: Page,
    profile: Any,
    events: list[dict[str, Any]],
) -> tuple[str, dict[str, bool]]:
    dwell_task = asyncio.create_task(dwell_on_list(page, profile, log=events))
    try:
        while True:
            done, _ = await asyncio.wait(
                {dwell_task},
                timeout=XHS_VISIBLE_CHECK_INTERVAL_SECONDS,
            )
            if dwell_task in done:
                await dwell_task
                return "", {}
            text_sample, markers = await visible_page_state(page)
            challenge = visible_challenge(markers)
            events.append(
                {
                    "event": "visible_state_check",
                    "challenge": challenge,
                    "markers": markers,
                }
            )
            if challenge:
                dwell_task.cancel()
                await asyncio.gather(dwell_task, return_exceptions=True)
                return text_sample, markers
    finally:
        if not dwell_task.done():
            dwell_task.cancel()
            await asyncio.gather(dwell_task, return_exceptions=True)


def behavior_evidence_valid(evidence: dict[str, Any] | None) -> bool:
    if not isinstance(evidence, dict) or evidence.get("status") != "completed":
        return False
    events = evidence.get("events") or []
    event_names = {str(item.get("event") or "") for item in events if isinstance(item, dict)}
    markers = evidence.get("visible_markers") or {}
    initial_markers = evidence.get("initial_visible_markers") or {}
    challenge = any(
        bool(marker_set.get(key))
        for marker_set in (initial_markers, markers)
        for key in (
            "platform_security_limit",
            "sms_verification_terminal",
            "captcha_or_verify",
            "rate_limited",
            "blocked",
            "login_required",
        )
    )
    base_ok = REQUIRED_BEHAVIOR_EVENTS.issubset(event_names) and not challenge
    if evidence.get("profile") != "xhs_guarded":
        return base_ok
    readiness = evidence.get("page_readiness") or {}
    return bool(
        base_ok
        and readiness.get("ready") is True
        and runtime_fingerprint_valid(evidence.get("runtime_fingerprint"))
        and effective_scroll_recorded(events)
    )


async def run_guarded_request_pause(
    *,
    evidence_path: str | Path,
    profile_name: str,
    stage: str,
    minimum: float,
    maximum: float,
    write_evidence: Callable[[str | Path, dict[str, Any]], None],
) -> dict[str, Any]:
    if profile_name != "xhs_guarded":
        raise RuntimeError("XHS request pacing requires the xhs_guarded behavior profile")
    low, high = sorted((max(0.0, float(minimum)), max(0.0, float(maximum))))
    seconds = REQUEST_RANDOM.uniform(low, high)
    await asyncio.sleep(seconds)

    path = Path(evidence_path).expanduser()
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot update XHS request pacing evidence: {exc}") from exc
    if not behavior_evidence_valid(evidence):
        raise RuntimeError("cannot append XHS request pacing to incomplete behavior evidence")
    event = {"stage": stage, "seconds": round(seconds, 3), "finished_at": utc_now()}
    events = evidence.setdefault("request_pacing_events", [])
    events.append(event)
    evidence["request_pacing_events"] = events[-200:]
    write_evidence(path, evidence)
    return event


async def run_page_behavior(
    page: Page,
    *,
    platform_key: str,
    evidence_path: str | Path,
    profile_name: str = "social_high_risk",
    write_evidence: Callable[[str | Path, dict[str, Any]], None],
    xhs_search_ready: Callable[[Page, list], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    if profile_name == "xhs_guarded" and xhs_search_ready is None:
        raise RuntimeError("xhs_guarded requires an injected xhs_search_ready")
    started_at = utc_now()
    events: list[dict[str, Any]] = []
    error = ""
    fingerprint: dict[str, Any] = {}
    visible_text_sample = ""
    visible_markers: dict[str, bool] = {}
    initial_visible_text_sample = ""
    initial_visible_markers: dict[str, bool] = {}
    screenshot_path = str(Path(evidence_path).expanduser().with_suffix(".png"))
    artifact_errors: list[str] = []
    page_readiness: dict[str, Any] = {}

    try:
        profile = load_behavior_profile(profile_name, strict=True)
        behavior_ready = True
        if profile_name == "xhs_guarded":
            page_readiness = await xhs_search_ready(page, events)
            behavior_ready = bool(page_readiness.get("ready"))
            if not behavior_ready:
                initial_visible_text_sample = str(page_readiness.get("visible_text_sample") or "")
                initial_visible_markers = dict(page_readiness.get("markers") or {})
                visible_text_sample = initial_visible_text_sample
                visible_markers = initial_visible_markers
                reason = str(page_readiness.get("reason") or "search_results_not_ready")
                if reason not in {
                    "platform_security_limit",
                    "sms_verification_terminal",
                    "captcha_or_verify",
                    "rate_limited",
                    "blocked",
                    "login_required",
                }:
                    raise RuntimeError(f"xhs_page_not_ready:{reason}")

        if behavior_ready:
            initial_visible_text_sample, initial_visible_markers = await visible_page_state(page)
            initial_challenge = visible_challenge(initial_visible_markers)
            if initial_challenge:
                visible_text_sample = initial_visible_text_sample
                visible_markers = initial_visible_markers
            else:
                if profile_name == "xhs_guarded":
                    visible_text_sample, visible_markers = await dwell_on_list_with_checks(page, profile, events)
                else:
                    await dwell_on_list(page, profile, log=events)
                if not visible_challenge(visible_markers):
                    fingerprint = await runtime_fingerprint(page)
                    visible_text_sample, visible_markers = await visible_page_state(page)
        try:
            await page.screenshot(path=screenshot_path, full_page=False, timeout=10_000, animations="disabled")
        except Exception as exc:
            artifact_errors.append(f"screenshot:{type(exc).__name__}:{exc}")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    challenge = visible_challenge(visible_markers)
    status = "completed" if not error and not challenge else "failed"
    evidence = {
        "schema_version": 1,
        "platform": platform_key,
        "phase": "pre_search_human_behavior",
        "profile": profile_name,
        "status": status,
        "started_at": started_at,
        "finished_at": utc_now(),
        "url": page.url,
        "events": events,
        "runtime_fingerprint": fingerprint,
        "page_readiness": page_readiness,
        "operator_verification_events": page_readiness.get("operator_verification_events") or [],
        "initial_visible_markers": initial_visible_markers,
        "initial_visible_text_sample": initial_visible_text_sample,
        "visible_markers": visible_markers,
        "visible_text_sample": visible_text_sample,
        "screenshot": screenshot_path,
        "artifact_errors": artifact_errors,
        "error": error,
        "challenge": challenge,
        "evidence_path": str(Path(evidence_path).expanduser()),
    }
    write_evidence(evidence_path, evidence)

    if error:
        raise RuntimeError(f"human_behavior_failed:{platform_key}:{error}")
    if challenge:
        raise RuntimeError(f"{challenge}_detected_during_human_behavior:{platform_key}")
    if not behavior_evidence_valid(evidence):
        raise RuntimeError(f"human_behavior_evidence_incomplete:{platform_key}")
    return evidence


def load_behavior_evidence(path: str | Path) -> dict[str, Any]:
    candidate = Path(path).expanduser()
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "missing",
            "evidence_path": str(candidate),
            "error": "behavior_evidence_missing_or_invalid",
        }
    return value if isinstance(value, dict) else {
        "status": "invalid",
        "evidence_path": str(candidate),
        "error": "behavior_evidence_not_an_object",
    }
