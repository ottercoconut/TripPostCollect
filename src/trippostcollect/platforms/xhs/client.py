# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/client.py
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

# TripPostCollect：T09 迁入根平台；来源 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303，许可见 resources/licenses/MediaCrawler-LICENSE。

import json
import logging
import time
from typing import Any, Dict, Optional, Union
from urllib.parse import quote

import httpx
from playwright.async_api import BrowserContext, Page
from tenacity import (
    RetryError,
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from trippostcollect.application.contracts import XhsClientPorts
from trippostcollect.platforms.xhs.behavior import run_required_api_captcha_verification
from trippostcollect.platforms.xhs.errors import (
    DataFetchError,
    IPBlockError,
    NoteNotFoundError,
    PlatformRuntimeError,
)
from trippostcollect.platforms.xhs.manual_wait import XHSManualWaitBudget
from trippostcollect.platforms.xhs.models import SearchNoteType, SearchSortType
from trippostcollect.platforms.xhs.parser import XiaoHongShuExtractor, get_search_id
from trippostcollect.platforms.xhs.signer import sign_with_xhshow
from trippostcollect.runtime.cookies import convert_browser_context_cookies
from trippostcollect.runtime.image_retry import ImageDownloadFetchError, classified_http_image_error

logger = logging.getLogger("MediaCrawler")


def unwrap_xhs_request_failure(exc: BaseException) -> BaseException:
    """Expose the final request failure hidden by tenacity."""
    if not isinstance(exc, RetryError):
        return exc
    try:
        nested = exc.last_attempt.exception()
    except Exception:
        nested = None
    return nested if isinstance(nested, BaseException) else exc


def is_recoverable_xhs_transport_failure(exc: BaseException) -> bool:
    """Return whether a failure is a temporary client/network transport outage."""
    nested = unwrap_xhs_request_failure(exc)
    return isinstance(
        nested,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ProxyError,
            httpx.RemoteProtocolError,
        ),
    )


