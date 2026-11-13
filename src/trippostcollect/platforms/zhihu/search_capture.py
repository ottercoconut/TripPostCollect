"""#185 知乎搜索与登录复验只消费页面自己发出的响应。

搜索页加载时的 ``/api/v4/search_v3`` 是第一页；之后用真人分段滚动触发下一页。
截获不到是运行级失败，不改走浏览器外请求，也不把缺失解释为来源耗尽。
``/api/v4/me`` 同样只认页面自己的响应。详情由点击打开的新标签读取页面状态，不走浏览器外 HTTP。
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any

from trippostcollect.platforms.zhihu.client import SearchResponseInvalid
from trippostcollect.platforms.zhihu.models import (
    DataFetchError, PlatformRuntimeError, ZhihuSearchPage,
)
from trippostcollect.platforms.zhihu.page_scripts import STATE_SCRIPTS
from trippostcollect.runtime.human_flow import human_scroll, load_behavior_profile
from trippostcollect.runtime.page_capture import (
    CaptureFailure, CaptureRule, CapturedResponse, PageCaptureChannel,
)
from trippostcollect.runtime.visit_ledger import VisitLedger, content_visits_path


logger = logging.getLogger("TripPostCollect")

BROWSER_SEARCH_RESPONSE_MISSING = "browser_search_response_missing"
# 首页响应在进入搜索页时已经发出；这里只等读体结束。
FIRST_PAGE_CAPTURE_TIMEOUT_SECONDS = 15.0
# 后续页：每次滚动后等待页面自己的下一页，最多 8 次。少一次滚动往往不够（侦察里 3 次滚动才有 2 次分页）。
FOLLOWUP_SCROLL_ATTEMPTS = 8
FOLLOWUP_CAPTURE_TIMEOUT_SECONDS = 8.0
ME_CAPTURE_TIMEOUT_SECONDS = 15.0
# 程序滚动触发下一页之前的翻页停顿。滚动本身的停顿由人类行为组件负责。
PAGE_TURN_PAUSE_SECONDS = (1.2, 2.8)
SEARCH_SETTLE_SECONDS = 5.0
API_RESOURCE_TYPES = frozenset({"xhr", "fetch"})

ZHIHU_SEARCH_RULE = CaptureRule(
    name="zhihu.search_v3",
    stage="search_results",
    method="GET",
    host="www.zhihu.com",
    path="/api/v4/search_v3",
    resource_types=API_RESOURCE_TYPES,
    body="json",
)
ZHIHU_ME_RULE = CaptureRule(
    name="zhihu.me",
    stage="login_state",
    method="GET",
    host="www.zhihu.com",
    path="/api/v4/me",
    resource_types=API_RESOURCE_TYPES,
    body="json",
)
ZHIHU_RULES = (ZHIHU_SEARCH_RULE, ZHIHU_ME_RULE)


class BrowserSearchResponseMissing(DataFetchError):
    """页面在等待内没有发出可截获的 search_v3。不是来源耗尽。"""

    def __init__(self) -> None:
        super().__init__("zhihu browser search response missing")
        self.code = BROWSER_SEARCH_RESPONSE_MISSING


def me_is_logged_in(captured: CapturedResponse | None) -> bool:
    """``/me`` 须为 HTTP 200 且同时带 uid 与 name。日志只记有无，不记响应体。"""

    if not isinstance(captured, CapturedResponse) or captured.status != 200 or captured.body_error:
        status = captured.status if isinstance(captured, CapturedResponse) else None
        logger.error(
            "[ZhihuPageCapture.confirm_login] /me capture missing or unusable, "
            f"status={status}"
        )
        return False
    payload = captured.json if isinstance(captured.json, dict) else {}
    if payload.get("error"):
        logger.error("[ZhihuPageCapture.confirm_login] /me reported an error object")
        return False
    uid_present = bool(payload.get("uid"))
    name_present = bool(payload.get("name"))
    if uid_present and name_present:
        logger.info("[ZhihuPageCapture.confirm_login] /me capture shows a logged-in session")
        return True
    logger.error(
        "[ZhihuPageCapture.confirm_login] /me capture is not logged in: "
        f"uid_present={uid_present}, name_present={name_present}"
    )
    return False


def search_page_from_captured(captured: CapturedResponse | CaptureFailure, extractor: Any) -> ZhihuSearchPage:
    """沿用 search_v3 的无效响应判定：缺 paging/data、空 data 却未声明 is_end、HTTP 404。"""

    if isinstance(captured, CaptureFailure):
        raise DataFetchError("search_v3 request failed")
    status = int(captured.status)
    if status in {401, 403}:
        raise PlatformRuntimeError(f"HTTP {status}", code="login_required")
    if status == 429:
        raise PlatformRuntimeError("HTTP 429", code="rate_limited")
    if status == 404:
        raise SearchResponseInvalid("search_v3 returned HTTP 404")
    if status != 200:
        raise DataFetchError(f"HTTP {status}")
    payload = captured.json if isinstance(captured.json, dict) else None
    if captured.body_error or payload is None:
        raise SearchResponseInvalid("search_v3 response has no paging/data")
    if payload.get("error"):
        raise DataFetchError("platform error")
    paging = payload.get("paging")
    raw_items = payload.get("data")
    if not isinstance(paging, dict) or not isinstance(raw_items, list):
        raise SearchResponseInvalid("search_v3 response has no paging/data")
    if not raw_items and paging.get("is_end") is not True:
        raise SearchResponseInvalid("search_v3 returned empty data without is_end")
    return extractor.extract_search_page(payload)


class ZhihuPageCapture:
    """本轮页面取数通道。账本与行为证据同目录，不经单独的环境变量。"""

    def __init__(self, channel: PageCaptureChannel) -> None:
        self.channel = channel
        self.page: Any = None
        self.after_seq = 0
        self.trigger_id: str | None = None
        self.closed = False

    @classmethod
    def open(cls, context: Any, *, mode: str = "discovery") -> ZhihuPageCapture:
        evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
        if not evidence_path:
            raise RuntimeError("zhihu_visit_ledger_path_missing")
        ledger = VisitLedger.create(
            content_visits_path(evidence_path),
            platform="zhihu",
            mode=mode if mode in {"discovery", "repair"} else "discovery",
        )
        channel = PageCaptureChannel(
            context,
            platform="zhihu",
            rules=ZHIHU_RULES,
            ledger=ledger,
            state_scripts=STATE_SCRIPTS,
        )
        return cls(channel)

    def install(self, page: Any) -> None:
        self.page = page
        self.channel.install(page)
        self.channel.attach(page, "primary")

    async def confirm_login(self) -> bool:
        captured = await self.channel.wait_for(
            ZHIHU_ME_RULE.name,
            after_seq=0,
            timeout=ME_CAPTURE_TIMEOUT_SECONDS,
        )
        if isinstance(captured, CapturedResponse):
            logged_in = me_is_logged_in(captured)
            self.channel.record_visit(
                captured,
                stage="login_state",
                subject_id=None,
                trigger=None,
                outcome="ok" if logged_in else "rejected",
            )
            return logged_in
        me_is_logged_in(None)
        return False

    def begin_navigation(self) -> None:
        """搜索页导航写入账本，并让随后截获的 search_v3 晚于这次动作。"""

        self.trigger_id = self.channel.action(
            "navigate",
            page_role="primary",
            target={"route": "/search", "query_keys": ["q", "type"]},
        )
        self.after_seq = self.channel.ledger.last_seq

    async def settle_pause(self) -> None:
        """搜索页打开后的固定停顿，与原先 5 秒等待相同，并写入账本。"""

        start_seq = self.channel.pause_started()
        await asyncio.sleep(SEARCH_SETTLE_SECONDS)
        self.channel.pause_finished("search_settle", start_seq, SEARCH_SETTLE_SECONDS, "n/a")

    async def next_page(self, page_number: int, extractor: Any) -> ZhihuSearchPage:
        captured = await self._take_search_response(page_number)
        try:
            search_page = search_page_from_captured(captured, extractor)
        except DataFetchError:
            if isinstance(captured, CapturedResponse):
                self._record_search(captured, page_number=page_number, outcome="invalid")
            raise
        self._record_search(
            captured,
            page_number=page_number,
            outcome="ok",
            is_end=search_page.is_end,
            raw_count=search_page.raw_count,
        )
        logger.info(
            f"[ZhihuPageCapture.next_page] captured search page {page_number}: "
            f"raw={search_page.raw_count}, contents={len(search_page.contents)}, "
            f"videos={len(search_page.video_ids)}, is_end={search_page.is_end}"
        )
        return search_page

    def _record_search(self, captured: CapturedResponse, *, page_number: int, outcome: str, **audit: Any) -> None:
        self.channel.record_visit(
            captured,
            stage="search_results",
            subject_id=None,
            trigger=self.trigger_id,
            outcome=outcome,
            search_page=page_number,
            **audit,
        )

    async def _take_search_response(self, page_number: int) -> CapturedResponse:
        if page_number <= 1:
            found = await self.channel.wait_for(
                ZHIHU_SEARCH_RULE.name,
                after_seq=self.after_seq,
                timeout=FIRST_PAGE_CAPTURE_TIMEOUT_SECONDS,
            )
        else:
            found = await self.channel.wait_for(
                ZHIHU_SEARCH_RULE.name,
                after_seq=self.after_seq,
                timeout=0,
            )
            if found is None:
                found = await self._scroll_for_next_page()
        if isinstance(found, CaptureFailure):
            raise DataFetchError("search_v3 request failed")
        if not isinstance(found, CapturedResponse):
            logger.error(
                "[ZhihuPageCapture.next_page] browser search response missing, "
                f"page={page_number}"
            )
            raise BrowserSearchResponseMissing()
        self.after_seq = found.seq
        return found

    async def _scroll_for_next_page(self) -> CapturedResponse | CaptureFailure | None:
        profile = load_behavior_profile("social_high_risk", strict=True)
        found: CapturedResponse | CaptureFailure | None = None
        for attempt in range(1, FOLLOWUP_SCROLL_ATTEMPTS + 1):
            await self._page_turn_pause()
            self.trigger_id = self.channel.action(
                "scroll",
                page_role="primary",
                target={"phase": "search_page", "attempt": attempt},
            )
            await human_scroll(self.page, profile, intent="search_page", max_passes=1)
            found = await self.channel.wait_for(
                ZHIHU_SEARCH_RULE.name,
                after_seq=self.after_seq,
                timeout=FOLLOWUP_CAPTURE_TIMEOUT_SECONDS,
            )
            if found is not None:
                logger.info(
                    "[ZhihuPageCapture.next_page] search page captured after scroll, "
                    f"attempt={attempt}"
                )
                return found
        logger.warning(
            "[ZhihuPageCapture.next_page] search page not captured after scrolls, "
            f"attempts={FOLLOWUP_SCROLL_ATTEMPTS}"
        )
        return found

    async def _page_turn_pause(self) -> None:
        seconds = random.uniform(*PAGE_TURN_PAUSE_SECONDS)
        start_seq = self.channel.pause_started()
        await self.page.wait_for_timeout(int(seconds * 1000))
        self.channel.pause_finished("search_page", start_seq, seconds, "n/a")

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.channel.aclose()
