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

"""小红书历史详情修复（设计 §9）：主页面直开保存的带 token 链接，读页面状态 note_detail_state 投影，翻图截获
图片，点击作者名读作者页；批次、候选失败分流与运行级阻断判定（按异常类型，不靠模块路径）。"""

import logging
import os
from typing import Any, Optional
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

from trippostcollect.platforms.xhs.author import XHS_CREATOR_RUNTIME_FINAL_REASONS
from trippostcollect.platforms.xhs.errors import (
    IPBlockError,
    PlatformRuntimeError,
    XHSCreatorProfileUnavailable,
    XHSImageDownloadError,
    XHSLoginExpired,
    XHSNetworkRecoveryTimeout,
    XhsNoteRedo,
    is_xhs_target_closed_failure,
)
from trippostcollect.platforms.xhs.models import NoteUrlInfo
from trippostcollect.platforms.xhs.overlay import XHS_NOTE_DETAIL_PAUSE_SECONDS, NoteVisit, note_card_failure
from trippostcollect.platforms.xhs.page_scripts import XHS_NOTE_PAGE_ROOTS
from trippostcollect.platforms.xhs.parser import (
    _xhs_image_assets,
    parse_note_info_from_note_url,
    read_note_state,
    xhs_note_state_spec,
)
from trippostcollect.runtime.browser import CDPBrowserLifecycleError
from trippostcollect.runtime.image_retry import is_runtime_blocking_image_error
from trippostcollect.runtime.page_capture import ImageWindow

logger = logging.getLogger("MediaCrawler")

# navigate 动作只记路由模板，不记链接与 token。
XHS_REPAIR_ROUTE = "/explore/<id>"
XHS_REPAIR_STATE_SECONDS = 10.0
XHS_REPAIR_STATE_POLL_SECONDS = 0.5
XHS_REPAIR_STATE_EVALUATE_SECONDS = 5.0
# 失效链接被前端跳转到 /404（F2）。
XHS_REPAIR_UNAVAILABLE_PREFIX = "/404"
# 重试也不会改变结论的候选失败码。
XHS_REPAIR_FINAL_CODES = frozenset(
    {
        "note_not_found",
        "video_skipped",
        "repair_link_unavailable",
        "detail_parse_failed",
        "note_id_mismatch",
        "image_source_unavailable",
        "image_decode_failed",
        "unsupported_media_type",
    }
)
_REPAIR_BLOCKING_TYPES = (
    IPBlockError,
    PlatformRuntimeError,
    CDPBrowserLifecycleError,
    XHSNetworkRecoveryTimeout,
    XHSLoginExpired,
    PlaywrightError,
)


class XhsRepairSkip(Exception):
    """单个 repair 目标的候选失败：记入报告后继续下一个目标。"""

    def __init__(
        self,
        code: str,
        *,
        failure_scope: str = "detail",
        retryable: Optional[bool] = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.failure_scope = failure_scope
        self.retryable = retryable
        self.attempts = attempts


def _repair_exception_is_blocking(exc: BaseException) -> bool:
    # 网络恢复预算耗尽终止本轮，与搜索阶段口径一致；不按候选失败逐个再等预算。
    if isinstance(exc, _REPAIR_BLOCKING_TYPES):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "platform_security_limit",
            "captcha",
            "rate_limit",
            "login_required",
            "ip_blocked",
            "browser_context_closed",
            "browser_runtime_failed",
            "verification_timeout",
        )
    )


