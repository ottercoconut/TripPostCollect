#!/usr/bin/env python
"""Run MediaCrawler with TripPostCollect's fail-closed export sanitizer."""

from __future__ import annotations

import asyncio
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


def install_douyin_browser_detail_fallback() -> None:
    """Use the signed, logged-in browser request when HTTP detail fetch is blocked.

    Douyin's Argus signature is bound to the browser runtime.  The MediaCrawler
    client normally reproduces the request with httpx, which can intermittently
    receive a plain-text ``Blocked by ArgusSecurityPlugin`` response even when
    the browser session is healthy.  A detail-page navigation lets Chromium
    issue the same request with its current cookies and browser-bound signature.
    """

    if os.environ.get("TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_FALLBACK") != "1":
        return
    from media_platform.douyin import client as douyin_client
    from media_platform.douyin.search_safety import decode_douyin_json_body

    client_class = douyin_client.DouYinClient
    if getattr(client_class, "_trippostcollect_browser_detail_fallback", False):
        return
    original_get_video_by_id = client_class.get_video_by_id

    def find_detail(value: Any, aweme_id: str, *, depth: int = 0) -> dict[str, Any] | None:
        if depth > 12:
            return None
        if isinstance(value, dict):
            if str(value.get("aweme_id") or "") == str(aweme_id):
                return value
            for nested in value.values():
                found = find_detail(nested, aweme_id, depth=depth + 1)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = find_detail(nested, aweme_id, depth=depth + 1)
                if found is not None:
                    return found
        return None

    async def browser_detail(self: Any, aweme_id: str) -> Any:
        page = getattr(self, "playwright_page", None)
        if page is None:
            raise RuntimeError("Douyin browser detail fallback requires a Playwright page")
        timeout_ms = max(
            5_000,
            int(os.environ.get("TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_TIMEOUT_MS", "30000")),
        )
        lock = getattr(self, "_trippostcollect_browser_detail_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._trippostcollect_browser_detail_lock = lock
        detail_url = f"https://www.douyin.com/video/{aweme_id}"
        async with lock:
            async with page.expect_response(
                lambda response: "/aweme/v1/web/aweme/detail/" in response.url,
                timeout=timeout_ms,
            ) as response_info:
                await page.goto(detail_url, wait_until="domcontentloaded", timeout=timeout_ms)
            response = await response_info.value
            try:
                payload = decode_douyin_json_body(await response.body())
            except Exception as exc:
                douyin_client.utils.logger.warning(
                    f"[TripPostCollect] Browser Douyin detail body unavailable for aweme_id:{aweme_id}; "
                    f"reason:{type(exc).__name__}"
                )
                payload = None
        detail = find_detail(payload, aweme_id)
        if detail is None:
            try:
                page_state = await page.evaluate(
                    """(targetId) => {
                        const roots = [
                            window.__UNIVERSAL_DATA_FOR_REHYDRATION__,
                            window._ROUTER_DATA,
                            window.__INITIAL_STATE__,
                            window.__NEXT_DATA__,
                        ];
                        const seen = new WeakSet();
                        const walk = (value, depth) => {
                            if (depth > 12 || value === null || value === undefined) return null;
                            if (typeof value !== 'object') return null;
                            if (seen.has(value)) return null;
                            seen.add(value);
                            if (String(value.aweme_id || '') === String(targetId)) return value;
                            for (const nested of Object.values(value)) {
                                const found = walk(nested, depth + 1);
                                if (found) return found;
                            }
                            return null;
                        };
                        for (const root of roots) {
                            const found = walk(root, 0);
                            if (found) return found;
                        }
                        return null;
                    }""",
                    aweme_id,
                )
                detail = find_detail(page_state, aweme_id)
            except Exception as exc:
                douyin_client.utils.logger.warning(
                    f"[TripPostCollect] Browser Douyin page-state extraction failed for aweme_id:{aweme_id}; "
                    f"reason:{type(exc).__name__}"
                )
        if not isinstance(detail, dict) or not detail.get("aweme_id"):
            payload_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
            raise RuntimeError(
                "browser detail response did not contain aweme_detail; "
                f"payload_keys={payload_keys}"
            )
        return detail

    async def resilient_get_video_by_id(self: Any, aweme_id: str) -> Any:
        original_error: Exception | None = None
        try:
            detail = await original_get_video_by_id(self, aweme_id)
            if isinstance(detail, dict) and detail.get("aweme_id"):
                return detail
        except Exception as exc:
            original_error = exc
        try:
            result = await browser_detail(self, aweme_id)
            douyin_client.utils.logger.warning(
                f"[TripPostCollect] Used browser-native Douyin detail fallback for aweme_id:{aweme_id}"
            )
            return result
        except Exception:
            if original_error is not None:
                raise original_error
            raise

    client_class.get_video_by_id = resilient_get_video_by_id
    client_class._trippostcollect_browser_detail_fallback = True


def main() -> None:
    main_path = MEDIACRAWLER_ROOT / "main.py"
    if not main_path.is_file():
        raise RuntimeError(f"MediaCrawler main module is missing: {main_path}")
    install_export_hook()
    install_xhs_repair_resilience()
    install_douyin_browser_detail_fallback()
    runpy.run_path(str(main_path), run_name="__main__")


if __name__ == "__main__":
    main()
