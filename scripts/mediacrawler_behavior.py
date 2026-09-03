#!/usr/bin/env python3
"""Required human-behavior stage for project-managed MediaCrawler runs."""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from playwright.async_api import Page

from human_flow import (
    dwell_on_list,
    human_pause,
    human_scroll,
    load_behavior_profile,
    random_mouse_moves,
)


HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS = 240
XHS_VISIBLE_CHECK_INTERVAL_SECONDS = 5.0
XHS_SEARCH_READY_TIMEOUT_SECONDS = 90.0
XHS_CONTINUITY_VERIFY_WAIT_SECONDS = 600.0
XHS_CONTINUITY_VERIFY_POLL_SECONDS = 2.0
REQUEST_RANDOM = random.SystemRandom()
XHS_POST_INTERACTION_MODES = frozenset({"comment-scroll", "like-one", "random"})
REQUIRED_BEHAVIOR_EVENTS = frozenset({"pause", "mouse_moves", "human_scroll_complete"})
CAPTCHA_VISIBLE_RE = re.compile(
    r"人机验证|安全验证|请完成验证|请通过验证|图形验证码|滑块验证码|拖动滑块|security verification|captcha|geetest",
    re.I,
)
XHS_SMS_VERIFICATION_RE = re.compile(r"sms verification|parameter error", re.I)
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
XHS_COMMENT_SELECTORS = (
    "[class*='comments-container']",
    "[class*='comment-list']",
    "[id*='comment']",
    "[class*='comment']",
    "[aria-label*='评论']",
)
XHS_LIKE_SELECTORS = (
    ".note-detail-mask .interact-container .like-wrapper",
    ".interact-container .like-wrapper",
    "[class*='engage-bar'] [class*='like-wrapper']",
    "[role='button'][aria-label*='点赞']",
    "button[aria-label*='点赞']",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_evidence(path: str | Path, evidence: dict[str, Any]) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)


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
            or (is_xhs_page and XHS_SMS_VERIFICATION_RE.search(normalized))
            or (is_xhs_page and XHS_CAPTCHA_URL_RE.search(page_url))
        ),
        "rate_limited": bool(RATE_LIMIT_VISIBLE_RE.search(normalized)),
        "blocked": bool(BLOCKED_VISIBLE_RE.search(normalized)),
        "login_required": bool(
            LOGIN_VISIBLE_RE.search(normalized)
            or (is_xhs_page and XHS_LOGIN_URL_RE.search(page_url))
        ),
    }
    return normalized[:360], markers


def visible_challenge(markers: dict[str, bool]) -> str:
    return next(
        (
            key
            for key in (
                "platform_security_limit",
                "rate_limited",
                "blocked",
                "captcha_or_verify",
                "login_required",
            )
            if markers.get(key)
        ),
        "",
    )


async def record_xhs_platform_security_limit(
    page: Page,
    *,
    evidence_path: str | Path,
    stage: str,
    visible_text_sample: str,
    visible_markers: dict[str, bool],
) -> dict[str, Any]:
    path = Path(evidence_path).expanduser()
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot update XHS platform security evidence: {exc}") from exc

    safe_stage = re.sub(r"[^a-zA-Z0-9_-]", "_", stage)[:80] or "unknown"
    screenshot_path = path.with_name(f"{path.stem}.security-limit.{safe_stage}.png")
    screenshot_error = ""
    try:
        await page.screenshot(
            path=str(screenshot_path),
            full_page=False,
            timeout=10_000,
            animations="disabled",
        )
    except Exception as exc:
        screenshot_error = f"{type(exc).__name__}: {exc}"

    event = {
        "at": utc_now(),
        "stage": stage,
        "challenge": "platform_security_limit",
        "classification": "platform_security_limit",
        "observed_error_code": (
            "300011"
            if re.search(
                r"\b300011\b",
                f"{visible_text_sample}\n{str(getattr(page, 'url', '') or '')}",
            )
            else ""
        ),
        "url": str(getattr(page, "url", "") or ""),
        "visible_text_sample": visible_text_sample[:360],
        "visible_markers": visible_markers,
        "screenshot": str(screenshot_path) if not screenshot_error else "",
        "screenshot_error": screenshot_error,
    }
    events = evidence.setdefault("platform_security_limit_events", [])
    events.append(event)
    evidence["platform_security_limit_events"] = events[-20:]
    evidence.update(
        {
            "status": "failed",
            "challenge": "platform_security_limit",
            "visible_text_sample": visible_text_sample[:360],
            "visible_markers": visible_markers,
        }
    )
    write_evidence(path, evidence)
    return event


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


