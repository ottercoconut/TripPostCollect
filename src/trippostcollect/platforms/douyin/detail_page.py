"""#222 抖音浮层详情与作者页。

图文候选在搜索页点击卡片，同一标签打开浮层；作者经浮层内两步点击打开新标签。
正文仍取本轮搜索卡片的 ``aweme_info``，不请求作品详情接口。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import random
import re
import time
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from trippostcollect.platforms.douyin.models import DataFetchError
from trippostcollect.platforms.douyin.page_scripts import corner_close_handle, read_dom
from trippostcollect.platforms.douyin.parser import _profile_verification, douyin_profile_url
from trippostcollect.platforms.douyin.search_page import (
    DOUYIN_AWEME_POST_RULE,
    DOUYIN_USER_PROFILE_RULE,
    blocking_code,
)
from trippostcollect.runtime.human_pointer import ElementNotInView, pointer_for
from trippostcollect.runtime.page_capture import CapturedResponse


logger = logging.getLogger("TripPostCollect")

DETAIL_ATTEMPTS = 3
DETAIL_RETRY_PAUSE_SECONDS = 1.0
DETAIL_DWELL_SECONDS = (2.0, 3.0)
AUTHOR_TAB_HOLD_SECONDS = (5.0, 9.0)
# 同一轮、同一 sec_uid 第二次起。首次成功访问仍用上面的 5–9 秒。
REPEAT_AUTHOR_TAB_HOLD_SECONDS = (2.0, 3.0)
SEARCH_RESULT_PAUSE_SECONDS = (1.2, 2.8)
AUTHOR_CLICK_PAUSE_SECONDS = (1.0, 2.0)
MODAL_WAIT_SECONDS = 8.0
MODAL_POLL_SECONDS = 0.2
AUTHOR_RESPONSE_TIMEOUT_SECONDS = 15.0
AUTHOR_URL_POLLS = 40
AUTHOR_URL_POLL_MS = 200
AUTOPLAY_SETTLE_POLLS = 6
AUTOPLAY_SETTLE_POLL_MS = 200
# 浮层出现后连播开关可能晚一拍渲染；读不到时先在这段预算内复读，仍读不到才停轮。
AUTOPLAY_READY_POLLS = 10
AUTOPLAY_READY_POLL_MS = 300
CLOSE_SETTLE_POLLS = 6
CLOSE_SETTLE_POLL_MS = 250
CARD_SEEK_STEPS = 16
CARD_SEEK_SETTLE_MS = 150
CLICK_TIMEOUT_MS = 8_000

DETAIL_CARD_MISSING = "detail_card_missing"
DETAIL_MODAL_MISMATCH = "detail_modal_mismatch"
MODAL_ADVANCED = "douyin_modal_advanced"
AUTOPLAY_STILL_ON = "douyin_autoplay_still_on"
MODAL_CLOSE_FAILED = "douyin_modal_close_failed"
AUTHOR_LINK_MISSING = "author_link_missing"
FILL_TARGET_NOT_IN_POST_LIST = "fill_target_not_in_post_list"
FILL_TARGET_REDIRECTED = "fill_target_redirected"
NOTE_NAVIGATION_FAILED = "note_navigation_failed"
NOTE_NAVIGATION_TIMEOUT_MS = 30_000
NOTE_URL_PREFIX = "https://www.douyin.com/note/"
NOTE_AUTHOR_LINK_SELECTOR = 'a[href*="/user/"]'
_LANDING_ID_RE = re.compile(r"[0-9a-fA-F]{16,}|\d{5,}|[A-Za-z0-9_-]{20,}")
AUTHOR_TAB_NOT_OPENED = "author_tab_not_opened"
AUTHOR_URL_MISMATCH = "author_url_mismatch"
AUTHOR_PROFILE_MISSING = "author_profile_missing"
BROWSER_TARGET_CLOSED = "browser_target_closed"
LOGIN_REQUIRED = "login_required"
RATE_LIMITED = "rate_limited"
BLOCKED_OR_FORBIDDEN = "blocked_or_forbidden"

CARD_SELECTOR = "#waterfall_item_{aweme_id}"
AVATAR_SELECTOR = '[data-e2e="feed-active-video"] [data-e2e="video-avatar"]'
SCOPED_AUTOPLAY_SELECTOR = '[data-e2e="feed-active-video"] [data-e2e="video-player-auto-play"] button'
FALLBACK_AUTOPLAY_SELECTOR = '[data-e2e="video-player-auto-play"] button[aria-checked]'
AUTOPLAY_FALLBACK_MAX_Y = 900.0
AUTHOR_LINK_SELECTOR = 'a[target="_blank"][href*="/user/"]'
# 侧栏标题在播放器右侧、靠近视口顶部。侦察里标题行 y 在 40–130，这里留到 220。
AUTHOR_TITLE_MAX_Y = 220.0
# 播放器里的头像本身也是指向同一作者的 target=_blank 链接，不能当成侧栏标题链接。
PLAYER_AVATAR_E2E = "video-avatar"
# 侧栏在点头像后约 1 秒出现（2026-11-23 实测）；在这段预算内轮询标题链接。
AUTHOR_LINK_POLLS = 25
AUTHOR_LINK_POLL_MS = 200
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


def author_page_url_matches(url: str, sec_uid: str) -> bool:
    """作者页必须是 ``https://www.douyin.com/user/{sec_uid}``。``about:blank`` 不算。"""

    if not sec_uid or user_sec_uid(url) != sec_uid:
        return False
    parts = urlsplit(str(url or "").strip())
    return parts.scheme == "https" and parts.netloc == "www.douyin.com"


