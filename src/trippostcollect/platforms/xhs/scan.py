"""小红书 B2 全量滚动扫描：首页输入搜索、点选“图文”、目标会话逐页登记、按序完成批次与来源耗尽。

前端每次从第 1 页开始的搜索是一个会话，键为 `(search_id, 筛选签名)`；只有 `is_target_search` 为真的
目标会话参与登记与批次。循环每步只读可见卡片的 ID 与几何：已处理的滑过、视频记决定、未知图文就地
处理；页按序完成。目标会话从第 1 页连续截获到 has_more=false 且全部完成才是来源耗尽。

翻页停顿不变式（与门禁 G3 同义）：目标会话每截获一页 has_more=true，此后任何一次列表滚动之前都
必须先完成一次 `search_page` 停顿，且停顿开始序号大于该页的到达序号；仍在读体的搜索响应按欠账处理。
运行级异常一律上抛，由 `core.search` 集中写停止事件。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from trippostcollect.platforms.xhs.behavior import list_area_box, merge_pending_behavior_evidence
from trippostcollect.platforms.xhs.capture import (
    XHS_SEARCH_RULE,
    classify_xhs_api_response,
    raise_for_runtime_verdict,
)
from trippostcollect.platforms.xhs.errors import (
    PlatformRuntimeError,
    XHSLoginExpired,
    XHSNetworkRecoveryTimeout,
    XHSRequestFailed,
)
from trippostcollect.platforms.xhs.navigation import (
    XHS_SEARCH_CAPTURE_SECONDS,
    XhsSearchEntryFailed,
    is_search_result_url,
    search_request_page,
)
from trippostcollect.platforms.xhs.page_scripts import (
    dom_card_diagnostic,
    dom_filter_diagnostic,
    dom_filter_state,
    dom_scroll_metrics,
    dom_visible_cards,
)
from trippostcollect.platforms.xhs.parser import search_items_from_response
from trippostcollect.runtime.human_flow import load_behavior_profile
from trippostcollect.runtime.human_pointer import Box, ClickPlan, pointer_for
from trippostcollect.runtime.page_capture import CaptureFailure, CapturedResponse
from trippostcollect.runtime.visit_ledger import line_seq
from trippostcollect.xhs.search_target import (
    XHS_TARGET_FILTER_LABEL,
    is_target_search,
    search_filter_signature,
)

logger = logging.getLogger("MediaCrawler")

SEARCH_RULE = XHS_SEARCH_RULE.name
XHS_SEARCH_LOAD_WAIT_SECONDS = 20.0
# 搜索两档节奏停顿（操作人 2026-10-14 要求全面提速，#79）：每页第一张未处理卡片点击前；目标会话页到达后、
# 下一次程序滚动之前。
XHS_SEARCH_RESULTS_PAUSE_SECONDS = (2.0, 5.0)
XHS_SEARCH_PAGE_PAUSE_SECONDS = (4.0, 10.0)
XHS_SCROLL_SETTLE_SECONDS = (0.4, 1.2)
# 滚动时光标位于结果列表与列表可见区的相交矩形内（与行为动作同一区域，见 behavior.list_area_box）；取不到矩形时
# 按列表可见区比例取区域。光标已在区域内就不动，每一步的位移由若干个触控板手势凑成，总和恰为该步距离。
# 滚动位置、可见区均指列表的实际滚动容器（见 page_scripts.scroll_metrics）。单步滚动不超过列表可见区高度 h 的
# 0.8 倍；可点击带为可见区顶部加 0.1h 到底边上方 8px，且封面中心须命中本卡片。h ≥ 80px 时 0.8h ≤ 0.9h − 8，
# 封面中心一步越不过整条带，向下扫描不跳过任何一行卡片。
XHS_SCROLL_VIEWPORT_RATIO = 0.8
XHS_CLICK_BAND_TOP = 0.1
XHS_CLICK_BAND_BOTTOM_MARGIN_PX = 8.0
XHS_STALL_JIGGLES = 3
XHS_UNREGISTERED_CARD_STEPS = 2
# 未登记卡片诊断最多描述的 ID 个数，以及每个 ID 最多列出的响应出处条数。
XHS_UNREGISTERED_DIAGNOSTIC_IDS = 8
XHS_UNREGISTERED_DIAGNOSTIC_ENTRIES = 6
# “搜索项从未渲染”诊断最多描述的 ID 个数（出处条数同上）。
XHS_UNRENDERED_DIAGNOSTIC_IDS = 10
# 向上补漏的上限：已读到、滑到上方仍未决定的卡片每张至多补漏这么多次；已越过的页里从未读到的项，该页至多向上
# 补漏滚动这么多步（见 `_backfill_unread`）。
XHS_BACKFILL_ATTEMPTS = 3
XHS_FILTER_POLLS = 20
XHS_FILTER_POLL_SECONDS = 0.25
# 收起面板先移开光标（#81，第 6 轮实证：光标离开入口与面板的并集时悬停打开的面板会收起）：落点取结果列表区域
# （同滚动，#88）中入口与面板下沿以下、留出间距的部分，不够高时直接点入口；移开后至多轮询这么多次确认收起，
# 确认不了再点入口。
XHS_FILTER_LEAVE_MARGIN_PX = 24.0
XHS_FILTER_LEAVE_MIN_HEIGHT_PX = 40.0
XHS_FILTER_LEAVE_POLLS = 8
# 入口相对第 1 页结果的渲染时机未经实测（Mac 首轮在结果到达约 1 秒后读到空入口）；每次尝试按轮询间隔
# 最多等这么久。入口出现后、点击之前的人手反应停顿是输入微停顿，不写 pause 行。
XHS_FILTER_ENTRY_WAIT_SECONDS = 15.0
XHS_BEFORE_FILTER_SECONDS = (0.6, 1.8)
XHS_FILTER_APPLIED_TEXT = "已筛选"
# 筛选失败时并入导航诊断的 filter_diagnostic 字段：只有计数与布尔，不含正文或地址；祖先链另并入、不进日志。
XHS_FILTER_DIAGNOSTIC_KEYS = (
    "layout_count", "entry_candidates", "entry_visible", "panel_count", "panel_visible_count", "label_nodes",
    "inner_width", "inner_height", "scroll_y",
)
XHS_SCROLL_TOP_MAX_STEPS = 400
# 读 DOM 之前先等在途的搜索响应读完体（列表可能已按它重新渲染）；单次最长约 10 秒。
XHS_SEARCH_SETTLE_ROUNDS = 20
XHS_SEARCH_SETTLE_POLL_SECONDS = 0.5
# 首页输入搜索的一次受控恢复覆盖这些码；筛选与收起面板各有自己的一次重试。
ENTRY_RECOVERABLE_CODES = frozenset({"explore_not_rendered", "search_not_rendered", "search_keyword_mismatch"})
SCAN_RANDOM = random.SystemRandom()


class XhsScanInterrupted(RuntimeError):
    """验证完成后需要重触发当前动作：仍在结果页则重滚一步，已离开则回首页重新输入。"""


class XhsFilterAttemptFailed(RuntimeError):
    """一次筛选尝试失败（入口未出现、面板未打开、选项缺失）；只在 `_apply_target_filter` 内部重试。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SearchItem:
    note_id: str
    xsec_token: str
    xsec_source: str
    card_type: str
    position: int


@dataclass(frozen=True)
class ResponseEntry:
    """一次搜索截获里的一个条目（含非目标会话与非笔记条目）；只留在内存中，供未登记卡片诊断。"""

    seq: int
    session: tuple[str, str]
    is_target: bool
    page: int
    position: int
    model_type: str
    card_type: str


@dataclass(eq=False)
class SearchSession:
    search_id: str
    filter_signature: str
    started_seq: int
    is_target: bool
    keyword_matches: bool
    trigger: Optional[str] = None
    pages: dict[int, "SearchPage"] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return self.search_id, self.filter_signature

    @property
    def last_page(self) -> Optional["SearchPage"]:
        return self.pages[max(self.pages)] if self.pages else None


