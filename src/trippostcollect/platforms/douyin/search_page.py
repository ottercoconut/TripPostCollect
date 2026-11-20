"""#222 抖音搜索页：登录确认、搜索访问账本、翻页停顿。

搜索响应仍由既有浏览器截获逻辑合并。这里只在每一页被采纳后写入访问账本。
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import parse_qs, urlsplit

from trippostcollect.platforms.douyin.models import SearchResponseError
from trippostcollect.platforms.douyin.page_scripts import STATE_SCRIPTS, read_dom
from trippostcollect.platforms.douyin.parser import (
    decode_douyin_json_body,
    is_douyin_image_note,
    validate_douyin_search_response,
)
from trippostcollect.runtime.page_capture import CapturedResponse, CaptureRule, PageCaptureChannel
from trippostcollect.runtime.visit_ledger import VisitLedger, content_visits_path


logger = logging.getLogger("TripPostCollect")

HOME_URL = "https://www.douyin.com/"
PAGE_TURN_PAUSE_SECONDS = (1.2, 2.8)
API_RESOURCE_TYPES = frozenset({"xhr", "fetch"})

# 风控只认可见特征。短语与容器选择器来自 2026-11-20 侦察的最终 ``_risk``。
CAPTCHA_DETECTED = "captcha_detected"
RATE_LIMITED = "rate_limited"
LOGIN_REQUIRED = "login_required"
CAPTCHA_PHRASES = frozenset({"验证码", "请完成下列验证"})
RATE_LIMIT_PHRASES = frozenset({"操作过于频繁", "请稍后再试", "访问太频繁", "请求过于频繁"})

DOUYIN_USER_PROFILE_RULE = CaptureRule(
    name="douyin.user_profile",
    stage="creator_profile",
    method="GET",
    host="www.douyin.com",
    path="/aweme/v1/web/user/profile/other/",
    resource_types=API_RESOURCE_TYPES,
    body="json",
)
# 首页 stream 与后续 single 共用一条规则，响应体只由通道读一次。
DOUYIN_SEARCH_RULE = CaptureRule(
    name="douyin.search_response",
    stage="search_results",
    method="GET",
    host="www.douyin.com",
    path="/aweme/v1/web/general/search/<kind>/",
    resource_types=API_RESOURCE_TYPES,
    body="json",
    decode=decode_douyin_json_body,
)
DOUYIN_CAPTURE_RULES = (DOUYIN_SEARCH_RULE, DOUYIN_USER_PROFILE_RULE)
SEARCH_STREAM_ENDPOINT = "GET www.douyin.com /aweme/v1/web/general/search/stream/"
SEARCH_SINGLE_ENDPOINT = "GET www.douyin.com /aweme/v1/web/general/search/single/"
BROWSER_SEARCH_RESPONSE_MISSING = "browser_search_response_missing"
SEARCH_SCROLL_ATTEMPTS = 8


def blocking_code(snapshot: Any) -> str | None:
    """可见验证先于频控，频控先于登录面板。仅有隐藏验证码容器时不命中。"""

    if not isinstance(snapshot, dict):
        return None
    hits = {str(item) for item in snapshot.get("hits") or []}
    captcha = bool(
        snapshot.get("captcha_visible")
        or snapshot.get("verify_host")
        or hits & CAPTCHA_PHRASES
    )
    if captcha:
        return CAPTCHA_DETECTED
    if hits & RATE_LIMIT_PHRASES:
        return RATE_LIMITED
    if snapshot.get("login_panel_visible"):
        return LOGIN_REQUIRED
    return None


def search_page_items(data: Any) -> list[dict[str, str]]:
    """本页登记的 aweme_id 与类型。图文为 ``image``，其余作品为 ``video``。"""

    items: list[dict[str, str]] = []
    if not isinstance(data, list):
        return items
    for post in data:
        aweme = _aweme_of(post)
        if aweme is None:
            continue
        aweme_id = str(aweme.get("aweme_id") or "")
        if not aweme_id:
            continue
        kind = "image" if is_douyin_image_note(aweme) else "video"
        items.append({"id": aweme_id, "type": kind})
    return items


def _aweme_of(post: Any) -> dict[str, Any] | None:
    if not isinstance(post, dict):
        return None
    aweme = post.get("aweme_info")
    if isinstance(aweme, dict):
        return aweme
    mix = post.get("aweme_mix_info")
    mix_items = mix.get("mix_items") if isinstance(mix, dict) else None
    if isinstance(mix_items, list) and mix_items and isinstance(mix_items[0], dict):
        return mix_items[0]
    return None


def _has_more(value: Any) -> bool:
    return value in (True, 1)


class DouyinPageCapture:
    """本轮页面取数通道。账本与行为证据同目录。"""

    def __init__(self, channel: PageCaptureChannel) -> None:
        self.channel = channel
        self.page: Any = None
        self.search_session = uuid.uuid4().hex
        self.page_trigger_id = ""
        self.last_search_visit_id = ""
        self.last_search_http_status = 200
        self._consumed_search: set[int] = set()

    @classmethod
    def open(cls, context: Any, *, mode: str = "discovery") -> DouyinPageCapture:
        evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
        if not evidence_path:
            raise RuntimeError("douyin_visit_ledger_path_missing")
        ledger = VisitLedger.create(
            content_visits_path(evidence_path),
            platform="douyin",
            mode=mode if mode in {"discovery", "repair"} else "discovery",
        )
        channel = PageCaptureChannel(
            context,
            platform="douyin",
            rules=DOUYIN_CAPTURE_RULES,
            ledger=ledger,
            state_scripts=STATE_SCRIPTS,
            # 新标签只有作者页。打开时就标成 author，避免资料响应先于二次 attach。
            new_page_role="author",
        )
        return cls(channel)

    def install(self, page: Any) -> None:
        self.page = page
        self.channel.install(page)

    async def confirm_login(self, page: Any) -> str | None:
        """返回阻断码；已登录返回 None。不读 Cookie，也不把 HasUserLogin 原值写入账本。"""

        code = blocking_code(await read_dom(page, "douyin.visible_risk"))
        if code:
            logger.error("[DouyinPageCapture.confirm_login] homepage blocked code=%s", code)
            return code
        value, _visit = await self.channel.read_state(
            page,
            "douyin.login_state",
            stage="login_state",
            subject_id=None,
            trigger=None,
        )
        logged_in = isinstance(value, dict) and value.get("logged_in") is True
        if logged_in:
            logger.info("[DouyinPageCapture.confirm_login] HasUserLogin logged_in=true")
            return None
        logger.error("[DouyinPageCapture.confirm_login] HasUserLogin logged_in=false")
        return LOGIN_REQUIRED

    def begin_search(self) -> None:
        """每个关键词一条搜索会话。随后采纳的首页晚于这次导航动作。"""

        self.search_session = uuid.uuid4().hex
        self.page_trigger_id = self.channel.action(
            "navigate",
            page_role="primary",
            target={"route": "/search", "query_keys": ["type"]},
        )
        self.last_search_visit_id = ""

    async def before_scroll(self, attempt: int) -> None:
        """滚轮前的翻页停顿，并记下随后那一页搜索访问的触发动作。"""

        page = self.page
        seconds = random.uniform(*PAGE_TURN_PAUSE_SECONDS)
        start_seq = self.channel.pause_started()
        waiter = getattr(page, "wait_for_timeout", None) if page is not None else None
        if waiter is None:
            await asyncio.sleep(seconds)
        else:
            await waiter(int(seconds * 1000))
        self.channel.pause_finished("search_page", start_seq, seconds, "completed")
        self.page_trigger_id = self.channel.action(
            "scroll",
            page_role="primary",
            target={"phase": "search_page", "attempt": int(attempt)},
        )

    def record_search_page(self, payload: dict[str, Any], *, page_number: int, http_status: int) -> str:
        """采纳后的搜索页写访问。不写关键词、查询值或正文。"""

        items = search_page_items(payload.get("data"))
        endpoint = SEARCH_STREAM_ENDPOINT if int(page_number) <= 1 else SEARCH_SINGLE_ENDPOINT
        seq = self.channel.next_seq()
        visit_id = self.channel.ledger.append(
            "visit",
            seq=seq,
            page_role="primary",
            at=self.channel._clock(),
            recorded_at=self.channel._clock(),
            stage="search_results",
            source="page_response",
            rule="douyin.search_response",
            endpoint=endpoint,
            status=int(http_status),
            outcome="ok",
            route="/search",
            query_keys=["type"],
            subject_id=None,
            trigger=self.page_trigger_id or None,
            search_page=int(page_number),
            is_target=True,
            has_more=_has_more(payload.get("has_more")),
            search_session=self.search_session,
            items=items,
        )
        self.last_search_visit_id = visit_id
        logger.info(
            "[DouyinPageCapture.record_search_page] page=%s items=%s has_more=%s",
            page_number,
            len(items),
            _has_more(payload.get("has_more")),
        )
        return visit_id

    async def search_info_by_keyword(
        self,
        *,
        keyword: str,
        offset: int = 0,
        search_id: str = "",
    ) -> dict[str, Any]:
        """只消费本轮搜索页已经截获的响应。后续页最多滚 8 次。"""

        found = await self._take_search_response(keyword=keyword, offset=offset, search_id=search_id)
        if found is None and offset > 0 and search_id:
            await self._scroll_for_search_response(keyword=keyword, offset=offset, search_id=search_id)
            found = await self._take_search_response(keyword=keyword, offset=offset, search_id=search_id)
        if found is None:
            logger.error(
                "[DouyinPageCapture.search_info_by_keyword] browser search response missing, "
                "offset=%s search_id_present=%s",
                offset,
                bool(search_id),
            )
            raise SearchResponseError(BROWSER_SEARCH_RESPONSE_MISSING)
        self.last_search_http_status = int(found.status or 200)
        payload = found.json if isinstance(found.json, dict) else {}
        return validate_douyin_search_response(payload)

    async def _take_search_response(
        self,
        *,
        keyword: str,
        offset: int,
        search_id: str,
    ) -> CapturedResponse | None:
        matched = await self._matching_search(keyword=keyword, offset=offset, search_id=search_id)
        if not matched:
            return None
        selected = _prefer_search_capture(matched)
        self._consumed_search.update(item.seq for item in matched)
        nil_info = selected.json.get("search_nil_info") if isinstance(selected.json, dict) else None
        logger.info(
            "[DouyinPageCapture.search_info_by_keyword] reuse browser search response, "
            "offset=%s search_id_present=%s duplicates=%s search_nil_type=%s",
            offset,
            bool(search_id),
            len(matched),
            nil_info.get("search_nil_type") if isinstance(nil_info, dict) else "",
        )
        return selected

    async def _matching_search(
        self,
        *,
        keyword: str,
        offset: int,
        search_id: str,
    ) -> list[CapturedResponse]:
        await self.channel.drain_pending()
        wanted = (keyword, int(offset), str(search_id or ""))
        matched: list[CapturedResponse] = []
        for item in self.channel.drain(DOUYIN_SEARCH_RULE.name, after_seq=0):
            if not isinstance(item, CapturedResponse) or item.seq in self._consumed_search:
                continue
            if item.body_error or not isinstance(item.json, dict):
                continue
            if search_request_key(item.request_url) == wanted:
                matched.append(item)
        return matched

    async def _scroll_for_search_response(
        self,
        *,
        keyword: str,
        offset: int,
        search_id: str,
    ) -> bool:
        """可见滚轮触发页面自己的下一页请求。目标只比已截获的页深一屏。"""

        page = self.page
        if page is None:
            return False
        for attempt in range(1, SEARCH_SCROLL_ATTEMPTS + 1):
            await self.before_scroll(attempt)
            try:
                viewport = page.viewport_size or {"width": 1920, "height": 1080}
                await page.mouse.move(
                    max(10, int(viewport["width"] * random.uniform(0.68, 0.88))),
                    max(10, int(viewport["height"] * random.uniform(0.55, 0.82))),
                    steps=random.randint(3, 7),
                )
                await page.mouse.wheel(0, random.randint(650, 1050))
                await page.wait_for_timeout(random.randint(1400, 2400))
            except Exception as exc:
                logger.warning(
                    "[DouyinPageCapture._scroll_for_search_response] visible scroll failed, "
                    "offset=%s attempt=%s reason=%s",
                    offset,
                    attempt,
                    type(exc).__name__,
                )
                return False
            if await self._matching_search(keyword=keyword, offset=offset, search_id=search_id):
                logger.info(
                    "[DouyinPageCapture._scroll_for_search_response] browser page reached offset, "
                    "offset=%s attempts=%s",
                    offset,
                    attempt,
                )
                return True
        logger.warning(
            "[DouyinPageCapture._scroll_for_search_response] browser page did not reach offset, "
            "offset=%s attempts=%s",
            offset,
            SEARCH_SCROLL_ATTEMPTS,
        )
        return False


def search_request_key(url: str) -> tuple[str, int, str] | None:
    """搜索请求上的关键词、offset 和 search_id。缺 offset 视为 0。"""

    query = parse_qs(urlsplit(str(url or "")).query)

    def first(name: str) -> str:
        values = query.get(name) or []
        return str(values[0]) if values else ""

    offset_text = first("offset") or "0"
    try:
        offset = int(offset_text)
    except ValueError:
        return None
    return first("keyword"), offset, first("search_id")


def _prefer_search_capture(matches: list[CapturedResponse]) -> CapturedResponse:
    """同一页多条响应时优先最新的非 verify_check；全部是验证信号时保留最后一条。"""

    selected = matches[-1]
    for candidate in reversed(matches):
        payload = candidate.json if isinstance(candidate.json, dict) else {}
        nil_info = payload.get("search_nil_info")
        if not (isinstance(nil_info, dict) and nil_info.get("search_nil_type") == "verify_check"):
            return candidate
    return selected


async def read_homepage_login(context: Any, page: Any) -> bool:
    """经已登记状态脚本判断首页 ``HasUserLogin`` 是否为 ``\"1\"``。异常视为未登录。"""

    try:
        with TemporaryDirectory() as directory:
            ledger = VisitLedger.create(
                Path(directory) / "content_visits.jsonl",
                platform="douyin",
                mode="discovery",
            )
            channel = PageCaptureChannel(
                context,
                platform="douyin",
                rules=(),
                ledger=ledger,
                state_scripts=STATE_SCRIPTS,
            )
            channel.install(page)
            value, _visit = await channel.read_state(
                page,
                "douyin.login_state",
                stage="login_state",
                subject_id=None,
                trigger=None,
            )
            channel.close()
    except Exception:
        return False
    return isinstance(value, dict) and value.get("logged_in") is True