async def wait_for_xhs_search_ready(
    page: Page,
    events: list[dict[str, Any]],
    *,
    timeout_seconds: float = XHS_SEARCH_READY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + max(0.1, float(timeout_seconds))
    verification_deadline: float | None = None
    operator_verification_events: list[dict[str, Any]] = []
    last_text = ""
    last_markers: dict[str, bool] = {}
    card_count = 0
    profile_count = 0

    while True:
        active_deadline = verification_deadline or deadline
        remaining = active_deadline - time.monotonic()
        if remaining <= 0:
            challenge = visible_challenge(last_markers)
            pending_event = operator_verification_events[-1] if operator_verification_events else None
            if pending_event and pending_event.get("status") == "waiting_for_operator":
                pending_event.update(
                    {
                        "finished_at": utc_now(),
                        "status": "failed",
                        "challenge": challenge or pending_event.get("initial_challenge"),
                        "visible_markers": last_markers,
                        "visible_text_sample": last_text,
                        "error": "operator_verification_timeout",
                    }
                )
            reason = challenge or (
                "login_required" if last_markers.get("login_required") else "search_results_not_ready"
            )
            return {
                "ready": False,
                "reason": reason,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "card_count": card_count,
                "profile_count": profile_count,
                "markers": last_markers,
                "visible_text_sample": last_text,
                "url": page.url,
                "operator_verification_events": operator_verification_events,
            }

        try:
            async with asyncio.timeout(min(6.0, remaining)):
                last_text, last_markers = await visible_page_state(page)
                counts = await page.evaluate(
                    """() => ({
                        card_count: document.querySelectorAll(
                            "a[href*='/explore/'], a[href*='/discovery/item/']"
                        ).length,
                        profile_count: document.querySelectorAll("a[href*='/user/profile/']").length,
                    })"""
                )
                card_count = int((counts or {}).get("card_count") or 0)
                profile_count = int((counts or {}).get("profile_count") or 0)
        except TimeoutError:
            events.append(
                {
                    "event": "page_readiness_check",
                    "ready": False,
                    "reason": "inspection_timeout",
                }
            )
        else:
            challenge = visible_challenge(last_markers)
            ready = not challenge and card_count > 0 and profile_count > 0
            events.append(
                {
                    "event": "page_readiness_check",
                    "ready": ready,
                    "challenge": challenge,
                    "card_count": card_count,
                    "profile_count": profile_count,
                    "markers": last_markers,
                }
            )

            if challenge in {"captcha_or_verify", "login_required"}:
                pending_event = operator_verification_events[-1] if operator_verification_events else None
                if not pending_event or pending_event.get("status") != "waiting_for_operator":
                    verification_deadline = time.monotonic() + max(
                        0.1,
                        float(XHS_CONTINUITY_VERIFY_WAIT_SECONDS),
                    )
                    verification_event = {
                        "stage": "pre_search_human_behavior",
                        "started_at": utc_now(),
                        "finished_at": None,
                        "status": "waiting_for_operator",
                        "initial_challenge": challenge,
                        "challenge": challenge,
                        "visible_markers": last_markers,
                        "visible_text_sample": last_text,
                        "timeout_seconds": round(
                            verification_deadline - time.monotonic(), 3
                        ),
                        "url": page.url,
                    }
                    operator_verification_events.append(verification_event)
                    try:
                        await page.bring_to_front()
                    except Exception as exc:
                        verification_event.update(
                            {
                                "finished_at": utc_now(),
                                "status": "failed",
                                "error": f"bring_to_front_failed:{type(exc).__name__}:{exc}",
                            }
                        )
                        verification_deadline = time.monotonic()
                await asyncio.sleep(
                    min(
                        max(0.1, float(XHS_CONTINUITY_VERIFY_POLL_SECONDS)),
                        max(0.0, verification_deadline - time.monotonic())
                        if verification_deadline is not None
                        else 0.0,
                    )
                )
                continue

            if operator_verification_events:
                pending_event = operator_verification_events[-1]
                if pending_event.get("status") == "waiting_for_operator":
                    pending_event.update(
                        {
                            "finished_at": utc_now(),
                            "status": "completed",
                            "challenge": "",
                            "visible_markers": last_markers,
                            "visible_text_sample": last_text,
                            "url": page.url,
                        }
                    )
                verification_deadline = None
                deadline = time.monotonic() + max(0.1, float(timeout_seconds))

            if ready:
                return {
                    "ready": True,
                    "reason": "",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "card_count": card_count,
                    "profile_count": profile_count,
                    "markers": last_markers,
                    "visible_text_sample": last_text,
                    "url": page.url,
                    "operator_verification_events": operator_verification_events,
                }
            if challenge:
                return {
                    "ready": False,
                    "reason": challenge,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "card_count": card_count,
                    "profile_count": profile_count,
                    "markers": last_markers,
                    "visible_text_sample": last_text,
                    "url": page.url,
                    "operator_verification_events": operator_verification_events,
                }

        await asyncio.sleep(min(2.0, max(0.0, deadline - time.monotonic())))


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


async def wait_for_xhs_continuity_verification(
    page: Page,
    *,
    evidence: dict[str, Any],
    evidence_path: str | Path,
    stage: str,
    initial_challenge: str,
    initial_text: str,
    initial_markers: dict[str, bool],
    timeout_seconds: float | None = None,
    poll_seconds: float | None = None,
) -> tuple[str, dict[str, bool], dict[str, Any]]:
    if initial_challenge not in {"captcha_or_verify", "login_required"}:
        raise RuntimeError(f"unsupported_xhs_operator_verification:{initial_challenge}")

    timeout = max(
        0.1,
        float(
            XHS_CONTINUITY_VERIFY_WAIT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        ),
    )
    poll = max(
        0.01,
        float(
            XHS_CONTINUITY_VERIFY_POLL_SECONDS
            if poll_seconds is None
            else poll_seconds
        ),
    )
    started = time.monotonic()
    event: dict[str, Any] = {
        "stage": stage,
        "started_at": utc_now(),
        "finished_at": None,
        "status": "waiting_for_operator",
        "initial_challenge": initial_challenge,
        "challenge": initial_challenge,
        "visible_markers": initial_markers,
        "visible_text_sample": initial_text,
        "timeout_seconds": round(timeout, 3),
        "url": page.url,
    }
    verification_events = evidence.setdefault("operator_verification_events", [])
    verification_events.append(event)
    evidence["operator_verification_events"] = verification_events[-50:]
    write_evidence(evidence_path, evidence)

    try:
        await page.bring_to_front()
    except Exception as exc:
        event.update(
            {
                "finished_at": utc_now(),
                "status": "failed",
                "error": f"bring_to_front_failed:{type(exc).__name__}:{exc}",
            }
        )
        evidence.update(
            {
                "status": "failed",
                "challenge": initial_challenge,
                "visible_markers": initial_markers,
                "visible_text_sample": initial_text,
            }
        )
        write_evidence(evidence_path, evidence)
        raise RuntimeError(f"xhs_continuity_page_unavailable:{stage}") from exc

    latest_challenge = initial_challenge
    latest_text = initial_text
    latest_markers = initial_markers
    while True:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            event.update(
                {
                    "finished_at": utc_now(),
                    "status": "failed",
                    "challenge": latest_challenge,
                    "visible_markers": latest_markers,
                    "visible_text_sample": latest_text,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "error": "operator_verification_timeout",
                }
            )
            evidence.update(
                {
                    "status": "failed",
                    "challenge": latest_challenge or initial_challenge,
                    "visible_markers": latest_markers,
                    "visible_text_sample": latest_text,
                }
            )
            write_evidence(evidence_path, evidence)
            raise RuntimeError(
                f"xhs_continuity_verification_timeout:{stage}:{initial_challenge}"
            )

        await asyncio.sleep(min(poll, remaining))
        latest_text, latest_markers = await visible_page_state(page)
        latest_challenge = visible_challenge(latest_markers)
        event.update(
            {
                "challenge": latest_challenge,
                "visible_markers": latest_markers,
                "visible_text_sample": latest_text,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "url": page.url,
            }
        )
        if not latest_challenge:
            event.update({"finished_at": utc_now(), "status": "completed"})
            write_evidence(evidence_path, evidence)
            return latest_text, latest_markers, event
        if latest_challenge in {"rate_limited", "blocked"}:
            event.update(
                {
                    "finished_at": utc_now(),
                    "status": "failed",
                    "error": f"{latest_challenge}_during_operator_verification",
                }
            )
            evidence.update(
                {
                    "status": "failed",
                    "challenge": latest_challenge,
                    "visible_markers": latest_markers,
                    "visible_text_sample": latest_text,
                }
            )
            write_evidence(evidence_path, evidence)
            raise RuntimeError(
                f"{latest_challenge}_detected_during_xhs_continuity:{stage}"
            )


async def run_xhs_api_captcha_verification(
    page: Page,
    *,
    evidence_path: str | Path,
    verify_type: str,
    verify_uuid: str,
    verify_biz: int,
    timeout_seconds: float | None = None,
    poll_seconds: float | None = None,
) -> dict[str, Any]:
    path = Path(evidence_path).expanduser()
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot update XHS API captcha evidence: {exc}") from exc
    if evidence.get("profile") != "xhs_guarded" or not behavior_evidence_valid(evidence):
        raise RuntimeError("XHS API captcha verification requires completed xhs_guarded evidence")
    if not verify_type or not verify_uuid:
        raise RuntimeError("XHS API captcha response is missing verification identifiers")

    timeout = max(
        0.1,
        float(
            XHS_CONTINUITY_VERIFY_WAIT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        ),
    )
    poll = max(
        0.01,
        float(
            XHS_CONTINUITY_VERIFY_POLL_SECONDS
            if poll_seconds is None
            else poll_seconds
        ),
    )
    redirect_url = page.url
    captcha_url = "https://www.xiaohongshu.com/website-login/captcha?" + urlencode(
        {
            "redirectPath": redirect_url,
            "verifyUuid": verify_uuid,
            "verifyType": verify_type,
            "verifyBiz": str(verify_biz),
        }
    )
    started = time.monotonic()
    event: dict[str, Any] = {
        "stage": "api_captcha",
        "started_at": utc_now(),
        "finished_at": None,
        "status": "waiting_for_operator",
        "initial_challenge": "api_captcha",
        "challenge": "api_captcha",
        "verify_type": verify_type,
        "verify_uuid": verify_uuid,
        "verify_biz": verify_biz,
        "redirect_url": redirect_url,
        "captcha_url": captcha_url,
        "timeout_seconds": round(timeout, 3),
    }
    verification_events = evidence.setdefault("operator_verification_events", [])
    verification_events.append(event)
    evidence["operator_verification_events"] = verification_events[-50:]
    write_evidence(path, evidence)

    try:
        await page.goto(captcha_url, wait_until="domcontentloaded", timeout=30_000)
        await page.bring_to_front()
    except Exception as exc:
        event.update(
            {
                "finished_at": utc_now(),
                "status": "failed",
                "error": f"captcha_navigation_failed:{type(exc).__name__}:{exc}",
            }
        )
        evidence.update(
            {
                "status": "failed",
                "challenge": "captcha_or_verify",
                "visible_markers": {"captcha_or_verify": True},
            }
        )
        write_evidence(path, evidence)
        raise RuntimeError("xhs_api_captcha_page_unavailable") from exc

    latest_text = ""
    latest_markers: dict[str, bool] = {"captcha_or_verify": True}
    ready_observations = 0
    while True:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            event.update(
                {
                    "finished_at": utc_now(),
                    "status": "failed",
                    "challenge": "api_captcha",
                    "visible_markers": latest_markers,
                    "visible_text_sample": latest_text,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "error": "operator_verification_timeout",
                    "url": page.url,
                }
            )
            evidence.update(
                {
                    "status": "failed",
                    "challenge": "captcha_or_verify",
                    "visible_markers": latest_markers,
                    "visible_text_sample": latest_text,
                }
            )
            write_evidence(path, evidence)
            raise RuntimeError("xhs_api_captcha_verification_timeout")

        await asyncio.sleep(min(poll, remaining))
        latest_text, latest_markers = await visible_page_state(page)
        current_url = page.url
        visible = visible_challenge(latest_markers)
        event.update(
            {
                "challenge": visible or "api_captcha",
                "visible_markers": latest_markers,
                "visible_text_sample": latest_text,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "url": current_url,
            }
        )
        redirect = urlparse(redirect_url)
        current = urlparse(current_url)
        returned_to_redirect_route = bool(
            current.hostname == redirect.hostname
            and current.path.rstrip("/") == redirect.path.rstrip("/")
        )
        page_ready = bool(
            "/website-login/captcha" not in current_url
            and returned_to_redirect_route
            and latest_text.strip()
            and not latest_markers.get("captcha_or_verify")
            and not latest_markers.get("login_required")
        )
        ready_observations = ready_observations + 1 if page_ready else 0
        event["ready_observations"] = ready_observations
        if ready_observations >= 2:
            event.update(
                {
                    "finished_at": utc_now(),
                    "status": "completed",
                    "challenge": "",
                }
            )
            write_evidence(path, evidence)
            return event
        if visible in {"rate_limited", "blocked"}:
            event.update(
                {
                    "finished_at": utc_now(),
                    "status": "failed",
                    "error": f"{visible}_during_operator_verification",
                }
            )
            evidence.update(
                {
                    "status": "failed",
                    "challenge": visible,
                    "visible_markers": latest_markers,
                    "visible_text_sample": latest_text,
                }
            )
            write_evidence(path, evidence)
            raise RuntimeError(f"{visible}_detected_during_xhs_api_captcha")


def persist_xhs_continuity_failure(
    evidence: dict[str, Any],
    *,
    evidence_path: str | Path,
    stage: str,
    started_at: str,
    challenge: str,
    text_sample: str,
    markers: dict[str, bool],
    initial_text: str,
    initial_markers: dict[str, bool],
    events: list[dict[str, Any]],
) -> None:
    continuity = {
        "stage": stage,
        "started_at": started_at,
        "finished_at": utc_now(),
        "status": "failed",
        "events": events,
        "initial_visible_markers": initial_markers,
        "initial_visible_text_sample": initial_text,
        "visible_markers": markers,
        "visible_text_sample": text_sample,
        "challenge": challenge,
    }
    continuity_events = evidence.setdefault("continuity_events", [])
    continuity_events.append(continuity)
    evidence["continuity_events"] = continuity_events[-50:]
    evidence.update(
        {
            "status": "failed",
            "challenge": challenge,
            "visible_markers": markers,
            "visible_text_sample": text_sample,
        }
    )
    write_evidence(evidence_path, evidence)


async def run_xhs_continuity_behavior(
    page: Page,
    *,
    evidence_path: str | Path,
    stage: str,
) -> dict[str, Any]:
    path = Path(evidence_path).expanduser()
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot update XHS continuity evidence: {exc}") from exc
    if evidence.get("profile") != "xhs_guarded" or not behavior_evidence_valid(evidence):
        raise RuntimeError("XHS continuity behavior requires completed xhs_guarded evidence")

    started_at = utc_now()
    initial_text, initial_markers = await visible_page_state(page)
    observed_initial_text = initial_text
    observed_initial_markers = initial_markers
    challenge = visible_challenge(initial_markers)
    if challenge:
        if challenge in {"captcha_or_verify", "login_required"}:
            initial_text, initial_markers, _ = await wait_for_xhs_continuity_verification(
                page,
                evidence=evidence,
                evidence_path=path,
                stage=stage,
                initial_challenge=challenge,
                initial_text=initial_text,
                initial_markers=initial_markers,
            )
        else:
            persist_xhs_continuity_failure(
                evidence,
                evidence_path=path,
                stage=stage,
                started_at=started_at,
                challenge=challenge,
                text_sample=initial_text,
                markers=initial_markers,
                initial_text=observed_initial_text,
                initial_markers=observed_initial_markers,
                events=[],
            )
            raise RuntimeError(f"{challenge}_detected_during_xhs_continuity:{stage}")

    events: list[dict[str, Any]] = []
    profile = load_behavior_profile("xhs_guarded", strict=True)
    await human_pause(page, (1.5, 4.5), reason=f"continuity_{stage}", log=events)
    await random_mouse_moves(page, profile, events)
    final_text, final_markers = await visible_page_state(page)
    final_challenge = visible_challenge(final_markers)
    if final_challenge in {"captcha_or_verify", "login_required"}:
        final_text, final_markers, _ = await wait_for_xhs_continuity_verification(
            page,
            evidence=evidence,
            evidence_path=path,
            stage=stage,
            initial_challenge=final_challenge,
            initial_text=final_text,
            initial_markers=final_markers,
        )
        final_challenge = ""
    elif final_challenge:
        persist_xhs_continuity_failure(
            evidence,
            evidence_path=path,
            stage=stage,
            started_at=started_at,
            challenge=final_challenge,
            text_sample=final_text,
            markers=final_markers,
            initial_text=observed_initial_text,
            initial_markers=observed_initial_markers,
            events=events,
        )
        raise RuntimeError(f"{final_challenge}_detected_during_xhs_continuity:{stage}")
    continuity = {
        "stage": stage,
        "started_at": started_at,
        "finished_at": utc_now(),
        "status": "completed" if not final_challenge else "failed",
        "events": events,
        "initial_visible_markers": observed_initial_markers,
        "initial_visible_text_sample": observed_initial_text,
        "visible_markers": final_markers,
        "visible_text_sample": final_text,
        "challenge": final_challenge,
        "url": page.url,
    }
    continuity_events = evidence.setdefault("continuity_events", [])
    continuity_events.append(continuity)
    evidence["continuity_events"] = continuity_events[-50:]
    write_evidence(path, evidence)
    return continuity


async def _first_visible_locator(page: Page, selectors: tuple[str, ...]):
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(min(await locator.count(), 8)):
            candidate = locator.nth(index)
            try:
                if await candidate.is_visible(timeout=1_000):
                    return selector, candidate
            except Exception:
                continue
    return "", None


async def _like_control_state(locator: Any) -> dict[str, Any]:
    return await locator.evaluate(
        """element => {
            const control = element.closest('button,[role="button"]') || element;
            const className = String(control.className || element.className || '');
            const activeDescendant = Boolean(
                control.querySelector('[class*="liked"], [class*="active"], [data-state="active"], [aria-pressed="true"]')
            );
            const useElement = control.querySelector('use');
            return {
                aria_pressed: control.getAttribute('aria-pressed') || '',
                aria_label: control.getAttribute('aria-label') || '',
                title: control.getAttribute('title') || '',
                data_state: control.getAttribute('data-state') || '',
                class_name: className.slice(0, 240),
                text: String(control.innerText || element.innerText || '').trim().slice(0, 120),
                icon_ref: useElement ? (useElement.getAttribute('href') || useElement.getAttribute('xlink:href') || '') : '',
                active_descendant: activeDescendant,
            };
        }"""
    )


def _looks_liked(state: dict[str, Any]) -> bool:
    pressed = str(state.get("aria_pressed") or "").lower()
    data_state = str(state.get("data_state") or "").lower()
    classes = str(state.get("class_name") or "").lower()
    label = f"{state.get('aria_label') or ''} {state.get('title') or ''}".strip()
    icon_ref = str(state.get("icon_ref") or "").lower()
    return (
        pressed == "true"
        or data_state in {"active", "checked", "liked", "selected"}
        or bool(state.get("active_descendant"))
        or bool(re.search(r"(?:^|[-_\s])(liked|selected)(?:$|[-_\s])", classes))
        or "取消点赞" in label
        or "liked" in icon_ref
    )


def _looks_unliked(state: dict[str, Any]) -> bool:
    pressed = str(state.get("aria_pressed") or "").lower()
    data_state = str(state.get("data_state") or "").lower()
    classes = str(state.get("class_name") or "").lower()
    label = f"{state.get('aria_label') or ''} {state.get('title') or ''}".strip()
    icon_ref = str(state.get("icon_ref") or "").lower()
    return (
        pressed == "false"
        or data_state in {"inactive", "unchecked", "unliked"}
        or bool(re.search(r"(?:^|[-_\s])(unliked|inactive)(?:$|[-_\s])", classes))
        or ("点赞" in label and "取消点赞" not in label)
        or bool(re.search(r"(?:^|[#/_-])like(?:$|[?#/_-])", icon_ref))
    )


async def _run_xhs_comment_scroll(page: Page, profile: Any, events: list[dict[str, Any]]) -> dict[str, Any]:
    await random_mouse_moves(page, profile, events)
    selector = ""
    locator = None
    for _ in range(4):
        selector, locator = await _first_visible_locator(page, XHS_COMMENT_SELECTORS)
        if locator is not None:
            break
        await human_scroll(page, profile, intent="find_comments", max_passes=1, log=events)
    if locator is None:
        return {"status": "failed", "action": "comment-scroll", "reason": "comment_container_not_found"}
    await locator.scroll_into_view_if_needed(timeout=5_000)
    await human_pause(page, (2.0, 8.0), reason="comments_arrival", log=events)
    await human_scroll(page, profile, intent="comments", max_passes=2, log=events)
    return {"status": "completed", "action": "comment-scroll", "selector": selector}


async def _run_xhs_like_once(page: Page, profile: Any, events: list[dict[str, Any]]) -> dict[str, Any]:
    await random_mouse_moves(page, profile, events)
    selector, locator = await _first_visible_locator(page, XHS_LIKE_SELECTORS)
    if locator is None:
        return {"status": "failed", "action": "like-one", "reason": "like_control_not_found"}
    before = await _like_control_state(locator)
    if _looks_liked(before):
        return {
            "status": "skipped_already_liked",
            "action": "like-one",
            "selector": selector,
            "before": before,
        }
    if not _looks_unliked(before):
        return {
            "status": "skipped_ambiguous_state",
            "action": "like-one",
            "selector": selector,
            "before": before,
        }
    await locator.scroll_into_view_if_needed(timeout=5_000)
    await locator.hover(timeout=3_000)
    await human_pause(page, (1.2, 4.5), reason="before_like", log=events)
    await locator.click(timeout=5_000, delay=REQUEST_RANDOM.randint(80, 220))
    await human_pause(page, (1.5, 4.0), reason="after_like", log=events)
    after = await _like_control_state(locator)
    verified = _looks_liked(after) or any(
        before.get(key) != after.get(key)
        for key in (
            "aria_pressed",
            "data_state",
            "class_name",
            "text",
            "icon_ref",
            "active_descendant",
        )
    )
    return {
        "status": "completed" if verified else "clicked_unverified",
        "action": "like-one",
        "selector": selector,
        "before": before,
        "after": after,
    }


async def run_xhs_post_interaction(
    page: Page,
    *,
    evidence_path: str | Path,
    requested_mode: str,
    note_id: str,
) -> dict[str, Any]:
    if requested_mode not in XHS_POST_INTERACTION_MODES:
        raise ValueError(f"unsupported XHS post interaction: {requested_mode}")
    path = Path(evidence_path).expanduser()
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot update XHS post interaction evidence: {exc}") from exc
    if evidence.get("profile") != "xhs_guarded" or not behavior_evidence_valid(evidence):
        raise RuntimeError("XHS post interaction requires completed xhs_guarded evidence")

    selected_mode = REQUEST_RANDOM.choice(("comment-scroll", "like-one")) if requested_mode == "random" else requested_mode
    events: list[dict[str, Any]] = []
    started_at = utc_now()
    initial_text, initial_markers = await visible_page_state(page)
    blocked = any(initial_markers.values())
    result: dict[str, Any]
    error = ""
    if blocked:
        result = {"status": "failed", "action": selected_mode, "reason": "visible_page_blocked"}
    else:
        try:
            profile = load_behavior_profile("xhs_guarded", strict=True)
            await human_pause(page, (8.0, 24.0), reason="post_interaction_arrival", log=events)
            if selected_mode == "comment-scroll":
                result = await _run_xhs_comment_scroll(page, profile, events)
            else:
                result = await _run_xhs_like_once(page, profile, events)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            result = {"status": "failed", "action": selected_mode, "reason": error}

    visible_text, visible_markers = await visible_page_state(page)
    final_blocked = any(visible_markers.values())
    if final_blocked:
        result = {
            "status": "failed",
            "action": selected_mode,
            "reason": "visible_page_blocked_after_interaction",
        }
    safe_note_id = re.sub(r"[^a-zA-Z0-9_-]", "_", note_id)[:80] or "unknown"
    screenshot_path = path.with_name(f"{path.stem}.interaction.{safe_note_id}.png")
    artifact_error = ""
    try:
        await page.screenshot(path=str(screenshot_path), full_page=False, timeout=10_000, animations="disabled")
    except Exception as exc:
        artifact_error = f"{type(exc).__name__}: {exc}"

    interaction = {
        "requested_mode": requested_mode,
        "selected_mode": selected_mode,
        "note_id": note_id,
        "url": page.url,
        "started_at": started_at,
        "finished_at": utc_now(),
        "status": result.get("status", "failed"),
        "result": result,
        "events": events,
        "initial_visible_markers": initial_markers,
        "initial_visible_text_sample": initial_text,
        "visible_markers": visible_markers,
        "visible_text_sample": visible_text,
        "screenshot": str(screenshot_path) if not artifact_error else "",
        "artifact_error": artifact_error,
        "error": error,
    }
    interactions = evidence.setdefault("post_interactions", [])
    interactions.append(interaction)
    evidence["post_interactions"] = interactions[-20:]
    write_evidence(path, evidence)
    return interaction


async def run_page_behavior(
    page: Page,
    *,
    platform_key: str,
    evidence_path: str | Path,
    profile_name: str = "social_high_risk",
) -> dict[str, Any]:
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
            page_readiness = await wait_for_xhs_search_ready(page, events)
            behavior_ready = bool(page_readiness.get("ready"))
            if not behavior_ready:
                initial_visible_text_sample = str(page_readiness.get("visible_text_sample") or "")
                initial_visible_markers = dict(page_readiness.get("markers") or {})
                visible_text_sample = initial_visible_text_sample
                visible_markers = initial_visible_markers
                reason = str(page_readiness.get("reason") or "search_results_not_ready")
                if reason not in {
                    "platform_security_limit",
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
