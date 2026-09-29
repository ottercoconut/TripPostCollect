# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/weibo/client.py
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

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2023/12/23 15:40
# @Desc    : Weibo crawler API request client


# TripPostCollect T05：迁自 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303；仅拆分职责与注入依赖。
import asyncio
import json
import logging
import re
from typing import Dict, List, Optional, Union
from urllib.parse import urlencode, urlparse

import httpx
from httpx import Response
from playwright.async_api import BrowserContext, Page, Error as PlaywrightError
from tenacity import retry, stop_after_attempt, wait_fixed

from trippostcollect.application.contracts import WeiboClientPorts
from .models import DataFetchError, PlatformRuntimeError, SearchType
from .parser import _find_weibo_detail

logger = logging.getLogger("MediaCrawler")

def weibo_image_request_urls(image_url: str) -> List[str]:
    """Return idempotent proxy/direct candidates for one authoritative image URL."""

    parsed = urlparse(str(image_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return []
    hostname = parsed.hostname.lower()
    candidates: List[str] = []

    if hostname == "i1.wp.com":
        candidates.append(parsed.geturl())
        embedded = parsed.path.lstrip("/")
        embedded_host, separator, embedded_path = embedded.partition("/")
        if separator and embedded_host.lower().endswith(".sinaimg.cn"):
            candidates.append(f"https://{embedded_host}/{embedded_path}")
    elif hostname.endswith(".sinaimg.cn"):
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) >= 2:
            path_parts[0] = "large"
        normalized_path = "/" + "/".join(path_parts)
        direct = parsed._replace(path=normalized_path, query="", fragment="").geturl()
        candidates.extend((f"https://i1.wp.com/{hostname}{normalized_path}", direct))
    else:
        candidates.append(parsed.geturl())

    return list(dict.fromkeys(candidates))


class WeiboClient:

    def __init__(
        self,
        timeout=60,  # If media crawling is enabled, Weibo images need a longer timeout
        proxy=None,
        *,
        headers: Dict[str, str],
        playwright_page: Page,
        cookie_dict: Dict[str, str],
        ports: WeiboClientPorts,
        post_repair: bool = False,
    ):
        self.proxy = proxy
        self.timeout = timeout
        self.headers = headers
        self._host = "https://m.weibo.cn"
        self.cookie_urls = [self._host]
        self.playwright_page = playwright_page
        self.cookie_dict = cookie_dict
        self.ports = ports
        self.post_repair = post_repair

    @retry(stop=stop_after_attempt(5), wait=wait_fixed(3))
    async def request(self, method, url, **kwargs) -> Union[Response, Dict]:
        enable_return_response = kwargs.pop("return_response", False)
        allow_empty_search = kwargs.pop("allow_empty_search", False)
        async with self.ports.make_async_client(proxy=self.proxy) as client:
            response = await client.request(method, url, timeout=self.timeout, **kwargs)

        if enable_return_response:
            return response

        try:
            data: Dict = response.json()
        except json.decoder.JSONDecodeError:
            # issue: #771 Search API returns error 432, retry multiple times + update h5 cookies
            logger.error(f"[WeiboClient.request] request {method}:{url} err code: {response.status_code} res:{response.text}")
            await self.playwright_page.goto(self._host)
            await asyncio.sleep(2)
            await self.update_cookies(browser_context=self.playwright_page.context)
            raise DataFetchError(f"get response code error: {response.status_code}")

        ok_code = data.get("ok")
        if ok_code == 0:  # response error
            response_data = data.get("data") or {}
            if (
                allow_empty_search
                and str(data.get("msg") or "").strip() == "这里还没有内容"
                and not (response_data.get("cards") or [])
            ):
                return response_data
            logger.error(f"[WeiboClient.request] request {method}:{url} err, res:{data}")
            raise DataFetchError(data.get("msg", "response error"))
        elif ok_code != 1:  # unknown error
            logger.error(f"[WeiboClient.request] request {method}:{url} err, res:{data}")
            raise DataFetchError(data.get("msg", "unknown error"))
        else:  # response right
            return data.get("data", {})

    async def get(self, uri: str, params=None, headers=None, **kwargs) -> Union[Response, Dict]:
        final_uri = uri
        if isinstance(params, dict):
            final_uri = (f"{uri}?"
                         f"{urlencode(params)}")

        if headers is None:
            headers = self.headers
        return await self.request(method="GET", url=f"{self._host}{final_uri}", headers=headers, **kwargs)

    async def post(self, uri: str, data: dict) -> Dict:
        json_str = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
        return await self.request(method="POST", url=f"{self._host}{uri}", data=json_str, headers=self.headers)

    async def pong(self) -> bool:
        """get a note to check if login state is ok"""
        logger.info("[WeiboClient.pong] Begin pong weibo...")
        ping_flag = False
        try:
            uri = "/api/config"
            resp_data: Dict = await self.request(method="GET", url=f"{self._host}{uri}", headers=self.headers)
            if resp_data.get("login"):
                ping_flag = True
            else:
                logger.error("[WeiboClient.pong] cookie may be invalid and again login...")
        except Exception as e:
            logger.error(f"[WeiboClient.pong] Pong weibo failed: {e}, and try to login again...")
            ping_flag = False
        return ping_flag

    async def update_cookies(self, browser_context: BrowserContext, urls: Optional[List[str]] = None):
        """
        Update cookies from browser context
        :param browser_context: Browser context
        :param urls: Optional list of URLs to filter cookies (e.g., ["https://m.weibo.cn"])
                     If provided, only cookies for these URLs will be retrieved
        """
        cookie_urls = urls or self.cookie_urls
        cookie_str, cookie_dict = await self.ports.convert_browser_context_cookies(
            browser_context,
            urls=cookie_urls,
        )
        self.headers["Cookie"] = cookie_str
        self.cookie_dict = cookie_dict
        logger.info(
            f"[WeiboClient.update_cookies] Cookie updated successfully for {cookie_urls}, total: {len(cookie_dict)} cookies"
        )

    async def get_note_by_keyword(
        self,
        keyword: str,
        page: int = 1,
        search_type: SearchType = SearchType.DEFAULT,
    ) -> Dict:
        """
        search note by keyword
        :param keyword: Search keyword for Weibo
        :param page: Pagination parameter - current page number
        :param search_type: Search type, see SearchType enum in weibo/field.py
        :return:
        """
        uri = "/api/container/getIndex"
        containerid = f"100103type={search_type.value}&q={keyword}"
        params = {
            "containerid": containerid,
            "page_type": "searchall",
            "page": page,
        }
        return await self.get(uri, params, allow_empty_search=True)




    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1), reraise=True)
    async def _get_note_info_direct(self, note_id: str) -> Dict:
        """
        Get note details by note ID
        :param note_id:
        :return:
        """
        url = f"{self._host}/detail/{note_id}"
        async with self.ports.make_async_client(proxy=self.proxy) as client:
            response = await client.request("GET", url, timeout=self.timeout, headers=self.headers)
            if response.status_code != 200:
                if response.status_code in {401, 403}:
                    raise PlatformRuntimeError(
                        f"get weibo detail HTTP {response.status_code}",
                        code="login_required",
                    )
                if response.status_code == 429:
                    raise PlatformRuntimeError(
                        "get weibo detail HTTP 429",
                        code="rate_limited",
                    )
                raise DataFetchError(f"get weibo detail err: {response.text}")
            match = re.search(r'var \$render_data = (\[.*?\])\[0\]', response.text, re.DOTALL)
            if match:
                render_data_json = match.group(1)
                render_data_dict = json.loads(render_data_json)
                note_detail = render_data_dict[0].get("status")
                note_item = {"mblog": note_detail}
                return note_item
            else:
                raise DataFetchError(
                    "get weibo detail err: $render_data value not found"
                )

    async def get_note_image(self, image_url: str) -> bytes:
        candidates = weibo_image_request_urls(image_url)
        if not candidates:
            raise self.ports.image_error(
                "invalid Weibo image URL",
                code="image_source_unavailable",
                retryable=False,
            )
        last_error: Exception | None = None
        headers = dict(getattr(self, "headers", {}) or {})
        headers["Referer"] = headers.get("Referer") or "https://m.weibo.cn/"
        headers["Accept"] = "image/avif,image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8"
        for final_uri in candidates:
            try:
                async with self.ports.make_async_client(proxy=self.proxy) as client:
                    response = await client.request(
                        "GET",
                        final_uri,
                        timeout=self.timeout,
                        headers=headers,
                    )
                response.raise_for_status()
                if len(response.content) > self.ports.image_max_bytes:
                    last_error = self.ports.image_error(
                        "Weibo image exceeded byte limit",
                        code="image_too_large",
                        retryable=False,
                        http_status=response.status_code,
                    )
                    continue
                return response.content
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                logger.error(
                    f"[WeiboClient.get_note_image] HTTP {status} for {exc.request.url}"
                )
                classified = self.ports.classified_http_image_error(status, f"HTTP {status}")
                if classified.code in {"image_auth_required", "image_rate_limited"}:
                    raise classified from exc
                last_error = classified
            except httpx.HTTPError as exc:  # transport error without an HTTP response
                logger.error(
                    f"[WeiboClient.get_note_image] {exc.__class__.__name__} "
                    f"for {exc.request.url} - {exc}"
                )
                last_error = self.ports.image_error(
                    str(exc), code="image_download_retryable", retryable=True
                )
        if last_error is not None:
            raise last_error
        raise self.ports.image_error(
            "Weibo image candidates exhausted",
            code="image_source_unavailable",
            retryable=False,
        )

    async def get_note_info_by_id(self, note_id: str) -> Dict:
        """修复轮显式走原浏览器回退；普通详情保留原 Tenacity 层。"""
        if self.post_repair:
            return await self._get_note_info_repair(note_id)
        return await self._get_note_info_direct(note_id)

    async def _get_note_info_repair(self, note_id: str) -> Dict:
        original_error: Exception | None = None
        try:
            result = await self._get_note_info_direct(note_id)
            original_detail = _find_weibo_detail(result, note_id)
            if original_detail is not None and str(original_detail.get("text") or "").strip():
                return {"mblog": original_detail}
        except DataFetchError as exc:
            original_error = exc

        page = getattr(self, "playwright_page", None)
        if page is None:
            if original_error is not None:
                raise original_error
            raise DataFetchError(
                "get weibo detail err: browser detail fallback has no Playwright page"
            )
        timeout_ms = self.ports.detail_timeout()
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
                            raise PlatformRuntimeError(
                                f"get weibo browser detail HTTP {api_status}",
                                code="login_required",
                            )
                        if api_status == 429:
                            raise PlatformRuntimeError(
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
                            logger.warning(
                                "[TripPostCollect] Used browser-context Weibo detail API "
                                f"for note_id:{note_id}"
                            )
                            return {"mblog": detail}
                        attempts.append(f"browser_api:http_{api_status}:empty_detail")
                    except PlatformRuntimeError:
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
            raise DataFetchError(
                "get weibo detail err: browser detail navigation failed "
                f"({type(exc).__name__})"
            ) from (original_error or exc)

        detail = _find_weibo_detail(page_state, note_id)
        if detail is None or not str(detail.get("text") or "").strip():
            raise DataFetchError(
                "get weibo detail err: browser fallbacks have no matching full mblog; "
                f"attempts={attempts}"
            ) from original_error
        detail = dict(detail)
        detail["id"] = str(detail.get("id") or detail.get("idstr") or note_id)
        logger.warning(
            f"[TripPostCollect] Used browser-native Weibo detail fallback for note_id:{note_id}"
        )
        return {"mblog": detail}


def _weibo_detail_api_url(note_id: str) -> str:
    return f"https://m.weibo.cn/statuses/show?id={note_id}"