def _xhs_repair_failure(
    *,
    note_id: str,
    batch: int,
    failure_scope: str,
    exc: BaseException | None = None,
    error_code: str = "",
    attempts: int = 1,
    retryable: bool | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    normalized_code = str(error_code or getattr(exc, "code", "") or "candidate_failed")
    if retryable is None:
        retryable = normalized_code not in XHS_REPAIR_FINAL_CODES
    error_type = type(exc).__name__ if exc is not None else ""
    return {
        "platform": "xhs",
        "identity": f"xhs:id:{note_id}",
        "platform_post_id": str(note_id),
        "batch": int(batch),
        "failure_scope": str(failure_scope),
        "detail": error_type if detail is None else detail,
        "error_type": error_type,
        "error_code": normalized_code,
        "attempts": max(1, int(getattr(exc, "attempts", attempts) or attempts)),
        "retryable": bool(retryable),
    }


def _xhs_repair_failure_scope(exc: BaseException) -> str:
    if isinstance(exc, XHSCreatorProfileUnavailable):
        return "creator"
    if isinstance(exc, XHSImageDownloadError):
        return "image"
    return "candidate"


def _xhs_repair_candidate_failure(exc: Exception, *, note_id: str, batch: int) -> dict[str, Any]:
    """候选级异常 → 报告失败行；运行级异常原样上抛（图片 401/403/429 转为运行级）。"""
    if isinstance(exc, XhsRepairSkip):
        return _xhs_repair_failure(
            note_id=note_id, batch=batch, failure_scope=exc.failure_scope, error_code=exc.code,
            attempts=exc.attempts, retryable=exc.retryable,
        )
    if isinstance(exc, XHSCreatorProfileUnavailable):
        return _xhs_repair_failure(
            note_id=note_id, batch=batch, failure_scope="creator", exc=exc, error_code="creator_profile_unavailable",
            retryable=not set(exc.reason.split(",")) & XHS_CREATOR_RUNTIME_FINAL_REASONS,
            detail=f"creator_profile_failed:{exc.reason}" if exc.reason else "creator_profile_failed",
        )
    if isinstance(exc, XHSImageDownloadError):
        if is_runtime_blocking_image_error(exc.code):
            raise PlatformRuntimeError(f"XHS image {exc.code}", code=exc.code) from exc
        return _xhs_repair_failure(
            note_id=note_id, batch=batch, failure_scope="image", exc=exc,
            retryable=True if exc.code == "image_response_not_observed" else None,
        )
    if _repair_exception_is_blocking(exc):
        raise exc
    return _xhs_repair_failure(note_id=note_id, batch=batch, failure_scope=_xhs_repair_failure_scope(exc), exc=exc)


def _xhs_repair_blocker(exc: BaseException) -> dict[str, str]:
    error_type = type(exc).__name__
    if isinstance(exc, CDPBrowserLifecycleError):
        # 与失败分类同族：结构化码为 browser_target_closed，具体生命周期码放 reason。
        return {
            "error_type": error_type,
            "error_code": "browser_target_closed",
            "reason": str(exc.event.get("code") or "xhs_cdp_lifecycle_failure"),
        }
    if isinstance(exc, XHSNetworkRecoveryTimeout):
        # 与搜索阶段 runtime_failed/network_recovery_timeout 同一口径：失败族 + 具体原因。
        return {
            "error_type": error_type,
            "error_code": "runtime_failed",
            "reason": "network_recovery_timeout",
        }
    code = str(getattr(exc, "code", "") or "")
    text = str(exc).lower()
    if not code and isinstance(exc, IPBlockError):
        code = "ip_blocked"
    if not code and isinstance(exc, PlaywrightError) and is_xhs_target_closed_failure(exc):
        # 必须先于通用 Playwright 分类，否则 TargetClosedError 会被归为 browser_runtime_failed。
        code = "browser_target_closed"
    if not code and isinstance(exc, PlaywrightError):
        code = "browser_runtime_failed"
    if not code:
        for marker, normalized in (
            ("platform_security_limit", "platform_security_limit"),
            ("captcha", "captcha_detected"),
            ("rate_limit", "rate_limited"),
            ("login_required", "login_required"),
            ("verification_timeout", "verification_timeout"),
            ("browser_context_closed", "browser_target_closed"),
        ):
            if marker in text:
                code = normalized
                break
    return {"error_type": error_type, "error_code": code or "runtime_failed"}


async def _read_repair_note_state(
    crawler: Any, note_id: str, trigger: str,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """有界轮询页面状态投影；接受时写 note_detail 访问（source=page_state）。跳到 /404 时为失效链接。"""
    page = crawler.context_page
    capture = crawler._require_capture()
    deadline = crawler._popup_monotonic() + XHS_REPAIR_STATE_SECONDS
    while True:
        if urlsplit(str(getattr(page, "url", "") or "")).path.startswith(XHS_REPAIR_UNAVAILABLE_PREFIX):
            capture.decision(note_id, "repair_link_unavailable", page_role="primary")
            raise XhsRepairSkip("repair_link_unavailable", retryable=False)
        outcome: dict[str, Any] = {}

        def accept(raw: Any) -> bool:
            outcome["note_card"], reason = read_note_state(raw)
            return reason == "ok"

        try:
            _raw, visit_id = await capture.read_state(
                page,
                "note_detail_state",
                xhs_note_state_spec(note_id),
                stage="note_detail",
                subject_id=note_id,
                trigger=trigger,
                accept=accept,
                timeout=XHS_REPAIR_STATE_EVALUATE_SECONDS,
            )
        except TimeoutError:
            visit_id = None
        except PlaywrightError as exc:
            # 前端跳转（如失效链接跳 /404）会销毁执行上下文：下一轮重读；页面或浏览器关闭照常上抛。
            if crawler._is_target_closed_error(exc) or crawler._page_is_closed(page):
                raise
            visit_id = None
        if visit_id is not None:
            return outcome["note_card"], visit_id
        remaining = deadline - crawler._popup_monotonic()
        if remaining <= 0:
            return None, None
        await crawler._popup_sleep(min(XHS_REPAIR_STATE_POLL_SECONDS, remaining))


async def _repair_note_once(
    crawler: Any, url: str, target: NoteUrlInfo, window: ImageWindow, *, attempt: int,
) -> None:
    """§9.2 第 3 步的一次尝试：直开链接 → 页面状态 → 期望图片 → 详情停顿 → 翻图 → 作者 → 暂存与写出。"""
    capture = crawler._require_capture()
    note_id = str(target.note_id)
    page = crawler.context_page
    trigger = capture.action(
        "navigate", page_role="primary", target={"note_id": note_id, "route": XHS_REPAIR_ROUTE},
    )
    await crawler._goto_with_deadline(page, url, stage="repair_note_detail")
    if not await crawler._wait_for_visible_page_shell(
        page,
        stage="repair_note_detail",
        timeout_seconds=crawler.inputs.search_shell_timeout_seconds(),
    ):
        raise XhsRepairSkip("note_page_not_rendered", retryable=True)
    await crawler._check_primary_before_click("repair_note_detail")
    note_card, detail_visit = await _read_repair_note_state(crawler, note_id, trigger)
    if note_card is None or detail_visit is None:
        raise XhsRepairSkip("note_detail_not_observed", retryable=True)
    failure_code = note_card_failure(note_card, note_id)
    if failure_code:
        raise XhsRepairSkip(failure_code, retryable=False)
    if crawler.is_video_note(note_card):
        capture.decision(note_id, "video_skipped_from_detail", page_role="primary")
        raise XhsRepairSkip("video_skipped", failure_scope="content_policy", retryable=False)
    note = NoteVisit(
        note_id=note_id, note_card=note_card, trigger=trigger, detail_visit=detail_visit, search_visit=None,
    )
    assets = _xhs_image_assets(note_card) if crawler.settings.ENABLE_GET_MEIDAS else []
    if assets:
        window.expect(
            {asset["url"]: asset["source_asset_key"] for asset in assets},
            subject_id=note_id, trigger=trigger, attempt=attempt,
        )
    await crawler._guarded_pause("note_detail", *XHS_NOTE_DETAIL_PAUSE_SECONDS)
    images = await crawler._collect_note_images(note_id, window, assets) if assets else {}
    creator, note.creator_visit = await crawler._enrich_creator_via_author_tab(note)
    note_detail = {
        **note_card,
        "xsec_token": target.xsec_token,
        "xsec_source": target.xsec_source or "pc_search",
        "content_detail_status": "detail_observed",
        "content_detail_source": "note_detail",
        "creator_profile": creator,
    }
    if assets:
        # 截获的图片数必须等于投影的资产数；缺图在暂存前记 image_response_not_observed。
        note.image_visits = await crawler._stage_note_images(note_id, window, assets, images)
    note_detail["content_visit_refs"] = crawler._build_content_visit_refs(note)
    await crawler.update_xhs_note(note_detail)


async def _repair_note(crawler: Any, url: str, target: NoteUrlInfo) -> None:
    """处理一个目标：图片窗口先于 navigate 打开；登录或验证恢复后重开同一链接一次（沿用窗口与已截获字节）。"""
    window = crawler._require_capture().open_image_window("primary")
    try:
        for attempt in (1, 2):
            try:
                await _repair_note_once(crawler, url, target, window, attempt=attempt)
                return
            except XHSLoginExpired:
                if attempt == 2:
                    raise
                if not await crawler._wait_for_midrun_login_recovery():
                    raise PlatformRuntimeError("XHS login remained expired", code="login_required")
            except XhsNoteRedo as exc:
                if attempt == 2:
                    raise XhsRepairSkip("note_redo_interrupted", retryable=True) from exc
            logger.info(f"[XiaoHongShuCrawler.repair] Reopening saved link after recovery: {target.note_id}")
    finally:
        window.close()


async def run_xhs_repair(self: Any) -> None:
    """历史详情修复：同一主页面按批逐个直开保存的链接，逐候选分流，运行级阻断立即停止。"""
    note_urls = list(self.settings.XHS_SPECIFIED_NOTE_URL_LIST)
    note_targets = [parse_note_info_from_note_url(url) for url in note_urls]
    batch_size = max(
        1,
        int(os.environ.get("TRIPPOSTCOLLECT_XHS_REPAIR_BATCH_SIZE", "5")),
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "platform": "xhs",
        "target_count": len(note_targets),
        "batch_size": batch_size,
        "batch_count": (len(note_targets) + batch_size - 1) // batch_size,
        "batches": [],
        "candidate_failures": [],
        "successful_ids": [],
        "runtime_blocker": None,
    }
    # 直开的是整页详情：翻图与点作者改读整页根节点。
    self._note_view_roots = XHS_NOTE_PAGE_ROOTS
    self.ports.write_repair_report(report)
    try:
        for offset in range(0, len(note_targets), batch_size):
            batch_number = offset // batch_size + 1
            batch_items = list(zip(note_urls, note_targets, strict=True))[offset : offset + batch_size]
            batch_result = {
                "batch": batch_number,
                "batch_no": batch_number,
                "target_count": len(batch_items),
                "batch_complete": True,
                "successful_ids": [],
                "failed_ids": [],
            }
            for url, target in batch_items:
                note_id = str(target.note_id)
                logger.info(
                    "[TripPostCollect] Open specified XHS repair note: "
                    f"batch={batch_number} note_id={note_id}"
                )
                try:
                    await _repair_note(self, url, target)
                except Exception as exc:
                    failure = _xhs_repair_candidate_failure(exc, note_id=note_id, batch=batch_number)
                    report["candidate_failures"].append(failure)
                    batch_result["failed_ids"].append(note_id)
                    logger.warning(
                        "[TripPostCollect] Skip failed XHS repair candidate: "
                        f"note_id={note_id} scope={failure['failure_scope']} "
                        f"error={failure['error_code']} attempts={failure['attempts']}"
                    )
                    continue
                report["successful_ids"].append(note_id)
                batch_result["successful_ids"].append(note_id)
            report["batches"].append(batch_result)
            self.ports.write_repair_report(report)
    except BaseException as exc:
        report["runtime_blocker"] = _xhs_repair_blocker(exc)
        raise
    finally:
        report["successful_count"] = len(report["successful_ids"])
        report["failed_count"] = len(report["candidate_failures"])
        self.ports.write_repair_report(report)
