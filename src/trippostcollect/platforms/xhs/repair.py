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

"""小红书历史详情修复：批次 gather、候选失败分流与运行级阻断判定（按根异常类型，不靠模块路径）。"""

import os
from asyncio import Semaphore, gather
import logging
from typing import Any

from playwright.async_api import Error as PlaywrightError

from trippostcollect.platforms.xhs.errors import IPBlockError, PlatformRuntimeError
from trippostcollect.platforms.xhs.parser import parse_note_info_from_note_url

logger = logging.getLogger("MediaCrawler")


def _repair_exception_is_blocking(crawler: Any, exc: BaseException) -> bool:
    request_failure = exc
    request_failure_factory = getattr(crawler, "_request_failure_exception", None)
    if callable(request_failure_factory):
        try:
            request_failure = request_failure_factory(exc)
        except Exception:
            request_failure = exc
    blocking_types = (IPBlockError, PlatformRuntimeError)
    if blocking_types and isinstance(request_failure, blocking_types):
        return True
    if isinstance(request_failure, PlaywrightError) or isinstance(exc, PlaywrightError):
        return True
    text = str(request_failure or exc).lower()
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
) -> dict[str, Any]:
    normalized_code = str(error_code or getattr(exc, "code", "") or "candidate_failed")
    if retryable is None:
        retryable = normalized_code not in {
            "note_not_found",
            "video_skipped",
            "image_decode_failed",
            "unsupported_media_type",
        }
    error_type = type(exc).__name__ if exc is not None else ""
    return {
        "platform": "xhs",
        "identity": f"xhs:id:{note_id}",
        "platform_post_id": str(note_id),
        "batch": int(batch),
        "failure_scope": str(failure_scope),
        "detail": error_type,
        "error_type": error_type,
        "error_code": normalized_code,
        "attempts": max(1, int(getattr(exc, "attempts", attempts) or attempts)),
        "retryable": bool(retryable),
    }


def _xhs_repair_failure_scope(exc: BaseException) -> str:
    name = type(exc).__name__
    if name == "XHSCreatorProfileUnavailable":
        return "creator"
    if name in {"XHSImageDownloadError", "ImageDownloadFetchError"}:
        return "image"
    return "candidate"


def _xhs_repair_blocker(crawler: Any, exc: BaseException) -> dict[str, str]:
    request_failure = exc
    request_failure_factory = getattr(crawler, "_request_failure_exception", None)
    if callable(request_failure_factory):
        try:
            request_failure = request_failure_factory(exc)
        except Exception:
            request_failure = exc
    error_type = type(request_failure).__name__
    code = str(getattr(request_failure, "code", "") or "")
    text = str(request_failure).lower()
    if not code and error_type == "IPBlockError":
        code = "ip_blocked"
    if not code and (
        isinstance(request_failure, PlaywrightError) or isinstance(exc, PlaywrightError)
    ):
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


async def run_xhs_repair(self: Any) -> None:
    """历史详情修复：同一 BrowserContext 内按批 gather，逐候选分流，运行级阻断立即停止。"""
    note_targets = [
        parse_note_info_from_note_url(full_note_url)
        for full_note_url in self.settings.XHS_SPECIFIED_NOTE_URL_LIST
    ]
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
    note_ids: list[str] = []
    xsec_tokens: list[str] = []
    self.ports.write_repair_report(report)
    try:
        for offset in range(0, len(note_targets), batch_size):
            batch_number = offset // batch_size + 1
            batch_targets = note_targets[offset : offset + batch_size]
            detail_semaphore = Semaphore(self.settings.MAX_CONCURRENCY_NUM)
            detail_tasks = []
            for note_url_info in batch_targets:
                logger.info(
                    "[TripPostCollect] Queue specified XHS repair note: "
                    f"batch={batch_number} note_id={note_url_info.note_id}"
                )
                detail_tasks.append(
                    self.get_note_detail_async_task(
                        note_id=note_url_info.note_id,
                        xsec_source=note_url_info.xsec_source,
                        xsec_token=note_url_info.xsec_token,
                        semaphore=detail_semaphore,
                    )
                )

            batch_result = {
                "batch": batch_number,
                "batch_no": batch_number,
                "target_count": len(batch_targets),
                "batch_complete": True,
                "successful_ids": [],
                "failed_ids": [],
            }
            note_details = await gather(*detail_tasks, return_exceptions=True)
            for note_url_info, note_detail in zip(batch_targets, note_details, strict=True):
                note_id = str(note_url_info.note_id)
                if isinstance(note_detail, BaseException):
                    if not isinstance(note_detail, Exception):
                        raise note_detail
                    if _repair_exception_is_blocking(self, note_detail):
                        raise note_detail
                    failure = _xhs_repair_failure(
                        note_id=note_id,
                        batch=batch_number,
                        failure_scope="detail",
                        exc=note_detail,
                    )
                    report["candidate_failures"].append(failure)
                    batch_result["failed_ids"].append(note_id)
                    logger.warning(
                        "[TripPostCollect] Skip failed XHS repair detail candidate: "
                        f"note_id={note_id} error={failure['error_code']} "
                        f"attempts={failure['attempts']}"
                    )
                    continue
                if not note_detail:
                    failure = _xhs_repair_failure(
                        note_id=note_id,
                        batch=batch_number,
                        failure_scope="detail",
                        error_code="note_not_found",
                        retryable=False,
                    )
                    report["candidate_failures"].append(failure)
                    batch_result["failed_ids"].append(note_id)
                    continue
                if self.is_video_note(note_detail):
                    failure = _xhs_repair_failure(
                        note_id=note_id,
                        batch=batch_number,
                        failure_scope="content_policy",
                        error_code="video_skipped",
                        retryable=False,
                    )
                    report["candidate_failures"].append(failure)
                    batch_result["failed_ids"].append(note_id)
                    logger.info(
                        "[TripPostCollect] Skip video XHS repair candidate: "
                        f"{note_id}"
                    )
                    continue
                try:
                    await self.enrich_note_creator(note_detail)
                    await self.get_notice_media(note_detail)
                    await self.update_xhs_note(note_detail)
                except Exception as exc:
                    if _repair_exception_is_blocking(self, exc):
                        raise
                    failure = _xhs_repair_failure(
                        note_id=note_id,
                        batch=batch_number,
                        failure_scope=_xhs_repair_failure_scope(exc),
                        exc=exc,
                    )
                    report["candidate_failures"].append(failure)
                    batch_result["failed_ids"].append(note_id)
                    logger.warning(
                        "[TripPostCollect] Skip failed XHS repair candidate: "
                        f"note_id={note_id} scope={failure['failure_scope']} "
                        f"error={failure['error_code']} attempts={failure['attempts']}"
                    )
                    continue
                note_ids.append(note_id)
                xsec_tokens.append(str(note_detail.get("xsec_token") or ""))
                report["successful_ids"].append(note_id)
                batch_result["successful_ids"].append(note_id)
            report["batches"].append(batch_result)
            self.ports.write_repair_report(report)
    except BaseException as exc:
        report["runtime_blocker"] = _xhs_repair_blocker(self, exc)
        raise
    finally:
        report["successful_count"] = len(report["successful_ids"])
        report["failed_count"] = len(report["candidate_failures"])
        self.ports.write_repair_report(report)
