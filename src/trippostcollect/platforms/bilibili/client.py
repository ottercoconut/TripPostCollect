"""B站article 请求与重试。"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from trippostcollect.application.contracts import (
    BilibiliImageFetchPorts,
    RemoteImageResponse as RemoteImagePreview,
)
from .models import BilibiliArticleDetailError, BilibiliFollowerFetchError
from .parser import clean_bilibili_article_body
from .signer import sign_bilibili_wbi_params


BILIBILI_ARTICLE_SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"


BILIBILI_ARTICLE_DETAIL_URL = "https://api.bilibili.com/x/article/view"


BILIBILI_RELATION_STAT_URL = "https://api.bilibili.com/x/relation/stat"


BILIBILI_ARTICLE_PAGE_SIZE = 20


BILIBILI_DETAIL_MAX_ATTEMPTS = 3


BILIBILI_DETAIL_RETRY_DELAY_SECONDS = (4.0, 7.0)


BILIBILI_DETAIL_RETRYABLE_CODES = frozenset({-509, -412, -352})


BILIBILI_RUNTIME_BLOCKING_CODES = frozenset({-101, -509, -412, -352})


BILIBILI_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)


def bilibili_detail_headers(post_id: str, cookie_header: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": BILIBILI_BROWSER_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://www.bilibili.com",
        "Referer": f"https://www.bilibili.com/read/cv{post_id}/",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def bilibili_image_headers(post_id: str, cookie_header: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": BILIBILI_BROWSER_USER_AGENT,
        "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif;q=0.9",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": f"https://www.bilibili.com/read/cv{post_id}/",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def fetch_bilibili_image_bytes(
    source_url: str,
    post_id: str,
    cookie_header: str = "",
    *,
    ports: BilibiliImageFetchPorts,
) -> RemoteImagePreview:
    fetch_remote_image_bytes = ports.fetch_remote_image_bytes
    DEFAULT_ARCHIVE_IMAGE_MAX_BYTES = ports.DEFAULT_ARCHIVE_IMAGE_MAX_BYTES
    SUPPORTED_IMAGE_MIME_TYPES = ports.SUPPORTED_IMAGE_MIME_TYPES
    return fetch_remote_image_bytes(
        source_url,
        headers=bilibili_image_headers(post_id, cookie_header),
        max_bytes=DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
        timeout_seconds=30,
        allowed_media_types=SUPPORTED_IMAGE_MIME_TYPES,
    )


def fetch_bilibili_article_detail(post_id: str, cookie_header: str = "") -> dict[str, Any]:
    request = Request(
        BILIBILI_ARTICLE_DETAIL_URL + "?" + urlencode({"id": post_id}),
        headers=bilibili_detail_headers(post_id, cookie_header),
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        raise BilibiliArticleDetailError(
            f"bilibili article detail HTTP {exc.code}",
            retryable=exc.code == 429 or exc.code >= 500,
            code=exc.code,
            runtime_blocking=exc.code in {401, 403, 429},
        ) from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise BilibiliArticleDetailError(
            f"bilibili article detail transport/parse failure: {type(exc).__name__}",
            retryable=True,
        ) from exc

    if not isinstance(payload, dict):
        raise BilibiliArticleDetailError(
            "bilibili article detail payload is not an object",
            retryable=True,
        )
    try:
        code = int(payload.get("code"))
    except (TypeError, ValueError):
        code = None
    if code != 0:
        raise BilibiliArticleDetailError(
            f"bilibili article detail failed: {code} {payload.get('message')}",
            retryable=code in BILIBILI_DETAIL_RETRYABLE_CODES,
            code=code,
            runtime_blocking=code in BILIBILI_RUNTIME_BLOCKING_CODES,
        )
    detail = payload.get("data")
    if not isinstance(detail, dict):
        raise BilibiliArticleDetailError(
            "bilibili article detail missing data object",
            retryable=True,
            code=code,
        )
    if not clean_bilibili_article_body(detail.get("content")):
        raise BilibiliArticleDetailError(
            "bilibili article detail has no parseable body",
            retryable=True,
            code=code,
        )
    return detail


def fetch_bilibili_article_detail_with_retry(
    post_id: str,
    cookie_header: str = "",
) -> tuple[dict[str, Any], int, float]:
    last_error: BilibiliArticleDetailError | None = None
    retry_wait_seconds = 0.0
    for attempt in range(1, BILIBILI_DETAIL_MAX_ATTEMPTS + 1):
        try:
            return (
                fetch_bilibili_article_detail(post_id, cookie_header),
                attempt,
                round(retry_wait_seconds, 3),
            )
        except BilibiliArticleDetailError as exc:
            last_error = exc
            if not exc.retryable or attempt >= BILIBILI_DETAIL_MAX_ATTEMPTS:
                exc.attempts = attempt
                exc.retry_wait_seconds = round(retry_wait_seconds, 3)
                raise
            delay = random.uniform(*BILIBILI_DETAIL_RETRY_DELAY_SECONDS) * attempt
            retry_wait_seconds += delay
            time.sleep(delay)
    assert last_error is not None
    last_error.attempts = BILIBILI_DETAIL_MAX_ATTEMPTS
    last_error.retry_wait_seconds = round(retry_wait_seconds, 3)
    raise last_error


def fetch_bilibili_wbi_keys(cookie_header: str = "") -> tuple[str, str]:
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = Request(
        "https://api.bilibili.com/x/web-interface/nav",
        headers=headers,
    )
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    data = payload.get("data") or {}
    wbi_img = data.get("wbi_img") or {}
    img_url = str(wbi_img.get("img_url") or "")
    sub_url = str(wbi_img.get("sub_url") or "")
    if not img_url or not sub_url:
        raise RuntimeError("bilibili nav response missing WBI keys")
    return Path(img_url).stem, Path(sub_url).stem


def fetch_bilibili_article_page(
    keyword: str,
    page: int,
    *,
    wbi_keys: tuple[str, str] | None = None,
    cookie_header: str = "",
) -> list[dict[str, Any]]:
    params = {
        "keyword": keyword,
        "page": page,
        "page_size": BILIBILI_ARTICLE_PAGE_SIZE,
        "search_type": "article",
    }
    img_key, sub_key = wbi_keys or fetch_bilibili_wbi_keys(cookie_header)
    signed_params = sign_bilibili_wbi_params(params, img_key, sub_key)
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://search.bilibili.com/article?keyword=" + quote(keyword),
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = Request(BILIBILI_ARTICLE_SEARCH_URL + "?" + urlencode(signed_params), headers=headers)
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    if payload.get("code") != 0:
        raise RuntimeError(f"bilibili article search failed: {payload.get('code')} {payload.get('message')}")
    result = (payload.get("data") or {}).get("result") or []
    return [item for item in result if isinstance(item, dict)]


def fetch_bilibili_follower_count(creator_id: str, cookie_header: str = "") -> int | None:
    if not creator_id:
        return None
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": f"https://space.bilibili.com/{creator_id}",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = Request(
        BILIBILI_RELATION_STAT_URL + "?" + urlencode({"vmid": creator_id}),
        headers=headers,
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        raise BilibiliFollowerFetchError(
            f"bilibili relation stat HTTP {exc.code}",
            code=exc.code,
            retryable=exc.code == 429 or exc.code >= 500,
            runtime_blocking=exc.code in {401, 403, 429},
        ) from exc
    try:
        code = int(payload.get("code"))
    except (TypeError, ValueError):
        code = None
    if code != 0:
        raise BilibiliFollowerFetchError(
            f"bilibili relation stat failed: {code} {payload.get('message')}",
            code=code,
            retryable=code in BILIBILI_DETAIL_RETRYABLE_CODES,
            runtime_blocking=code in BILIBILI_RUNTIME_BLOCKING_CODES,
        )
    value = (payload.get("data") or {}).get("follower")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
