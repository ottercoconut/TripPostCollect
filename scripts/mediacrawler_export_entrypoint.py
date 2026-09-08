#!/usr/bin/env python
"""Run MediaCrawler with TripPostCollect's fail-closed export sanitizer."""

from __future__ import annotations

import asyncio
import json
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


def _find_nested_platform_record(
    value: Any,
    target_id: str,
    *,
    id_keys: tuple[str, ...],
    shape_keys: tuple[str, ...],
    depth: int = 0,
) -> dict[str, Any] | None:
    if depth > 12:
        return None
    if isinstance(value, dict):
        identity = next(
            (
                str(value.get(key) or "")
                for key in id_keys
                if value.get(key) not in (None, "")
            ),
            "",
        )
        if identity == str(target_id) and any(key in value for key in shape_keys):
            return value
        for nested in value.values():
            found = _find_nested_platform_record(
                nested,
                target_id,
                id_keys=id_keys,
                shape_keys=shape_keys,
                depth=depth + 1,
            )
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_nested_platform_record(
                nested,
                target_id,
                id_keys=id_keys,
                shape_keys=shape_keys,
                depth=depth + 1,
            )
            if found is not None:
                return found
    return None


def _douyin_detail_urls(aweme_id: str) -> tuple[str, str]:
    return (
        f"https://www.douyin.com/note/{aweme_id}",
        f"https://www.douyin.com/video/{aweme_id}",
    )


def _find_douyin_detail(value: Any, aweme_id: str) -> dict[str, Any] | None:
    return _find_nested_platform_record(
        value,
        aweme_id,
        id_keys=("aweme_id",),
        shape_keys=("desc", "author", "statistics", "images", "video", "create_time"),
    )


def _find_weibo_detail(value: Any, note_id: str) -> dict[str, Any] | None:
    return _find_nested_platform_record(
        value,
        note_id,
        id_keys=("id", "idstr", "mid"),
        shape_keys=("text", "pics", "user", "created_at", "isLongText"),
    )


def _weibo_detail_api_url(note_id: str) -> str:
    return f"https://m.weibo.cn/statuses/show?id={note_id}"


def sanitize_export_item(item: dict[str, Any]) -> dict[str, Any]:
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
    install_batch_checkpoint_hook()


