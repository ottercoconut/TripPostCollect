"""B站article 请求与重试。"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from trippostcollect.application.contracts import (
    BilibiliImageFetchPorts,
    RemoteImageResponse as RemoteImagePreview,
)
from .models import (
    BilibiliArticleDetailError,
    BilibiliFollowerFetchError,
    BilibiliRuntimeBlocked,
    BilibiliSearchPage,
)
from .parser import clean_bilibili_article_body
from .signer import sign_bilibili_wbi_params


BILIBILI_ARTICLE_SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"


BILIBILI_ARTICLE_DETAIL_URL = "https://api.bilibili.com/x/article/view"


BILIBILI_RELATION_STAT_URL = "https://api.bilibili.com/x/relation/stat"


BILIBILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"


BILIBILI_ARTICLE_PAGE_SIZE = 20


BILIBILI_DETAIL_MAX_ATTEMPTS = 3


BILIBILI_DETAIL_RETRY_DELAY_SECONDS = (4.0, 7.0)


# retryable 只说明失败是暂时的、不是帖子本身的属性（历史修复脚本据此进入全局冷却）。
BILIBILI_DETAIL_RETRYABLE_CODES = frozenset({-509, -412, -352})


# 登录、频控与风控命中即停：所有重试循环见到 runtime_blocking 都不再发请求。
BILIBILI_RUNTIME_BLOCKING_CODES = frozenset({-101, -509, -412, -352})


BILIBILI_RUNTIME_BLOCKING_HTTP_STATUSES = frozenset({401, 403, 412, 429})


# #129：重试用尽后仍是网络传输失败时的运行级停止细节后缀。
BILIBILI_NETWORK_UNREACHABLE = "network_unreachable"


# 图片重定向只跟随 B站图床；目标是其他主机时拒绝跟随。
BILIBILI_IMAGE_REDIRECT_HOSTS = ("hdslb.com", "biliimg.com")


# #127：B站请求的 User-Agent 由调用方给出，取自本轮行为会话浏览器的 navigator.userAgent（repair 取自
# warmup 快照），不再写死版本串。


def bilibili_detail_headers(post_id: str, cookie_header: str = "", *, user_agent: str) -> dict[str, str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://www.bilibili.com",
        "Referer": f"https://www.bilibili.com/read/cv{post_id}/",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def bilibili_image_headers(post_id: str, *, user_agent: str) -> dict[str, str]:
    # 图床不需要登录态，bilibili.com 的 Cookie 不发往 CDN。
    return {
        "User-Agent": user_agent,
        "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif;q=0.9",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": f"https://www.bilibili.com/read/cv{post_id}/",
    }


def fetch_bilibili_image_bytes(
    source_url: str,
    post_id: str,
    *,
    user_agent: str,
    ports: BilibiliImageFetchPorts,
) -> RemoteImagePreview:
    fetch_remote_image_bytes = ports.fetch_remote_image_bytes
    DEFAULT_ARCHIVE_IMAGE_MAX_BYTES = ports.DEFAULT_ARCHIVE_IMAGE_MAX_BYTES
    SUPPORTED_IMAGE_MIME_TYPES = ports.SUPPORTED_IMAGE_MIME_TYPES
    return fetch_remote_image_bytes(
        source_url,
        headers=bilibili_image_headers(post_id, user_agent=user_agent),
        max_bytes=DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
        timeout_seconds=30,
        allowed_media_types=SUPPORTED_IMAGE_MIME_TYPES,
        allowed_redirect_hosts=BILIBILI_IMAGE_REDIRECT_HOSTS,
    )


def bilibili_request_stop_detail(stage: str, exc: BilibiliArticleDetailError) -> str:
    """分类后的请求错误的运行级停止细节：阶段、是否阻断，以及平台码、HTTP 码、network_unreachable 或 parse。

    #129：重试用尽后的网络传输失败记 ``network_unreachable``；其余没有平台码与 HTTP 码的失败
    （坏 JSON、非对象响应）记 ``parse``。
    """

    kind = "blocked" if exc.runtime_blocking else "failed"
    if exc.code is not None:
        code = exc.code
    elif getattr(exc, "network_unreachable", False):
        code = BILIBILI_NETWORK_UNREACHABLE
    else:
        code = "parse"
    return f"bilibili_{stage}_{kind}:{code}"


def _payload_code(payload: dict[str, Any]) -> int | None:
    try:
        return int(payload.get("code"))
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _request_bilibili_json(
    request: Request,
    label: str,
    error_type: type[BilibiliArticleDetailError] | type[BilibiliFollowerFetchError] = (
        BilibiliArticleDetailError
    ),
) -> dict[str, Any]:
    """发出一个 B站 JSON 请求：阻断状态码命中即停，5xx、超时、断连和坏 JSON 可重试。

    #129：超时、断连等网络传输失败带 ``network_unreachable=True``，仍按原预算重试；重试用尽后由
    调用方按运行级失败停止，不按候选跳过。坏 JSON 只是可重试的候选错误。
    """

    try:
        with urlopen(request, timeout=30) as response:
            body = response.read()
    except HTTPError as exc:
        raise error_type(
            f"{label} HTTP {exc.code}",
            retryable=exc.code == 429 or exc.code >= 500,
            code=exc.code,
            runtime_blocking=exc.code in BILIBILI_RUNTIME_BLOCKING_HTTP_STATUSES,
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise error_type(
            f"{label} network failure: {type(exc).__name__}",
            retryable=True,
            code=None,
            runtime_blocking=False,
            network_unreachable=True,
        ) from exc
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise error_type(
            f"{label} parse failure: {type(exc).__name__}",
            retryable=True,
            code=None,
            runtime_blocking=False,
        ) from exc
    if not isinstance(payload, dict):
        raise error_type(
            f"{label} payload is not an object",
            retryable=True,
            code=None,
            runtime_blocking=False,
        )
    return payload


def _with_transient_retry(call: Callable[[], Any]) -> Any:
    """搜索与 nav 请求：瞬时错误有限重试，阻断与不可重试错误立即抛出。"""

    retry_wait_seconds = 0.0
    for attempt in range(1, BILIBILI_DETAIL_MAX_ATTEMPTS + 1):
        try:
            return call()
        except BilibiliArticleDetailError as exc:
            if exc.runtime_blocking or not exc.retryable or attempt >= BILIBILI_DETAIL_MAX_ATTEMPTS:
                exc.attempts = attempt
                exc.retry_wait_seconds = round(retry_wait_seconds, 3)
                raise
            delay = random.uniform(*BILIBILI_DETAIL_RETRY_DELAY_SECONDS) * attempt
            retry_wait_seconds += delay
            time.sleep(delay)
    raise AssertionError("retry loop must return or raise")


def fetch_bilibili_article_detail(post_id: str, cookie_header: str = "", *, user_agent: str) -> dict[str, Any]:
    request = Request(
        BILIBILI_ARTICLE_DETAIL_URL + "?" + urlencode({"id": post_id}),
        headers=bilibili_detail_headers(post_id, cookie_header, user_agent=user_agent),
    )
    payload = _request_bilibili_json(request, "bilibili article detail")
    code = _payload_code(payload)
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
    *,
    user_agent: str,
) -> tuple[dict[str, Any], int, float]:
    last_error: BilibiliArticleDetailError | None = None
    retry_wait_seconds = 0.0
    for attempt in range(1, BILIBILI_DETAIL_MAX_ATTEMPTS + 1):
        try:
            return (
                fetch_bilibili_article_detail(post_id, cookie_header, user_agent=user_agent),
                attempt,
                round(retry_wait_seconds, 3),
            )
        except BilibiliArticleDetailError as exc:
            last_error = exc
            if exc.runtime_blocking or not exc.retryable or attempt >= BILIBILI_DETAIL_MAX_ATTEMPTS:
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


def fetch_bilibili_wbi_keys(cookie_header: str = "", *, user_agent: str) -> tuple[str, str]:
    headers = {"User-Agent": user_agent, "Referer": "https://www.bilibili.com/"}
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = Request(BILIBILI_NAV_URL, headers=headers)
    payload = _with_transient_retry(lambda: _request_bilibili_json(request, "bilibili nav"))
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    # 过期会话的 SESSDATA 仍会留在浏览器里；只有 nav 明确返回 isLogin=true 才算已登录。
    if data.get("isLogin") is not True:
        raise BilibiliRuntimeBlocked("bilibili_nav_login_required", stop_reason="login_required")
    code = _payload_code(payload)
    if code != 0:
        raise BilibiliArticleDetailError(
            f"bilibili nav failed: {code} {payload.get('message')}",
            retryable=code in BILIBILI_DETAIL_RETRYABLE_CODES,
            code=code,
            runtime_blocking=code in BILIBILI_RUNTIME_BLOCKING_CODES,
        )
    wbi_img = data.get("wbi_img") or {}
    img_url = str(wbi_img.get("img_url") or "")
    sub_url = str(wbi_img.get("sub_url") or "")
    if not img_url or not sub_url:
        raise RuntimeError("bilibili nav response missing WBI keys")
    return Path(img_url).stem, Path(sub_url).stem


def _fetch_bilibili_article_page_once(request: Request) -> BilibiliSearchPage:
    payload = _request_bilibili_json(request, "bilibili article search")
    data = payload.get("data")
    # 风控响应可能仍是 code=0，只在 data 里带验证凭据；这是验证阻断，不是空页。
    if isinstance(data, dict) and data.get("v_voucher"):
        raise BilibiliRuntimeBlocked(
            "bilibili_article_search_risk_control:v_voucher",
            stop_reason="captcha_detected",
        )
    code = _payload_code(payload)
    if code != 0:
        raise BilibiliArticleDetailError(
            f"bilibili article search failed: {code} {payload.get('message')}",
            retryable=code in BILIBILI_DETAIL_RETRYABLE_CODES,
            code=code,
            runtime_blocking=code in BILIBILI_RUNTIME_BLOCKING_CODES,
        )
    if not isinstance(data, dict):
        raise BilibiliArticleDetailError(
            "bilibili article search missing data object",
            retryable=True,
            code=code,
        )
    result = data.get("result")
    return BilibiliSearchPage(
        items=[item for item in result if isinstance(item, dict)] if isinstance(result, list) else [],
        num_pages=_optional_int(data.get("numPages")),
        num_results=_optional_int(data.get("numResults")),
    )


def fetch_bilibili_article_page(
    keyword: str,
    page: int,
    *,
    wbi_keys: tuple[str, str] | None = None,
    cookie_header: str = "",
    user_agent: str,
) -> BilibiliSearchPage:
    params = {
        "keyword": keyword,
        "page": page,
        "page_size": BILIBILI_ARTICLE_PAGE_SIZE,
        "search_type": "article",
    }
    img_key, sub_key = wbi_keys or fetch_bilibili_wbi_keys(cookie_header, user_agent=user_agent)
    signed_params = sign_bilibili_wbi_params(params, img_key, sub_key)
    headers = {
        "User-Agent": user_agent,
        "Referer": "https://search.bilibili.com/article?keyword=" + quote(keyword),
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = Request(BILIBILI_ARTICLE_SEARCH_URL + "?" + urlencode(signed_params), headers=headers)
    return _with_transient_retry(lambda: _fetch_bilibili_article_page_once(request))


def fetch_bilibili_follower_count(creator_id: str, cookie_header: str = "", *, user_agent: str) -> int | None:
    if not creator_id:
        return None
    headers = {
        "User-Agent": user_agent,
        "Referer": f"https://space.bilibili.com/{creator_id}",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = Request(
        BILIBILI_RELATION_STAT_URL + "?" + urlencode({"vmid": creator_id}),
        headers=headers,
    )
    payload = _request_bilibili_json(request, "bilibili relation stat", BilibiliFollowerFetchError)
    code = _payload_code(payload)
    if code != 0:
        raise BilibiliFollowerFetchError(
            f"bilibili relation stat failed: {code} {payload.get('message')}",
            code=code,
            retryable=code in BILIBILI_DETAIL_RETRYABLE_CODES,
            runtime_blocking=code in BILIBILI_RUNTIME_BLOCKING_CODES,
        )
    return _optional_int((payload.get("data") or {}).get("follower"))
