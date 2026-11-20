"""抖音正文图 CDN 直连，以及空首页的可见页面核对。

只请求 ``*.douyinpic.com``。请求头只有浏览器 UA、站点 Referer 和图片 Accept。
不带 Cookie，不经第三方代理。搜索、详情和作者不再走浏览器外 HTTP。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from trippostcollect.application.contracts import DouyinClientPorts
from trippostcollect.platforms.douyin.parser import (
    DOUYIN_NO_RESULT_MARKERS,
    DOUYIN_RESULT_LINK_SELECTOR,
    classify_empty_first_page,
)

logger = logging.getLogger("TripPostCollect")

DOUYIN_IMAGE_REFERER = "https://www.douyin.com/"
DOUYIN_IMAGE_ACCEPT = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
DOUYIN_CONTENT_IMAGE_RULE = "douyin.content_image"


def is_douyinpic_url(image_url: str) -> bool:
    parsed = urlparse(str(image_url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    return host == "douyinpic.com" or host.endswith(".douyinpic.com")


class DouYinClient:
    """正文图 CDN。``user_agent`` 来自浏览器 ``navigator.userAgent``。"""

    def __init__(self, *, user_agent: str, ports: DouyinClientPorts, timeout: float = 60):
        self.user_agent = str(user_agent or "")
        self.ports = ports
        self.timeout = timeout

    async def get_note_image(self, image_url: str) -> bytes:
        """下载一张正文图。地址必须落在 ``*.douyinpic.com``。"""

        final_uri = str(image_url or "").strip()
        if not is_douyinpic_url(final_uri):
            raise self.ports.image_error(
                "invalid Douyin image URL",
                code="image_source_unavailable",
                retryable=False,
            )
        headers = {
            "user-agent": self.user_agent,
            "accept": DOUYIN_IMAGE_ACCEPT,
            "referer": DOUYIN_IMAGE_REFERER,
        }
        try:
            async with self.ports.make_async_client(proxy=None) as client:
                async with client.stream(
                    "GET", final_uri, timeout=self.timeout, headers=headers,
                ) as response:
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.ports.image_max_bytes:
                            raise self.ports.image_error(
                                "Douyin image exceeded byte limit",
                                code="image_too_large",
                                retryable=False,
                                http_status=response.status_code,
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.error("[DouYinClient.get_note_image] HTTP %s", status)
            raise self.ports.classified_http_image_error(status, f"HTTP {status}") from exc
        except httpx.HTTPError as exc:
            logger.error("[DouYinClient.get_note_image] %s", exc.__class__.__name__)
            raise self.ports.image_error(
                str(exc), code="image_download_retryable", retryable=True,
            ) from exc


async def inspect_empty_first_page(page: Any) -> dict[str, Any]:
    """只看可见 DOM。隐藏的无结果标记不算数。"""

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
