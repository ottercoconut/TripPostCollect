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

import json
import logging
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlencode

import httpx
from httpx import Response
from playwright.async_api import BrowserContext, Page
from tenacity import retry, stop_after_attempt, wait_fixed
from trippostcollect.application.contracts import ZhihuClientPorts
from trippostcollect.runtime.image_retry import (
    IMAGE_DOWNLOAD_MAX_BYTES, ImageDownloadFetchError, classified_http_image_error,
)
from . import models as zhihu_constant
from .models import ZhihuContent, DataFetchError, PlatformRuntimeError, SearchSort, SearchTime, SearchType
from .parser import ZhihuExtractor
from .signer import sign

logger = logging.getLogger("MediaCrawler")

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
        self._extractor = ZhihuExtractor()
        self.ports = ports

    async def _pre_headers(self, url: str) -> Dict:
        """
        Sign request headers
        Args:
            url: Request URL with query parameters
        Returns:

        """
        d_c0 = self.cookie_dict.get("d_c0")
        if not d_c0:
            raise Exception("d_c0 not found in cookies")
        sign_res = sign(url, self.default_headers["cookie"])
        headers = self.default_headers.copy()
        headers['x-zst-81'] = sign_res["x-zst-81"]
        headers['x-zse-96'] = sign_res["x-zse-96"]
        return headers

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1), reraise=True)
    async def request(self, method, url, **kwargs) -> Union[str, Any]:
        """
        Wrapper for httpx common request method with response handling
        Args:
            method: Request method
            url: Request URL
            **kwargs: Other request parameters such as headers, body, etc.

        Returns:

        """

        # return response.text
        return_response = kwargs.pop('return_response', False)

        async with self.ports.make_async_client(proxy=self.proxy) as client:
            response = await client.request(method, url, timeout=self.timeout, **kwargs)

        if response.status_code != 200:
            logger.error(f"[ZhiHuClient.request] Requset Url: {url}, Request error: {response.text}")
            if response.status_code in {401, 403}:
                raise PlatformRuntimeError(
                    response.text or f"HTTP {response.status_code}",
                    code="login_required",
                )
            if response.status_code == 429:
                raise PlatformRuntimeError(
                    response.text or "HTTP 429",
                    code="rate_limited",
                )
            elif response.status_code == 404:  # Content without comments also returns 404
                return {}

            raise DataFetchError(response.text)

        if return_response:
            return response.text
        try:
            data: Dict = response.json()
            if data.get("error"):
                logger.error(f"[ZhiHuClient.request] Request error: {data}")
                raise DataFetchError(data.get("error", {}).get("message"))
            return data
        except json.JSONDecodeError:
            logger.error(f"[ZhiHuClient.request] Request error: {response.text}")
            raise DataFetchError(response.text)

    async def get(self, uri: str, params=None, **kwargs) -> Union[Response, Dict, str]:
        """
        GET request with header signing
        Args:
            uri: Request URI
            params: Request parameters

        Returns:

        """
        final_uri = uri
        if isinstance(params, dict):
            final_uri += '?' + urlencode(params)
        headers = await self._pre_headers(final_uri)
        base_url = (zhihu_constant.ZHIHU_URL if "/p/" not in uri else zhihu_constant.ZHIHU_ZHUANLAN_URL)
        return await self.request(method="GET", url=base_url + final_uri, headers=headers, **kwargs)

    async def pong(self) -> bool:
        """
        Check if login status is still valid
        Returns:

        """
        logger.info("[ZhiHuClient.pong] Begin to pong zhihu...")
        ping_flag = False
        try:
            res = await self.get_current_user_info()
            if res.get("uid") and res.get("name"):
                ping_flag = True
                logger.info("[ZhiHuClient.pong] Ping zhihu successfully")
            else:
                logger.error(f"[ZhiHuClient.pong] Ping zhihu failed, response data: {res}")
        except Exception as e:
            logger.error(f"[ZhiHuClient.pong] Ping zhihu failed: {e}, and try to login again...")
            ping_flag = False
        return ping_flag

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

    async def get_current_user_info(self) -> Dict:
        """
        Get current logged-in user information
        Returns:

        """
        params = {"include": "email,is_active,is_bind_phone"}
        return await self.get("/api/v4/me", params)

    async def get_content_image(self, image_url: str, *, referer: str) -> bytes | None:
        """Fetch one body image with the active proxy, Cookie and User-Agent session."""

        headers = self.default_headers.copy()
        headers["referer"] = referer or "https://www.zhihu.com/"
        headers["accept"] = "image/avif,image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8"
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

    async def get_note_by_keyword(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 20,
        sort: SearchSort = SearchSort.DEFAULT,
        note_type: SearchType = SearchType.DEFAULT,
        search_time: SearchTime = SearchTime.DEFAULT,
    ) -> List[ZhihuContent]:
        """
        Search by keyword
        Args:
            keyword: Search keyword
            page: Page number
            page_size: Page size
            sort: Sorting method
            note_type: Search result type
            search_time: Time range for search results

        Returns:

        """
        uri = "/api/v4/search_v3"
        params = {
            "gk_version": "gz-gaokao",
            "t": "general",
            "q": keyword,
            "correction": 1,
            "offset": (page - 1) * page_size,
            "limit": page_size,
            "filter_fields": "",
            "lc_idx": (page - 1) * page_size,
            "show_all_topics": 0,
            "search_source": "Filter",
            "time_interval": search_time.value,
            "sort": sort.value,
            "vertical": note_type.value,
        }
        search_res = await self.get(uri, params)
        logger.info(f"[ZhiHuClient.get_note_by_keyword] Search result: {search_res}")
        return self._extractor.extract_contents_from_search(search_res)

        # uri = f"/api/v4/{content_type}s/{content_id}/root_comments"
        # params = {
        #     "order": order_by,
        #     "offset": offset,
        #     "limit": limit
        # }
        # return await self.get(uri, params)











    async def get_answer_info(
        self,
        question_id: str,
        answer_id: str,
    ) -> Optional[ZhihuContent]:
        """
        Get answer information
        Args:
            question_id:
            answer_id:

        Returns:

        """
        uri = f"/question/{question_id}/answer/{answer_id}"
        response_html = await self.get(uri, return_response=True)
        return self._extractor.extract_answer_content_from_html(response_html, answer_id)

    async def get_article_info(self, article_id: str) -> Optional[ZhihuContent]:
        """
        Get article information
        Args:
            article_id:

        Returns:

        """
        uri = f"/p/{article_id}"
        response_html = await self.get(uri, return_response=True)
        return self._extractor.extract_article_content_from_html(response_html, article_id)

    async def get_video_info(self, video_id: str) -> Optional[ZhihuContent]:
        """
        Get video information
        Args:
            video_id:

        Returns:

        """
        uri = f"/zvideo/{video_id}"
        response_html = await self.get(uri, return_response=True)
        return self._extractor.extract_zvideo_content_from_html(response_html)