def _element_of(handle: Any) -> Any | None:
    if handle is None:
        return None
    as_element = getattr(handle, "as_element", None)
    if as_element is None:
        return handle
    return as_element()


async def _dispose_handle(handle: Any) -> None:
    if handle is None:
        return
    dispose = getattr(handle, "dispose", None)
    if dispose is None:
        return
    result = dispose()
    if hasattr(result, "__await__"):
        await result


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


def note_page_url(aweme_id: str) -> str:
    return f"{NOTE_URL_PREFIX}{aweme_id}"


def note_page_matches(url: str, aweme_id: str) -> bool:
    parts = urlsplit(str(url or "").strip())
    return (
        parts.scheme == "https"
        and parts.netloc == "www.douyin.com"
        and parts.path.rstrip("/") == f"/note/{aweme_id}"
    )


def landing_shape(url: str) -> str:
    """主机加路径。数字段和长 ID 段换成 ``{id}``，不保留查询串。"""

    parts = urlsplit(str(url or "").strip())
    host = parts.hostname or ""
    segments = []
    for segment in parts.path.split("/"):
        if segment and (_LANDING_ID_RE.fullmatch(segment) or segment.isdigit()):
            segments.append("{id}")
        else:
            segments.append(segment)
    path = "/".join(segments) or "/"
    return f"{host}{path}" if host else path


def aweme_in_post_list(payload: Any, aweme_id: str) -> dict[str, Any] | None:
    """只认 ``aweme_id`` 等于目标的对象，不限下标。"""

    if not isinstance(payload, dict):
        return None
    items = payload.get("aweme_list")
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and str(item.get("aweme_id") or "") == aweme_id:
            return item
    return None


