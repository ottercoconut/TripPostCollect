"""#211 微博桌面搜索：登录确认、搜索页 DOM 读取、翻页与来源耗尽。

搜索只读本轮 ``s.weibo.com`` 页面。不拼接页码地址，不走浏览器外搜索接口。
"""

from __future__ import annotations

import logging
import os
import random
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

from trippostcollect.platforms.weibo.models import DataFetchError, PlatformRuntimeError
from trippostcollect.platforms.weibo.page_scripts import STATE_SCRIPTS, read_dom
from trippostcollect.runtime.human_flow import human_scroll, load_behavior_profile
from trippostcollect.runtime.page_capture import PageCaptureChannel
from trippostcollect.runtime.visit_ledger import VisitLedger, content_visits_path


logger = logging.getLogger("TripPostCollect")

HOME_URL = "https://weibo.com/"
SEARCH_HOST = "https://s.weibo.com/weibo"
FILTER_SIGNATURE = "nodup=1"
PAGE_CAP = 50
PAGE_TURN_PAUSE_SECONDS = (1.2, 2.8)
NEXT_SELECTOR = "div.m-page2 a.next"
SEARCH_NAVIGATION_TIMEOUT_MS = 15_000
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# 待实测。#211 侦察时会话已登录，没有落到登录页、访客页、安全验证或频控页。
# 登录/访客：passport.weibo.com（含 /visitor）以及独立访客路径。
WEIBO_PASSPORT_URL_RE = re.compile(
    r"(?:https?:)?//passport\.weibo\.com(?:[/?#]|$)",
    re.I,
)
WEIBO_VISITOR_URL_RE = re.compile(
    r"(?:https?:)?//(?:visitor\.weibo\.com|passport\.weibo\.com/visitor|weibo\.com/visitor)(?:[/?#]|$)",
    re.I,
)
# 安全验证：假设路径 captcha/verify/challenge/security，并认可见文案。
WEIBO_VERIFY_URL_RE = re.compile(
    r"(?:https?:)?//(?:passport\.weibo\.com|weibo\.com|s\.weibo\.com)"
    r"/(?:captcha|verify|challenge|security)(?:[/?#]|$)",
    re.I,
)
WEIBO_VERIFY_LOCATORS = (
    "text=安全验证",
    "text=验证码",
    "text=拖动滑块",
)
# 频控：假设路径 sorry/frequency/abuse，并认可见「访问频繁」「操作过于频繁」。
WEIBO_RATE_LIMIT_URL_RE = re.compile(
    r"(?:https?:)?//(?:weibo\.com|s\.weibo\.com|passport\.weibo\.com)"
    r"/(?:sorry|frequency|abuse)(?:[/?#]|$)",
    re.I,
)
WEIBO_RATE_LIMIT_LOCATORS = (
    "text=访问频繁",
    "text=操作过于频繁",
)


class WeiboSearchPageInvalid(DataFetchError):
    """搜索页不符合结束标记或分页结构。不是来源耗尽。"""

    def __init__(self) -> None:
        super().__init__("weibo search page invalid")
        self.code = "search_page_invalid"


@dataclass(frozen=True)
class WeiboSearchCandidate:
    mid: str
    uid: str
    mblogid: str

    @property
    def note_url(self) -> str:
        return f"https://weibo.com/{self.uid}/{self.mblogid}"


@dataclass
class WeiboSearchSnapshot:
    cards: list[WeiboSearchCandidate]
    current_page: int | None
    page_info: str
    max_page: int | None
    has_pager: bool
    has_more: bool
    stop_detail: str | None
    page_cap_reached: bool
    visit_id: str = ""


def search_url(keyword: str) -> str:
    """桌面「查看全部搜索结果」。不带页码。"""

    return f"{SEARCH_HOST}?q={quote(keyword.strip(), safe='')}&{FILTER_SIGNATURE}"


def blocking_code_for_url(url: str) -> str | None:
    """URL 命中验证、频控、登录或访客时返回运行级阻断码。验证先于登录。"""

    text = str(url or "")
    path = urlsplit(text).path or ""
    for probe in (text, path):
        if WEIBO_VERIFY_URL_RE.search(probe):
            return "captcha_detected"
        if WEIBO_RATE_LIMIT_URL_RE.search(probe):
            return "rate_limited"
        if WEIBO_PASSPORT_URL_RE.search(probe) or WEIBO_VISITOR_URL_RE.search(probe):
            return "login_required"
    return None


