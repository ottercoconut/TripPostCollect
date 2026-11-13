# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/zhihu/client.py
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

# TripPostCollect：T07 迁入根平台；来源 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303，许可见 resources/licenses/MediaCrawler-LICENSE。

import logging
from typing import Dict, Optional

import httpx
from playwright.async_api import BrowserContext, Page
from trippostcollect.application.contracts import ZhihuClientPorts
from trippostcollect.runtime.image_retry import (
    IMAGE_DOWNLOAD_MAX_BYTES, ImageDownloadFetchError, classified_http_image_error,
)
from .models import DataFetchError

logger = logging.getLogger("TripPostCollect")

IMAGE_REFERER = "https://www.zhihu.com/"


class SearchResponseInvalid(DataFetchError):
    """A search_v3 response that cannot prove either more pages or a verified end."""

    def __init__(self, message: str):
        super().__init__(message)
        self.code = "search_response_invalid"


class ZhiHuClient:

    def __init__(
        self,
        timeout=10,
        proxy=None,
        *,
        headers: Dict[str, str],
        playwright_page: Page,
        cookie_dict: Dict[str, str],
        ports: ZhihuClientPorts,
    ):
        self.proxy = proxy
        self.timeout = timeout
        self.default_headers = headers
        self.cookie_urls = ["https://www.zhihu.com"]
        self.cookie_dict = cookie_dict
        self.ports = ports

    async def update_cookies(self, browser_context: BrowserContext, urls: Optional[list[str]] = None):
        """
        Update cookies method provided by API client, typically called after successful login
        Args:
            browser_context: Browser context object

        Returns:

        """
        cookie_str, cookie_dict = await self.ports.convert_browser_context_cookies(
            browser_context,
            urls=urls or self.cookie_urls,
        )
        self.default_headers["cookie"] = cookie_str
        self.cookie_dict = cookie_dict

    def set_search_referer(self, referer: str) -> None:
        """搜索 API 的 Referer 指向本轮实际检索词的搜索页。"""
        self.default_headers["referer"] = referer

    async def get_content_image(self, image_url: str, *, referer: str) -> bytes | None:
        """Fetch one body image from the zhimg CDN with UA, Accept and site Referer only.

        The CDN does not need the zhihu.com session: Cookie and API signing headers stay
        on www.zhihu.com requests.
        """

        headers = {
            "user-agent": self.default_headers.get("user-agent", ""),
            "accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8",
            "referer": IMAGE_REFERER,
        }
        try:
            async with self.ports.make_async_client(proxy=self.proxy) as client:
                async with client.stream(
                    "GET", image_url, timeout=self.timeout, headers=headers
                ) as response:
                    response.raise_for_status()
                    chunks = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > IMAGE_DOWNLOAD_MAX_BYTES:
                            logger.error(
                                "[ZhiHuClient.get_content_image] image exceeded byte limit"
                            )
                            raise ImageDownloadFetchError(
                                "image exceeded byte limit",
                                code="image_too_large",
                                retryable=False,
                                http_status=response.status_code,
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)
        except ImageDownloadFetchError:
            raise
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.error(
                f"[ZhiHuClient.get_content_image] HTTP {status}: {exc}"
            )
            raise classified_http_image_error(status, f"HTTP {status}") from exc
        except httpx.HTTPError as exc:
            logger.error(
                f"[ZhiHuClient.get_content_image] {exc.__class__.__name__}: {exc}"
            )
            raise ImageDownloadFetchError(
                str(exc), code="image_download_retryable", retryable=True
            ) from exc