def _verify_check(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("search_nil_type") == "verify_check":
        return True
    info = payload.get("search_nil_info")
    return isinstance(info, dict) and info.get("search_nil_type") == "verify_check"


def captured_runtime_code(captured: CapturedResponse) -> str | None:
    """作品列表或资料响应上的运行级分类。``verify_check`` 与可见验证码同一停轮码。"""

    code = profile_runtime_code(captured)
    if code:
        return code
    if _verify_check(captured.json):
        return "captcha_detected"
    return None


def profile_sec_uid_matches(payload: Any, sec_uid: str) -> bool:
    """按 ``user.sec_uid`` 挑选。登录账号自己的资料会混在同一标签的响应里。"""

    if not sec_uid or not isinstance(payload, dict) or payload.get("status_code") not in (0, "0"):
        return False
    user = payload.get("user")
    return isinstance(user, dict) and str(user.get("sec_uid") or "") == sec_uid


@dataclass(frozen=True)
class DouyinFillObservation:
    """补缺观察。不含正文、昵称、头像或原始查询串。"""

    platform_post_id: str
    kind: str
    error_code: str = ""
    landing_shape: str = ""
    note_visit_id: str = ""
    creator_visit_id: str = ""
    ledger_id: str = ""
    author_profile_url: str = ""
    author_uid: str = ""
    author_verified: int | None = None
    author_verified_text: str | None = None
    content_detail_observed: bool = False
    unresolved_fields: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "platform_post_id": self.platform_post_id,
            "kind": self.kind,
            "error_code": self.error_code,
            "landing_shape": self.landing_shape,
            "note_visit_id": self.note_visit_id,
            "creator_visit_id": self.creator_visit_id,
            "ledger_id": self.ledger_id,
            "author_profile_url": self.author_profile_url,
            "author_uid": self.author_uid,
            "content_detail_observed": self.content_detail_observed,
            "unresolved_fields": list(self.unresolved_fields),
        }
        if self.author_verified is not None:
            payload["author_verified"] = self.author_verified
        if self.author_verified_text:
            payload["author_verified_text"] = self.author_verified_text
        return payload


