"""微博正文图 CDN 直连。

只请求 ``*.sinaimg.cn``。请求头只有浏览器 UA、站点 Referer 和图片 Accept。
不带 Cookie、Origin、Content-Type，不经代理。移动详情已删除。
"""

from __future__ import annotations

import logging
from typing import Iterable
from urllib.parse import urlparse

import httpx

from trippostcollect.application.contracts import WeiboClientPorts

logger = logging.getLogger("TripPostCollect")

WEIBO_IMAGE_REFERER = "https://weibo.com/"
WEIBO_IMAGE_ACCEPT = "image/avif,image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8"
WEIBO_CONTENT_IMAGE_RULE = "weibo.content_image"
WEIBO_STORED_IMAGE_RULE = "weibo.stored_image"
_BLOCKING_IMAGE_CODES = frozenset({"image_auth_required", "image_rate_limited"})


def is_sinaimg_url(image_url: str) -> bool:
    parsed = urlparse(str(image_url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    return host == "sinaimg.cn" or host.endswith(".sinaimg.cn")


class WeiboClient:
    """正文图 CDN。``user_agent`` 来自浏览器 ``navigator.userAgent``。"""

    def __init__(self, *, user_agent: str, ports: WeiboClientPorts, timeout: float = 60):
        self.user_agent = str(user_agent or "")
        self.ports = ports
        self.timeout = timeout

    async def get_note_image(self, image_urls: str | Iterable[str]) -> bytes:
        """按给定顺序请求规格。首选失败或超限时退到下一规格。"""

        if isinstance(image_urls, str):
            raw_urls = [image_urls]
        else:
            raw_urls = [str(url) for url in image_urls]
        candidates = [url for url in raw_urls if is_sinaimg_url(url)]
        if not candidates:
            raise self.ports.image_error(
                "invalid Weibo image URL",
                code="image_source_unavailable",
                retryable=False,
            )
        headers = {
            "user-agent": self.user_agent,
            "accept": WEIBO_IMAGE_ACCEPT,
            "referer": WEIBO_IMAGE_REFERER,
        }
        last_error: Exception | None = None
        for final_uri in candidates:
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
                                last_error = self.ports.image_error(
                                    "Weibo image exceeded byte limit",
                                    code="image_too_large",
                                    retryable=False,
                                    http_status=response.status_code,
                                )
                                chunks = []
                                break
                            chunks.append(chunk)
                        else:
                            return b"".join(chunks)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                logger.error("[WeiboClient.get_note_image] HTTP %s", status)
                classified = self.ports.classified_http_image_error(status, f"HTTP {status}")
                if classified.code in _BLOCKING_IMAGE_CODES:
                    raise classified from exc
                last_error = classified
            except httpx.HTTPError as exc:
                logger.error(
                    "[WeiboClient.get_note_image] %s",
                    exc.__class__.__name__,
                )
                last_error = self.ports.image_error(
                    str(exc), code="image_download_retryable", retryable=True,
                )
        if last_error is not None:
            raise last_error
        raise self.ports.image_error(
            "Weibo image candidates exhausted",
            code="image_source_unavailable",
            retryable=False,
        )