def install_batch_checkpoint_hook() -> None:
    from trippostcollect.xhs.batch_checkpoint import ENABLED_ENV, publish_batch

    if os.environ.get(ENABLED_ENV) != "1":
        return
    from tools import trippostcollect_adaptive as adaptive

    if getattr(adaptive, "_trippostcollect_batch_checkpoint", False):
        return
    original = adaptive.append_execution_event

    def append_and_checkpoint(event_type: str, details: dict[str, Any]) -> None:
        original(event_type, details)
        if event_type == "adaptive_batch_completed":
            try:
                publish_batch(details)
            except Exception as exc:
                detail = str(exc)
                if not detail.startswith("xhs_batch_checkpoint_"):
                    detail = f"xhs_batch_checkpoint_{type(exc).__name__.lower()}"
                original("xhs_runtime_terminal", {
                    "phase": "batch_checkpoint",
                    "failure_type": "runtime_failed",
                    "stop_reason": "runtime_failed",
                    "stop_detail": detail,
                    "retryable": False,
                })
                raise RuntimeError(detail) from exc

    adaptive.append_execution_event = append_and_checkpoint
    adaptive._trippostcollect_batch_checkpoint = True


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
    if os.environ.get("TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_FALLBACK") != "1":
        return
    from media_platform.douyin import client as douyin_client
    from media_platform.douyin.search_safety import decode_douyin_json_body

    client_class = douyin_client.DouYinClient
    if getattr(client_class, "_trippostcollect_browser_detail_fallback", False):
        return
    original_get_video_by_id = client_class.get_video_by_id

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
        attempts: list[str] = []
        async with lock:
            for detail_url in _douyin_detail_urls(aweme_id):
                payload: Any = None
                response_reason = ""
                try:
                    async with page.expect_response(
                        lambda response: "/aweme/v1/web/aweme/detail/" in response.url,
                        timeout=timeout_ms,
                    ) as response_info:
                        await page.goto(
                            detail_url,
                            wait_until="domcontentloaded",
                            timeout=timeout_ms,
                        )
                    response = await response_info.value
                    try:
                        payload = decode_douyin_json_body(await response.body())
                    except Exception as exc:
                        response_reason = f"response_body:{type(exc).__name__}"
                except PlaywrightError as exc:
                    response_reason = f"navigation_or_response:{type(exc).__name__}"

                detail = _find_douyin_detail(payload, aweme_id)
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
                            const hasDetailShape = [
                                'desc', 'author', 'statistics', 'images', 'video', 'create_time'
                            ].some(key => Object.prototype.hasOwnProperty.call(value, key));
                            if (hasDetailShape && String(value.aweme_id || '') === String(targetId)) return value;
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
                        for (const script of document.querySelectorAll('script[type="application/json"]')) {
                            try {
                                const found = walk(JSON.parse(script.textContent || ''), 0);
                                if (found) return found;
                            } catch (_) {
                                continue;
                            }
                        }
                        return null;
                    }""",
                            aweme_id,
                        )
                        detail = _find_douyin_detail(page_state, aweme_id)
                    except Exception as exc:
                        if not response_reason:
                            response_reason = f"page_state:{type(exc).__name__}"
                if detail is not None:
                    return detail

                route = "note" if "/note/" in detail_url else "video"
                payload_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
                attempts.append(
                    f"{route}:reason={response_reason or 'empty_detail'}:payload_keys={payload_keys}"
                )

        raise RuntimeError(
            "browser detail response did not contain aweme_detail; "
            f"attempts={attempts}"
        )

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
        except Exception as fallback_error:
            if original_error is not None:
                raise fallback_error from original_error
            raise

    client_class.get_video_by_id = resilient_get_video_by_id
    client_class._trippostcollect_browser_detail_fallback = True


def install_weibo_browser_detail_fallback() -> None:
    if os.environ.get("TRIPPOSTCOLLECT_POST_REPAIR") != "1":
        return
    from media_platform.weibo import client as weibo_client

    client_class = weibo_client.WeiboClient
    if getattr(client_class, "_trippostcollect_browser_detail_fallback", False):
        return
    original_get_note_info_by_id = client_class.get_note_info_by_id

    async def resilient_get_note_info_by_id(self: Any, note_id: str) -> dict[str, Any]:
        original_error: Exception | None = None
        try:
            result = await original_get_note_info_by_id(self, note_id)
            original_detail = _find_weibo_detail(result, note_id)
            if original_detail is not None and str(original_detail.get("text") or "").strip():
                return {"mblog": original_detail}
        except weibo_client.DataFetchError as exc:
            original_error = exc

        page = getattr(self, "playwright_page", None)
        if page is None:
            if original_error is not None:
                raise original_error
            raise weibo_client.DataFetchError(
                "get weibo detail err: browser detail fallback has no Playwright page"
            )
        timeout_ms = max(
            5_000,
            int(os.environ.get("TRIPPOSTCOLLECT_WEIBO_BROWSER_DETAIL_TIMEOUT_MS", "30000")),
        )
        lock = getattr(self, "_trippostcollect_browser_detail_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._trippostcollect_browser_detail_lock = lock
        attempts: list[str] = []
        try:
            async with lock:
                request_context = getattr(page, "request", None)
                if request_context is not None:
                    try:
                        api_response = await request_context.get(
                            _weibo_detail_api_url(note_id),
                            headers={"Referer": f"https://m.weibo.cn/detail/{note_id}"},
                            timeout=timeout_ms,
                        )
                        api_status = int(api_response.status)
                        if api_status in {401, 403}:
                            raise weibo_client.PlatformRuntimeError(
                                f"get weibo browser detail HTTP {api_status}",
                                code="login_required",
                            )
                        if api_status == 429:
                            raise weibo_client.PlatformRuntimeError(
                                "get weibo browser detail HTTP 429",
                                code="rate_limited",
                            )
                        api_payload = await api_response.json() if api_status == 200 else None
                        api_detail = _find_weibo_detail(api_payload, note_id)
                        if api_detail is not None and str(
                            api_detail.get("text") or ""
                        ).strip():
                            detail = dict(api_detail)
                            detail["id"] = str(
                                detail.get("id") or detail.get("idstr") or note_id
                            )
                            weibo_client.utils.logger.warning(
                                "[TripPostCollect] Used browser-context Weibo detail API "
                                f"for note_id:{note_id}"
                            )
                            return {"mblog": detail}
                        attempts.append(f"browser_api:http_{api_status}:empty_detail")
                    except weibo_client.PlatformRuntimeError:
                        raise
                    except Exception as exc:
                        attempts.append(f"browser_api:{type(exc).__name__}")

                await page.goto(
                    f"https://m.weibo.cn/detail/{note_id}",
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                await page.wait_for_timeout(min(2_000, timeout_ms))
                page_state = await page.evaluate(
                    """(targetId) => {
                        const roots = [
                            window.$render_data,
                            window.__INITIAL_STATE__,
                            window.__NEXT_DATA__,
                        ];
                        const seen = new WeakSet();
                        const walk = (value, depth) => {
                            if (depth > 12 || value === null || value === undefined) return null;
                            if (typeof value !== 'object') return null;
                            if (seen.has(value)) return null;
                            seen.add(value);
                            const identity = String(value.id || value.idstr || value.mid || '');
                            const shaped = ['text', 'pics', 'user', 'created_at', 'isLongText']
                                .some(key => Object.prototype.hasOwnProperty.call(value, key));
                            if (identity === String(targetId) && shaped) return value;
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
                        for (const script of document.querySelectorAll('script')) {
                            const text = (script.textContent || '').trim();
                            if (!text || (!text.startsWith('{') && !text.startsWith('['))) continue;
                            try {
                                const found = walk(JSON.parse(text), 0);
                                if (found) return found;
                            } catch (_) {
                                continue;
                            }
                        }
                        return null;
                    }""",
                    note_id,
                )
                attempts.append("page_state:empty_detail")
        except PlaywrightError as exc:
            raise weibo_client.DataFetchError(
                "get weibo detail err: browser detail navigation failed "
                f"({type(exc).__name__})"
            ) from (original_error or exc)

        detail = _find_weibo_detail(page_state, note_id)
        if detail is None or not str(detail.get("text") or "").strip():
            raise weibo_client.DataFetchError(
                "get weibo detail err: browser fallbacks have no matching full mblog; "
                f"attempts={attempts}"
            ) from original_error
        detail = dict(detail)
        detail["id"] = str(detail.get("id") or detail.get("idstr") or note_id)
        weibo_client.utils.logger.warning(
            f"[TripPostCollect] Used browser-native Weibo detail fallback for note_id:{note_id}"
        )
        return {"mblog": detail}

    client_class.get_note_info_by_id = resilient_get_note_info_by_id
    client_class._trippostcollect_browser_detail_fallback = True


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