def _response_status(response: Any) -> int | None:
    if response is None:
        return None
    status = getattr(response, "status", None)
    if callable(status):
        try:
            status = status()
        except Exception:
            return None
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    return status


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
        # start() 里一轮只建一个实例。只在资料成功、即将停留时记入，不落盘，不跨轮。
        self._visited_author_keys: set[str] = set()

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
            await self._seek_card(card, aweme_id)
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
        # 读不到当前开关不能当成已关闭：规格默认开启，继续停留会把 modal_id 切走。
        # 这和「关不掉」一样是运行级，避免候选失败把图文写进已处理。
        state = await self._wait_autoplay_readable(aweme_id, state)
        if not isinstance(state, dict) or state.get("autoplay_readable") is False:
            raise self._runtime(aweme_id, AUTOPLAY_STILL_ON)
        if not state.get("autoplay_on"):
            return
        await self._click_autoplay(aweme_id)
        if not await self._wait_autoplay_off(aweme_id):
            raise self._runtime(aweme_id, AUTOPLAY_STILL_ON)

    async def _wait_autoplay_readable(self, aweme_id: str, state: Any) -> Any:
        for _poll in range(AUTOPLAY_READY_POLLS):
            if isinstance(state, dict) and state.get("autoplay_readable") is not False:
                return state
            await self._wait_ms(self.search_page, AUTOPLAY_READY_POLL_MS)
            self._raise_if_closed(aweme_id)
            state = await self._read_modal()
        return state

    async def _click_autoplay(self, aweme_id: str) -> None:
        visible = await self._find_autoplay_switch(aweme_id)
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

    async def _find_autoplay_switch(self, aweme_id: str) -> Any | None:
        scoped = await self._first_visible(self.search_page.locator(SCOPED_AUTOPLAY_SELECTOR), aweme_id)
        if scoped is not None:
            return scoped
        fallback = self.search_page.locator(FALLBACK_AUTOPLAY_SELECTOR)
        try:
            count = await fallback.count()
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(self.search_page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
            return None
        for index in range(min(count, 10)):
            cand = fallback.nth(index)
            if not await self._is_visible(cand):
                continue
            try:
                box = await cand.bounding_box()
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                if _closed_error(exc) or _page_closed(self.search_page):
                    raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
                continue
            if not isinstance(box, dict):
                continue
            y = float(box.get("y") or 0)
            if 0 < y < AUTOPLAY_FALLBACK_MAX_Y:
                return cand
        return None

    async def _wait_autoplay_off(self, aweme_id: str) -> bool:
        for _poll in range(AUTOPLAY_SETTLE_POLLS):
            await self._wait_ms(self.search_page, AUTOPLAY_SETTLE_POLL_MS)
            self._raise_if_closed(aweme_id)
            state = await self._read_modal()
            if not isinstance(state, dict) or state.get("autoplay_readable") is False:
                return False
            if not state.get("autoplay_on"):
                return True
        return False

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
        # 资料响应可能在点击当时就进通道。起点取点击前的序号，后面等 URL 时不能后移。
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
            await self._wait_author_url(author_page, sec_uid, aweme_id)
            await self._raise_if_blocking(author_page, aweme_id)
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
            await self._pause(
                author_page,
                "author_tab",
                *self._author_tab_hold(sec_uid),
                page_role="author",
            )
            return dict(captured.json), visit_id
        finally:
            await self._close_author_tab(author_page, aweme_id)

    async def _panel_author_link(self, sec_uid: str, aweme_id: str) -> Any | None:
        """侧栏标题上的作者链接；侧栏晚于点击出现，在预算内轮询。"""

        for poll in range(AUTHOR_LINK_POLLS):
            link = await self._panel_author_link_once(sec_uid, aweme_id)
            if link is not None:
                return link
            if poll + 1 < AUTHOR_LINK_POLLS:
                await self._wait_ms(self.search_page, AUTHOR_LINK_POLL_MS)
                self._raise_if_closed(aweme_id)
        return None

    async def _panel_author_link_once(self, sec_uid: str, aweme_id: str) -> Any | None:
        """2026-11-23 实测（1450×777 视口）：``feed-active-video`` 盖住整个视口，不能用它的右边缘区分
        侧栏；侧栏标题链接在纵坐标约 63 处。同作者的 ``target=_blank`` 链接只有两条：播放器头像
        （``data-e2e=video-avatar``，纵坐标约 316）和侧栏标题。所以排除头像与路径段为 ``self`` 的当前
        账号链接，只取标题带（纵坐标不超过 220）内路径段等于目标 ``sec_uid`` 的链接。
        """

        locator = self.search_page.locator(AUTHOR_LINK_SELECTOR)
        try:
            count = await locator.count()
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(self.search_page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
            return None
        for index in range(count):
            item = locator.nth(index)
            if not await self._is_visible(item):
                continue
            try:
                href = str(await item.get_attribute("href") or "")
                e2e = str(await item.get_attribute("data-e2e") or "")
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                if _closed_error(exc) or _page_closed(self.search_page):
                    raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
                continue
            if e2e == PLAYER_AVATAR_E2E:
                continue
            if user_sec_uid(href) in {"", "self"} or user_sec_uid(href) != sec_uid:
                continue
            try:
                box = await item.bounding_box()
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                if _closed_error(exc) or _page_closed(self.search_page):
                    raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
                continue
            if not isinstance(box, dict) or not box.get("width"):
                continue
            if float(box.get("y") or 0) > AUTHOR_TITLE_MAX_Y:
                continue
            return item
        return None

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
            if await self._click_close(aweme_id) and await self._wait_modal_absent(aweme_id):
                return
        state = await self._read_modal()
        if isinstance(state, dict) and str(state.get("modal_id") or ""):
            raise self._runtime(aweme_id, MODAL_CLOSE_FAILED)

    async def _wait_modal_absent(self, aweme_id: str) -> bool:
        for _poll in range(CLOSE_SETTLE_POLLS):
            await self._wait_ms(self.search_page, CLOSE_SETTLE_POLL_MS)
            self._raise_if_closed(aweme_id)
            state = await self._read_modal()
            if not isinstance(state, dict) or not str(state.get("modal_id") or ""):
                return True
        return False

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

    async def _click_close(self, aweme_id: str) -> bool:
        handle = None
        try:
            handle = await corner_close_handle(self.search_page)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(self.search_page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
            return False
        try:
            element = _element_of(handle)
            if element is None:
                return False
            try:
                await element.click(timeout=CLICK_TIMEOUT_MS)
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                if _closed_error(exc) or _page_closed(self.search_page):
                    raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
                return False
            self.channel.action(
                "close_overlay",
                page_role="primary",
                target={"aweme_id": aweme_id},
            )
            return True
        finally:
            await _dispose_handle(handle)

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

    async def _wait_author_url(self, author_page: Any, sec_uid: str, aweme_id: str) -> None:
        for index in range(AUTHOR_URL_POLLS + 1):
            if _page_closed(author_page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED)
            url = str(getattr(author_page, "url", "") or "")
            if author_page_url_matches(url, sec_uid):
                return
            if index >= AUTHOR_URL_POLLS:
                break
            await self._wait_ms(author_page, AUTHOR_URL_POLL_MS)
        raise self._candidate(aweme_id, AUTHOR_URL_MISMATCH)

    async def _seek_card(self, card: Any, aweme_id: str) -> None:
        """卡片不在 DOM 时，先用滚轮回到顶部，再逐屏往下找。"""

        size = getattr(self.search_page, "viewport_size", None) or {}
        step = max(400, int((size or {}).get("height") or 900) - 120)
        for delta in (-step,) * CARD_SEEK_STEPS + (step,) * CARD_SEEK_STEPS:
            try:
                await self.search_page.mouse.wheel(0, delta)
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                if _closed_error(exc) or _page_closed(self.search_page):
                    raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
                return
            await self._wait_ms(self.search_page, CARD_SEEK_SETTLE_MS)
            try:
                if await card.count() >= 1:
                    return
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                if _closed_error(exc) or _page_closed(self.search_page):
                    raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
                return

    async def _wait_ms(self, page: Any, milliseconds: int) -> None:
        if milliseconds <= 0:
            return
        waiter = getattr(page, "wait_for_timeout", None)
        if waiter is None:
            await asyncio.sleep(milliseconds / 1000)
            return
        await waiter(milliseconds)

    def _author_tab_hold(self, sec_uid: str) -> tuple[float, float]:
        """即将停留时调用。同一 sec_uid 已成功访问过则用短停留，并记下这次。"""

        repeat = bool(sec_uid) and sec_uid in self._visited_author_keys
        if sec_uid:
            self._visited_author_keys.add(sec_uid)
        if repeat:
            return REPEAT_AUTHOR_TAB_HOLD_SECONDS
        return AUTHOR_TAB_HOLD_SECONDS

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

    async def open_note_for_fill(self, aweme_id: str, *, need_author: bool) -> DouyinFillObservation:
        """在主标签 navigate 到作品页。新标签只留给作者页，和发现链的页面角色一致。

        只等这一次作品列表，不翻页、不滚动、不改地址、不重试。
        """

        page = self.search_page
        self._raise_if_closed(aweme_id)
        await self._raise_if_blocking(page, aweme_id)
        url = note_page_url(aweme_id)
        trigger_id = self.channel.action(
            "navigate",
            page_role="primary",
            target={"aweme_id": aweme_id},
        )
        after_seq = self.channel.ledger.last_seq
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=NOTE_NAVIGATION_TIMEOUT_MS)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
            return self._fill_candidate(aweme_id, NOTE_NAVIGATION_FAILED)
        status = _response_status(response)
        if status in (401, 403):
            raise self._runtime(aweme_id, LOGIN_REQUIRED if status == 401 else BLOCKED_OR_FORBIDDEN)
        if status == 429:
            raise self._runtime(aweme_id, RATE_LIMITED)
        await self._raise_if_blocking(page, aweme_id)
        final_url = str(getattr(page, "url", "") or "")
        if not note_page_matches(final_url, aweme_id):
            shape = landing_shape(final_url)
            logger.info(
                "[DouyinDetailPages] fill redirected aweme_id=%s landing=%s",
                aweme_id,
                shape,
            )
            return self._fill_unavailable(aweme_id, FILL_TARGET_REDIRECTED, landing=shape)
        captured = await self._take_post_list(aweme_id, after_seq)
        if captured is None:
            return self._fill_unavailable(aweme_id, FILL_TARGET_NOT_IN_POST_LIST)
        code = captured_runtime_code(captured)
        if code:
            raise self._runtime(aweme_id, code)
        aweme = aweme_in_post_list(captured.json, aweme_id)
        if aweme is None:
            return self._fill_unavailable(aweme_id, FILL_TARGET_NOT_IN_POST_LIST)
        visit_id = self.channel.record_visit(
            captured,
            stage="note_detail",
            subject_id=aweme_id,
            trigger=trigger_id,
        )
        await self._pause(page, "note_detail", *DETAIL_DWELL_SECONDS)
        author = aweme.get("author") if isinstance(aweme.get("author"), dict) else {}
        sec_uid = str(author.get("sec_uid") or author.get("sec_user_id") or "").strip()
        observed = DouyinFillObservation(
            platform_post_id=aweme_id,
            kind="observed",
            note_visit_id=visit_id,
            ledger_id=self.channel.ledger.ledger_id,
            content_detail_observed=True,
        )
        if not need_author:
            return observed
        author_obs = await self._fill_author(page, aweme_id, sec_uid, visit_id)
        return DouyinFillObservation(
            platform_post_id=aweme_id,
            kind=author_obs.kind,
            error_code=author_obs.error_code,
            note_visit_id=visit_id,
            creator_visit_id=author_obs.creator_visit_id,
            ledger_id=observed.ledger_id,
            author_profile_url=author_obs.author_profile_url,
            author_uid=author_obs.author_uid,
            author_verified=author_obs.author_verified,
            author_verified_text=author_obs.author_verified_text,
            content_detail_observed=True,
            unresolved_fields=author_obs.unresolved_fields,
        )

    async def _fill_author(
        self,
        page: Any,
        aweme_id: str,
        sec_uid: str,
        detail_visit: str,
    ) -> DouyinFillObservation:
        if not sec_uid:
            return self._fill_candidate(aweme_id, AUTHOR_LINK_MISSING)
        await self._pause(page, "creator_profile", *AUTHOR_CLICK_PAUSE_SECONDS)
        link = await self._note_author_link(page, sec_uid, aweme_id)
        if link is None:
            return self._fill_candidate(aweme_id, AUTHOR_LINK_MISSING)
        try:
            box = await link.bounding_box()
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
            return self._fill_candidate(aweme_id, AUTHOR_LINK_MISSING)
        if not isinstance(box, dict) or not box.get("width") or not box.get("height"):
            return self._fill_candidate(aweme_id, AUTHOR_LINK_MISSING)
        click_id = self.channel.action(
            "click_author",
            page_role="primary",
            target={"aweme_id": aweme_id},
            from_visit=detail_visit,
        )
        after_seq = self.channel.ledger.last_seq
        author_page = None
        try:
            async with self.channel.context.expect_page(timeout=CLICK_TIMEOUT_MS) as opened:
                await link.click(timeout=CLICK_TIMEOUT_MS)
            author_page = await opened.value
        except (PlaywrightTimeoutError, TimeoutError) as exc:
            if _closed_error(exc) or _page_closed(page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
            return self._fill_candidate(aweme_id, AUTHOR_TAB_NOT_OPENED)
        self.channel.attach(author_page, "author")
        try:
            if _page_closed(author_page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED)
            await self._wait_author_url(author_page, sec_uid, aweme_id)
            await self._raise_if_blocking(author_page, aweme_id)
            captured = await self._take_profile_by_sec_uid(aweme_id, sec_uid, after_seq)
            user = captured.json.get("user") if isinstance(captured.json, dict) else None
            if not isinstance(user, dict) or str(user.get("sec_uid") or "") != sec_uid:
                return self._fill_candidate(aweme_id, AUTHOR_PROFILE_MISSING)
            uid = str(user.get("uid") or "").strip() or sec_uid
            verified, verified_text = _profile_verification(user)
            profile_url = douyin_profile_url(str(user.get("sec_uid") or ""))
            if not profile_url:
                return self._fill_candidate(aweme_id, AUTHOR_PROFILE_MISSING)
            visit_id = self.channel.record_visit(
                captured,
                stage="creator_profile",
                subject_id=uid,
                trigger=click_id,
                url_user_id=uid,
            )
            unresolved: list[str] = []
            if verified is None:
                unresolved.append("author_verified")
            await self._pause(
                author_page,
                "author_tab",
                *self._author_tab_hold(sec_uid),
                page_role="author",
            )
            return DouyinFillObservation(
                platform_post_id=aweme_id,
                kind="observed",
                creator_visit_id=visit_id,
                author_profile_url=profile_url,
                author_uid=uid,
                author_verified=verified,
                author_verified_text=verified_text,
                unresolved_fields=tuple(unresolved),
            )
        finally:
            await self._close_author_tab(author_page, aweme_id)

    async def _note_author_link(self, page: Any, sec_uid: str, aweme_id: str) -> Any | None:
        """作品页上 href 的 sec_uid 与作品作者一致的可见链接。找不到不拼地址。"""

        locator = page.locator(NOTE_AUTHOR_LINK_SELECTOR)
        try:
            count = await locator.count()
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _closed_error(exc) or _page_closed(page):
                raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
            return None
        for index in range(count):
            item = locator.nth(index)
            if not await self._is_visible(item):
                continue
            try:
                href = str(await item.get_attribute("href") or "")
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                if _closed_error(exc) or _page_closed(page):
                    raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
                continue
            if user_sec_uid(href) != sec_uid:
                continue
            try:
                await pointer_for(page).scroll_into_view(item)
            except ElementNotInView:
                continue
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                if _closed_error(exc) or _page_closed(page):
                    raise self._runtime(aweme_id, BROWSER_TARGET_CLOSED) from exc
                continue
            if await self._is_visible(item):
                return item
        return None

    async def _take_post_list(self, aweme_id: str, after_seq: int) -> CapturedResponse | None:
        def predicate(item: Any) -> bool:
            if not isinstance(item, CapturedResponse):
                return False
            if captured_runtime_code(item):
                return True
            return aweme_in_post_list(item.json, aweme_id) is not None

        captured = await self.channel.wait_for(
            DOUYIN_AWEME_POST_RULE.name,
            after_seq=after_seq,
            predicate=predicate,
            timeout=MODAL_WAIT_SECONDS,
        )
        if not isinstance(captured, CapturedResponse):
            return None
        return captured

    async def _take_profile_by_sec_uid(self, aweme_id: str, sec_uid: str, after_seq: int) -> CapturedResponse:
        def predicate(item: Any) -> bool:
            if not isinstance(item, CapturedResponse):
                return False
            if captured_runtime_code(item):
                return True
            return profile_sec_uid_matches(item.json, sec_uid)

        captured = await self.channel.wait_for(
            DOUYIN_USER_PROFILE_RULE.name,
            after_seq=after_seq,
            predicate=predicate,
            timeout=AUTHOR_RESPONSE_TIMEOUT_SECONDS,
        )
        if not isinstance(captured, CapturedResponse):
            raise self._candidate(aweme_id, AUTHOR_PROFILE_MISSING)
        code = captured_runtime_code(captured)
        if code:
            raise self._runtime(aweme_id, code)
        if not profile_sec_uid_matches(captured.json, sec_uid):
            raise self._candidate(aweme_id, AUTHOR_PROFILE_MISSING)
        return captured

    def _fill_unavailable(self, aweme_id: str, code: str, *, landing: str = "") -> DouyinFillObservation:
        return DouyinFillObservation(
            platform_post_id=aweme_id,
            kind="unavailable",
            error_code=code,
            landing_shape=landing,
            ledger_id=self.channel.ledger.ledger_id,
        )

    def _fill_candidate(self, aweme_id: str, code: str) -> DouyinFillObservation:
        return DouyinFillObservation(
            platform_post_id=aweme_id,
            kind="candidate_failed",
            error_code=code,
            ledger_id=self.channel.ledger.ledger_id,
        )