def uid_is_logged_in(value: Any) -> bool:
    """状态脚本只给出 exists/truthy。两者都为真才算已登录。"""

    if not isinstance(value, dict):
        return False
    return bool(value.get("exists")) and bool(value.get("truthy"))


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _pure_mid(value: Any) -> str:
    text = str(value or "")
    if text.isascii() and text.isdigit():
        return text
    raise WeiboSearchPageInvalid()


def classify_search_page(raw: Any, *, page_number: int) -> WeiboSearchSnapshot:
    """按文档判定继续、三种来源耗尽，或 ``search_page_invalid``。

    ``page_number`` 是本轮从 1 连续数的期望页。有分页控件时 ``li.cur`` 必须等于它，
    因此点击下一页后页码没前进、或发生跳页，都在这里失败。
    """

    if page_number < 1 or not isinstance(raw, dict):
        raise WeiboSearchPageInvalid()
    raw_cards = raw.get("cards")
    if not isinstance(raw_cards, list):
        raise WeiboSearchPageInvalid()
    cards: list[WeiboSearchCandidate] = []
    for item in raw_cards:
        if not isinstance(item, dict):
            raise WeiboSearchPageInvalid()
        mid = _pure_mid(item.get("mid"))
        uid = str(item.get("uid") or "")
        mblogid = str(item.get("mblogid") or "")
        if _ID_RE.fullmatch(uid) is None or _ID_RE.fullmatch(mblogid) is None:
            raise WeiboSearchPageInvalid()
        cards.append(WeiboSearchCandidate(mid=mid, uid=uid, mblogid=mblogid))

    has_pager = bool(raw.get("has_pager"))
    current = _optional_int(raw.get("current_text"))
    if has_pager and current != page_number:
        raise WeiboSearchPageInvalid()
    if page_number >= 2 and (not has_pager or not cards):
        raise WeiboSearchPageInvalid()

    page_info = str(raw.get("page_info") or "")
    max_page = _optional_int(raw.get("max_page"))
    next_disabled = raw.get("next_disabled")
    if next_disabled is not None and not isinstance(next_disabled, bool):
        raise WeiboSearchPageInvalid()
    no_results = bool(raw.get("no_results"))

    stop_detail: str | None
    if page_number == 1 and not cards and no_results:
        stop_detail = "no_results"
    elif page_number == 1 and cards and not has_pager:
        stop_detail = "single_page"
    elif cards and next_disabled is True:
        stop_detail = "next_disabled"
    elif cards and has_pager and next_disabled is False:
        stop_detail = None
    elif page_number == 1 and not cards:
        raise WeiboSearchPageInvalid()
    else:
        raise WeiboSearchPageInvalid()

    return WeiboSearchSnapshot(
        cards=cards,
        current_page=current,
        page_info=page_info,
        max_page=max_page,
        has_pager=has_pager,
        has_more=stop_detail is None,
        stop_detail=stop_detail,
        page_cap_reached=max_page == PAGE_CAP,
    )