@dataclass(eq=False)
class SearchPage:
    session: SearchSession
    page: int
    seq: int
    visit_id: str
    has_more: Optional[bool]
    raw_count: int
    items: tuple[SearchItem, ...]
    # 本页的非笔记条目（model_type 不是 note、带 id，如 hot_query）：列表里同样渲染成卡片，扫描时跳过。
    non_note_ids: frozenset[str] = frozenset()
    non_note_count: int = 0
    non_notes_seen: set[str] = field(default_factory=set)
    observed: set[str] = field(default_factory=set)
    passed: bool = False
    # 首次判为越过时的来源（见 `_mark_observed`、`_handle_list_bottom`），只供诊断。
    passed_by: Optional[dict[str, Any]] = None
    # 已越过却从未读到的未决定项向上补漏（见 `_backfill_unread`）：仍在找的 ID 与每次补漏滚动后的读数。
    backfill_pending: set[str] = field(default_factory=set)
    backfill_steps: list[dict[str, Any]] = field(default_factory=list)
    began: bool = False
    results_paused: bool = False
    finished: bool = False

    @property
    def note_ids(self) -> list[str]:
        return [item.note_id for item in self.items]


class CardRegistry:
    """note_id → (页, 项)，非笔记条目 id → 页。会话重置时整体换成新会话；同一会话内以首次出现的页为准，
    保证页按序完成。"""

    def __init__(self) -> None:
        self._cards: dict[str, tuple[SearchPage, SearchItem]] = {}
        self._non_notes: dict[str, SearchPage] = {}

    def register(self, page: SearchPage) -> None:
        for item in page.items:
            self._cards.setdefault(item.note_id, (page, item))
        for item_id in page.non_note_ids:
            self._non_notes.setdefault(item_id, page)

    def get(self, note_id: str) -> Optional[tuple[SearchPage, SearchItem]]:
        return self._cards.get(note_id)

    def non_note(self, item_id: str) -> Optional[SearchPage]:
        return self._non_notes.get(item_id)


class PageTracker:
    """目标会话的页按序完成：第 k 页完成后才轮到第 k+1 页；负责 begin_batch / finish_batch 时机。"""

    def __init__(self, session: SearchSession, accumulator: Any) -> None:
        self.session = session
        self.accumulator = accumulator
        self.next_page = 1

    @property
    def current(self) -> Optional[SearchPage]:
        return self.session.pages.get(self.next_page)

    def begin(self, page: SearchPage) -> None:
        if not page.began:
            self.accumulator.begin_batch()
            page.began = True

    def ready(self, page: SearchPage) -> bool:
        return (page.passed or not page.items) and all(
            self.accumulator.is_known(note_id) for note_id in page.note_ids
        )

    def _finish(self, page: SearchPage, *, complete: bool) -> None:
        self.accumulator.finish_batch(
            source_page=page.page,
            source_cursor=self.session.search_id,
            source_has_more=page.has_more,
            raw_batch_count=page.raw_count,
            resume_page=None,
            resume_cursor=None,
            batch_complete=complete,
            discovery_phase="full_scan",
            count_stagnation=True,
        )

    def finish(self, page: SearchPage) -> None:
        self.begin(page)
        self._finish(page, complete=True)
        page.finished = True
        self.next_page += 1

    def abandon(self) -> None:
        """会话重置时未完成的当前页记一次未完成批次（不发布），已决定的候选照常保留。"""
        page = self.current
        if page is not None and page.began and not page.finished:
            self._finish(page, complete=False)


@dataclass
class ScanState:
    keyword: str
    accumulator: Any
    sessions: dict[tuple[str, str], SearchSession] = field(default_factory=dict)
    target: Optional[SearchSession] = None
    tracker: Optional[PageTracker] = None
    registry: CardRegistry = field(default_factory=CardRegistry)
    search_cursor: int = 0
    owed_page_seq: int = 0
    page_pause_cleared_seq: int = 0
    entering: bool = False
    needs_entry: bool = True
    entry_navigate: bool = False
    needs_top: bool = False
    pending_session: Optional[SearchSession] = None
    retrigger: bool = False
    exhausted: bool = False
    network_state: dict[str, object] = field(default_factory=dict)
    unregistered: Counter = field(default_factory=Counter)
    # 条目 id → 本轮全部搜索截获中的出处（见 ResponseEntry）。
    response_entries: dict[str, list[ResponseEntry]] = field(default_factory=dict)
    backfill_attempts: Counter = field(default_factory=Counter)
    observed_index: dict[str, int] = field(default_factory=dict)
    scroll_container: str = ""


def _rect(value: Any) -> Optional[tuple[float, float, float, float]]:
    """页面脚本给出的 {left, top, right, bottom}；缺失或为空时为 None。"""
    if not isinstance(value, dict):
        return None
    left, top, right, bottom = (float(value.get(key) or 0) for key in ("left", "top", "right", "bottom"))
    return (left, top, right, bottom) if right > left and bottom > top else None


def _panel_ready(state: dict[str, Any]) -> bool:
    return bool(state.get("panel_visible") and state.get("option"))


def _hover_region(state: dict[str, Any]) -> tuple[Box, ...]:
    """悬停打开的面板在光标离开入口与面板时收起（#81）：接近“图文”的轨迹限定在入口框与面板框的并集内；收起面板时
    移开光标的落点取在这个并集下方（见 `_leave_area`）。"""
    return tuple(Box.from_point(state[key]) for key in ("entry", "panel") if state.get(key))


def _leave_area(area: Optional[Box], region: tuple[Box, ...]) -> Optional[Box]:
    """移开光标收起面板时的落点区域：列表区域中位于入口与面板下沿以下、留出间距的部分；不够高时为 None。"""
    if area is None:
        return None
    bottom = max((box.top + box.height for box in region), default=area.top)
    top = max(area.top, bottom + XHS_FILTER_LEAVE_MARGIN_PX)
    height = area.top + area.height - top
    return Box(area.left, top, area.width, height) if height >= XHS_FILTER_LEAVE_MIN_HEIGHT_PX else None


def _filter_moment(state: dict[str, Any]) -> dict[str, Any]:
    """筛选尝试诊断里一个时刻的状态：只记布尔与入口文字（filter_state 只给“筛选”“已筛选”或空串）。"""
    return {
        "panel_visible": bool(state.get("panel_visible")),
        "option": bool(state.get("option")),
        "entry_text": str(state.get("entry_text") or ""),
    }


def _origins_text(responses: Optional[list[dict[str, Any]]]) -> str:
    """日志里的出处摘要：会话（current 为当前目标会话，target 为其他目标会话，non_target）、页码与 model_type。"""
    if responses is None:
        return "none"

    def kind(item: dict[str, Any]) -> str:
        if item["current_target"]:
            return "current"
        return "target" if item["is_target"] else "non_target"

    return "+".join(f"{kind(item)}:p{item['page']}:{item['model_type'] or '?'}" for item in responses)


def _index_range(values: Any) -> Optional[list[int]]:
    indices = [value for value in values if value >= 0]
    return [min(indices), max(indices)] if indices else None


