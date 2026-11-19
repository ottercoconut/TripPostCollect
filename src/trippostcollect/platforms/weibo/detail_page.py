"""#211 微博详情：搜索页点击时间链接，或修复链 navigate 打开桌面详情。

不另开作者页，不走浏览器外详情请求。作者资料来自同一标签的 sideUser。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from trippostcollect.platforms.weibo.models import WeiboDetailFetchError
from trippostcollect.platforms.weibo.parser import (
    AUTHOR_PROFILE_MISSING,
    CREATED_AT_UNPARSEABLE,
    DETAIL_CONTENT_EMPTY,
    parse_weibo_detail,
    show_matches,
    show_user_id,
    side_user_matches,
    side_user_of,
)
from trippostcollect.platforms.weibo.search_page import (
    WEIBO_SHOW_RULE,
    WEIBO_SIDE_USER_RULE,
    blocking_code_for_page,
)
from trippostcollect.runtime.page_capture import CapturedResponse


logger = logging.getLogger("TripPostCollect")

DETAIL_ATTEMPTS = 3
DETAIL_RETRY_PAUSE_SECONDS = 1.0
DETAIL_DWELL_SECONDS = (5.0, 9.0)
SEARCH_RESULT_PAUSE_SECONDS = (1.2, 2.8)
DETAIL_TAB_TIMEOUT_MS = 8_000
DETAIL_LOAD_TIMEOUT_MS = 15_000
DETAIL_RESPONSE_TIMEOUT_SECONDS = 15.0

DETAIL_CARD_MISSING = "detail_card_missing"
DETAIL_TAB_NOT_OPENED = "detail_tab_not_opened"
DETAIL_SHOW_MISSING = "detail_show_missing"
DETAIL_UNAVAILABLE = "detail_unavailable"
BROWSER_TARGET_CLOSED = "browser_target_closed"
LOGIN_REQUIRED = "login_required"
RATE_LIMITED = "rate_limited"
BLOCKED_OR_FORBIDDEN = "blocked_or_forbidden"
CAPTCHA_DETECTED = "captcha_detected"

SHOW_STATUS_CODES = {
    401: LOGIN_REQUIRED,
    403: LOGIN_REQUIRED,
    429: RATE_LIMITED,
    418: BLOCKED_OR_FORBIDDEN,
    432: BLOCKED_OR_FORBIDDEN,
}


@dataclass(frozen=True)
class WeiboOpened:
    record: dict[str, Any] | None = None
    video: bool = False


def _closed_error(exc: BaseException) -> bool:
    if isinstance(exc, PlaywrightError) and not isinstance(exc, PlaywrightTimeoutError):
        text = str(exc).lower()
        return "closed" in text or "target page" in text
    text = str(exc).lower()
    return "target closed" in text or "has been closed" in text or "browser has been closed" in text


def _page_closed(page: Any) -> bool:
    closed = getattr(page, "is_closed", None)
    if closed is None:
        return False
    try:
        return bool(closed() if callable(closed) else closed)
    except Exception:
        return True


def _navigation_status(response: Any) -> int | None:
    if response is None:
        return None
    status = getattr(response, "status", None)
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    return status


def _repair_identity(url: str) -> tuple[str, str]:
    """``https://weibo.com/{作者平台 ID}/{mid}``。不符合时两个字段都为空。"""

    parts = urlsplit(str(url or "").strip())
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in {"weibo.com", "www.weibo.com"}:
        return "", ""
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) != 2:
        return "", ""
    author_id, mid = segments
    if not author_id.isalnum() or not mid.isascii() or not mid.isdigit():
        return "", ""
    return author_id, mid


def _href_matches(href: str, candidate: Any) -> bool:
    text = str(href or "").strip()
    if text.startswith("//"):
        text = "https:" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if host not in {"weibo.com", "www.weibo.com"}:
        return False
    segments = [segment for segment in parts.path.split("/") if segment]
    uid = str(getattr(candidate, "uid", "") or "")
    mblogid = str(getattr(candidate, "mblogid", "") or "")
    return len(segments) >= 2 and segments[0] == uid and segments[1] == mblogid


