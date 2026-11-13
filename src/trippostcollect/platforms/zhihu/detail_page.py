"""#185 知乎详情改为点击搜索卡片，在新标签读取页面状态。

每条未知候选都打开详情页，包括搜索结果里正文已经完整的候选。正文、图片、点赞、
评论、收藏、分享和发布时间以详情页 ``script#js-initialData`` 为准。不发浏览器外详情请求。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from trippostcollect.platforms.zhihu import models as constant
from trippostcollect.platforms.zhihu.models import ZhihuContent, ZhihuDetailFetchError
from trippostcollect.platforms.zhihu.parser import (
    ZhihuExtractor, apply_author_page, user_from_initial_data,
)
from trippostcollect.runtime.human_flow import human_scroll, load_behavior_profile
from trippostcollect.runtime.human_pointer import ElementNotInView, pointer_for


logger = logging.getLogger("TripPostCollect")

DETAIL_ATTEMPTS = 3
DETAIL_RETRY_PAUSE_SECONDS = 1.0
DETAIL_DWELL_SECONDS = (5.0, 9.0)
DETAIL_TAB_TIMEOUT_MS = 8_000
DETAIL_LOAD_TIMEOUT_MS = 15_000

# 待实测核实。侦察时会话已登录，没有落到登录页或安全验证页；下面的 URL 与可见特征是假设。
# 登录页：www.zhihu.com/signin（以及 /login、/signup）。页面上另认侦察用过的 .signFlowModal
# 和登录框文案「密码登录」。
ZHIHU_LOGIN_URL_RE = re.compile(
    r"(?:^|zhihu\.com)/(?:signin|login|signup)(?:[/?#]|$)",
    re.I,
)
ZHIHU_LOGIN_LOCATORS = (
    ".signFlowModal",
    "text=密码登录",
)
# 安全验证页：www.zhihu.com/account/unhuman。页面上另认「安全验证」「人机验证」
# 「请完成验证」「拖动滑块」。
ZHIHU_VERIFY_URL_RE = re.compile(
    r"(?:^|zhihu\.com)/(?:account/unhuman|challenge|captcha)(?:[/?#]|$)",
    re.I,
)
ZHIHU_VERIFY_LOCATORS = (
    "text=安全验证",
    "text=人机验证",
    "text=请完成验证",
    "text=拖动滑块",
)

DETAIL_CARD_MISSING = "detail_card_missing"
DETAIL_TAB_NOT_OPENED = "detail_tab_not_opened"
DETAIL_ENTITY_MISSING = "detail_entity_missing"
DETAIL_CONTENT_MISSING = "detail_content_missing"
BROWSER_TARGET_CLOSED = "browser_target_closed"
# 没有 /people/ 作者链接，或新标签没打开。候选级失败，有限重试后进入已处理记忆。
AUTHOR_LINK_MISSING = "author_link_missing"
# 作者页页面状态里没有该 url_token 的用户。同样是候选级失败。
AUTHOR_PROFILE_MISSING = "author_profile_missing"
# 点作者之前的节奏停顿（门禁要求停在点击之前）。作者标签打开后的停留另计。
AUTHOR_CLICK_PAUSE_SECONDS = (1.0, 2.0)
AUTHOR_TAB_HOLD_SECONDS = (5.0, 9.0)
_PEOPLE_TOKEN_RE = re.compile(r"/people/([^/?#]+)")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")

# 侦察里能点开的标题链接，收窄到本条候选的 id。answer_heading、zhuanlan_anchor 在前。
ANSWER_TITLE_SELECTORS = (
    "h2 a[href*='/answer/{content_id}']",
    ".ContentItem-title a[href*='/answer/{content_id}']",
    "a[href*='/answer/{content_id}']",
)
ARTICLE_TITLE_SELECTORS = (
    "a[href*='zhuanlan.zhihu.com/p/{content_id}']",
    "a[href*='/p/{content_id}']",
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def blocking_code_for_url(url: str) -> str | None:
    """URL 命中登录或安全验证时返回运行级阻断码。验证先于登录判断。"""

    text = str(url or "")
    path = urlsplit(text).path or ""
    for probe in (text, path):
        if ZHIHU_VERIFY_URL_RE.search(probe):
            return "captcha_detected"
        if ZHIHU_LOGIN_URL_RE.search(probe):
            return "login_required"
    return None


def people_token(url: str) -> str:
    """作者页路径 ``/people/{url_token}`` 中的 token。不是作者页时为空串。"""

    match = _PEOPLE_TOKEN_RE.search(urlsplit(str(url or "")).path)
    return match.group(1) if match else ""


def _href_targets(href: str, content: ZhihuContent) -> bool:
    path = urlsplit(str(href or "")).path
    content_id = str(content.content_id or "")
    if content.content_type == constant.ANSWER_NAME:
        return re.search(rf"/answer/{re.escape(content_id)}(?:/|$)", path) is not None
    return re.search(rf"/p/{re.escape(content_id)}(?:/|$)", path) is not None


def _closed_error(exc: BaseException) -> bool:
    if isinstance(exc, PlaywrightError) and not isinstance(exc, PlaywrightTimeoutError):
        text = str(exc).lower()
        return "closed" in text or "target page" in text
    text = str(exc).lower()
    return "target closed" in text or "has been closed" in text or "browser has been closed" in text


def _page_closed(page: Any) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:
        return True


class ZhihuDetailPages:
    """在搜索页点击标题打开详情，或按已知 URL 直接打开，然后读取页面状态。"""

    def __init__(self, capture: Any, extractor: ZhihuExtractor, search_page: Any) -> None:
        self.capture = capture
        self.extractor = extractor
        self.search_page = search_page
        self.channel = capture.channel
        self._opened: Any = None

    async def open_from_search(self, content: ZhihuContent) -> ZhihuContent:
        return await self._attempts(content, direct_url="")

    async def open_direct(self, content: ZhihuContent, url: str) -> ZhihuContent:
        return await self._attempts(content, direct_url=url)

    async def _attempts(self, content: ZhihuContent, *, direct_url: str) -> ZhihuContent:
        last_code = DETAIL_ENTITY_MISSING
        for attempt in range(1, DETAIL_ATTEMPTS + 1):
            try:
                return await self._open_once(content, direct_url=direct_url)
            except ZhihuDetailFetchError as exc:
                if exc.runtime_blocking:
                    raise
                last_code = exc.code
                logger.warning(
                    "[ZhihuDetailPages] detail attempt failed "
                    f"content_id={content.content_id} type={content.content_type} "
                    f"attempt={attempt} code={exc.code}"
                )
                if attempt < DETAIL_ATTEMPTS and DETAIL_RETRY_PAUSE_SECONDS > 0:
                    await asyncio.sleep(DETAIL_RETRY_PAUSE_SECONDS)
        raise ZhihuDetailFetchError(str(content.content_id or ""), last_code, attempts=DETAIL_ATTEMPTS)

    async def _open_once(self, content: ZhihuContent, *, direct_url: str) -> ZhihuContent:
        self._raise_if_closed(self.search_page, "search page")
        self._opened = None
        detail_page: Any = None
        try:
            if direct_url:
                detail_page, trigger_id = await self._open_direct_tab(content, direct_url)
            else:
                detail_page, trigger_id = await self._click_open(content)
            detail_page = detail_page or self._opened
            await self._wait_loaded(detail_page)
            await self._raise_if_blocking(detail_page)
            await self._raise_if_blocking(self.search_page)
            state_text, visit_id = await self._read_initial_data(detail_page, content, trigger_id)
            parsed, code = self.extractor.content_from_initial_data(
                state_text, str(content.content_id or ""), content.content_type,
            )
            await self._dwell(detail_page, content)
            if _page_closed(detail_page) or _page_closed(self.search_page):
                raise self._runtime(content, BROWSER_TARGET_CLOSED)
            if parsed is None or not visit_id:
                raise self._candidate(content, code or DETAIL_ENTITY_MISSING)
            author_visit = await self._visit_author(detail_page, parsed, visit_id)
            refs = {
                "ledger_id": self.channel.ledger.ledger_id,
                "note_detail": visit_id,
                "creator_profile": author_visit,
                "images": [],
            }
            if not direct_url:
                search_visit = str(getattr(self.capture, "last_search_visit_id", "") or "")
                if search_visit:
                    refs["search_results"] = search_visit
            parsed.content_visit_refs = refs
            return parsed
        except ZhihuDetailFetchError:
            raise
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(self.search_page) or (
                detail_page is not None and _page_closed(detail_page)
            ):
                raise self._runtime(content, BROWSER_TARGET_CLOSED) from exc
            raise self._candidate(content, DETAIL_TAB_NOT_OPENED) from exc
        finally:
            await self._best_effort_close(detail_page or self._opened, content)
            if _page_closed(self.search_page):
                raise self._runtime_bare(BROWSER_TARGET_CLOSED)
            await self._best_effort_restore()

    async def _click_open(self, content: ZhihuContent) -> tuple[Any, str]:
        link = await self._find_title_link(self.search_page, content)
        if link is None:
            await self._scroll_search()
            link = await self._find_title_link(self.search_page, content)
        if link is None:
            raise self._candidate(content, DETAIL_CARD_MISSING)
        if not await self._is_visible(link):
            try:
                await pointer_for(self.search_page).scroll_into_view(link)
            except ElementNotInView as exc:
                raise self._candidate(content, DETAIL_CARD_MISSING) from exc
        trigger_id = self.channel.action(
            "click_card",
            page_role="primary",
            target={"content_id": str(content.content_id or ""), "content_type": content.content_type},
        )
        context = self.channel.context
        try:
            async with context.expect_page(timeout=DETAIL_TAB_TIMEOUT_MS) as opened:
                await link.click(timeout=DETAIL_TAB_TIMEOUT_MS)
            detail_page = await opened.value
        except (PlaywrightTimeoutError, TimeoutError) as exc:
            raise self._candidate(content, DETAIL_TAB_NOT_OPENED) from exc
        self._opened = detail_page
        self.channel.attach(detail_page, "detail")
        return detail_page, trigger_id

    async def _open_direct_tab(self, content: ZhihuContent, url: str) -> tuple[Any, str]:
        context = self.channel.context
        detail_page = await context.new_page()
        self._opened = detail_page
        self.channel.attach(detail_page, "detail")
        trigger_id = self.channel.action(
            "navigate",
            page_role="detail",
            target={"content_id": str(content.content_id or ""), "content_type": content.content_type},
        )
        try:
            await detail_page.goto(url, wait_until="domcontentloaded", timeout=DETAIL_LOAD_TIMEOUT_MS)
        except (PlaywrightTimeoutError, TimeoutError) as exc:
            raise self._candidate(content, DETAIL_TAB_NOT_OPENED) from exc
        return detail_page, trigger_id

    async def _find_title_link(self, page: Any, content: ZhihuContent) -> Any | None:
        content_id = str(content.content_id or "")
        if not _SAFE_ID.fullmatch(content_id):
            return None
        templates = (
            ANSWER_TITLE_SELECTORS
            if content.content_type == constant.ANSWER_NAME
            else ARTICLE_TITLE_SELECTORS
        )
        for template in templates:
            locator = page.locator(template.format(content_id=content_id))
            try:
                count = await locator.count()
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                if _closed_error(exc) or _page_closed(page):
                    raise self._runtime(content, BROWSER_TARGET_CLOSED) from exc
                continue
            for index in range(count):
                item = locator.nth(index)
                try:
                    href = await item.get_attribute("href")
                except (PlaywrightError, PlaywrightTimeoutError) as exc:
                    if _closed_error(exc) or _page_closed(page):
                        raise self._runtime(content, BROWSER_TARGET_CLOSED) from exc
                    continue
                if _href_targets(str(href or ""), content):
                    return item
        return None

    async def _read_initial_data(self, page: Any, content: ZhihuContent, trigger_id: str) -> tuple[Any, str | None]:
        return await self.channel.read_state(
            page,
            "zhihu.initial_data",
            stage="note_detail",
            subject_id=str(content.content_id or ""),
            trigger=trigger_id,
        )

    async def _visit_author(self, detail_page: Any, content: ZhihuContent, detail_visit_id: str) -> str:
        """在详情标签停留结束、关闭前点击作者名，读作者页状态，停留后关闭作者标签。"""

        token = str(content.creator_url_token or "")
        if not _SAFE_TOKEN.fullmatch(token):
            raise self._candidate(content, AUTHOR_LINK_MISSING)
        link = await self._find_author_link(detail_page, content, token)
        if link is None:
            raise self._candidate(content, AUTHOR_LINK_MISSING)
        if not await self._is_visible(link):
            try:
                await pointer_for(detail_page).scroll_into_view(link)
            except ElementNotInView as exc:
                raise self._candidate(content, AUTHOR_LINK_MISSING) from exc
        await self._pause(detail_page, "creator_profile", *AUTHOR_CLICK_PAUSE_SECONDS, page_role="detail")
        click_id = self.channel.action(
            "click_author",
            page_role="detail",
            target={"content_id": str(content.content_id or "")},
            from_visit=detail_visit_id,
        )
        try:
            async with self.channel.context.expect_page(timeout=DETAIL_TAB_TIMEOUT_MS) as opened:
                await link.click(timeout=DETAIL_TAB_TIMEOUT_MS)
            author_page = await opened.value
        except (PlaywrightTimeoutError, TimeoutError) as exc:
            raise self._candidate(content, AUTHOR_LINK_MISSING) from exc
        self.channel.attach(author_page, "author")
        try:
            await self._wait_loaded(author_page)
            await self._raise_if_blocking(author_page)
            if _page_closed(detail_page) or _page_closed(self.search_page):
                raise self._runtime(content, BROWSER_TARGET_CLOSED)
            url_token = people_token(getattr(author_page, "url", "") or "")
            if not url_token:
                raise self._candidate(content, AUTHOR_PROFILE_MISSING)
            state_text, visit_id = await self.channel.read_state(
                author_page,
                "zhihu.initial_data",
                stage="creator_profile",
                subject_id=url_token,
                trigger=click_id,
                url_user_id=url_token,
            )
            user = user_from_initial_data(state_text, url_token)
            if user is None or not visit_id:
                raise self._candidate(content, AUTHOR_PROFILE_MISSING)
            apply_author_page(content, user, url_token)
            await self._pause(author_page, "author_tab", *AUTHOR_TAB_HOLD_SECONDS, page_role="author")
            return visit_id
        finally:
            await self._close_author_tab(author_page, content)
            await self._bring_detail_forward(detail_page)

    async def _find_author_link(self, page: Any, content: ZhihuContent, token: str) -> Any | None:
        locator = page.locator(f"a[href*='/people/{token}']")
        try:
            count = await locator.count()
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(page):
                raise self._runtime(content, BROWSER_TARGET_CLOSED) from exc
            return None
        for index in range(count):
            item = locator.nth(index)
            try:
                href = await item.get_attribute("href")
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                if _closed_error(exc) or _page_closed(page):
                    raise self._runtime(content, BROWSER_TARGET_CLOSED) from exc
                continue
            if people_token(str(href or "")) == token:
                return item
        return None

    async def _pause(
        self, page: Any, stage: str, low: float, high: float, *, page_role: str,
    ) -> float:
        seconds = random.uniform(low, high)
        start_seq = self.channel.pause_started()
        if seconds > 0:
            waiter = getattr(page, "wait_for_timeout", None)
            if waiter is None:
                await asyncio.sleep(seconds)
            else:
                await waiter(int(seconds * 1000))
        self.channel.pause_finished(stage, start_seq, seconds, "n/a", page_role=page_role)
        return seconds

    async def _close_author_tab(self, page: Any, content: ZhihuContent) -> None:
        if page is None or _page_closed(page):
            return
        try:
            self.channel.action(
                "close_tab",
                page_role="author",
                target={"content_id": str(content.content_id or "")},
            )
            await page.close()
        except Exception as exc:
            logger.info(
                "[ZhihuDetailPages] author tab close failed "
                f"content_id={content.content_id} error={type(exc).__name__}"
            )

    async def _bring_detail_forward(self, page: Any) -> None:
        bring = getattr(page, "bring_to_front", None)
        if bring is None or _page_closed(page):
            return
        try:
            await bring()
        except Exception as exc:
            logger.info(
                "[ZhihuDetailPages] could not bring the detail page forward "
                f"error={type(exc).__name__}"
            )

    async def _dwell(self, page: Any, content: ZhihuContent) -> None:
        seconds = random.uniform(*DETAIL_DWELL_SECONDS)
        start_seq = self.channel.pause_started()
        began = time.monotonic()
        await self._scroll_detail(page)
        remaining = seconds - (time.monotonic() - began)
        if remaining > 0:
            await asyncio.sleep(remaining)
        self.channel.pause_finished(
            "note_detail", start_seq, seconds, "n/a", page_role="detail",
        )
        logger.info(
            "[ZhihuDetailPages] detail dwell "
            f"content_id={content.content_id} seconds={seconds:.1f}"
        )

    async def _scroll_detail(self, page: Any) -> None:
        try:
            profile = load_behavior_profile("social_high_risk", strict=True)
            await human_scroll(page, profile, intent="detail_page", max_passes=1)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(page):
                raise
        except Exception as exc:
            logger.info(
                "[ZhihuDetailPages] detail scroll skipped "
                f"error={type(exc).__name__}"
            )

    async def _scroll_search(self) -> None:
        try:
            profile = load_behavior_profile("social_high_risk", strict=True)
            await human_scroll(self.search_page, profile, intent="find_detail_card", max_passes=1)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(self.search_page):
                raise self._runtime_bare(BROWSER_TARGET_CLOSED) from exc
        except Exception as exc:
            logger.info(
                "[ZhihuDetailPages] search scroll skipped "
                f"error={type(exc).__name__}"
            )

    async def _best_effort_close(self, page: Any, content: ZhihuContent) -> None:
        if page is None or _page_closed(page):
            return
        try:
            self.channel.action(
                "close_tab",
                page_role="detail",
                target={
                    "content_id": str(content.content_id or ""),
                    "content_type": content.content_type,
                },
            )
            await page.close()
        except Exception as exc:
            logger.info(
                "[ZhihuDetailPages] detail tab close failed "
                f"content_id={content.content_id} error={type(exc).__name__}"
            )

    async def _best_effort_restore(self) -> None:
        bring = getattr(self.search_page, "bring_to_front", None)
        if bring is None or _page_closed(self.search_page):
            return
        try:
            await bring()
        except Exception as exc:
            logger.info(
                "[ZhihuDetailPages] could not bring the search page forward "
                f"error={type(exc).__name__}"
            )

    async def _wait_loaded(self, page: Any) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=DETAIL_LOAD_TIMEOUT_MS)
        except (PlaywrightTimeoutError, TimeoutError):
            return
        except (PlaywrightError,) as exc:
            if _closed_error(exc) or _page_closed(page):
                raise

    async def _raise_if_blocking(self, page: Any) -> None:
        self._raise_if_closed(page, "detail page")
        code = blocking_code_for_url(getattr(page, "url", "") or "")
        if code is None and await self._markers_visible(page, ZHIHU_VERIFY_LOCATORS):
            code = "captcha_detected"
        if code is None and await self._markers_visible(page, ZHIHU_LOGIN_LOCATORS):
            code = "login_required"
        if code is not None:
            raise self._runtime_bare(code)

    async def _markers_visible(self, page: Any, selectors: tuple[str, ...]) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if await locator.count() == 0:
                    continue
                if await locator.first.is_visible():
                    return True
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                if _closed_error(exc) or _page_closed(page):
                    raise self._runtime_bare(BROWSER_TARGET_CLOSED) from exc
            except Exception:
                continue
        return False

    async def _is_visible(self, locator: Any) -> bool:
        try:
            return bool(await locator.is_visible())
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc):
                raise self._runtime_bare(BROWSER_TARGET_CLOSED) from exc
            return False

    def _raise_if_closed(self, page: Any, where: str) -> None:
        if _page_closed(page):
            logger.error(f"[ZhihuDetailPages] {where} is closed")
            raise self._runtime_bare(BROWSER_TARGET_CLOSED)

    def _candidate(self, content: ZhihuContent, code: str) -> ZhihuDetailFetchError:
        return ZhihuDetailFetchError(str(content.content_id or ""), code, attempts=1)

    def _runtime(self, content: ZhihuContent, code: str) -> ZhihuDetailFetchError:
        return ZhihuDetailFetchError(
            str(content.content_id or ""), code, attempts=1, runtime_blocking=True,
        )

    def _runtime_bare(self, code: str) -> ZhihuDetailFetchError:
        return ZhihuDetailFetchError("", code, attempts=1, runtime_blocking=True)
