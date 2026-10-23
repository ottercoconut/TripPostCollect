# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/core.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

# TripPostCollect：T09 迁入根平台；来源 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303，许可见 resources/licenses/MediaCrawler-LICENSE。

"""小红书特有行为：搜索就绪、连续性与前端验证等待、平台限制证据、显式互动。

原 scripts/mediacrawler_behavior.py 的小红书函数与 fork 行为桥中的小红书入口合并于此；
证据写出与启用判定经注入端口提供，平台层不导入应用或产物实现。
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict
from urllib.parse import urlparse

from playwright.async_api import Page

from trippostcollect.application.contracts import XhsBehaviorPorts
from trippostcollect.platforms.xhs.errors import PlatformRuntimeError, XHSLoginExpired, XHSNetworkRecoveryTimeout
from trippostcollect.platforms.xhs.page_scripts import (
    dom_like_control_state,
    dom_scroll_metrics,
    dom_search_ready_counts,
)
from trippostcollect.runtime.browser import CDPBrowserLifecycleError
from trippostcollect.runtime import behavior as _behavior
from trippostcollect.runtime.behavior import (
    REQUEST_RANDOM,
    behavior_evidence_valid,
    visible_challenge,
    visible_page_state,
)
from trippostcollect.runtime.helpers import utc_now
from trippostcollect.runtime.human_pointer import Box, pointer_for
from trippostcollect.xhs.operator_wait import OperatorWaitEpisode, current_operator_wait_signal
from trippostcollect.runtime.human_flow import (
    ListArea,
    human_pause,
    human_scroll,
    load_behavior_profile,
    random_mouse_moves,
)

logger = logging.getLogger("MediaCrawler")


XHS_SEARCH_READY_TIMEOUT_SECONDS = 90.0
XHS_CONTINUITY_VERIFY_WAIT_SECONDS = 600.0
XHS_CONTINUITY_VERIFY_POLL_SECONDS = 2.0
# 搜索两档节奏停顿之后连续性行为的停顿（操作人 2026-10-14 要求全面提速，#79）。
XHS_CONTINUITY_PAUSE_SECONDS = (0.8, 2.0)
XHS_POST_INTERACTION_MODES = frozenset({"comment-scroll", "like-one", "random"})
XHS_OPERATOR_CHALLENGES = frozenset({"captcha_or_verify", "login_required"})
# §3.8.2：461/471 之后等待验证界面出现的上限（不是人工等待），出现后转入共享人工预算。
XHS_VERIFICATION_SURFACE_SECONDS = 30.0
XHS_VERIFICATION_POLL_SECONDS = 1.0
XHS_CAPTCHA_ROUTE = "/website-login/captcha"
# 互动只在当前笔记浮层内进行（§3.4 第 8 条），选择器一律限定在 .note-detail-mask 内。
XHS_OVERLAY_SCOPE = ".note-detail-mask"
XHS_COMMENT_SELECTORS = tuple(
    f"{XHS_OVERLAY_SCOPE} {selector}"
    for selector in (
        "[class*='comments-container']",
        "[class*='comment-list']",
        "[id*='comment']",
        "[class*='comment']",
        "[aria-label*='评论']",
    )
)
XHS_LIKE_SELECTORS = tuple(
    f"{XHS_OVERLAY_SCOPE} {selector}"
    for selector in (
        ".interact-container .like-wrapper",
        "[class*='engage-bar'] [class*='like-wrapper']",
        "[role='button'][aria-label*='点赞']",
        "button[aria-label*='点赞']",
    )
)
# 搜索结果页上光标与滚轮的区域：结果列表 .search-layout 与列表可见区（滚动容器可见区与视口的交集，整页滚动时为
# 视口）的相交矩形，横纵各向内缩这一比例。目标点与滚轮落点因此离开右侧对话栏；顶部固定的搜索框区域与吸顶的标签排
# 另由 `list_top_floor` 排除（#102）。轨迹越过目标至多 115px，小于列表宽度的 20%，不会进入对话栏。
# 扫描滚动与行为动作共用（#88）。
XHS_LIST_AREA_INSET = 0.2
# 列表可见区上部不能落点的两类东西：列表顶部的标签行（scroll_metrics.top_row_rect，里有点了会重新打开“点点AI”对话栏的
# “问点点 ai”标签，#99），以及视口顶部的固定或吸顶遮挡（top_occlusion_bottom：V16 搜索栏与吸顶后的热门筛选标签排，
# 点中标签即带 filter_hot 重新搜索，#102）。区域、卡片可点击带与移开光标的落点的上沿都至少在两者下沿之下这么多像素。
# 轨迹从上面经过可以（悬停无反应），落点不行。
XHS_LIST_TOP_MARGIN_PX = 8.0


class XhsListAreaUnavailable(RuntimeError):
    """搜索结果页上取不到结果列表区域；行为动作不退回整个视口：行为阶段按失败处理，连续性行为跳过随机移动。"""

    def __init__(self, reason: str) -> None:
        super().__init__(f"xhs_list_area_unavailable:{reason}")
        self.reason = reason


def list_occlusion_bottom(metrics: dict[str, Any]) -> float | None:
    """视口顶部固定或吸顶遮挡的下沿（视口纵坐标，见 page_scripts.scroll_metrics）；没有时为 None。"""
    value = metrics.get("top_occlusion_bottom")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def list_top_floor(metrics: dict[str, Any]) -> float | None:
    """标签行与顶部遮挡中较低的下沿加 `XHS_LIST_TOP_MARGIN_PX`（视口纵坐标）；两者都不在列表可见区内时为 None。"""
    bottoms = []
    rect = metrics.get("top_row_rect")
    if isinstance(rect, dict) and float(rect.get("bottom") or 0) > float(rect.get("top") or 0):
        bottoms.append(float(rect["bottom"]))
    occlusion = list_occlusion_bottom(metrics)
    if occlusion is not None:
        bottoms.append(occlusion)
    return max(bottoms) + XHS_LIST_TOP_MARGIN_PX if bottoms else None


def list_area_box(metrics: dict[str, Any]) -> Box | None:
    """scroll_metrics 的 list_rect 按 `XHS_LIST_AREA_INSET` 内缩后的区域，再去掉标签行、顶部遮挡及其下方留边以上的
    部分（见 `list_top_floor`）；没有 list_rect 或去掉后不剩时为 None。"""
    rect = metrics.get("list_rect")
    if not isinstance(rect, dict):
        return None
    left, top, right, bottom = (float(rect.get(key) or 0) for key in ("left", "top", "right", "bottom"))
    if right <= left or bottom <= top:
        return None
    inset_x = (right - left) * XHS_LIST_AREA_INSET
    inset_y = (bottom - top) * XHS_LIST_AREA_INSET
    area_top, area_bottom = top + inset_y, bottom - inset_y
    floor = list_top_floor(metrics)
    if floor is not None:
        area_top = max(area_top, floor)
    if area_bottom <= area_top:
        return None
    return Box(left + inset_x, area_top, right - left - 2 * inset_x, area_bottom - area_top)


async def xhs_list_area(page: Page) -> ListArea:
    """行为阶段与连续性行为在搜索结果页上的区域和列表滚动位置（布局 A 为滚动容器，布局 B 为 window）。"""
    metrics = await dom_scroll_metrics(page)
    box = list_area_box(metrics)
    if box is None:
        raise XhsListAreaUnavailable("list_not_in_view" if metrics.get("layout_present") else "search_layout_missing")
    return ListArea(
        box=box,
        scroll_y=int(metrics.get("scroll_y") or 0),
        scroll_container=str(metrics.get("scroll_container") or ""),
    )


async def record_xhs_platform_security_limit(
    page: Page,
    *,
    evidence_path: str | Path,
    stage: str,
    visible_text_sample: str,
    visible_markers: dict[str, bool],
    write_evidence: Callable[..., Any],
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


async def wait_for_xhs_search_ready(
    page: Page,
    events: list[dict[str, Any]],
    *,
    timeout_seconds: float = XHS_SEARCH_READY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    # 只有验证码等待段是人工等待；就绪轮询本身仍受无进展看门狗约束。
    with current_operator_wait_signal().episode("pre_search_verification") as operator_wait:
        return await _wait_for_xhs_search_ready(
            page,
            events,
            timeout_seconds=timeout_seconds,
            operator_wait=operator_wait,
        )


async def _wait_for_xhs_search_ready(
    page: Page,
    events: list[dict[str, Any]],
    *,
    timeout_seconds: float,
    operator_wait: OperatorWaitEpisode,
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
                counts = await dom_search_ready_counts(page)
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

            if challenge in XHS_OPERATOR_CHALLENGES:
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
                    operator_wait.begin()
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

            if challenge:
                pending_event = (
                    operator_verification_events[-1]
                    if operator_verification_events
                    else None
                )
                if pending_event and pending_event.get("status") == "waiting_for_operator":
                    pending_event.update(
                        {
                            "finished_at": utc_now(),
                            "status": "failed",
                            "challenge": challenge,
                            "visible_markers": last_markers,
                            "visible_text_sample": last_text,
                            "url": page.url,
                            "error": f"{challenge}_during_operator_verification",
                        }
                    )
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
                operator_wait.end()
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
        await asyncio.sleep(min(2.0, max(0.0, deadline - time.monotonic())))


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
    write_evidence: Callable[..., Any],
) -> tuple[str, dict[str, bool], dict[str, Any]]:
    with current_operator_wait_signal().waiting(f"continuity_verification:{stage}"):
        return await _wait_for_xhs_continuity_verification(
            page,
            evidence=evidence,
            evidence_path=evidence_path,
            stage=stage,
            initial_challenge=initial_challenge,
            initial_text=initial_text,
            initial_markers=initial_markers,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            write_evidence=write_evidence,
        )


async def _wait_for_xhs_continuity_verification(
    page: Page,
    *,
    evidence: dict[str, Any],
    evidence_path: str | Path,
    stage: str,
    initial_challenge: str,
    initial_text: str,
    initial_markers: dict[str, bool],
    timeout_seconds: float | None,
    poll_seconds: float | None,
    write_evidence: Callable[..., Any],
) -> tuple[str, dict[str, bool], dict[str, Any]]:
    if initial_challenge not in XHS_OPERATOR_CHALLENGES:
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
        if latest_challenge not in XHS_OPERATOR_CHALLENGES:
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
    write_evidence: Callable[..., Any],
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
    write_evidence: Callable[..., Any],
    pending: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`pending` 为行为证据创建前的暂存：事件记入暂存，失败与验证等待照常写出，行为阶段后并入。"""
    path = Path(evidence_path).expanduser()
    if pending is not None:
        evidence = pending
    else:
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
        if challenge in XHS_OPERATOR_CHALLENGES:
            initial_text, initial_markers, _ = await wait_for_xhs_continuity_verification(
                page,
                evidence=evidence,
                evidence_path=path,
                stage=stage,
                initial_challenge=challenge,
                initial_text=initial_text,
                initial_markers=initial_markers,
                write_evidence=write_evidence,
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
                write_evidence=write_evidence,
            )
            raise RuntimeError(f"{challenge}_detected_during_xhs_continuity:{stage}")

    events: list[dict[str, Any]] = []
    profile = load_behavior_profile("xhs_guarded", strict=True)
    await human_pause(page, XHS_CONTINUITY_PAUSE_SECONDS, reason=f"continuity_{stage}", log=events)
    try:
        await random_mouse_moves(page, profile, events, list_area=xhs_list_area)
    except XhsListAreaUnavailable as exc:
        # 随机移动只是装饰性动作：取不到列表区域（例如人工验证刚结束、列表尚未渲染）时跳过，不退回整个视口，
        # 也不让本轮失败；页面不对由扫描自身的检查处理。
        events.append({"event": "mouse_moves_skipped", "reason": exc.reason})
    final_text, final_markers = await visible_page_state(page)
    final_challenge = visible_challenge(final_markers)
    if final_challenge in XHS_OPERATOR_CHALLENGES:
        final_text, final_markers, _ = await wait_for_xhs_continuity_verification(
            page,
            evidence=evidence,
            evidence_path=path,
            stage=stage,
            initial_challenge=final_challenge,
            initial_text=final_text,
            initial_markers=final_markers,
            write_evidence=write_evidence,
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
            write_evidence=write_evidence,
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
    if pending is None:
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
    return await dom_like_control_state(locator)


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
    # 用滚轮手势滚到评论区；超出上限抛 ElementNotInView，按本次互动失败处理，不退回程序滚动。
    gestures = await pointer_for(page).scroll_into_view(locator)
    events.append({"event": "wheel_into_view", "target": "comments", "gestures": gestures})
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
    pointer = pointer_for(page)
    gestures = await pointer.scroll_into_view(locator)
    events.append({"event": "wheel_into_view", "target": "like", "gestures": gestures})
    # 先取元素框再按轨迹移上去（悬停），停顿后原地按下。
    plan = await pointer.plan_click(Box.from_bounding_box(await locator.bounding_box(timeout=3_000)))
    await pointer.approach(plan)
    await human_pause(page, (1.2, 4.5), reason="before_like", log=events)
    await pointer.settle(plan)
    await pointer.press(plan)
    events.append({"event": "pointer_click", "target": "like", **plan.evidence})
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
    write_evidence: Callable[..., Any],
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


async def run_required_request_pause(
    stage: str,
    minimum: float,
    maximum: float,
    *,
    ports: XhsBehaviorPorts,
    pending: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not ports.enabled():
        raise RuntimeError("required TripPostCollect request pacing is disabled")
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
    profile_name = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE", "").strip()
    if not scripts_dir.is_dir() or not evidence_path:
        raise RuntimeError("required TripPostCollect request pacing configuration is incomplete")

    if pending is not None:
        event = await _behavior.guarded_request_sleep(
            profile_name=profile_name, stage=stage, minimum=minimum, maximum=maximum,
        )
        pending.setdefault("request_pacing_events", []).append(event)
        return event
    return await _behavior.run_guarded_request_pause(
        evidence_path=evidence_path,
        profile_name=profile_name,
        stage=stage,
        minimum=minimum,
        maximum=maximum,
        write_evidence=ports.write_evidence,
    )


async def run_required_continuity_behavior(
    page: Page,
    stage: str,
    *,
    ports: XhsBehaviorPorts,
    pending: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not ports.enabled():
        raise RuntimeError("required TripPostCollect continuity behavior is disabled")
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
    profile_name = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE", "").strip()
    if not scripts_dir.is_dir() or not evidence_path or profile_name != "xhs_guarded":
        raise RuntimeError("required TripPostCollect continuity behavior configuration is incomplete")

    return await run_xhs_continuity_behavior(
        page,
        evidence_path=evidence_path,
        stage=stage,
        write_evidence=ports.write_evidence,
        pending=pending,
    )


PENDING_EVIDENCE_LISTS = ("request_pacing_events", "continuity_events", "operator_verification_events")


def merge_pending_behavior_evidence(pending: dict[str, Any], *, ports: XhsBehaviorPorts) -> None:
    """行为阶段写出证据后，把之前暂存的节奏、连续性与验证事件按时间先后并入（放在已有事件之前）。"""
    if not any(pending.get(key) for key in PENDING_EVIDENCE_LISTS):
        return
    path = Path(os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()).expanduser()
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot merge pending XHS behavior evidence: {exc}") from exc
    if evidence.get("profile") != "xhs_guarded" or not behavior_evidence_valid(evidence):
        raise RuntimeError("pending XHS behavior events require completed xhs_guarded evidence")
    for key, limit in zip(PENDING_EVIDENCE_LISTS, (200, 50, 50)):
        merged = [*(pending.get(key) or []), *(evidence.get(key) or [])]
        if merged:
            evidence[key] = merged[-limit:]
    ports.write_evidence(path, evidence)


def append_operator_verification_event(
    event: dict[str, Any],
    *,
    ports: XhsBehaviorPorts,
    pending: dict[str, Any] | None = None,
) -> None:
    """前端验证的人工等待记录；行为证据创建前记入暂存。"""
    if pending is not None:
        pending.setdefault("operator_verification_events", []).append(event)
        return
    path = Path(os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()).expanduser()
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot record XHS operator verification: {exc}") from exc
    events = evidence.setdefault("operator_verification_events", [])
    events.append(event)
    evidence["operator_verification_events"] = events[-50:]
    ports.write_evidence(path, evidence)


# 业务阶段按需置前的两个判定点（#107）：任何新标签页出现时，以及关闭作者标签页之后的主页面。
XHS_FOCUS_POINTS = ("new_tab", "primary_after_author_tab")


def empty_page_focus() -> dict[str, Any]:
    """行为证据 `page_focus` 的初值：抽样与读取失败按判定点稀疏计数，置前与跳过两个判定点都从 0 计。"""
    return {
        "samples": {},
        "read_failures": {},
        "bring_to_front": dict.fromkeys(XHS_FOCUS_POINTS, 0),
        "bring_to_front_skipped": dict.fromkeys(XHS_FOCUS_POINTS, 0),
    }


def record_xhs_page_focus(page_focus: dict[str, Any], *, ports: XhsBehaviorPorts) -> None:
    """把本轮累计的焦点与可见性计数整体写入行为证据 `page_focus`；行为证据创建之前只在内存累计，之后的下一次写入全量。"""
    raw_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
    path = Path(raw_path).expanduser()
    if not raw_path or not path.is_file():
        return
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot record XHS page focus: {exc}") from exc
    evidence["page_focus"] = page_focus
    ports.write_evidence(path, evidence)


async def inspect_visible_page_state(page: Page) -> tuple[str, dict[str, bool]]:
    """Reuse the project's visible challenge checks without replacing run evidence."""
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    if not scripts_dir.is_dir():
        raise RuntimeError("required TripPostCollect page inspection configuration is incomplete")

    return await visible_page_state(page)


async def record_platform_security_limit(
    page: Page,
    *,
    stage: str,
    visible_text_sample: str,
    visible_markers: dict[str, bool],
    ports: XhsBehaviorPorts,
) -> dict[str, Any]:
    """Persist an XHS terminal account restriction through the project evidence writer."""
    if not ports.enabled():
        raise RuntimeError("required TripPostCollect platform security evidence is disabled")
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
    if not scripts_dir.is_dir() or not evidence_path:
        raise RuntimeError("required TripPostCollect platform security evidence configuration is incomplete")

    return await record_xhs_platform_security_limit(
        page,
        evidence_path=evidence_path,
        stage=stage,
        visible_text_sample=visible_text_sample,
        visible_markers=visible_markers,
        write_evidence=ports.write_evidence,
    )


async def run_requested_post_interaction(
    page: Page,
    *,
    platform_key: str,
    requested_mode: str,
    note_id: str,
    ports: XhsBehaviorPorts,
) -> dict[str, Any]:
    if requested_mode == "none":
        return {"status": "disabled", "platform": platform_key}
    if platform_key != "xhs" or not ports.enabled():
        raise RuntimeError("XHS post interaction requires the enabled project behavior bridge")
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
    if not scripts_dir.is_dir() or not evidence_path:
        raise RuntimeError("required TripPostCollect post interaction configuration is incomplete")

    return await run_xhs_post_interaction(
        page,
        evidence_path=evidence_path,
        requested_mode=requested_mode,
        note_id=note_id,
        write_evidence=ports.write_evidence,
    )


class XhsBehaviorMixin:
    """显式互动入口与前端验证的被动等待；作为 XiaoHongShuCrawler 的 mixin。"""

    async def _verification_surface(self) -> Any:
        """所有页面（含子 frame 路由）中出现验证界面的那一页；终态标记立即上抛，登录界面转轮中恢复。"""
        for page in list(self.browser_context.pages):
            if self._page_is_closed(page):
                continue
            state = await self._popup_checkpoint_state(page)
            self._raise_for_terminal_popup_state(state, reason="frontend_verification")
            markers = set(state.get("manual_markers") or [])
            if markers == {"login_required"}:
                raise XHSLoginExpired("frontend_verification")
            routes = [str(getattr(page, "url", "") or "")]
            routes.extend(str(getattr(frame, "url", "") or "") for frame in getattr(page, "frames", []) or [])
            if markers or any(urlparse(route).path.startswith(XHS_CAPTCHA_ROUTE) for route in routes):
                return page
        return None

    async def _wait_for_frontend_verification(
        self,
        *,
        required: bool,
        data_arrived: Callable[[], bool],
    ) -> bool:
        """§3.8.2 被动等待：30 秒内观察验证界面；出现则置前并在共享人工预算内等它消失。

        完成条件：验证界面消失，且主页面连续两次有可见文本。规则端点（required）30 秒内既无验证界面
        也无原数据时为运行级失败；返回是否出现过验证界面。
        """
        surfaced = None
        polls = max(1, int(XHS_VERIFICATION_SURFACE_SECONDS / XHS_VERIFICATION_POLL_SECONDS))
        for attempt in range(polls):
            surfaced = await self._verification_surface()
            if surfaced is not None or data_arrived():
                break
            if attempt + 1 < polls:
                await self._popup_sleep(XHS_VERIFICATION_POLL_SECONDS)
        if surfaced is None:
            if required and not data_arrived():
                raise PlatformRuntimeError(
                    "XHS frontend verification did not surface", code="xhs_verification_not_surfaced",
                )
            return False
        event = {"stage": "frontend_verification", "started_at": utc_now(), "status": "waiting_for_operator"}
        ticket = self._get_manual_wait_budget().start("frontend_verification")
        clear_observations = 0
        try:
            logger.warning(
                "[XiaoHongShuCrawler] Frontend verification surfaced; waiting for the operator within the "
                f"shared manual budget ({ticket.remaining_seconds:.1f}s left)."
            )
            while True:
                ticket.raise_if_exhausted()
                try:
                    await surfaced.bring_to_front()
                except Exception:
                    pass
                still_surfaced = await self._verification_surface()
                primary = await self._popup_checkpoint_state(self.context_page)
                self._raise_for_terminal_popup_state(primary, reason="frontend_verification")
                if (
                    still_surfaced is None
                    and not primary.get("manual_markers")
                    and str(primary.get("visible_text") or "").strip()
                ):
                    clear_observations += 1
                    if clear_observations >= 2:
                        break
                else:
                    clear_observations = 0
                remaining = ticket.remaining_seconds
                if remaining <= 0:
                    ticket.raise_if_exhausted()
                await self._popup_sleep(min(2.0, remaining))
        finally:
            ticket.close()
        event.update({"status": "completed", "finished_at": utc_now()})
        append_operator_verification_event(
            event, ports=self.ports.behavior, pending=getattr(self, "_pending_behavior_evidence", None),
        )
        return True

    async def _maybe_run_post_interaction(self, note_detail: Dict) -> None:
        """仅在请求了互动、本轮尚未执行且本篇有效时，在当前浮层内执行；不新开标签页、不直接打开地址。"""
        if self.post_interaction_mode == "none" or self.post_interaction_attempted:
            return
        self.post_interaction_attempted = True
        note_id = str(note_detail.get("note_id") or "").strip()
        if not note_id:
            logger.warning("[XiaoHongShuCrawler] Skip requested post interaction: missing note_id")
            return
        page = self.context_page
        try:
            interaction = await run_requested_post_interaction(
                page,
                platform_key="xhs",
                requested_mode=self.post_interaction_mode,
                note_id=note_id,
                ports=self.ports.behavior,
            )
            logger.info(
                "[XiaoHongShuCrawler] Post interaction "
                f"mode={self.post_interaction_mode} note_id={note_id} status={interaction.get('status')}"
            )
            blocked_markers = {
                key
                for markers_key in ("initial_visible_markers", "visible_markers")
                for key, present in (interaction.get(markers_key) or {}).items()
                if present
            }
            if blocked_markers:
                raise RuntimeError(f"xhs_post_interaction_visible_block:{','.join(sorted(blocked_markers))}")
        except RuntimeError as exc:
            # 浏览器生命周期与网络恢复超时是本轮失败；只有互动自身失败才不停抓。
            if isinstance(exc, (CDPBrowserLifecycleError, XHSNetworkRecoveryTimeout)):
                raise
            if str(exc).startswith("xhs_post_interaction_visible_block:"):
                raise
            logger.warning(
                f"[XiaoHongShuCrawler] Requested post interaction failed without stopping crawl: {type(exc).__name__}: {exc}"
            )
        except Exception as exc:
            logger.warning(
                f"[XiaoHongShuCrawler] Requested post interaction failed without stopping crawl: {type(exc).__name__}: {exc}"
            )