class WeiboDetailPages:
    """在搜索页点击卡片时间链接，截获匹配的 show 与 sideUser。"""

    def __init__(self, capture: Any, search_page: Any) -> None:
        self.capture = capture
        self.search_page = search_page
        self.channel = capture.channel
        self._opened: Any = None

    async def open_from_search(self, candidate: Any) -> WeiboOpened:
        mid = str(getattr(candidate, "mid", "") or "")
        last_code = DETAIL_SHOW_MISSING
        for attempt in range(1, DETAIL_ATTEMPTS + 1):
            try:
                return await self._open_once(candidate)
            except WeiboDetailFetchError as exc:
                if exc.runtime_blocking or not exc.retryable:
                    exc.attempts = attempt
                    raise
                last_code = exc.code
                logger.warning(
                    "[WeiboDetailPages] detail attempt failed mid=%s attempt=%s code=%s",
                    mid,
                    attempt,
                    exc.code,
                )
                if attempt < DETAIL_ATTEMPTS and DETAIL_RETRY_PAUSE_SECONDS > 0:
                    await asyncio.sleep(DETAIL_RETRY_PAUSE_SECONDS)
        raise WeiboDetailFetchError(mid, last_code, attempts=DETAIL_ATTEMPTS, retryable=False)

    async def open_by_navigate(self, url: str) -> WeiboOpened:
        """修复链：navigate 打开 ``https://weibo.com/{uid}/{mid}``，截获规则与点击相同。"""

        author_id, mid = _repair_identity(url)
        if not author_id or not mid:
            raise self._candidate(mid, DETAIL_UNAVAILABLE, retryable=False)
        self._opened = None
        detail_page: Any = None
        try:
            detail_page = await self._new_detail_page()
            self._opened = detail_page
            self.channel.attach(detail_page, "detail")
            after_seq = self.channel.ledger.last_seq
            trigger_id = self.channel.action(
                "navigate",
                page_role="detail",
                target={"note_id": mid, "mid": mid},
            )
            try:
                response = await detail_page.goto(
                    url, wait_until="domcontentloaded", timeout=DETAIL_LOAD_TIMEOUT_MS,
                )
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                if _closed_error(exc):
                    raise self._runtime(mid, BROWSER_TARGET_CLOSED) from exc
                raise self._candidate(mid, DETAIL_TAB_NOT_OPENED) from exc
            status = _navigation_status(response)
            if status in (401, 403):
                raise self._runtime(mid, LOGIN_REQUIRED)
            if status == 429:
                raise self._runtime(mid, RATE_LIMITED)
            if status is None or status < 200 or status >= 300:
                raise self._candidate(mid, DETAIL_UNAVAILABLE, retryable=False)
            return await self._observe_open_detail(
                detail_page,
                mid,
                trigger_id,
                after_seq,
                mblogid="",
                include_search=False,
            )
        except WeiboDetailFetchError:
            raise
        finally:
            await self._best_effort_close(detail_page or self._opened, mid)

    async def _new_detail_page(self) -> Any:
        factory = self.channel.context.new_page
        opened = factory()
        if inspect.isawaitable(opened):
            return await opened
        return opened

    async def _open_once(self, candidate: Any) -> WeiboOpened:
        mid = str(getattr(candidate, "mid", "") or "")
        self._raise_if_closed(self.search_page)
        self._opened = None
        detail_page: Any = None
        try:
            detail_page, trigger_id, after_seq = await self._click_open(candidate)
            detail_page = detail_page or self._opened
            return await self._observe_open_detail(
                detail_page,
                mid,
                trigger_id,
                after_seq,
                mblogid=str(getattr(candidate, "mblogid", "") or ""),
                include_search=True,
            )
        except WeiboDetailFetchError:
            raise
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(self.search_page) or (
                detail_page is not None and _page_closed(detail_page)
            ):
                raise self._runtime(mid, BROWSER_TARGET_CLOSED) from exc
            raise self._candidate(mid, DETAIL_TAB_NOT_OPENED) from exc
        finally:
            await self._best_effort_close(detail_page or self._opened, mid)
            if _page_closed(self.search_page):
                raise self._runtime(mid, BROWSER_TARGET_CLOSED)
            await self._best_effort_restore()

    async def _observe_open_detail(
        self,
        detail_page: Any,
        mid: str,
        trigger_id: str,
        after_seq: int,
        *,
        mblogid: str,
        include_search: bool,
    ) -> WeiboOpened:
        await self._wait_loaded(detail_page)
        await self._raise_if_blocking(detail_page)
        show = await self._wait_show(mid, after_seq)
        if isinstance(show, CapturedResponse) and show.status in SHOW_STATUS_CODES:
            self._record(show, stage="note_detail", subject_id=mid, trigger=trigger_id)
            raise self._runtime(mid, SHOW_STATUS_CODES[show.status])
        if not isinstance(show, CapturedResponse):
            await self._dwell(detail_page, mid)
            raise self._candidate(mid, DETAIL_SHOW_MISSING)
        user_id = show_user_id(show.json)
        show_visit = self._record(show, stage="note_detail", subject_id=mid, trigger=trigger_id)
        side = await self._wait_side_user(user_id, after_seq)
        if not isinstance(side, CapturedResponse) or not user_id:
            await self._dwell(detail_page, mid)
            raise self._candidate(mid, AUTHOR_PROFILE_MISSING)
        side_visit = self._record(
            side, stage="creator_profile", subject_id=user_id, trigger=trigger_id,
        )
        refs = {
            "ledger_id": self.channel.ledger.ledger_id,
            "note_detail": show_visit,
            "creator_profile": side_visit,
            "images": [],
        }
        if include_search:
            refs["search_results"] = str(getattr(self.capture, "last_search_visit_id", "") or "")
        status = show.json if isinstance(show.json, dict) else {}
        parsed = parse_weibo_detail(
            status,
            side_user_of(side.json) or {},
            mblogid=mblogid or str(status.get("mblogid") or ""),
            source_keyword="",
            current_timestamp=lambda: 0,
            visit_refs=refs,
        )
        await self._dwell(detail_page, mid)
        if parsed.video:
            return WeiboOpened(video=True)
        if parsed.skip_code == CREATED_AT_UNPARSEABLE:
            raise self._candidate(mid, CREATED_AT_UNPARSEABLE, retryable=False)
        if parsed.record is None or parsed.skip_code == DETAIL_CONTENT_EMPTY:
            raise self._candidate(mid, DETAIL_CONTENT_EMPTY)
        record = dict(parsed.record)
        record["content_visit_refs"] = refs
        return WeiboOpened(record=record)

    async def _click_open(self, candidate: Any) -> tuple[Any, str, int]:
        mid = str(getattr(candidate, "mid", "") or "")
        await self._pause(
            self.search_page,
            "search_results",
            *SEARCH_RESULT_PAUSE_SECONDS,
            page_role="primary",
            continuity="completed",
        )
        link = await self._find_time_link(candidate)
        if link is None:
            raise self._candidate(mid, DETAIL_CARD_MISSING)
        after_seq = self.channel.ledger.last_seq
        trigger_id = self.channel.action(
            "click_card",
            page_role="primary",
            target={"note_id": mid, "mid": mid},
        )
        context = self.channel.context
        try:
            async with context.expect_page(timeout=DETAIL_TAB_TIMEOUT_MS) as opened:
                await link.click(timeout=DETAIL_TAB_TIMEOUT_MS)
            detail_page = await opened.value
        except (PlaywrightTimeoutError, TimeoutError) as exc:
            raise self._candidate(mid, DETAIL_TAB_NOT_OPENED) from exc
        self._opened = detail_page
        self.channel.attach(detail_page, "detail")
        return detail_page, trigger_id, after_seq

    async def _find_time_link(self, candidate: Any) -> Any | None:
        mid = str(getattr(candidate, "mid", "") or "")
        if not mid.isascii() or not mid.isdigit():
            return None
        selector = (
            f'div.card-wrap[action-type="feed_list_item"][mid="{mid}"] div.from a'
        )
        locator = self.search_page.locator(selector)
        try:
            count = await locator.count()
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(self.search_page):
                raise self._runtime(str(getattr(candidate, "mid", "") or ""), BROWSER_TARGET_CLOSED) from exc
            return None
        for index in range(count):
            item = locator.nth(index) if hasattr(locator, "nth") else locator
            try:
                href = await item.get_attribute("href")
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                if _closed_error(exc) or _page_closed(self.search_page):
                    raise self._runtime(mid, BROWSER_TARGET_CLOSED) from exc
                continue
            if _href_matches(str(href or ""), candidate):
                return item
        return None

    async def _wait_show(self, mid: str, after_seq: int) -> CapturedResponse | None:
        captured = await self.channel.wait_for(
            WEIBO_SHOW_RULE.name,
            after_seq=after_seq,
            predicate=lambda item: self._show_wanted(item, mid),
            timeout=DETAIL_RESPONSE_TIMEOUT_SECONDS,
        )
        return captured if isinstance(captured, CapturedResponse) else None

    async def _wait_side_user(self, user_id: str, after_seq: int) -> CapturedResponse | None:
        if not user_id:
            return None
        captured = await self.channel.wait_for(
            WEIBO_SIDE_USER_RULE.name,
            after_seq=after_seq,
            predicate=lambda item: self._side_wanted(item, user_id),
            timeout=DETAIL_RESPONSE_TIMEOUT_SECONDS,
        )
        return captured if isinstance(captured, CapturedResponse) else None

    def _show_wanted(self, item: Any, mid: str) -> bool:
        if not isinstance(item, CapturedResponse) or item.page_role != "detail":
            return False
        if item.status in SHOW_STATUS_CODES:
            return True
        if item.status != 200 or item.body_error:
            return False
        return show_matches(item.json, mid)

    def _side_wanted(self, item: Any, user_id: str) -> bool:
        if not isinstance(item, CapturedResponse) or item.page_role != "detail":
            return False
        if item.status != 200 or item.body_error:
            return False
        return side_user_matches(item.json, user_id)

    def _record(self, captured: CapturedResponse, *, stage: str, subject_id: str, trigger: str) -> str:
        outcome = "ok" if captured.status == 200 and not captured.body_error else "http_error"
        return self.channel.record_visit(
            captured,
            stage=stage,
            subject_id=subject_id,
            trigger=trigger,
            outcome=outcome,
        )

    async def _pause(
        self,
        page: Any,
        stage: str,
        low: float,
        high: float,
        *,
        page_role: str,
        continuity: str = "n/a",
    ) -> None:
        seconds = random.uniform(low, high)
        start_seq = self.channel.pause_started()
        if seconds > 0:
            waiter = getattr(page, "wait_for_timeout", None)
            if waiter is None:
                await asyncio.sleep(seconds)
            else:
                await waiter(int(seconds * 1000))
        self.channel.pause_finished(stage, start_seq, seconds, continuity, page_role=page_role)

    async def _dwell(self, page: Any, mid: str) -> None:
        seconds = random.uniform(*DETAIL_DWELL_SECONDS)
        start_seq = self.channel.pause_started()
        if seconds > 0:
            waiter = getattr(page, "wait_for_timeout", None)
            if waiter is None:
                await asyncio.sleep(seconds)
            else:
                await waiter(int(seconds * 1000))
        self.channel.pause_finished("note_detail", start_seq, seconds, "n/a", page_role="detail")
        logger.info("[WeiboDetailPages] detail dwell mid=%s seconds=%.1f", mid, seconds)

    async def _best_effort_close(self, page: Any, mid: str) -> None:
        if page is None or _page_closed(page):
            return
        try:
            self.channel.action("close_tab", page_role="detail", target={"note_id": mid, "mid": mid})
            await page.close()
        except Exception as exc:
            logger.info(
                "[WeiboDetailPages] detail tab close failed mid=%s error=%s",
                mid,
                type(exc).__name__,
            )

    async def _best_effort_restore(self) -> None:
        bring = getattr(self.search_page, "bring_to_front", None)
        if bring is None or _page_closed(self.search_page):
            return
        try:
            await bring()
        except Exception as exc:
            logger.info(
                "[WeiboDetailPages] could not bring the search page forward error=%s",
                type(exc).__name__,
            )

    async def _wait_loaded(self, page: Any) -> None:
        waiter = getattr(page, "wait_for_load_state", None)
        if waiter is None:
            return
        try:
            await waiter("domcontentloaded", timeout=DETAIL_LOAD_TIMEOUT_MS)
        except (PlaywrightTimeoutError, TimeoutError):
            return
        except PlaywrightError as exc:
            if _closed_error(exc) or _page_closed(page):
                raise

    async def _raise_if_blocking(self, page: Any) -> None:
        self._raise_if_closed(page)
        code = await blocking_code_for_page(page)
        if code is not None:
            raise self._runtime("", code)

    def _raise_if_closed(self, page: Any) -> None:
        if _page_closed(page):
            raise self._runtime("", BROWSER_TARGET_CLOSED)

    def _candidate(self, mid: str, code: str, *, retryable: bool = True) -> WeiboDetailFetchError:
        return WeiboDetailFetchError(mid, code, attempts=1, retryable=retryable)

    def _runtime(self, mid: str, code: str) -> WeiboDetailFetchError:
        return WeiboDetailFetchError(mid, code, attempts=1, runtime_blocking=True)
