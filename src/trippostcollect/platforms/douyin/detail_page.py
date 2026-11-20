"""#222 抖音浮层详情与作者页。

图文候选在搜索页点击卡片，同一标签打开浮层；作者经浮层内两步点击打开新标签。
正文仍取本轮搜索卡片的 ``aweme_info``，不请求作品详情接口。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from trippostcollect.platforms.douyin.models import DataFetchError
from trippostcollect.platforms.douyin.page_scripts import corner_close_handle, read_dom
from trippostcollect.platforms.douyin.search_page import (
    DOUYIN_USER_PROFILE_RULE,
    blocking_code,
)
from trippostcollect.runtime.human_pointer import ElementNotInView, pointer_for
from trippostcollect.runtime.page_capture import CapturedResponse


logger = logging.getLogger("TripPostCollect")

DETAIL_ATTEMPTS = 3
DETAIL_RETRY_PAUSE_SECONDS = 1.0
DETAIL_DWELL_SECONDS = (5.0, 9.0)
AUTHOR_TAB_HOLD_SECONDS = (5.0, 9.0)
SEARCH_RESULT_PAUSE_SECONDS = (1.2, 2.8)
AUTHOR_CLICK_PAUSE_SECONDS = (1.0, 2.0)
MODAL_WAIT_SECONDS = 8.0
MODAL_POLL_SECONDS = 0.2
AUTHOR_RESPONSE_TIMEOUT_SECONDS = 15.0
CLICK_TIMEOUT_MS = 8_000

DETAIL_CARD_MISSING = "detail_card_missing"
DETAIL_MODAL_MISMATCH = "detail_modal_mismatch"
MODAL_ADVANCED = "douyin_modal_advanced"
AUTOPLAY_STILL_ON = "douyin_autoplay_still_on"
MODAL_CLOSE_FAILED = "douyin_modal_close_failed"
AUTHOR_LINK_MISSING = "author_link_missing"
AUTHOR_TAB_NOT_OPENED = "author_tab_not_opened"
AUTHOR_URL_MISMATCH = "author_url_mismatch"
AUTHOR_PROFILE_MISSING = "author_profile_missing"
BROWSER_TARGET_CLOSED = "browser_target_closed"
LOGIN_REQUIRED = "login_required"
RATE_LIMITED = "rate_limited"
BLOCKED_OR_FORBIDDEN = "blocked_or_forbidden"

CARD_SELECTOR = "#waterfall_item_{aweme_id}"
AVATAR_SELECTOR = '[data-e2e="feed-active-video"] [data-e2e="video-avatar"]'
AUTOPLAY_SELECTOR = '[data-e2e="video-player-auto-play"] button'
AUTHOR_LINK_SELECTOR = 'a[target="_blank"][href*="/user/"]'
ACTIVE_VIDEO_SELECTOR = '[data-e2e="feed-active-video"]'
# 侧栏标题在播放器右侧、靠近视口顶部。侦察里标题行 y 在 40–130，这里留到 220。
AUTHOR_TITLE_MAX_Y = 220.0
CLOSE_ATTEMPTS = 3


class DouyinDetailFetchError(DataFetchError):
    """详情或作者的候选失败，或必须停止本轮的运行级阻断。"""

    def __init__(
        self,
        aweme_id: str,
        code: str,
        attempts: int = 1,
        *,
        runtime_blocking: bool = False,
        retryable: bool = True,
    ):
        super().__init__(f"douyin detail failed: aweme_id={aweme_id or '<missing>'}, code={code}")
        self.aweme_id = aweme_id
        self.code = code
        self.attempts = max(1, int(attempts))
        self.runtime_blocking = runtime_blocking
        self.retryable = bool(retryable) and not runtime_blocking


def user_sec_uid(url: str) -> str:
    """``/user/{sec_uid}`` 的路径段。不符合时为空串。"""

    text = str(url or "").strip()
    if text.startswith("//"):
        text = "https:" + text
    parts = urlsplit(text)
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) >= 2 and segments[0] == "user":
        return segments[1]
    return ""


def profile_runtime_code(captured: CapturedResponse) -> str | None:
    """截获的资料响应上的运行级 HTTP 分类。403 一律阻断，不看响应体。"""

    status = captured.status
    if status == 401:
        return LOGIN_REQUIRED
    if status == 429:
        return RATE_LIMITED
    if status == 403:
        return BLOCKED_OR_FORBIDDEN
    return None


def profile_matches(payload: Any, *, sec_uid: str, uid: str) -> bool:
    if not isinstance(payload, dict) or payload.get("status_code") not in (0, "0"):
        return False
    user = payload.get("user")
    if not isinstance(user, dict):
        return False
    return str(user.get("sec_uid") or "") == sec_uid and str(user.get("uid") or "") == str(uid)


def _modal_ok(state: Any, aweme_id: str) -> bool:
    if not isinstance(state, dict):
        return False
    return str(state.get("modal_id") or "") == aweme_id and bool(state.get("has_active_video"))


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


class DouyinDetailPages:
    """在搜索页点击图文卡片，读浮层状态，再两步打开作者页。"""

    def __init__(self, capture: Any, search_page: Any) -> None:
        self.capture = capture
        self.search_page = search_page
        self.channel = capture.channel

    async def open_from_search(self, aweme: dict[str, Any], *, search_visit_id: str = "") -> dict[str, Any]:
        aweme_id = str(aweme.get("aweme_id") or "")
        last_code = DETAIL_MODAL_MISMATCH
        for attempt in range(1, DETAIL_ATTEMPTS + 1):
            try:
                return await self._open_once(aweme, search_visit_id=search_visit_id)
            except DouyinDetailFetchError as exc:
                if exc.runtime_blocking or not exc.retryable:
                    exc.attempts = attempt
                    raise
                last_code = exc.code
                logger.warning(
                    "[DouyinDetailPages] detail attempt failed aweme_id=%s attempt=%s code=%s",
                    aweme_id,
                    attempt,
                    exc.code,
                )
                await self._best_effort_close(aweme_id)
                if attempt < DETAIL_ATTEMPTS and DETAIL_RETRY_PAUSE_SECONDS > 0:
                    await asyncio.sleep(DETAIL_RETRY_PAUSE_SECONDS)
        raise DouyinDetailFetchError(aweme_id, last_code, attempts=DETAIL_ATTEMPTS, retryable=False)

    async def _open_once(self, aweme: dict[str, Any], *, search_visit_id: str) -> dict[str, Any]:
        aweme_id = str(aweme.get("aweme_id") or "")
        author = aweme.get("author") if isinstance(aweme.get("author"), dict) else {}
        sec_uid = str(author.get("sec_uid") or author.get("sec_user_id") or "")
        uid = str(author.get("uid") or "")
        self._raise_if_closed()
        await self._raise_if_blocking(self.search_page, aweme_id)
        click_id = await self._click_card(aweme_id)
        state, detail_visit = await self._wait_modal(aweme_id, click_id)
        if not detail_visit:
            raise self._candidate(aweme_id, DETAIL_MODAL_MISMATCH)
        await self._ensure_autoplay_off(aweme_id, state)
        await self._pause(self.search_page, "note_detail", *DETAIL_DWELL_SECONDS)
        await self._raise_if_advanced(aweme_id)
        profile, author_visit = await self._open_author(aweme_id, sec_uid, uid, detail_visit)
        await self._close_until_gone(aweme_id)
        opened = dict(aweme)
        opened["creator_profile"] = profile
        opened["content_visit_refs"] = {
            "ledger_id": self.channel.ledger.ledger_id,
            "search_results": search_visit_id,
            "note_detail": detail_visit,
            "creator_profile": author_visit,
            "images": [],
        }
        return opened

    async def _click_card(self, aweme_id: str) -> str:
        if not aweme_id.isdigit():
            raise self._candidate(aweme_id, DETAIL_CARD_MISSING)
        card = self.search_page.locator(CARD_SELECTOR.format(aweme_id=aweme_id))
        try:
            count = await card.count()
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(self.search_page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
            raise self._candidate(aweme_id, DETAIL_CARD_MISSING) from exc
        if count < 1:
            raise self._candidate(aweme_id, DETAIL_CARD_MISSING)
        target = card.first
        if not await self._is_visible(target):
            try:
                await pointer_for(self.search_page).scroll_into_view(target)
            except ElementNotInView as exc:
                raise self._candidate(aweme_id, DETAIL_CARD_MISSING) from exc
            if not await self._is_visible(target):
                raise self._candidate(aweme_id, DETAIL_CARD_MISSING)
        await self._pause(
            self.search_page,
            "search_results",
            *SEARCH_RESULT_PAUSE_SECONDS,
            continuity="completed",
        )
        click_id = self.channel.action(
            "click_card",
            page_role="primary",
            target={"aweme_id": aweme_id},
        )
        try:
            await target.click(timeout=CLICK_TIMEOUT_MS)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(self.search_page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
            raise self._candidate(aweme_id, DETAIL_MODAL_MISMATCH) from exc
        return click_id

    async def _wait_modal(self, aweme_id: str, click_id: str) -> tuple[Any, str | None]:
        deadline = time.monotonic() + MODAL_WAIT_SECONDS
        while True:
            self._raise_if_closed(aweme_id)
            value, visit_id = await self.channel.read_state(
                self.search_page,
                "douyin.modal_state",
                stage="note_detail",
                subject_id=aweme_id,
                trigger=click_id,
                accept=lambda state, expected=aweme_id: _modal_ok(state, expected),
            )
            if visit_id:
                return value, visit_id
            if time.monotonic() >= deadline:
                return value, None
            await asyncio.sleep(MODAL_POLL_SECONDS)

    async def _ensure_autoplay_off(self, aweme_id: str, state: Any) -> None:
        if not isinstance(state, dict) or not state.get("autoplay_on"):
            return
        await self._click_autoplay(aweme_id)
        checked = await self._read_modal()
        if isinstance(checked, dict) and checked.get("autoplay_on"):
            raise self._runtime(aweme_id, AUTOPLAY_STILL_ON)

    async def _click_autoplay(self, aweme_id: str) -> None:
        switch = self.search_page.locator(AUTOPLAY_SELECTOR)
        visible = await self._first_visible(switch, aweme_id)
        if visible is None:
            raise self._runtime(aweme_id, AUTOPLAY_STILL_ON)
        self.channel.action(
            "toggle_autoplay",
            page_role="primary",
            target={"aweme_id": aweme_id},
        )
        try:
            await visible.click(timeout=CLICK_TIMEOUT_MS)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(self.search_page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
            raise self._runtime(aweme_id, AUTOPLAY_STILL_ON) from exc

    async def _raise_if_advanced(self, aweme_id: str) -> None:
        state = await self._read_modal()
        if not _modal_ok(state, aweme_id):
            raise self._candidate(aweme_id, MODAL_ADVANCED)
        await self._ensure_autoplay_off(aweme_id, state)

    async def _read_modal(self) -> Any:
        value, _visit = await self.channel.read_state(
            self.search_page,
            "douyin.modal_state",
            stage="note_detail",
            subject_id=None,
            trigger=None,
            accept=lambda _state: False,
        )
        return value

    async def _open_author(
        self,
        aweme_id: str,
        sec_uid: str,
        uid: str,
        detail_visit: str,
    ) -> tuple[dict[str, Any], str]:
        if not sec_uid or not uid:
            raise self._candidate(aweme_id, AUTHOR_LINK_MISSING)
        avatar = await self._first_visible(self.search_page.locator(AVATAR_SELECTOR), aweme_id)
        if avatar is None:
            raise self._candidate(aweme_id, AUTHOR_LINK_MISSING)
        self.channel.action(
            "open_author_panel",
            page_role="primary",
            target={"aweme_id": aweme_id},
        )
        try:
            await avatar.click(timeout=CLICK_TIMEOUT_MS)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(self.search_page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
            raise self._candidate(aweme_id, AUTHOR_LINK_MISSING) from exc
        await self._pause(self.search_page, "creator_profile", *AUTHOR_CLICK_PAUSE_SECONDS)
        link = await self._panel_author_link(sec_uid, aweme_id)
        if link is None:
            raise self._candidate(aweme_id, AUTHOR_LINK_MISSING)
        click_id = self.channel.action(
            "click_author",
            page_role="primary",
            target={"aweme_id": aweme_id},
            from_visit=detail_visit,
        )
        after_seq = self.channel.ledger.last_seq
        try:
            async with self.channel.context.expect_page(timeout=CLICK_TIMEOUT_MS) as opened:
                await link.click(timeout=CLICK_TIMEOUT_MS)
            author_page = await opened.value
        except (PlaywrightTimeoutError, TimeoutError) as exc:
            raise self._candidate(aweme_id, AUTHOR_TAB_NOT_OPENED) from exc
        self.channel.attach(author_page, "author")
        try:
            if _page_closed(author_page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED)
            await self._raise_if_blocking(author_page, aweme_id)
            if user_sec_uid(str(getattr(author_page, "url", "") or "")) != sec_uid:
                raise self._candidate(aweme_id, AUTHOR_URL_MISMATCH)
            captured = await self._take_profile(aweme_id, sec_uid, uid, after_seq)
            user = captured.json.get("user") if isinstance(captured.json, dict) else None
            if not isinstance(user, dict) or str(user.get("sec_uid") or "") != sec_uid:
                raise self._candidate(aweme_id, AUTHOR_PROFILE_MISSING)
            visit_id = self.channel.record_visit(
                captured,
                stage="creator_profile",
                subject_id=str(user.get("uid") or ""),
                trigger=click_id,
                url_user_id=str(user.get("uid") or ""),
            )
            await self._pause(author_page, "author_tab", *AUTHOR_TAB_HOLD_SECONDS, page_role="author")
            return dict(captured.json), visit_id
        finally:
            await self._close_author_tab(author_page, aweme_id)

    async def _panel_author_link(self, sec_uid: str, aweme_id: str) -> Any | None:
        """侧栏标题上的作者链接。

        侦察用固定坐标 ``x>1000`` 且 ``40<=y<=130`` 把侧栏标题和顶部 ``/user/self`` 分开，
        这依赖 1450 宽的窗口。这里改为：``target=_blank`` 的 ``/user/{sec_uid}``，排除
        ``/user/self``，并且链接左边缘不小于当前播放项右边缘、纵坐标不超过标题带。
        播放器内的连播作者链接落在播放项内部，不会被选中。
        """

        locator = self.search_page.locator(AUTHOR_LINK_SELECTOR)
        try:
            count = await locator.count()
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(self.search_page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
            return None
        right_edge = await self._active_video_right_edge()
        for index in range(count):
            item = locator.nth(index)
            if not await self._is_visible(item):
                continue
            try:
                href = str(await item.get_attribute("href") or "")
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                if _closed_error(exc) or _page_closed(self.search_page):
                    raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
                continue
            if "/user/self" in href or user_sec_uid(href) != sec_uid:
                continue
            if right_edge is None:
                return item
            try:
                box = await item.bounding_box()
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                if _closed_error(exc) or _page_closed(self.search_page):
                    raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
                continue
            if not isinstance(box, dict):
                continue
            if float(box.get("x") or 0) < right_edge:
                continue
            if float(box.get("y") or 0) > AUTHOR_TITLE_MAX_Y:
                continue
            return item
        return None

    async def _active_video_right_edge(self) -> float | None:
        active = self.search_page.locator(ACTIVE_VIDEO_SELECTOR)
        try:
            if await active.count() < 1:
                return None
            box = await active.first.bounding_box()
        except (PlaywrightError, PlaywrightTimeoutError):
            return None
        if not isinstance(box, dict) or not box.get("width"):
            return None
        return float(box["x"]) + float(box["width"]) - 8

    async def _take_profile(self, aweme_id: str, sec_uid: str, uid: str, after_seq: int) -> CapturedResponse:
        def predicate(item: Any) -> bool:
            if not isinstance(item, CapturedResponse):
                return False
            if profile_runtime_code(item):
                return True
            return profile_matches(item.json, sec_uid=sec_uid, uid=uid)

        captured = await self.channel.wait_for(
            DOUYIN_USER_PROFILE_RULE.name,
            after_seq=after_seq,
            predicate=predicate,
            timeout=AUTHOR_RESPONSE_TIMEOUT_SECONDS,
        )
        if not isinstance(captured, CapturedResponse):
            raise self._candidate(aweme_id, AUTHOR_PROFILE_MISSING)
        code = profile_runtime_code(captured)
        if code:
            raise self._runtime(aweme_id, code)
        if not profile_matches(captured.json, sec_uid=sec_uid, uid=uid):
            raise self._candidate(aweme_id, AUTHOR_PROFILE_MISSING)
        return captured

    async def _close_until_gone(self, aweme_id: str) -> None:
        for _attempt in range(CLOSE_ATTEMPTS):
            state = await self._read_modal()
            if not isinstance(state, dict) or not str(state.get("modal_id") or ""):
                return
            await self._click_close(aweme_id)
        state = await self._read_modal()
        if isinstance(state, dict) and str(state.get("modal_id") or ""):
            raise self._runtime(aweme_id, MODAL_CLOSE_FAILED)

    async def _best_effort_close(self, aweme_id: str) -> None:
        try:
            if _page_closed(self.search_page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED)
            await self._close_until_gone(aweme_id)
        except DouyinDetailFetchError:
            raise
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(self.search_page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc

    async def _click_close(self, aweme_id: str) -> None:
        self.channel.action(
            "close_overlay",
            page_role="primary",
            target={"aweme_id": aweme_id},
        )
        try:
            element = await corner_close_handle(self.search_page)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(self.search_page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
            return
        if element is None:
            return
        try:
            await element.click(timeout=CLICK_TIMEOUT_MS)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(self.search_page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc

    async def _close_author_tab(self, page: Any, aweme_id: str) -> None:
        if page is None or _page_closed(page):
            return
        try:
            self.channel.action(
                "close_tab",
                page_role="author",
                target={"aweme_id": aweme_id},
            )
            await page.close()
        except Exception as exc:
            logger.info(
                "[DouyinDetailPages] author tab close failed aweme_id=%s error=%s",
                aweme_id,
                type(exc).__name__,
            )
        bring = getattr(self.search_page, "bring_to_front", None)
        if bring is not None and not _page_closed(self.search_page):
            await bring()

    async def _pause(
        self,
        page: Any,
        stage: str,
        low: float,
        high: float,
        *,
        page_role: str = "primary",
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

    async def _raise_if_blocking(self, page: Any, aweme_id: str) -> None:
        try:
            code = blocking_code(await read_dom(page, "douyin.visible_risk"))
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
            return
        if code:
            raise self._runtime(aweme_id, code)

    async def _first_visible(self, locator: Any, aweme_id: str) -> Any | None:
        try:
            count = await locator.count()
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(self.search_page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
            return None
        for index in range(count):
            item = locator.nth(index)
            if await self._is_visible(item):
                return item
        return None

    async def _is_visible(self, locator: Any) -> bool:
        try:
            visible = getattr(locator, "is_visible", None)
            if visible is None:
                return True
            return bool(await visible())
        except (PlaywrightError, PlaywrightTimeoutError):
            return False

    def _raise_if_closed(self, aweme_id: str = "") -> None:
        if _page_closed(self.search_page):
            raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED)

    def _candidate(self, aweme_id: str, code: str) -> DouyinDetailFetchError:
        return DouyinDetailFetchError(aweme_id, code)

    def _runtime(self, aweme_id: str, code: str) -> DouyinDetailFetchError:
        return DouyinDetailFetchError(aweme_id, code, runtime_blocking=True, retryable=False)
