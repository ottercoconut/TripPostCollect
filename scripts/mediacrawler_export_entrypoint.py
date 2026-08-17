#!/usr/bin/env python
"""Run MediaCrawler with TripPostCollect's fail-closed export sanitizer."""

from __future__ import annotations

import os
import runpy
import sys
from asyncio import Semaphore, gather
from functools import wraps
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
MEDIACRAWLER_ROOT = ROOT / "tools" / "MediaCrawler"
EXPORT_METHODS = (
    "write_to_csv",
    "write_to_jsonl",
    "write_single_item_to_json",
)


def sanitize_export_item(item: dict[str, Any]) -> dict[str, Any]:
    """Sanitize one item immediately before MediaCrawler serializes it."""
    if os.environ.get("TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS") != "1":
        raise RuntimeError("TripPostCollect MediaCrawler export sanitizer is not enabled")
    source_text = str(SOURCE_ROOT)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    from trippostcollect.records.sanitization import sanitize_author_avatar_data

    sanitized = sanitize_author_avatar_data(item).value
    if not isinstance(sanitized, dict):
        raise RuntimeError("sanitized MediaCrawler item must remain an object")
    return sanitized


def install_export_hook() -> None:
    """Wrap every MediaCrawler structured-data writer before loading its main module."""
    media_root_text = str(MEDIACRAWLER_ROOT)
    if media_root_text not in sys.path:
        sys.path.insert(0, media_root_text)
    from tools.async_file_writer import AsyncFileWriter

    if getattr(AsyncFileWriter, "_trippostcollect_avatar_sanitizer", False):
        return
    for method_name in EXPORT_METHODS:
        original = getattr(AsyncFileWriter, method_name)

        @wraps(original)
        async def sanitized_writer(
            self: Any,
            item: dict[str, Any],
            item_type: str,
            *,
            _original: Any = original,
        ) -> Any:
            return await _original(self, sanitize_export_item(item), item_type)

        setattr(AsyncFileWriter, method_name, sanitized_writer)
    AsyncFileWriter._trippostcollect_avatar_sanitizer = True


def _repair_exception_is_blocking(crawler: Any, exc: BaseException) -> bool:
    """Keep platform-wide failures fatal while isolating one repair candidate."""

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


def install_xhs_repair_resilience() -> None:
    """Make specified-note repair skip ordinary candidate failures independently."""

    if os.environ.get("TRIPPOSTCOLLECT_XHS_REPAIR") != "1":
        return
    from media_platform.xhs import core as xhs_core

    crawler_class = xhs_core.XiaoHongShuCrawler
    if getattr(crawler_class, "_trippostcollect_repair_resilience", False):
        return

    async def resilient_get_specified_notes(self: Any) -> None:
        detail_tasks = []
        detail_semaphore = Semaphore(xhs_core.config.MAX_CONCURRENCY_NUM)
        for full_note_url in xhs_core.config.XHS_SPECIFIED_NOTE_URL_LIST:
            note_url_info = xhs_core.parse_note_info_from_note_url(full_note_url)
            xhs_core.utils.logger.info(
                "[TripPostCollect] Queue specified XHS repair note: "
                f"{note_url_info.note_id}"
            )
            detail_tasks.append(
                self.get_note_detail_async_task(
                    note_id=note_url_info.note_id,
                    xsec_source=note_url_info.xsec_source,
                    xsec_token=note_url_info.xsec_token,
                    semaphore=detail_semaphore,
                )
            )

        note_details = await gather(*detail_tasks, return_exceptions=True)
        note_ids: list[str] = []
        xsec_tokens: list[str] = []
        for note_detail in note_details:
            if isinstance(note_detail, BaseException):
                if not isinstance(note_detail, Exception):
                    raise note_detail
                if _repair_exception_is_blocking(self, note_detail):
                    raise note_detail
                xhs_core.utils.logger.warning(
                    "[TripPostCollect] Skip failed XHS repair detail candidate: "
                    f"{type(note_detail).__name__}"
                )
                continue
            if not note_detail:
                continue
            if self.is_video_note(note_detail):
                xhs_core.utils.logger.info(
                    "[TripPostCollect] Skip video XHS repair candidate: "
                    f"{note_detail.get('note_id')}"
                )
                continue
            try:
                await self.enrich_note_creator(note_detail)
                await xhs_core.xhs_store.update_xhs_note(note_detail)
                await self.get_notice_media(note_detail)
            except Exception as exc:
                if _repair_exception_is_blocking(self, exc):
                    raise
                xhs_core.utils.logger.warning(
                    "[TripPostCollect] Skip failed XHS repair enrichment candidate: "
                    f"{type(exc).__name__}"
                )
                continue
            note_ids.append(str(note_detail.get("note_id") or ""))
            xsec_tokens.append(str(note_detail.get("xsec_token") or ""))
        await self.batch_get_note_comments(note_ids, xsec_tokens)

    crawler_class.get_specified_notes = resilient_get_specified_notes
    crawler_class._trippostcollect_repair_resilience = True


def main() -> None:
    main_path = MEDIACRAWLER_ROOT / "main.py"
    if not main_path.is_file():
        raise RuntimeError(f"MediaCrawler main module is missing: {main_path}")
    install_export_hook()
    install_xhs_repair_resilience()
    runpy.run_path(str(main_path), run_name="__main__")


if __name__ == "__main__":
    main()
