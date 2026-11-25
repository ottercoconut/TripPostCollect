"""#211 微博详情：搜索页点击时间链接，或修复链 navigate 打开桌面详情。

不另开作者页，不走浏览器外详情请求。作者资料来自同一标签的 sideUser。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
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
    WEIBO_PASSPORT_URL_RE,
    WEIBO_SHOW_RULE,
    WEIBO_SIDE_USER_RULE,
    WEIBO_VISITOR_URL_RE,
    blocking_code_for_page,
)
from trippostcollect.runtime.page_capture import (
    CaptureChannelError, CaptureFailure, CapturedResponse,
)


logger = logging.getLogger("TripPostCollect")

DETAIL_ATTEMPTS = 3
DETAIL_RETRY_PAUSE_SECONDS = 1.0
DETAIL_DWELL_SECONDS = (2.0, 3.0)
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

# 这三种失败多带一份证据；判定、重试和阻断仍走原来的码。
_EVIDENCE_CODES = frozenset({
    DETAIL_TAB_NOT_OPENED,
    DETAIL_SHOW_MISSING,
    DETAIL_UNAVAILABLE,
})
_DETAIL_HOSTS = frozenset({"weibo.com", "www.weibo.com"})
_ERROR_FIRST_SEGMENTS = frozenset({"sorry", "404", "error", "pagenotfound"})
# 两段路径里这些首段不是作者 uid：资料页 /u/{uid}、专题、接口和错误页。
_NON_UID_FIRST = frozenset({
    "u", "n", "p", "ajax", "tv", "status", "login", "visitor",
    "sorry", "404", "error", "pagenotfound", "captcha", "verify",
    "challenge", "security", "frequency", "abuse",
})
_UNMATCHED_LIMIT = 5

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


def classify_final_page_kind(url: str | None, *, closed: bool = False) -> str | None:
    """最终地址的类型。只返回类型名，不返回地址。

    - ``closed``：页面已关闭。
    - ``login``：与搜索页同一组 passport / visitor 正则。
    - ``search``：主机是 ``s.weibo.com``。
    - ``error_page``：路径首段是 ``sorry``、``404``、``error`` 或 ``pagenotfound``。
    - ``detail``：``https://weibo.com`` 或 ``www.weibo.com`` 上恰好 ``/{uid}/{mid 或 mblogid}``。
      uid 与帖子段都是字母数字；``/u/``、``/n/`` 等保留首段不是详情。
    - ``other``：其余能读到的地址。
    - ``None``：没有地址。
    """

    if closed:
        return "closed"
    if not isinstance(url, str) or not url.strip():
        return None
    text = url.strip()
    if WEIBO_PASSPORT_URL_RE.search(text) or WEIBO_VISITOR_URL_RE.search(text):
        return "login"
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if host == "s.weibo.com":
        return "search"
    segments = [segment for segment in parts.path.split("/") if segment]
    if segments and segments[0].lower() in _ERROR_FIRST_SEGMENTS:
        return "error_page"
    if (
        parts.scheme == "https"
        and host in _DETAIL_HOSTS
        and len(segments) == 2
        and _detail_uid(segments[0])
        and _detail_post_id(segments[1])
    ):
        return "detail"
    return "other"


def _detail_uid(segment: str) -> bool:
    return bool(segment) and segment.isalnum() and segment.lower() not in _NON_UID_FIRST


def _detail_post_id(segment: str) -> bool:
    """mid（纯数字）或 mblogid（字母数字）。"""

    return bool(segment) and segment.isascii() and segment.isalnum()


def public_detail_evidence(evidence: Any) -> dict[str, Any] | None:
    """证据的可落盘副本。只保留登记过的键，丢掉地址、正文和其他多余字段。"""

    if not isinstance(evidence, dict):
        return None
    if not any(key in evidence for key in (
        "navigation_status",
        "final_page_kind",
        "show_responses_unmatched",
        "show_request_failures",
        "show_response_pending",
        "waited_seconds",
    )):
        return None
    unmatched: list[dict[str, Any]] = []
    raw_items = evidence.get("show_responses_unmatched")
    if isinstance(raw_items, list):
        for item in raw_items[:_UNMATCHED_LIMIT]:
            if not isinstance(item, dict):
                continue
            status = item.get("status")
            ok = item.get("ok")
            body_error = item.get("body_error")
            unmatched.append({
                "status": status if isinstance(status, int) and not isinstance(status, bool) else None,
                "ok": ok if isinstance(ok, bool) or (isinstance(ok, int) and not isinstance(ok, bool)) else None,
                "id_matched": bool(item.get("id_matched")),
                "body_error": body_error if isinstance(body_error, str) and body_error.isidentifier() else "",
            })
    kind = evidence.get("final_page_kind")
    nav = evidence.get("navigation_status")
    waited = evidence.get("waited_seconds")
    pending = evidence.get("show_response_pending")
    failures = evidence.get("show_request_failures")
    waited_value = None
    if isinstance(waited, (int, float)) and not isinstance(waited, bool):
        waited_value = float(round(float(waited), 1))
    failure_count = failures if isinstance(failures, int) and not isinstance(failures, bool) and failures >= 0 else 0
    return {
        "navigation_status": nav if isinstance(nav, int) and not isinstance(nav, bool) else None,
        "final_page_kind": kind if kind in {"detail", "login", "error_page", "search", "other", "closed"} else None,
        "show_responses_unmatched": unmatched,
        "show_request_failures": failure_count,
        "show_response_pending": pending if isinstance(pending, bool) else None,
        "waited_seconds": waited_value,
    }


def _top_ok(payload: Any) -> int | bool | None:
    """JSON 顶层 ``ok``。只保留 int、bool；其他类型记为空。"""

    if not isinstance(payload, dict) or "ok" not in payload:
        return None
    value = payload["ok"]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    return None


def _final_page_kind(page: Any) -> str | None:
    if page is None:
        return None
    if _page_closed(page):
        return "closed"
    try:
        url = getattr(page, "url", None)
    except Exception:
        return None
    if not isinstance(url, str):
        return None
    return classify_final_page_kind(url)


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
        self._nav_status: int | None = None
        self._after_seq: int | None = None
        self._waited_show: float | None = None

    async def open_from_search(self, candidate: Any) -> WeiboOpened:
        mid = str(getattr(candidate, "mid", "") or "")
        last_code = DETAIL_SHOW_MISSING
        last_evidence: dict[str, Any] = {}
        for attempt in range(1, DETAIL_ATTEMPTS + 1):
            try:
                return await self._open_once(candidate)
            except WeiboDetailFetchError as exc:
                if exc.runtime_blocking or not exc.retryable:
                    exc.attempts = attempt
                    raise
                last_code = exc.code
                last_evidence = dict(exc.evidence)
                logger.warning(
                    "[WeiboDetailPages] detail attempt failed mid=%s attempt=%s code=%s",
                    mid,
                    attempt,
                    exc.code,
                )
                if attempt < DETAIL_ATTEMPTS and DETAIL_RETRY_PAUSE_SECONDS > 0:
                    await asyncio.sleep(DETAIL_RETRY_PAUSE_SECONDS)
        raise WeiboDetailFetchError(
            mid,
            last_code,
            attempts=DETAIL_ATTEMPTS,
            retryable=False,
            evidence=last_evidence,
        )

    async def open_by_navigate(self, url: str) -> WeiboOpened:
        """修复链：navigate 打开 ``https://weibo.com/{uid}/{mid}``，截获规则与点击相同。"""

        self._reset_attempt_evidence()
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
            self._after_seq = after_seq
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
            self._nav_status = status
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
        self._reset_attempt_evidence()
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
        self._after_seq = after_seq
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
        started = time.monotonic()
        try:
            captured = await self.channel.wait_for(
                WEIBO_SHOW_RULE.name,
                after_seq=after_seq,
                predicate=lambda item: self._show_wanted(item, mid),
                timeout=DETAIL_RESPONSE_TIMEOUT_SECONDS,
            )
        finally:
            self._waited_show = time.monotonic() - started
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

    def _reset_attempt_evidence(self) -> None:
        self._nav_status = None
        self._after_seq = None
        self._waited_show = None
        self._opened = None

    def _attempt_evidence(self, mid: str) -> dict[str, Any]:
        """只读本轮已经截获的 show。不发请求，也不再等待。"""

        unmatched: list[dict[str, Any]] = []
        failures = 0
        pending: bool | None = None
        after_seq = self._after_seq
        if after_seq is not None:
            items, pending = self._observed_show(after_seq)
            for item in items:
                if isinstance(item, CaptureFailure):
                    failures += 1
                    continue
                if not isinstance(item, CapturedResponse) or self._show_wanted(item, mid):
                    continue
                if len(unmatched) >= _UNMATCHED_LIMIT:
                    continue
                status = item.status
                unmatched.append({
                    "status": status if isinstance(status, int) and not isinstance(status, bool) else None,
                    "ok": _top_ok(item.json),
                    "id_matched": bool(show_matches(item.json, mid)),
                    "body_error": item.body_error if isinstance(item.body_error, str) else "",
                })
        waited = self._waited_show
        return {
            "navigation_status": self._nav_status,
            "final_page_kind": _final_page_kind(self._opened),
            "show_responses_unmatched": unmatched,
            "show_request_failures": failures,
            "show_response_pending": pending,
            "waited_seconds": None if waited is None else float(round(float(waited), 1)),
        }

    def _observed_show(self, after_seq: int) -> tuple[list[Any], bool | None]:
        """证据只是附带信息：通道已故障时留空，让原来的候选失败照常抛出。"""

        rule = WEIBO_SHOW_RULE.name
        try:
            items = self.channel.drain(rule, after_seq=after_seq)
            return items, self.channel.latest_pending_seq(rule) > after_seq
        except CaptureChannelError:
            return [], None

    def _candidate(self, mid: str, code: str, *, retryable: bool = True) -> WeiboDetailFetchError:
        evidence = self._attempt_evidence(mid) if code in _EVIDENCE_CODES else None
        return WeiboDetailFetchError(mid, code, attempts=1, retryable=retryable, evidence=evidence)

    def _runtime(self, mid: str, code: str) -> WeiboDetailFetchError:
        return WeiboDetailFetchError(mid, code, attempts=1, runtime_blocking=True)
