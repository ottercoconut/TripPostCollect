"""#222 抖音搜索页：登录确认、搜索访问账本、翻页停顿。

搜索响应仍由既有浏览器截获逻辑合并。这里只在每一页被采纳后写入访问账本。
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import uuid
from typing import Any

from trippostcollect.platforms.douyin.page_scripts import STATE_SCRIPTS, read_dom
from trippostcollect.platforms.douyin.parser import is_douyin_image_note
from trippostcollect.runtime.page_capture import CaptureRule, PageCaptureChannel
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
DOUYIN_CAPTURE_RULES = (DOUYIN_USER_PROFILE_RULE,)
SEARCH_STREAM_ENDPOINT = "GET www.douyin.com /aweme/v1/web/general/search/stream/"
SEARCH_SINGLE_ENDPOINT = "GET www.douyin.com /aweme/v1/web/general/search/single/"


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
