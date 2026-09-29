"""Validation and visible-page checks for Douyin search responses."""

from __future__ import annotations

import json
from typing import Any, Dict

from playwright.async_api import Page

from .exception import SearchResponseError


DOUYIN_RESULT_LINK_SELECTOR = ', '.join(
    (
        'a[href*="/video/"]:visible',
        'a[href*="/note/"]:visible',
        '[data-e2e*="search-result"]:visible',
        '.search-result-card:visible',
        '[id^="waterfall_item_"]:visible',
    )
)
DOUYIN_NO_RESULT_MARKERS = (
    "暂无搜索结果",
    "没有找到相关结果",
    "没有找到你想要的内容",
    "未搜索到相关内容",
    "搜索结果为空",
    "换个关键词试试",
)


def decode_douyin_json_body(body: bytes) -> Dict[str, Any]:
    """Decode normal JSON or Douyin's raw HTTP-chunk-framed stream body."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload_bytes = bytearray()
        raw_chunks: list[bytes] = []
        cursor = 0
        while True:
            line_end = body.find(b"\r\n", cursor)
            if line_end < 0:
                raise SearchResponseError("invalid_stream_chunk_header")
            size_text = body[cursor:line_end].split(b";", maxsplit=1)[0].strip()
            try:
                chunk_size = int(size_text, 16)
            except ValueError as exc:
                raise SearchResponseError("invalid_stream_chunk_size") from exc
            cursor = line_end + 2
            if chunk_size == 0:
                break
            chunk_end = cursor + chunk_size
            if chunk_end > len(body) or body[chunk_end:chunk_end + 2] != b"\r\n":
                raise SearchResponseError("truncated_stream_chunk")
            chunk = body[cursor:chunk_end]
            raw_chunks.append(chunk)
            payload_bytes.extend(chunk)
            cursor = chunk_end + 2
        try:
            payload = json.loads(payload_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            documents = []
            try:
                for chunk in raw_chunks:
                    document = json.loads(chunk)
                    if not isinstance(document, dict):
                        raise TypeError("stream document is not an object")
                    documents.append(document)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as chunk_exc:
                raise SearchResponseError("invalid_stream_json") from chunk_exc
            if not documents:
                raise SearchResponseError("invalid_stream_json") from exc
            payload = dict(documents[-1])
            merged_data = []
            verify_search_nil_info = None
            for document in documents:
                document_data = document.get("data")
                if document_data is None:
                    continue
                if not isinstance(document_data, list):
                    raise SearchResponseError("invalid_stream_json") from exc
                merged_data.extend(document_data)
                if document.get("status_code") not in (None, 0, "0"):
                    payload["status_code"] = document["status_code"]
                search_nil_info = document.get("search_nil_info")
                if (
                    isinstance(search_nil_info, dict)
                    and search_nil_info.get("search_nil_type") == "verify_check"
                ):
                    verify_search_nil_info = search_nil_info
            payload["data"] = merged_data
            if verify_search_nil_info is not None:
                payload["search_nil_info"] = verify_search_nil_info
            for document in reversed(documents):
                if "has_more" in document:
                    payload["has_more"] = document["has_more"]
                    break
            for document in reversed(documents):
                if isinstance(document.get("extra"), dict):
                    payload["extra"] = document["extra"]
                    break
    if not isinstance(payload, dict):
        raise SearchResponseError("search_response_not_object")
    return payload


def validate_douyin_search_response(payload: Any) -> Dict[str, Any]:
    """Reject parseable error envelopes before pagination interprets them as data."""
    if not isinstance(payload, dict):
        raise SearchResponseError("search_response_not_object")

    search_nil_info = payload.get("search_nil_info")
    if isinstance(search_nil_info, dict) and search_nil_info.get("search_nil_type") == "verify_check":
        raise SearchResponseError("search_verify_check")

    status_code = payload.get("status_code")
    if status_code not in (None, 0, "0"):
        raise SearchResponseError("search_business_status_nonzero")

    if "data" not in payload or payload.get("data") is None:
        raise SearchResponseError("missing_data_field")
    if not isinstance(payload.get("data"), list):
        raise SearchResponseError("invalid_data_field")

    if "has_more" not in payload:
        raise SearchResponseError("missing_has_more_field")
    has_more = payload.get("has_more")
    if not isinstance(has_more, (bool, int)) or has_more not in (False, True, 0, 1):
        raise SearchResponseError("invalid_has_more_field")

    extra = payload.get("extra")
    if extra is None:
        extra = {}
    if not isinstance(extra, dict):
        raise SearchResponseError("invalid_extra_field")
    next_search_id = extra.get("logid", "")
    if has_more in (True, 1) and not str(next_search_id or "").strip():
        raise SearchResponseError("missing_next_search_id")
    return payload


def classify_empty_first_page(*, visible_result_count: int, visible_text: str) -> str:
    if visible_result_count > 0:
        return "visible_results"
    if any(marker in visible_text for marker in DOUYIN_NO_RESULT_MARKERS):
        return "explicit_no_results"
    return "ambiguous"


async def inspect_empty_first_page(page: Page) -> dict[str, Any]:
    """Inspect visible DOM only; hidden HTML markers are intentionally ignored."""
    visible_result_count = 0
    visible_text = ""
    errors: list[str] = []
    try:
        visible_result_count = await page.locator(DOUYIN_RESULT_LINK_SELECTOR).count()
    except Exception as exc:
        errors.append(f"result_count:{type(exc).__name__}")
    try:
        visible_text = (await page.locator("body").inner_text(timeout=5_000))[:30_000]
    except Exception as exc:
        errors.append(f"visible_text:{type(exc).__name__}")

    classification = classify_empty_first_page(
        visible_result_count=visible_result_count,
        visible_text=visible_text,
    )
    matched_marker = next(
        (marker for marker in DOUYIN_NO_RESULT_MARKERS if marker in visible_text),
        "",
    )
    return {
        "classification": classification,
        "visible_result_count": visible_result_count,
        "matched_no_result_marker": matched_marker,
        "url": page.url,
        "inspection_errors": errors,
    }
