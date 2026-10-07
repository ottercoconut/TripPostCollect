#!/usr/bin/env python
"""Run MediaCrawler with TripPostCollect's fail-closed export sanitizer."""

from __future__ import annotations

import json
import os
import runpy
import sys
from asyncio import Semaphore, gather
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError



ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
# 两站的纯辅助直接重导出；脚本直启时先提供根源码路径。
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from trippostcollect.runtime.helpers import _find_nested_platform_record as _find_nested_platform_record  # noqa: E402
from trippostcollect.platforms.weibo.parser import _find_weibo_detail as _find_weibo_detail  # noqa: E402
from trippostcollect.platforms.weibo.client import _weibo_detail_api_url as _weibo_detail_api_url  # noqa: E402
from trippostcollect.platforms.douyin.parser import (  # noqa: E402
    _douyin_detail_urls as _douyin_detail_urls,
    _find_douyin_detail as _find_douyin_detail,
)

MEDIACRAWLER_ROOT = ROOT / "tools" / "MediaCrawler"
EXPORT_METHODS = (
    "write_to_csv",
    "write_to_jsonl",
    "write_single_item_to_json",
)

def sanitize_export_item(item: dict[str, Any]) -> dict[str, Any]:
    source_text = str(SOURCE_ROOT)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    from trippostcollect.records.sanitization import sanitize_export_item as sanitize

    return sanitize(item)


def install_export_hook() -> None:
    media_root_text = str(MEDIACRAWLER_ROOT)
    if media_root_text not in sys.path:
        sys.path.insert(0, media_root_text)
    from tools.async_file_writer import AsyncFileWriter
    from trippostcollect.records.sanitization import install_export_hook as install_writer_hook

    if install_writer_hook(AsyncFileWriter):
        install_batch_checkpoint_hook()


def install_batch_checkpoint_hook() -> None:
    from trippostcollect.xhs.batch_checkpoint import ENABLED_ENV, publish_batch

    if os.environ.get(ENABLED_ENV) != "1":
        return
    from tools import trippostcollect_adaptive as adaptive
    from trippostcollect.application.events import install_batch_checkpoint_hook as install_legacy_hook

    install_legacy_hook(adaptive, publish_batch=publish_batch)


def _repair_exception_is_blocking(crawler: Any, exc: BaseException) -> bool:
    request_failure = exc
    request_failure_factory = getattr(crawler, "_request_failure_exception", None)
    if callable(request_failure_factory):
        try:
            request_failure = request_failure_factory(exc)
        except Exception:
            request_failure = exc
    exception_types = getattr(sys.modules.get("media_platform.xhs.core"), "__dict__", {})
    blocking_types = tuple(
        value
        for name in ("IPBlockError", "PlatformRuntimeError")
        if isinstance(value := exception_types.get(name), type)
    )
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


def _write_xhs_repair_report(report: dict[str, Any]) -> None:
    raw_path = os.environ.get("TRIPPOSTCOLLECT_XHS_REPAIR_REPORT_PATH", "").strip()
    if not raw_path:
        return
    path = Path(raw_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


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


def install_xhs_repair_resilience() -> None:
    if os.environ.get("TRIPPOSTCOLLECT_XHS_REPAIR") != "1":
        return
    from media_platform.xhs import core as xhs_core

    crawler_class = xhs_core.XiaoHongShuCrawler
    if getattr(crawler_class, "_trippostcollect_repair_resilience", False):
        return

    async def resilient_get_specified_notes(self: Any) -> None:
        note_targets = [
            xhs_core.parse_note_info_from_note_url(full_note_url)
            for full_note_url in xhs_core.config.XHS_SPECIFIED_NOTE_URL_LIST
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
        _write_xhs_repair_report(report)
        try:
            for offset in range(0, len(note_targets), batch_size):
                batch_number = offset // batch_size + 1
                batch_targets = note_targets[offset : offset + batch_size]
                detail_semaphore = Semaphore(xhs_core.config.MAX_CONCURRENCY_NUM)
                detail_tasks = []
                for note_url_info in batch_targets:
                    xhs_core.utils.logger.info(
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
                        xhs_core.utils.logger.warning(
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
                        xhs_core.utils.logger.info(
                            "[TripPostCollect] Skip video XHS repair candidate: "
                            f"{note_id}"
                        )
                        continue
                    try:
                        await self.enrich_note_creator(note_detail)
                        await self.get_notice_media(note_detail)
                        await xhs_core.xhs_store.update_xhs_note(note_detail)
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
                        xhs_core.utils.logger.warning(
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
                _write_xhs_repair_report(report)
            await self.batch_get_note_comments(note_ids, xsec_tokens)
        except BaseException as exc:
            report["runtime_blocker"] = _xhs_repair_blocker(self, exc)
            raise
        finally:
            report["successful_count"] = len(report["successful_ids"])
            report["failed_count"] = len(report["candidate_failures"])
            _write_xhs_repair_report(report)

    crawler_class.get_specified_notes = resilient_get_specified_notes
    crawler_class._trippostcollect_repair_resilience = True


def install_douyin_browser_detail_fallback() -> None:
    # 旧桥只在安装时锁存开关；根 crawler 和 fork client 装配共同消费该值。
    from trippostcollect.application.worker_inputs import douyin_browser_detail_fallback_reader
    from trippostcollect.platforms import entry

    if douyin_browser_detail_fallback_reader()():
        entry._douyin_browser_detail_fallback = True


def install_weibo_browser_detail_fallback() -> None:
    # 保留旧 hook 的读取时点与幂等锁存，回退算法只存在于根客户端。
    if os.environ.get("TRIPPOSTCOLLECT_POST_REPAIR") != "1":
        return
    from media_platform.weibo import core as weibo_core

    weibo_core._post_repair = True


def main() -> None:
    main_path = MEDIACRAWLER_ROOT / "main.py"
    if not main_path.is_file():
        raise RuntimeError(f"MediaCrawler main module is missing: {main_path}")
    source_text = str(SOURCE_ROOT)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    install_export_hook()
    install_xhs_repair_resilience()
    install_douyin_browser_detail_fallback()
    install_weibo_browser_detail_fallback()
    runpy.run_path(str(main_path), run_name="__main__")


if __name__ == "__main__":
    main()