async def _any_visible(page: Any, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if await locator.count() <= 0:
                continue
            target = locator.first if hasattr(locator, "first") else locator
            if await target.is_visible():
                return True
        except Exception:
            logger.debug("[WeiboPageCapture] visible probe failed selector=%s", selector)
            continue
    return False


async def blocking_code_for_page(page: Any) -> str | None:
    """可见特征先于 URL。验证先于频控，频控先于登录。"""

    if await _any_visible(page, WEIBO_VERIFY_LOCATORS):
        return "captcha_detected"
    url_code = blocking_code_for_url(str(getattr(page, "url", "") or ""))
    if url_code == "captcha_detected":
        return url_code
    if await _any_visible(page, WEIBO_RATE_LIMIT_LOCATORS) or url_code == "rate_limited":
        return "rate_limited"
    return url_code


class WeiboPageCapture:
    """本轮页面取数通道。账本与行为证据同目录。"""

    def __init__(self, channel: PageCaptureChannel) -> None:
        self.channel = channel
        self.page: Any = None
        self.trigger_id: str | None = None
        self.search_session = uuid.uuid4().hex
        self.closed = False

    @classmethod
    def open(cls, context: Any, *, mode: str = "discovery") -> WeiboPageCapture:
        evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
        if not evidence_path:
            raise RuntimeError("weibo_visit_ledger_path_missing")
        ledger = VisitLedger.create(
            content_visits_path(evidence_path),
            platform="weibo",
            mode=mode if mode in {"discovery", "repair"} else "discovery",
        )
        channel = PageCaptureChannel(
            context,
            platform="weibo",
            rules=(),
            ledger=ledger,
            state_scripts=STATE_SCRIPTS,
        )
        return cls(channel)

    def install(self, page: Any) -> None:
        self.page = page
        self.channel.install(page)

    async def confirm_login(self, page: Any) -> str | None:
        """返回阻断码；已登录返回 None。uid 原值不进日志，也不进账本。"""

        code = await blocking_code_for_page(page)
        if code:
            logger.error("[WeiboPageCapture.confirm_login] homepage blocked code=%s", code)
            return code
        value, _visit = await self.channel.read_state(
            page,
            "weibo.config_uid",
            stage="login_state",
            subject_id=None,
            trigger=None,
        )
        logged_in = uid_is_logged_in(value)
        exists = bool(value.get("exists")) if isinstance(value, dict) else False
        truthy = bool(value.get("truthy")) if isinstance(value, dict) else False
        if logged_in:
            logger.info(
                "[WeiboPageCapture.confirm_login] homepage uid exists=%s truthy=%s",
                exists,
                truthy,
            )
            return None
        logger.error(
            "[WeiboPageCapture.confirm_login] homepage uid exists=%s truthy=%s",
            exists,
            truthy,
        )
        return "login_required"

    def begin_navigation(self) -> None:
        """搜索页导航写入账本。随后的搜索读取晚于这次动作。"""

        self.search_session = uuid.uuid4().hex
        self.trigger_id = self.channel.action(
            "navigate",
            page_role="primary",
            target={"route": "/weibo", "query_keys": ["q", "nodup"]},
        )

    async def read_search(self, page_number: int) -> WeiboSearchSnapshot:
        page = self.page
        if page is None:
            raise RuntimeError("weibo_capture_channel_missing")
        code = await blocking_code_for_page(page)
        if code:
            raise PlatformRuntimeError(f"weibo search blocked: {code}", code=code)
        raw = await read_dom(page, "weibo.search_cards")
        try:
            snapshot = classify_search_page(raw, page_number=page_number)
        except WeiboSearchPageInvalid:
            self._record_search(page, page_number, outcome="invalid", cards=[], has_more=False)
            raise
        visit_id = self._record_search(
            page,
            page_number,
            outcome="ok",
            cards=snapshot.cards,
            has_more=snapshot.has_more,
        )
        snapshot.visit_id = visit_id
        logger.info(
            "[WeiboPageCapture.read_search] page=%s cards=%s has_more=%s stop=%s",
            page_number,
            len(snapshot.cards),
            snapshot.has_more,
            snapshot.stop_detail or "",
        )
        return snapshot

    def _record_search(
        self,
        page: Any,
        page_number: int,
        *,
        outcome: str,
        cards: list[WeiboSearchCandidate],
        has_more: bool,
    ) -> str:
        items = [{"id": card.mid} for card in cards]
        return self.channel.record_dom_visit(
            page,
            rule="weibo.search_cards",
            stage="search_results",
            subject_id=None,
            trigger=self.trigger_id,
            outcome=outcome,
            search_page=page_number,
            is_target=True,
            has_more=has_more,
            search_session=self.search_session,
            filter_signature=FILTER_SIGNATURE,
            items=items,
        )

    async def turn_page(self) -> None:
        """翻页停顿、真人分段滚到底，再点击下一页。不拼接页码。"""

        page = self.page
        if page is None:
            raise RuntimeError("weibo_capture_channel_missing")
        seconds = random.uniform(*PAGE_TURN_PAUSE_SECONDS)
        start_seq = self.channel.pause_started()
        await page.wait_for_timeout(int(seconds * 1000))
        self.channel.pause_finished("search_page", start_seq, seconds, "completed")

        profile = load_behavior_profile("social_high_risk", strict=True)
        limit = max(1, profile.scroll_passes[1])
        for attempt in range(1, limit + 1):
            self.channel.action(
                "scroll",
                page_role="primary",
                target={"phase": "search_page", "attempt": attempt},
            )
            await human_scroll(page, profile, intent="search_page", max_passes=1)
            try:
                at_bottom = bool(await read_dom(page, "weibo.at_bottom"))
            except Exception as exc:
                logger.warning(
                    "[WeiboPageCapture.turn_page] bottom probe failed type=%s",
                    type(exc).__name__,
                )
                at_bottom = False
            if at_bottom:
                break

        self.trigger_id = self.channel.action(
            "click_next",
            page_role="primary",
            target={"control": "m-page2.next"},
        )
        try:
            async with page.expect_navigation(
                wait_until="domcontentloaded",
                timeout=SEARCH_NAVIGATION_TIMEOUT_MS,
            ):
                await page.locator(NEXT_SELECTOR).click()
        except Exception as exc:
            logger.error(
                "[WeiboPageCapture.turn_page] next click failed type=%s",
                type(exc).__name__,
            )
            raise WeiboSearchPageInvalid() from exc

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.channel.aclose()