class XhsScanMixin:
    """全量滚动扫描；作为 XiaoHongShuCrawler 的 mixin。单篇处理由 `overlay.XhsOverlayMixin._process_note_card` 提供。"""

    @staticmethod
    async def _scan_sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

    def _scan_position(self) -> dict[str, Any]:
        """运行级失败的停止事件位置：目标会话中正在处理（或等待截获）的页与 search_id；全量扫描没有恢复位置。"""
        scan = getattr(self, "_scan", None)
        tracker = scan.tracker if scan is not None else None
        return {
            "source_page": tracker.next_page if tracker is not None else None,
            "source_cursor": tracker.session.search_id if tracker is not None else None,
        }

    async def _run_full_scan(self, keyword: str, accumulator: Any) -> None:
        """一个关键词的全量扫描：直到来源耗尽或运行级异常上抛。"""
        scan = self._scan = ScanState(keyword=keyword, accumulator=accumulator)
        if not self._behavior_stage_done:
            self._pending_behavior_evidence = {}
        while accumulator.can_continue and not scan.exhausted:
            try:
                if scan.needs_entry:
                    await self._start_target_session()
                elif not self._behavior_stage_done:
                    await self._run_initial_behavior_stage()
                    scan.needs_top = True
                elif scan.needs_top:
                    await self._scroll_to_top()
                    scan.needs_top = False
                elif scan.retrigger:
                    scan.retrigger = False
                    await self._retrigger_scroll()
                else:
                    await self._scan_step()
            except XHSLoginExpired as exc:
                logger.warning(f"[XiaoHongShuCrawler.scan] Login expired during full scan: {exc}")
                if not await self._wait_for_midrun_login_recovery():
                    raise PlatformRuntimeError("XHS login remained expired", code="login_required") from exc
                self._after_scan_interruption()
            except XHSRequestFailed as exc:
                await self._pause_scan_for_network(exc)
            except XhsScanInterrupted as exc:
                logger.info(f"[XiaoHongShuCrawler.scan] Re-triggering after {exc}")
                self._after_scan_interruption()

    def _after_scan_interruption(self) -> None:
        scan = self._scan
        if scan.needs_entry or not is_search_result_url(self.context_page.url):
            scan.needs_entry = True
            scan.entry_navigate = True
        else:
            scan.retrigger = True

    async def _pause_scan_for_network(self, exc: XHSRequestFailed) -> None:
        """§3.8.3：搜索请求 net::ERR_* 失败进入网络暂停，退避后重滚一步；不可恢复的失败为运行级。"""
        scan = self._scan
        if not exc.recoverable:
            raise PlatformRuntimeError(str(exc), code="search_request_failed") from exc
        if not await self._pause_for_network_recovery(exc, stage="full_scan", state=scan.network_state):
            started_at = float(scan.network_state.get("started_at") or self._popup_monotonic())
            raise XHSNetworkRecoveryTimeout("full_scan", self._popup_monotonic() - started_at) from exc
        self._after_scan_interruption()

    # 进入搜索与目标会话 ---------------------------------------------------------

    async def _start_target_session(self) -> None:
        """回车 → 非目标第 1 页 → 点选“图文” → 目标会话第 1 页 → 收起面板（§3.2 第 1–5 条）。"""
        scan = self._scan
        for attempt in (0, 1):
            scan.entering = True
            try:
                await self._enter_search(navigate=scan.entry_navigate or attempt > 0)
                session = await self._apply_target_filter()
                if not session.keyword_matches:
                    raise XhsSearchEntryFailed("search_keyword_mismatch")
                await self._close_filter_panel()
            except XhsSearchEntryFailed as exc:
                if attempt or exc.code not in ENTRY_RECOVERABLE_CODES:
                    raise
                logger.warning(
                    "[XiaoHongShuCrawler.scan] Search entry failed; re-entering from the home page once: "
                    f"{exc.code}"
                )
                continue
            finally:
                scan.entering = False
            self._adopt_target_session(session)
            scan.needs_entry = False
            scan.entry_navigate = False
            return

    async def _enter_search(self, *, navigate: bool) -> None:
        try:
            await self._open_behavior_search_page_on_primary_page(self._scan.keyword, navigate=navigate)
        except XhsSearchEntryFailed:
            # 网络失败、验证或登录失效优先于“未出结果”。
            await self._consume_search_captures()
            raise
        await self._consume_search_captures()

    async def _apply_target_filter(self) -> SearchSession:
        """等入口、打开面板、按精确文字点选“图文”；以“已筛选”加目标会话第 1 页判定成功。

        入口始终未出现、面板未打开、选项缺失或点选后未确认都只让本次尝试失败；重试一次，两次都失败才停止。
        每次失败的尝试各写一条诊断（含重试成功的情况），两次都失败时另写最终的一条。
        """
        page = self.context_page
        after_seq = self._require_capture().ledger.last_seq
        reason = ""
        clicks: list[str] = []
        for attempt in (1, 2):
            session = self._newest_target_session(after_seq)
            if attempt > 1 and session is not None and await self._filter_applied():
                self._note_target_trigger(session, clicks)
                return session
            moments: dict[str, dict[str, Any]] = {}
            try:
                state, hovered = await self._open_filter_panel(moments)
                click = await self._press_filter_option(state, hovered=hovered, moments=moments)
            except XhsFilterAttemptFailed as exc:
                reason = exc.reason
                await self._record_filter_failure(reason, attempt=attempt, moments=moments)
                continue
            clicks.append(click)
            session = await self._await_target_page_one(after_seq=line_seq(click))
            if session is not None and await self._filter_applied():
                self._note_target_trigger(session, clicks)
                return session
            reason = "filter_not_confirmed"
            logger.warning(
                "[XiaoHongShuCrawler.scan] Target filter not confirmed "
                f"(attempt {attempt}): target_session={session is not None}, url_path_ok="
                f"{is_search_result_url(page.url)}"
            )
            await self._record_filter_failure(reason, attempt=attempt, moments=moments)
        await self._record_filter_failure(reason)
        raise XhsSearchEntryFailed("target_filter_not_applied")

    async def _filter_applied(self) -> bool:
        state = await dom_filter_state(self.context_page, XHS_TARGET_FILTER_LABEL)
        return state.get("entry_text") == XHS_FILTER_APPLIED_TEXT

    def _note_target_trigger(self, session: SearchSession, clicks: list[str]) -> None:
        """目标会话第 1 页的 trigger 不是本次点选“图文”时记一行日志（门禁 G1 会因此失败），判定不变。

        日志带 trigger 的动作种类、target 与序号，以及该会话与最近回车会话的起始序号（回车会话是否已是目标会话）。
        """
        if session.trigger in clicks:
            return
        seq, kind, target = self._trigger_action(session.trigger) or (None, "unknown", {})
        enter = max(
            (
                other
                for other in self._scan.sessions.values()
                if (self._trigger_action(other.trigger) or (None, "", {}))[1] == "press_enter"
            ),
            key=lambda other: other.started_seq,
            default=None,
        )
        logger.warning(
            "[XiaoHongShuCrawler.scan] Target session page 1 was not triggered by this note-type click: "
            f"trigger={kind}, trigger_target={json.dumps(target, ensure_ascii=False, sort_keys=True)}, "
            f"trigger_seq={seq}, target_session_seq={session.started_seq}, "
            f"enter_session_seq={enter.started_seq if enter else None}, "
            f"enter_session_target={enter.is_target if enter else None}, note_type_clicks={len(clicks)}"
        )

    async def _open_filter_panel(self, moments: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], bool]:
        """本次尝试的面板与它是否由悬停打开：已开且有“图文”直接用；开着却没有选项先收起再重开，以免点入口反把它关上。

        光标先接近入口并按样本静止，再读一次状态：面板已因悬停打开且有“图文”就不再按下（按下会把它关上，#81），
        否则照常按下。`moments` 记下接近前、按下前、按下后第 1 次轮询与轮询结束时的筛选状态，供失败诊断。
        """
        page = self.context_page
        state = await dom_filter_state(page, XHS_TARGET_FILTER_LABEL)
        if _panel_ready(state):
            return state, False
        if state.get("panel_visible"):
            await self._close_filter_panel()
        entry = await self._wait_for_filter_entry()
        await self._scan_sleep(SCAN_RANDOM.uniform(*XHS_BEFORE_FILTER_SECONDS))
        moments["before_approach"] = _filter_moment(await dom_filter_state(page, XHS_TARGET_FILTER_LABEL))
        pointer = pointer_for(page)
        plan = await self._plan_click(entry)
        self._record_trigger_action("apply_filter", {"filter": "panel_open"}, pointer=plan.evidence)
        await pointer.approach(plan)
        await pointer.settle(plan)
        state = await dom_filter_state(page, XHS_TARGET_FILTER_LABEL)
        moments["before_press"] = _filter_moment(state)
        if _panel_ready(state):
            logger.info("[XiaoHongShuCrawler.scan] Filter panel opened on hover; entry not pressed")
            return state, True
        await pointer.press(plan)
        opened = await self._poll_filter_state(_panel_ready, moments=moments)
        if opened is not None:
            return opened, False
        panel_visible = moments.get("poll_end", {}).get("panel_visible")
        raise XhsFilterAttemptFailed("filter_option_missing" if panel_visible else "filter_panel_not_opened")

    async def _press_filter_option(
        self, state: dict[str, Any], *, hovered: bool, moments: dict[str, dict[str, Any]],
    ) -> str:
        """点选“图文”，返回账本动作 id。面板由悬停打开时，接近轨迹的每个点都在入口框与面板框的并集内（见
        `_hover_region`）；按下入口打开的照常接近。到达并静止后再读一次状态（`moments.before_option_press`）：面板仍可见
        且选项仍在才按下，否则本次尝试以 `filter_panel_closed_before_option` 失败，不再空等目标会话第 1 页。
        """
        page = self.context_page
        pointer = pointer_for(page)
        plan = await self._plan_click(state["option"], region=_hover_region(state) if hovered else ())
        click = self._record_trigger_action(
            "apply_filter", {"filter": "note_type", "label": XHS_TARGET_FILTER_LABEL}, pointer=plan.evidence,
        )
        await pointer.approach(plan)
        await pointer.settle(plan)
        current = await dom_filter_state(page, XHS_TARGET_FILTER_LABEL)
        moments["before_option_press"] = _filter_moment(current)
        if not _panel_ready(current):
            raise XhsFilterAttemptFailed("filter_panel_closed_before_option")
        await pointer.press(plan)
        return click

    async def _wait_for_filter_entry(self) -> dict[str, Any]:
        polls = max(1, round(XHS_FILTER_ENTRY_WAIT_SECONDS / XHS_FILTER_POLL_SECONDS))
        for poll in range(polls + 1):
            state = await dom_filter_state(self.context_page, XHS_TARGET_FILTER_LABEL)
            if state.get("entry"):
                return state["entry"]
            if poll < polls:
                await self._scan_sleep(XHS_FILTER_POLL_SECONDS)
        raise XhsFilterAttemptFailed("filter_entry_missing")

    async def _close_filter_panel(self) -> None:
        """任何滚动之前收起面板，确认 .filters-wrapper 不可见（#81）。先把光标移出入口与面板的并集（见 `_leave_filter_panel`），
        确认不了才点入口收起，失败重试一次。两种方式都写 `apply_filter panel_close`，`method` 分别为 `pointer_leave`
        与 `entry_press`；从“图文”移回入口再按下会让悬停打开的面板先自行收起、进入口又被悬停打开（开 → 关 → 再开）。
        """
        state = await dom_filter_state(self.context_page, XHS_TARGET_FILTER_LABEL)
        if not state.get("panel_visible"):
            return
        if await self._leave_filter_panel(state):
            return
        logger.info("[XiaoHongShuCrawler.scan] Filter panel stayed open after the pointer left; pressing the entry")
        for _ in (0, 1):
            state = await dom_filter_state(self.context_page, XHS_TARGET_FILTER_LABEL)
            if not state.get("panel_visible"):
                return
            entry = state.get("entry")
            if not entry:
                break
            plan = await self._plan_click(entry)
            self._record_trigger_action(
                "apply_filter", {"filter": "panel_close", "method": "entry_press"}, pointer=plan.evidence,
            )
            await self._perform_click(plan)
            if await self._poll_filter_state(lambda value: not value.get("panel_visible")) is not None:
                return
        await self._record_filter_failure("filter_panel_not_closed")
        raise XhsSearchEntryFailed("filter_panel_not_closed")

    async def _leave_filter_panel(self, state: dict[str, Any]) -> bool:
        """把光标移到 `_leave_area` 内的随机点，不按下：落点分布与轨迹按 HumanPointer 的人类样本（与点击相同，只是
        不静止、不按下，账本的指针记录相应不含 `settle_ms`、`hold_ms`）。之后至多轮询 `XHS_FILTER_LEAVE_POLLS` 次，
        面板收起返回 True；落点区域取不到时不移动、返回 False。"""
        page = self.context_page
        target = _leave_area(self._scroll_area(await dom_scroll_metrics(page)), _hover_region(state))
        if target is None:
            return False
        pointer = pointer_for(page)
        plan = await pointer.plan_click(target)
        evidence = {key: value for key, value in plan.evidence.items() if key not in ("settle_ms", "hold_ms")}
        self._record_trigger_action(
            "apply_filter", {"filter": "panel_close", "method": "pointer_leave"}, pointer=evidence,
        )
        await pointer.approach(plan)
        closed = await self._poll_filter_state(
            lambda value: not value.get("panel_visible"), polls=XHS_FILTER_LEAVE_POLLS,
        )
        return closed is not None

    async def _record_filter_failure(
        self, reason: str, *, attempt: int = 0, moments: Optional[dict[str, dict[str, Any]]] = None,
    ) -> None:
        """把筛选子原因与页面计数写入导航诊断，并在日志留一行（不含地址与入口文字以外的页面文字）。

        给出 `attempt` 时是一次失败的尝试：`outcome` 为 `attempt_<n>:<子原因>`，另带本次尝试各时刻的筛选状态
        `moments`；不给时是停止前的最终诊断，`outcome` 只是子原因。
        """
        page = self.context_page
        error = ""
        try:
            state = await dom_filter_diagnostic(page)
        except Exception as exc:
            state, error = {}, f"filter_diagnostic: {type(exc).__name__}"
        counts = {key: state[key] for key in XHS_FILTER_DIAGNOSTIC_KEYS if key in state}
        extra: dict[str, Any] = {**counts, "label_chains": state.get("label_chains") or []}
        outcome = reason
        if attempt:
            outcome = f"attempt_{attempt}:{reason}"
            extra["moments"] = moments or {}
        await self._record_navigation_diagnostic(page, stage="search_filter", outcome=outcome, error=error, extra=extra)
        summary = ", ".join(f"{key}={value}" for key, value in counts.items())
        if attempt:
            logger.warning(
                f"[XiaoHongShuCrawler.scan] Target filter attempt {attempt} failed: {reason}, {summary}, moments="
                + json.dumps(moments or {}, ensure_ascii=False, separators=(",", ":"))
            )
        else:
            logger.warning(f"[XiaoHongShuCrawler.scan] Search filter failed: reason={reason}, {summary}")

    async def _poll_filter_state(
        self,
        accept: Callable[[dict[str, Any]], bool],
        *,
        moments: Optional[dict[str, dict[str, Any]]] = None,
        polls: int = XHS_FILTER_POLLS,
    ) -> Optional[dict[str, Any]]:
        """按间隔读筛选状态直到满足 `accept`，至多 `polls` 次；给出 `moments` 时记下第 1 次与最后一次读到的状态。"""
        for poll in range(polls):
            await self._scan_sleep(XHS_FILTER_POLL_SECONDS)
            state = await dom_filter_state(self.context_page, XHS_TARGET_FILTER_LABEL)
            if moments is not None:
                moment = _filter_moment(state)
                moments.setdefault("first_poll", moment)
                moments["poll_end"] = moment
            if accept(state):
                return state
        return None

    async def _plan_click(
        self,
        target: dict[str, Any],
        *,
        within: Optional[tuple[float, float]] = None,
        region: tuple[Box, ...] = (),
    ) -> ClickPlan:
        """在主页面上计划一次点击：落点在页面脚本给出的元素框内，从当前光标位置分段接近（给出 `region` 时轨迹限定
        在其中）。调用方把 `plan.evidence` 写进账本动作后再执行（账本动作先于界面动作）。"""
        return await pointer_for(self.context_page).plan_click(Box.from_point(target), within=within, region=region)

    async def _perform_click(self, plan: ClickPlan) -> None:
        await pointer_for(self.context_page).perform_click(plan)

    async def _await_target_page_one(self, *, after_seq: int) -> Optional[SearchSession]:
        await self._require_capture().wait_for(
            SEARCH_RULE,
            after_seq=after_seq,
            predicate=lambda item: (
                isinstance(item, CapturedResponse)
                and search_request_page(item) == 1
                and is_target_search(item.request_json)
            ),
            timeout=XHS_SEARCH_CAPTURE_SECONDS,
        )
        await self._consume_search_captures()
        return self._newest_target_session(after_seq)

    def _newest_target_session(self, after_seq: int) -> Optional[SearchSession]:
        sessions = [
            session
            for session in self._scan.sessions.values()
            if session.is_target and session.started_seq > after_seq and 1 in session.pages
        ]
        return max(sessions, key=lambda session: session.started_seq, default=None)

    def _adopt_target_session(self, session: SearchSession) -> None:
        scan = self._scan
        if scan.tracker is not None and scan.tracker.session is not session:
            scan.tracker.abandon()
        scan.target = session
        scan.tracker = PageTracker(session, scan.accumulator)
        scan.registry = CardRegistry()
        for number in sorted(session.pages):
            scan.registry.register(session.pages[number])
        scan.pending_session = None
        scan.unregistered.clear()
        scan.backfill_attempts.clear()
        scan.observed_index.clear()

    async def _run_initial_behavior_stage(self) -> None:
        """§3.2 第 6 条：行为阶段的滚轮是目标会话第 1 页之后的第一次滚动，之前按不变式做翻页停顿。

        此时行为证据尚未创建：停顿照常执行并实时写账本（含连续性结果），行为证据的事件先暂存，
        行为阶段写出证据后并入。
        """
        await self._ensure_page_pause()
        self._record_trigger_action("scroll", {"phase": "behavior_stage"})
        self._assert_primary_page_alive("initial_behavior")
        evidence = await self._run_human_behavior_on_primary_page(self._scan.keyword)
        if evidence.get("status") != "completed":
            raise RuntimeError("XHS required human behavior stage did not complete")
        merge_pending_behavior_evidence(self._pending_behavior_evidence or {}, ports=self.ports.behavior)
        self._pending_behavior_evidence = None
        self._behavior_stage_done = True

    # 搜索截获 -------------------------------------------------------------------

    async def _consume_search_captures(self) -> None:
        """按到达序号处理已结算的搜索截获与请求失败，再处理非规则端点的验证哨兵。"""
        scan = self._scan
        capture = self._require_capture()
        for item in capture.drain(SEARCH_RULE, after_seq=scan.search_cursor):
            scan.search_cursor = max(scan.search_cursor, item.seq)
            if isinstance(item, CaptureFailure):
                raise XHSRequestFailed("search", item.failure_text)
            verdict = classify_xhs_api_response(item)
            if verdict.kind == "verification":
                await self._wait_for_frontend_verification(
                    required=True,
                    data_arrived=lambda seq=item.seq: capture.latest(
                        SEARCH_RULE, after_seq=seq, predicate=lambda later: later.status == 200,
                    ) is not None,
                )
                raise XhsScanInterrupted("frontend_verification:search")
            raise_for_runtime_verdict(verdict, stage="search")
            if not verdict.ok:
                raise PlatformRuntimeError(
                    f"XHS search response failed: {verdict.detail}", code="search_request_failed",
                )
            self._on_search_captured(item)
            if scan.network_state:
                await self._finish_network_recovery(stage="full_scan", state=scan.network_state)
        hits = capture.take_sentinel_hits()
        if hits:
            # 同一批非规则端点的 461/471 共用一次 30 秒的验证界面检查。
            surfaced = await self._wait_for_frontend_verification(required=False, data_arrived=lambda: False)
            for hit in hits:
                capture.signal(hit, outcome="verification_surfaced" if surfaced else "ignored")
            if surfaced:
                raise XhsScanInterrupted("frontend_verification:sentinel")

    def _on_search_captured(self, captured: CapturedResponse) -> None:
        """写搜索访问；目标会话的页按序登记，has_more=true 的页置“欠一次翻页停顿”。

        已登记页的重复送达（例如前端在网络恢复后重新请求同一页）不再登记，但同样置欠账：门禁 G3 从第 k−1 页
        最近一次送达算起，要求下一次程序滚动之前有翻页停顿（#95）。

        每个响应的全部条目（含非目标会话、非笔记条目）按 id 记下出处，只留在内存中供未登记卡片诊断。
        """
        scan = self._scan
        request = captured.request_json if isinstance(captured.request_json, dict) else {}
        data = captured.json.get("data") if isinstance(captured.json, dict) else None
        data = data if isinstance(data, dict) else {}
        raw_items = data.get("items") if isinstance(data.get("items"), list) else []
        page_no = search_request_page(captured)
        search_id = str(request.get("search_id") or "")
        has_more = data.get("has_more") if isinstance(data.get("has_more"), bool) else None
        items = tuple(
            SearchItem(
                note_id=item["id"],
                xsec_token=item["xsec_token"],
                xsec_source=item["xsec_source"],
                card_type=item["type"],
                position=item["position"],
            )
            for item in search_items_from_response(captured.json)
        )
        signature = search_filter_signature(request)
        is_target = is_target_search(request)
        keyword_matches = request.get("keyword") == scan.keyword
        trigger = self._trigger_before(captured.seq)
        visit_id = self._require_capture().record_visit(
            captured,
            stage="search_results",
            subject_id=None,
            trigger=trigger,
            search_session=search_id,
            filter_signature=signature,
            is_target=is_target,
            keyword_matches_target=keyword_matches,
            search_page=page_no,
            has_more=has_more,
            items=[{"id": item.note_id, "type": item.card_type} for item in items],
        )
        if page_no is None or not search_id:
            raise PlatformRuntimeError("XHS search request lacks page or search_id", code="search_request_failed")
        key = (search_id, signature)
        non_notes: list[str] = []
        for position, raw in enumerate(raw_items):
            if isinstance(raw, dict) and str(raw.get("id") or ""):
                card = raw.get("note_card") if isinstance(raw.get("note_card"), dict) else {}
                scan.response_entries.setdefault(str(raw["id"]), []).append(
                    ResponseEntry(
                        captured.seq, key, is_target, page_no, position,
                        str(raw.get("model_type") or "")[:40], str(card.get("type") or "")[:40],
                    )
                )
                if raw.get("model_type") != "note":
                    non_notes.append(str(raw["id"]))
        session = scan.sessions.get(key)
        if session is None:
            if page_no != 1:
                logger.info(f"[XiaoHongShuCrawler.scan] Ignore page {page_no} of an unseen search session")
                return
            session = SearchSession(search_id, signature, captured.seq, is_target, keyword_matches, trigger)
            scan.sessions[key] = session
            if not scan.entering and scan.target is not None:
                scan.pending_session = session
        if not session.is_target:
            return
        if has_more is True:
            scan.owed_page_seq = max(scan.owed_page_seq, captured.seq)
        if page_no in session.pages:
            return
        if page_no != (max(session.pages) if session.pages else 0) + 1:
            raise PlatformRuntimeError(
                f"XHS target search page {page_no} arrived without its previous page", code="xhs_search_page_gap",
            )
        page = SearchPage(
            session=session,
            page=page_no,
            seq=captured.seq,
            visit_id=visit_id,
            has_more=has_more,
            raw_count=len(raw_items),
            items=items,
            non_note_ids=frozenset(non_notes),
            non_note_count=len(non_notes),
        )
        session.pages[page_no] = page
        if session is scan.target:
            scan.registry.register(page)

    # 扫描一步 -------------------------------------------------------------------

    async def _settle_search_captures(self) -> None:
        scan = self._scan
        capture = self._require_capture()
        await self._consume_search_captures()
        for _ in range(XHS_SEARCH_SETTLE_ROUNDS):
            if capture.latest_pending_seq(SEARCH_RULE) <= scan.search_cursor:
                return
            await capture.wait_for(SEARCH_RULE, after_seq=scan.search_cursor, timeout=XHS_SEARCH_SETTLE_POLL_SECONDS)
            await self._consume_search_captures()

    async def _scan_step(self) -> None:
        scan = self._scan
        await self._settle_search_captures()
        if scan.pending_session is not None:
            await self._reset_search_session(scan.pending_session)
            return
        self._assert_primary_page_alive("full_scan")
        page = self.context_page
        if not is_search_result_url(page.url):
            logger.warning("[XiaoHongShuCrawler.scan] Main page left the search results; re-entering the search")
            scan.needs_entry = True
            scan.entry_navigate = True
            return
        metrics = await dom_scroll_metrics(page)
        self._note_scroll_container(metrics)
        cards = await dom_visible_cards(page)
        if await self._decide_visible_cards(cards, metrics):
            return
        next_page = scan.tracker.next_page
        if self._finish_ready_batches() or scan.tracker.next_page != next_page:
            return  # 耗尽，或已轮到下一页：重新读取可见卡片
        if await self._backfill_missed(cards, metrics):
            return
        if self._at_list_bottom(metrics):
            await self._handle_list_bottom()
            return
        await self._scroll_feed_step(1, phase="scan")

    async def _decide_visible_cards(self, cards: list[dict[str, Any]], metrics: dict[str, Any]) -> bool:
        """按 data-index 升序：已知滑过、视频记决定、当前页的未知图文就地处理（每页先做一次搜索结果停顿）。

        本会话已知的非笔记条目卡片直接跳过：不计未登记、不作候选、不点击，也不参与“越过本页”的判断。
        """
        scan = self._scan
        tracker = scan.tracker
        accumulator = scan.accumulator
        band = self._click_band(metrics)
        registered: list[tuple[dict[str, Any], SearchPage, SearchItem]] = []
        unregistered: set[str] = set()
        pending = tracker.current.backfill_pending if tracker.current is not None else set()
        backfilled: list[int] = []
        for card in cards:
            note_id = str(card.get("note_id") or "")
            if not note_id:
                continue
            entry = scan.registry.get(note_id)
            if entry is None:
                non_note_page = scan.registry.non_note(note_id)
                if non_note_page is None:
                    unregistered.add(note_id)
                else:
                    non_note_page.non_notes_seen.add(note_id)
                continue
            registered.append((card, *entry))
            scan.observed_index[note_id] = int(card.get("data_index", -1))
            self._mark_observed(*entry, card, metrics)
            if note_id in pending:
                pending.discard(note_id)
                backfilled.append(scan.observed_index[note_id])
        if backfilled:
            logger.info(
                f"[XiaoHongShuCrawler.scan] Backfill reached unread search items: page={tracker.current.page}, "
                f"found={len(backfilled)}, index={_index_range(backfilled)}, still_unread={len(pending)}, "
                f"steps={len(tracker.current.backfill_steps)}"
            )
        await self._count_unregistered(unregistered, metrics)
        decided = False
        for card, search_page, item in registered:
            note_id = item.note_id
            current = tracker.current
            if search_page is not current or accumulator.is_known(note_id) or not self._card_clickable(card, band):
                continue
            tracker.begin(current)
            if item.card_type == "video":
                self._require_capture().decision(
                    note_id, "video_skipped_from_search", from_visit=current.visit_id, page_role="primary",
                )
                accumulator.consider(note_id, valid=False)
                decided = True
                continue
            if not current.results_paused:
                await self._guarded_pause("search_results", *XHS_SEARCH_RESULTS_PAUSE_SECONDS)
                current.results_paused = True
            await self._process_note_card(item, current, {**card, "click_band": band})
            return True
        return decided

    async def _count_unregistered(self, unregistered: set[str], metrics: dict[str, Any]) -> None:
        """有 data-note-id 却既不是本会话笔记、也不是已知非笔记条目的卡片，连续 2 次读数仍未登记即停止；
        搜索响应仍在读体时不计。"""
        scan = self._scan
        for note_id in list(scan.unregistered):
            if note_id not in unregistered:
                del scan.unregistered[note_id]
        if not unregistered or self._require_capture().latest_pending_seq(SEARCH_RULE):
            return
        for note_id in unregistered:
            scan.unregistered[note_id] += 1
        if max(scan.unregistered[note_id] for note_id in unregistered) >= XHS_UNREGISTERED_CARD_STEPS:
            await self._record_unregistered_cards(sorted(unregistered), metrics)
            raise PlatformRuntimeError(
                "XHS search card stayed unregistered", code="xhs_search_card_unregistered",
            )

    def _response_origins(self, note_id: str) -> Optional[list[dict[str, Any]]]:
        """条目在本轮搜索响应中的出处：每次出现的截获序号、所在会话的起始序号、是否目标会话 / 当前目标会话、页码、
        页内位置、model_type 与卡片类型（note_card.type），至多 XHS_UNREGISTERED_DIAGNOSTIC_ENTRIES 条；从未出现为 None。"""
        scan = self._scan
        entries = scan.response_entries.get(note_id)
        if not entries:
            return None
        target_key = scan.target.key if scan.target is not None else None
        return [
            {
                "seq": entry.seq,
                "session_seq": session.started_seq if (session := scan.sessions.get(entry.session)) else None,
                "is_target": entry.is_target,
                "current_target": entry.session == target_key,
                "page": entry.page,
                "position": entry.position,
                "model_type": entry.model_type,
                "card_type": entry.card_type,
            }
            for entry in entries[:XHS_UNREGISTERED_DIAGNOSTIC_ENTRIES]
        ]

    async def _card_diagnostics(self, note_ids: list[str]) -> tuple[dict[str, dict[str, Any]], str]:
        """note_id → card_diagnostic 读数（结果列表内的卡片个数、data-index、位置等）；读取失败时为空并给出错误类型。"""
        try:
            cards = await dom_card_diagnostic(self.context_page, note_ids)
        except Exception as exc:
            return {}, f"card_diagnostic: {type(exc).__name__}"
        return {str(card.get("note_id")): card for card in cards}, ""

    async def _record_unregistered_cards(self, note_ids: list[str], metrics: dict[str, Any]) -> None:
        """停止前把每个未登记 ID 的卡片结构与它在本轮搜索响应中的出处（见 `_response_origins`）写入导航诊断，并在
        日志留一行。不含 token、地址查询串或正文。
        """
        page = self.context_page
        note_ids = note_ids[:XHS_UNREGISTERED_DIAGNOSTIC_IDS]
        dom, error = await self._card_diagnostics(note_ids)
        cards = [
            {"note_id": note_id, "dom": dom.get(note_id), "responses": self._response_origins(note_id)}
            for note_id in note_ids
        ]
        await self._record_navigation_diagnostic(
            page, stage="search_scan", outcome="card_unregistered", error=error,
            extra={
                "scroll_container": metrics.get("scroll_container"),
                "scroll_y": metrics.get("scroll_y"),
                "view_rect": metrics.get("view_rect"),
                "cards": cards,
            },
        )

        def summary(card: dict[str, Any]) -> str:
            shape = card["dom"] or {}
            return (
                f"{card['note_id']}(index={shape.get('data_index')}, cover={shape.get('cover_path')}, "
                f"card={shape.get('card')}, seen={_origins_text(card['responses'])})"
            )

        logger.warning(
            "[XiaoHongShuCrawler.scan] Search card stayed unregistered: " + ", ".join(summary(card) for card in cards)
        )

    @staticmethod
    def _click_band(metrics: dict[str, Any]) -> Optional[tuple[float, float]]:
        """可点击带的上下沿（视口纵坐标）：列表可见区顶部加其高度的 10% 到底边上方 8px；没有可见区时为 None。"""
        view = _rect(metrics.get("view_rect"))
        if view is None:
            return None
        top, bottom = view[1], view[3]
        return top + XHS_CLICK_BAND_TOP * (bottom - top), bottom - XHS_CLICK_BAND_BOTTOM_MARGIN_PX

    @staticmethod
    def _card_clickable(card: dict[str, Any], band: Optional[tuple[float, float]]) -> bool:
        """封面中心在可点击带内，且该点命中这张卡片（没有被固定头部、对话栏或弹层遮住）。"""
        cover = card.get("cover")
        if band is None or not isinstance(cover, dict) or card.get("cover_hit") is not True:
            return False
        return band[0] <= float(cover.get("y") or 0) <= band[1]

    def _note_scroll_container(self, metrics: dict[str, Any]) -> None:
        """列表滚动容器类型（document / element）变化时记一行诊断日志，只含几何数值。"""
        kind = str(metrics.get("scroll_container") or "")
        if kind == self._scan.scroll_container:
            return
        self._scan.scroll_container = kind
        view = _rect(metrics.get("view_rect"))
        logger.info(
            f"[XiaoHongShuCrawler.scan] List scroll container: {kind or 'unknown'}, "
            f"view_rect={tuple(round(value) for value in view) if view else None}, "
            f"inner_height={metrics.get('inner_height')}, scroll_height={metrics.get('scroll_height')}"
        )

    def _mark_observed(
        self, search_page: SearchPage, item: SearchItem, card: dict[str, Any], metrics: dict[str, Any],
    ) -> None:
        """列表可见区出现过某页的卡片，即越过了此前各页；一页的卡片全部出现过也算越过。

        每页首次判为越过时记下来源卡片：所在页码、ID、页内位置、data-index、相对列表可见区顶部的纵坐标与当时的滚动位置。
        """
        search_page.observed.add(item.note_id)
        pages = [page for number, page in search_page.session.pages.items() if number < search_page.page]
        if search_page.observed.issuperset(search_page.note_ids):
            pages.append(search_page)
        view = _rect(metrics.get("view_rect"))
        for page in pages:
            if page.passed:
                continue
            page.passed = True
            page.passed_by = {
                "reason": "all_observed" if page is search_page else "later_page",
                "page": search_page.page,
                "note_id": item.note_id,
                "position": item.position,
                "data_index": int(card.get("data_index", -1)),
                "top": round(float(card.get("top") or 0) - (view[1] if view else 0.0)),
                "scroll_y": metrics.get("scroll_y"),
            }

    def _finish_ready_batches(self) -> bool:
        """按序完成已越过且全部决定的页；目标会话末页 has_more=false 完成即来源耗尽。"""
        scan = self._scan
        tracker = scan.tracker
        while (page := tracker.current) is not None and tracker.ready(page):
            tracker.finish(page)
            if page.non_note_count:
                logger.info(
                    f"[XiaoHongShuCrawler.scan] Search page {page.page} finished: non_note_items={page.non_note_count}, "
                    f"non_note_cards_skipped={len(page.non_notes_seen)}"
                )
            if page.has_more is False:
                scan.accumulator.mark_source_exhausted(
                    "has_more_false",
                    source_page=page.page,
                    source_cursor=tracker.session.search_id,
                    source_has_more=False,
                    raw_batch_count=page.raw_count,
                    batch_complete=True,
                    discovery_phase="full_scan",
                    source_visit_id=page.visit_id,
                )
                scan.exhausted = True
                return True
        return False

    async def _backfill_missed(self, cards: list[dict[str, Any]], metrics: dict[str, Any]) -> bool:
        """当前页中已越到列表可见区上方、却仍未决定的卡片：向上滚回让虚拟列表重新渲染它们。

        “上方”指仍可见但封面中心高于可点击带，或已不可见且序号小于最小可见序号；可点击带以下的卡片
        由向下滚动处理。已越过的页里从未读到的未决定项见 `_backfill_unread`。
        """
        scan = self._scan
        current = scan.tracker.current
        if current is None:
            return False
        accumulator = scan.accumulator
        band = self._click_band(metrics)
        band_top = band[0] if band is not None else 0.0
        visible = {str(card.get("note_id") or ""): card for card in cards}
        min_index = min((int(card.get("data_index", -1)) for card in cards), default=-1)
        undecided = [note_id for note_id in current.note_ids if not accumulator.is_known(note_id)]

        def above(note_id: str) -> bool:
            card = visible.get(note_id)
            if card is not None:
                cover = card.get("cover")
                return isinstance(cover, dict) and float(cover.get("y") or 0) < band_top
            return note_id in scan.observed_index and scan.observed_index[note_id] < min_index

        missed = [note_id for note_id in undecided if above(note_id)]
        if missed:
            for note_id in missed:
                scan.backfill_attempts[note_id] += 1
                if scan.backfill_attempts[note_id] > XHS_BACKFILL_ATTEMPTS:
                    raise PlatformRuntimeError(
                        "XHS search card could not be reached again", code="xhs_search_items_unresolved",
                    )
            await self._scroll_feed_step(-1, phase="backfill")
            return True
        unread = [note_id for note_id in undecided if note_id not in scan.observed_index]
        if current.passed and unread:
            return await self._backfill_unread(current, unread, undecided=len(undecided), cards=cards, metrics=metrics)
        return False

    async def _backfill_unread(
        self,
        current: SearchPage,
        unread: list[str],
        *,
        undecided: int,
        cards: list[dict[str, Any]],
        metrics: dict[str, Any],
    ) -> bool:
        """已越过的页里从未读到的未决定项：先向上补漏，补不到才停止（#92）。

        扫描以外的滚动（重触发、行为阶段、关闭浮层后前端恢复位置）可能在读卡片之前就越过一行。补漏用同一种滚轮手势
        在列表区域内向上滚一步（`phase=backfill_unread`，写账本），立即读一次可见卡片记下读数，交回扫描：读到的项照常登记、
        决定。列表已到顶或本页已补漏 XHS_BACKFILL_ATTEMPTS 步仍没读到，写诊断后按 `xhs_search_items_unresolved` 停止；
        这些项不记候选失败，也不跳过。诊断的补漏记录带目标：这些项中页内位置最靠前者的 data-index 下限（见
        `_unread_index_floor`），与各步可见范围对照即知是否已滚到它应在的位置。
        """
        current.backfill_pending.update(unread)
        at_top = int(metrics.get("scroll_y") or 0) <= 0
        if at_top or len(current.backfill_steps) >= XHS_BACKFILL_ATTEMPTS:
            backfill = {
                "attempts": len(current.backfill_steps),
                "limit": XHS_BACKFILL_ATTEMPTS,
                "target_index": self._unread_index_floor(current, unread),
                "at_top": at_top,
                "steps": list(current.backfill_steps),
            }
            await self._record_items_never_rendered(
                current, unread, undecided=undecided, cards=cards, metrics=metrics, backfill=backfill,
            )
            raise PlatformRuntimeError(
                "XHS search items were never rendered", code="xhs_search_items_unresolved",
            )
        after = await self._scroll_feed_step(-1, phase="backfill_unread")
        seen = await dom_visible_cards(self.context_page)
        current.backfill_steps.append(
            {
                "scroll_y": after.get("scroll_y"),
                "visible_index_range": _index_range(int(card.get("data_index", -1)) for card in seen),
                "at_top": int(after.get("scroll_y") or 0) <= 0,
            }
        )
        return True

    def _unread_index_floor(self, current: SearchPage, unread: list[str]) -> int:
        """从未读到项中页内位置最靠前者的 data-index 下限：本页已读到的项在它之前为其序号 +1、在它之后为其序号减去
        页内位置差，更早页已读到的项为其序号 +1，取最大者（至少 0）。页内位置含非笔记条目；没渲染或没有卡片的条目只会
        让实际序号更靠后，所以是下限。跨页重复的项读到的序号可能属于另一页，不参与。"""
        scan = self._scan
        first = min(item.position for item in current.items if item.note_id in unread)
        pages = current.session.pages.values()
        repeated = {note_id for note_id, count in Counter(n for page in pages for n in page.note_ids).items() if count > 1}
        floor = 0
        for page in pages:
            if page.page > current.page:
                continue
            positions = {item.note_id: item.position for item in page.items}
            for note_id in page.observed - repeated:
                index = scan.observed_index.get(note_id)
                if index is None:
                    continue
                position = positions.get(note_id, -1)
                if page is current and position > first:
                    floor = max(floor, index - (position - first))
                else:
                    floor = max(floor, index + 1)
        return floor

    async def _record_items_never_rendered(
        self,
        current: SearchPage,
        unrendered: list[str],
        *,
        undecided: int,
        cards: list[dict[str, Any]],
        metrics: dict[str, Any],
        backfill: dict[str, Any],
    ) -> None:
        """补漏仍没读到、停止前把当前页从未出现在列表中的未决定项写入导航诊断，并在日志留一行。

        每项（至多 XHS_UNRENDERED_DIAGNOSTIC_IDS 个）给出页内位置与卡片类型、目标会话中也含它的其他页、结果列表内
        的卡片读数（card_diagnostic，没有为 nodes=0）与本轮搜索响应中的出处（见 `_response_origins`）。整页给出条目数、
        未决定数、从未出现数、已观察数及其 data-index 范围、已跳过的非笔记卡片数、把本页判为越过的来源（passed_by），
        以及可见卡片的 data-index 范围、列表滚动位置与可见区。`backfill` 为补漏记录：已补漏步数与上限、目标 data-index
        下限、停止时是否到顶，以及每步滚动后的滚动位置、可见卡片 data-index 范围与是否到顶。只含 ID、计数、页码、位置与类型。
        """
        scan = self._scan
        items = {item.note_id: item for item in current.items}
        note_ids = unrendered[:XHS_UNRENDERED_DIAGNOSTIC_IDS]
        dom, error = await self._card_diagnostics(note_ids)
        entries = [
            {
                "note_id": note_id,
                "position": items[note_id].position,
                "card_type": items[note_id].card_type,
                "other_pages": sorted(
                    number for number, page in current.session.pages.items()
                    if page is not current and note_id in page.note_ids
                ),
                "dom": dom.get(note_id),
                "responses": self._response_origins(note_id),
            }
            for note_id in note_ids
        ]
        observed_range = _index_range(
            scan.observed_index[note_id] for note_id in current.observed if note_id in scan.observed_index
        )
        visible_range = _index_range(int(card.get("data_index", -1)) for card in cards)
        await self._record_navigation_diagnostic(
            self.context_page, stage="search_scan", outcome="items_never_rendered", error=error,
            extra={
                "page": current.page,
                "page_items": len(current.items),
                "undecided": undecided,
                "never_rendered": len(unrendered),
                "observed": len(current.observed),
                "observed_index_range": observed_range,
                "non_note_cards_seen": len(current.non_notes_seen),
                "passed_by": current.passed_by,
                "visible_index_range": visible_range,
                "scroll_container": metrics.get("scroll_container"),
                "scroll_y": metrics.get("scroll_y"),
                "scroll_height": metrics.get("scroll_height"),
                "inner_height": metrics.get("inner_height"),
                "view_rect": metrics.get("view_rect"),
                "backfill": backfill,
                "items": entries,
            },
        )
        passed = current.passed_by or {"reason": "none"}
        passed_text = (
            f"{passed['reason']}:p{passed['page']}:index={passed['data_index']}" if "page" in passed else passed["reason"]
        )

        def summary(entry: dict[str, Any]) -> str:
            shape = entry["dom"] or {}
            return (
                f"{entry['note_id']}(pos={entry['position']}, type={entry['card_type']}, nodes={shape.get('nodes')}, "
                f"index={shape.get('data_index')}, other_pages={entry['other_pages']}, "
                f"seen={_origins_text(entry['responses'])})"
            )

        logger.warning(
            "[XiaoHongShuCrawler.scan] Search items were never rendered: "
            f"page={current.page}, items={len(current.items)}, undecided={undecided}, never_rendered={len(unrendered)}, "
            f"observed={len(current.observed)}, observed_index={observed_range}, visible_index={visible_range}, "
            f"passed_by={passed_text}, scroll_y={metrics.get('scroll_y')}/{metrics.get('scroll_height')}, "
            f"backfill={backfill['attempts']}/{backfill['limit']}:target={backfill['target_index']}:"
            f"top={backfill['at_top']}; "
            + ", ".join(summary(entry) for entry in entries)
        )

    @staticmethod
    def _at_list_bottom(metrics: dict[str, Any]) -> bool:
        scroll_y = float(metrics.get("scroll_y") or 0)
        inner_height = float(metrics.get("inner_height") or 0)
        scroll_height = float(metrics.get("scroll_height") or 0)
        return scroll_y + inner_height >= scroll_height - 2

    async def _handle_list_bottom(self) -> None:
        """已到列表底部：末页 has_more=false 时完成剩余页；否则等待新页，停滞则检查可见状态并抖动。"""
        scan = self._scan
        last = scan.target.last_page
        if last is not None and last.has_more is False:
            for page in scan.target.pages.values():
                if not page.passed:
                    page.passed, page.passed_by = True, {"reason": "list_bottom"}
            if self._finish_ready_batches():
                return
            if await self._await_new_search_page():
                return  # 前端可能正在重新搜索，列表已先行替换
            raise PlatformRuntimeError(
                "XHS search reached the end with undecided items", code="xhs_search_items_unresolved",
            )
        if await self._await_new_search_page():
            return
        await self._check_stalled_page_state()
        for _ in range(XHS_STALL_JIGGLES):
            await self._scroll_feed_step(-1, phase="stall")
            await self._scroll_feed_step(1, phase="stall")
            if await self._await_new_search_page():
                return
        raise PlatformRuntimeError("XHS search stopped loading new pages", code="xhs_search_load_stalled")

    async def _await_new_search_page(self) -> bool:
        scan = self._scan
        before = len(scan.target.pages)
        await self._require_capture().wait_for(
            SEARCH_RULE, after_seq=scan.search_cursor, timeout=XHS_SEARCH_LOAD_WAIT_SECONDS,
        )
        await self._consume_search_captures()
        return len(scan.target.pages) > before or scan.pending_session is not None

    async def _check_stalled_page_state(self) -> None:
        state = await self._popup_checkpoint_state(self.context_page)
        self._raise_for_terminal_popup_state(state, reason="search_load_stalled")
        markers = list(state.get("manual_markers") or [])
        if "login_required" in markers:
            raise XHSLoginExpired("search_load_stalled")
        if markers:
            await self._wait_for_frontend_verification(required=False, data_arrived=lambda: False)
            raise XhsScanInterrupted("visible_verification:search_load_stalled")

    async def _reset_search_session(self, session: SearchSession) -> None:
        """前端从第 1 页重新搜索：目标会话直接采用；丢了“图文”筛选则重新点选。都从新会话顶部继续。"""
        scan = self._scan
        scan.pending_session = None
        if not session.is_target:
            scan.entering = True
            try:
                session = await self._apply_target_filter()
                await self._close_filter_panel()
            finally:
                scan.entering = False
        if not session.keyword_matches:
            scan.needs_entry = True
            scan.entry_navigate = True
            return
        logger.info("[XiaoHongShuCrawler.scan] Frontend restarted the search; continuing from the new session top")
        self._adopt_target_session(session)
        scan.needs_top = True

    # 滚动 -----------------------------------------------------------------------

    async def _ensure_page_pause(self) -> None:
        """翻页停顿不变式：欠账（含仍在读体的搜索响应）未被停顿覆盖时，先停顿再滚动。"""
        scan = self._scan
        capture = self._require_capture()
        while True:
            await self._consume_search_captures()
            owed = max(scan.owed_page_seq, capture.latest_pending_seq(SEARCH_RULE))
            if owed <= scan.page_pause_cleared_seq:
                return
            covered = capture.ledger.last_seq
            await self._guarded_pause("search_page", *XHS_SEARCH_PAGE_PAUSE_SECONDS)
            scan.page_pause_cleared_seq = covered

    async def _scroll_feed_step(self, direction: int, *, phase: str) -> dict[str, Any]:
        """在列表区域用触控板手势滚动一步：先满足翻页停顿不变式，光标不在列表区域内时先移进去，写 scroll 动作（带各手势
        的事件数与位移）后再滚动，停 0.4–1.2 秒后核实位移。"""
        await self._ensure_page_pause()
        page = self.context_page
        before = await dom_scroll_metrics(page)
        view = _rect(before.get("view_rect"))
        scroll_y = int(before.get("scroll_y") or 0)
        low, high = load_behavior_profile("xhs_guarded", strict=True).scroll_delta_px
        step = SCAN_RANDOM.randint(int(low), int(high))
        if view is not None:
            step = min(step, max(1, int((view[3] - view[1]) * XHS_SCROLL_VIEWPORT_RATIO)))
        if direction < 0 and scroll_y > 0:
            step = min(step, scroll_y)
        delta = step if direction > 0 else -step
        pointer = pointer_for(page)
        area = self._scroll_area(before)
        if area is not None:
            await pointer.move_into(area)
        plan = pointer.plan_scroll(delta)
        self._record_trigger_action(
            "scroll", {"phase": phase, "delta_y": delta, "scroll_y_before": scroll_y}, pointer=plan.evidence,
        )
        await pointer.perform_scroll(plan)
        await self._scan_sleep(SCAN_RANDOM.uniform(*XHS_SCROLL_SETTLE_SECONDS))
        return await dom_scroll_metrics(page)

    @staticmethod
    def _scroll_area(metrics: dict[str, Any]) -> Optional[Box]:
        """滚动时光标所在的区域：结果列表与列表可见区相交矩形、四周留边，避开 AI 对话布局右侧可独立滚动的对话栏；
        取不到该矩形时为列表可见区内横 30%–70%、纵 35%–75% 的部分。"""
        area = list_area_box(metrics)
        if area is not None:
            return area
        view = _rect(metrics.get("view_rect"))
        if view is not None:
            left, top, right, bottom = view
            return Box(left + 0.3 * (right - left), top + 0.35 * (bottom - top), 0.4 * (right - left), 0.4 * (bottom - top))
        return None

    async def _scroll_to_top(self) -> None:
        for _ in range(XHS_SCROLL_TOP_MAX_STEPS):
            metrics = await dom_scroll_metrics(self.context_page)
            if int(metrics.get("scroll_y") or 0) <= 0:
                return
            await self._scroll_feed_step(-1, phase="to_top")
        logger.warning("[XiaoHongShuCrawler.scan] Could not reach the list top within the step bound")

    async def _retrigger_scroll(self) -> None:
        """验证或网络恢复后重触发：上滚一步（不在顶部时）再下滚一步。"""
        metrics = await dom_scroll_metrics(self.context_page)
        if int(metrics.get("scroll_y") or 0) > 0:
            await self._scroll_feed_step(-1, phase="retrigger")
        await self._scroll_feed_step(1, phase="retrigger")