class XiaoHongShuClient:

    def __init__(
        self,
        timeout=60,  # If media crawling is enabled, Xiaohongshu long videos need longer timeout
        proxy=None,
        *,
        headers: Dict[str, str],
        playwright_page: Page,
        cookie_dict: Dict[str, str],
        manual_wait_budget: Optional[XHSManualWaitBudget] = None,
        ports: XhsClientPorts,
    ):
        self.proxy = proxy
        self.timeout = timeout
        self.headers = headers
        self._ports = ports
        if ports.xhs_international:
            self._host = "https://webapi.rednote.com"
            self._domain = "https://www.rednote.com"
        else:
            self._host = "https://edith.xiaohongshu.com"
            self._domain = "https://www.xiaohongshu.com"
        self.cookie_urls = [self._domain]
        self.IP_ERROR_STR = "Network connection error, please check network settings or restart"
        self.IP_ERROR_CODE = 300012
        self.SECURITY_LIMIT_CODE = 300011
        self.NOTE_NOT_FOUND_CODE = -510000
        self.NOTE_ABNORMAL_STR = "Note status abnormal, please check later"
        self.NOTE_ABNORMAL_CODE = -510001
        self.playwright_page = playwright_page
        self.cookie_dict = cookie_dict
        self._extractor = XiaoHongShuExtractor()
        self._manual_wait_budget = manual_wait_budget

    def _get_manual_wait_budget(self) -> XHSManualWaitBudget:
        if self._manual_wait_budget is None:
            self._manual_wait_budget = XHSManualWaitBudget.from_environment(
                monotonic=lambda: time.monotonic(),
            )
        return self._manual_wait_budget

    async def _pre_headers(self, url: str, params: Optional[Dict] = None, payload: Optional[Dict] = None) -> Dict:
        """请求头参数签名 (使用 xhshow 纯算法)

        Args:
            url: 请求 URI path
            params: GET 请求参数
            payload: POST 请求参数

        Returns:
            Dict: 签名后的请求头参数
        """
        if params is not None:
            data = params
            method = "GET"
        elif payload is not None:
            data = payload
            method = "POST"
        else:
            raise ValueError("params or payload is required")

        # 使用 xhshow 纯算法生成签名
        signs = sign_with_xhshow(
            uri=url,
            data=data,
            cookie_str=self.headers.get("Cookie", ""),
            method=method,
        )

        headers = {
            "X-S": signs["x-s"],
            "X-T": signs["x-t"],
            "x-S-Common": signs["x-s-common"],
            "X-B3-Traceid": signs["x-b3-traceid"],
        }
        self.headers.update(headers)
        return self.headers

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
        retry=retry_if_not_exception_type(
            (NoteNotFoundError, IPBlockError, PlatformRuntimeError)
        ),
    )
    async def request(self, method, url, **kwargs) -> Union[str, Any]:
        """
        Wrapper for httpx common request method, processes request response
        Args:
            method: Request method
            url: Request URL
            **kwargs: Other request parameters, such as headers, body, etc.

        Returns:

        """
        # return response.text
        return_response = kwargs.pop("return_response", False)
        async with self._ports.make_async_client(proxy=self.proxy) as client:
            response = await client.request(method, url, timeout=self.timeout, **kwargs)

        if response.status_code in {401, 403}:
            raise PlatformRuntimeError(
                f"XHS request HTTP {response.status_code}",
                code="login_required",
            )
        if response.status_code == 429:
            raise PlatformRuntimeError(
                "XHS request HTTP 429",
                code="rate_limited",
            )

        if response.status_code == 471 or response.status_code == 461:
            verify_type = response.headers.get("Verifytype", "")
            verify_uuid = response.headers.get("Verifyuuid", "")
            logger.warning(
                "[XiaoHongShuClient.request] API CAPTCHA requires operator verification: "
                f"Verifytype={verify_type}, Verifyuuid={verify_uuid}, status={response.status_code}"
            )
            budget = self._get_manual_wait_budget()
            ticket = budget.start("api_captcha_verification")
            try:
                try:
                    await run_required_api_captcha_verification(
                        self.playwright_page,
                        verify_type=verify_type,
                        verify_uuid=verify_uuid,
                        verify_biz=response.status_code,
                        timeout_seconds=ticket.remaining_seconds,
                        ports=self._ports.behavior,
                    )
                except Exception:
                    # The shared terminal reason wins whenever the operator
                    # budget elapsed during CAPTCHA navigation or observation,
                    # even if the bridge surfaced a different final error.
                    ticket.raise_if_exhausted()
                    raise
                ticket.raise_if_exhausted()
            finally:
                ticket.close()
            await self.update_cookies(
                browser_context=self.playwright_page.context,
                urls=self.cookie_urls,
            )
            raise DataFetchError("XHS API CAPTCHA completed; retrying the original request")

        response_data: Dict | None = None
        try:
            candidate_data = response.json()
            if isinstance(candidate_data, dict):
                response_data = candidate_data
        except (ValueError, TypeError):
            response_data = None
        response_code = (
            str(response_data.get("code")).strip()
            if response_data is not None and response_data.get("code") is not None
            else ""
        )
        if response_code == str(self.IP_ERROR_CODE):
            raise IPBlockError(self.IP_ERROR_STR)
        security_limit_code = getattr(self, "SECURITY_LIMIT_CODE", 300011)
        if response_code == str(security_limit_code):
            raise PlatformRuntimeError(
                f"XHS platform security limit, code {security_limit_code}",
                code=f"platform_security_limit_{security_limit_code}",
            )

        if return_response:
            return response.text
        data: Dict = response_data if response_data is not None else response.json()
        if data["success"]:
            return data.get("data", data.get("success", {}))
        # IP_ERROR_CODE / SECURITY_LIMIT_CODE are already handled above, before return_response.
        elif data["code"] in (self.NOTE_NOT_FOUND_CODE, self.NOTE_ABNORMAL_CODE):
            raise NoteNotFoundError(f"Note not found or abnormal, code: {data['code']}")
        else:
            err_msg = data.get("msg", None) or f"{response.text}"
            raise DataFetchError(err_msg)

    @staticmethod
    def _build_query_string(params: Dict) -> str:
        """Build URL query string with encoding matching browser behavior (commas not encoded)"""
        parts = []
        for key, value in params.items():
            value_str = str(value) if value is not None else ""
            parts.append(f"{key}={quote(value_str, safe=',')}")
        return "&".join(parts)

    async def get(self, uri: str, params: Optional[Dict] = None) -> Dict:
        """
        GET request, signs request headers
        Args:
            uri: Request route
            params: Request parameters

        Returns:

        """
        headers = await self._pre_headers(uri, params)
        # Build URL manually to ensure query string encoding matches the sign string
        # (httpx's default params encoding differs from browser/XHS frontend behavior)
        if params:
            full_url = f"{self._host}{uri}?{self._build_query_string(params)}"
        else:
            full_url = f"{self._host}{uri}"

        return await self.request(
            method="GET", url=full_url, headers=headers
        )

    async def post(self, uri: str, data: dict, **kwargs) -> Dict:
        """
        POST request, signs request headers
        Args:
            uri: Request route
            data: Request body parameters

        Returns:

        """
        headers = await self._pre_headers(uri, payload=data)
        json_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        return await self.request(
            method="POST",
            url=f"{self._host}{uri}",
            data=json_str,
            headers=headers,
            **kwargs,
        )

    async def get_note_media(self, url: str) -> Union[bytes, None]:
        async with self._ports.make_async_client(proxy=self.proxy) as client:
            try:
                response = await client.request("GET", url, timeout=self.timeout)
                response.raise_for_status()
                return response.content
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                logger.error(
                    f"[XiaoHongShuClient.get_note_media] HTTP {status} for {exc.request.url}"
                )
                raise classified_http_image_error(status, f"HTTP {status}") from exc
            except httpx.HTTPError as exc:  # transport error without an HTTP response
                logger.error(
                    f"[XiaoHongShuClient.get_note_media] {exc.__class__.__name__} "
                    f"for {exc.request.url} - {exc}"
                )
                raise ImageDownloadFetchError(
                    str(exc), code="image_download_retryable", retryable=True
                ) from exc

    async def query_self(self) -> Optional[Dict]:
        """
        Query self user info to check login state
        Returns:
            Dict: User info if logged in, None otherwise
        """
        uri = "/api/sns/web/v1/user/selfinfo"
        headers = await self._pre_headers(uri, params={})
        async with self._ports.make_async_client(proxy=self.proxy) as client:
            response = await client.get(
                f"{self._host}{uri}",
                headers=headers,
                timeout=min(self.timeout, 15),
            )
            if response.status_code == 200:
                data = response.json()
                response_code = (
                    str(data.get("code", "")).strip()
                    if isinstance(data, dict)
                    else ""
                )
                if response_code == str(self.IP_ERROR_CODE):
                    raise IPBlockError(self.IP_ERROR_STR)
                if response_code == str(self.SECURITY_LIMIT_CODE):
                    raise PlatformRuntimeError(
                        f"XHS platform security limit, code {self.SECURITY_LIMIT_CODE}",
                        code=f"platform_security_limit_{self.SECURITY_LIMIT_CODE}",
                    )
                return data
            if response.status_code in {401, 403}:
                return None
            if response.status_code == 429:
                raise PlatformRuntimeError(
                    "XHS self-info HTTP 429",
                    code="rate_limited",
                )
            if response.status_code in {461, 471}:
                raise PlatformRuntimeError(
                    f"XHS self-info HTTP {response.status_code}",
                    code="verification_required",
                )
            response.raise_for_status()
            raise DataFetchError(
                f"Unexpected XHS self-info HTTP {response.status_code}"
            )

    async def pong(self) -> bool:
        """
        Check if login state is still valid by querying self user info
        Returns:
            bool: True if logged in, False otherwise
        """
        logger.info("[XiaoHongShuClient.pong] Begin to check login state...")
        try:
            self_info: Dict = await self.query_self()
        except Exception as exc:
            logger.error(
                "[XiaoHongShuClient.pong] Login probe was inconclusive; "
                f"propagating {type(exc).__name__}: {exc}"
            )
            raise
        ping_flag = bool(
            self_info
            and self_info.get("data", {}).get("result", {}).get("success")
        )
        logger.info(f"[XiaoHongShuClient.pong] Login state result: {ping_flag}")
        return ping_flag

    async def update_cookies(self, browser_context: BrowserContext, urls: Optional[list[str]] = None):
        """
        Update cookies method provided by API client, usually called after successful login
        Args:
            browser_context: Browser context object

        Returns:

        """
        cookie_str, cookie_dict = await convert_browser_context_cookies(
            browser_context,
            urls=urls or self.cookie_urls,
        )
        self.headers["Cookie"] = cookie_str
        self.cookie_dict = cookie_dict

    async def get_note_by_keyword(
        self,
        keyword: str,
        search_id: str = get_search_id(),
        page: int = 1,
        page_size: int = 20,
        sort: SearchSortType = SearchSortType.GENERAL,
        note_type: SearchNoteType = SearchNoteType.ALL,
    ) -> Dict:
        """
        Search notes by keyword
        Args:
            keyword: Keyword parameter
            page: Page number
            page_size: Page data length
            sort: Search result sorting specification
            note_type: Type of note to search

        Returns:

        """
        uri = "/api/sns/web/v1/search/notes"
        data = {
            "keyword": keyword,
            "page": page,
            "page_size": page_size,
            "search_id": search_id,
            "sort": sort.value,
            "note_type": note_type.value,
        }
        return await self.post(uri, data)

    async def get_note_by_id(
        self,
        note_id: str,
        xsec_source: str,
        xsec_token: str,
    ) -> Dict:
        """
        Get note detail API
        Args:
            note_id: Note ID
            xsec_source: Channel source
            xsec_token: Token returned from search keyword result list

        Returns:

        """
        if xsec_source == "":
            xsec_source = "pc_search"

        data = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": 1},
            "xsec_source": xsec_source,
            "xsec_token": xsec_token,
        }
        uri = "/api/sns/web/v1/feed"
        res = await self.post(uri, data)
        if res and res.get("items"):
            res_dict: Dict = res["items"][0]["note_card"]
            return res_dict
        # When crawling frequently, some notes may have results while others don't
        logger.error(
            f"[XiaoHongShuClient.get_note_by_id] get note id:{note_id} empty and res:{res}"
        )
        return dict()

    async def get_creator_info(
        self, user_id: str, xsec_token: str = "", xsec_source: str = ""
    ) -> Optional[Dict]:
        """
        Get user profile brief information by parsing user homepage HTML
        The PC user homepage has window.__INITIAL_STATE__ variable, just parse it

        Args:
            user_id: User ID
            xsec_token: Verification token (optional, pass if included in URL)
            xsec_source: Channel source (optional, pass if included in URL)

        Returns:
            Dict: Creator information
        """
        # Build URI, add xsec parameters to URL if available
        uri = f"/user/profile/{user_id}"
        if xsec_token and xsec_source:
            uri = f"{uri}?xsec_token={xsec_token}&xsec_source={xsec_source}"

        html_content = await self.request(
            "GET", self._domain + uri, return_response=True, headers=self.headers
        )
        return self.extract_creator_info_from_html(html_content)

    def extract_creator_info_from_html(self, html_content: str) -> Optional[Dict]:
        """Expose creator parsing for HTML loaded through the signed-in browser."""
        return self._extractor.extract_creator_info_from_html(html_content)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
        retry=retry_if_not_exception_type(
            (RetryError, IPBlockError, PlatformRuntimeError)
        ),
    )
    async def get_note_by_id_from_html(
        self,
        note_id: str,
        xsec_source: str,
        xsec_token: str,
        enable_cookie: bool = False,
    ) -> Optional[Dict]:
        """
        Get note details by parsing note detail page HTML, this interface may fail, retry 3 times here
        copy from https://github.com/ReaJason/xhs/blob/eb1c5a0213f6fbb592f0a2897ee552847c69ea2d/xhs/core.py#L217-L259
        thanks for ReaJason
        Args:
            note_id:
            xsec_source:
            xsec_token:
            enable_cookie:

        Returns:

        """
        url = (
            f"{self._domain}/explore/"
            + note_id
            + f"?xsec_token={xsec_token}&xsec_source={xsec_source}"
        )
        copy_headers = self.headers.copy()
        if not enable_cookie:
            del copy_headers["Cookie"]

        html = await self.request(
            method="GET", url=url, return_response=True, headers=copy_headers
        )

        return self._extractor.extract_note_detail_from_html(note_id, html)
