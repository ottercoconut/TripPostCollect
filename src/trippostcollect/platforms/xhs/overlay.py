"""单篇处理（设计 §3.4）：点卡片打开浮层，截获 feed 与图片，点击作者名读作者页投影，浮层内互动，关闭浮层。

本篇的图片窗口在第一次点击前打开，覆盖两次详情尝试与一次整篇重做（§3.8.4），离开时关闭。
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any, Optional

from trippostcollect.platforms.xhs.author import XHS_CREATOR_RUNTIME_FINAL_REASONS
from trippostcollect.platforms.xhs.capture import (
    XHS_FEED_RULE,
    classify_xhs_api_response,
    raise_for_runtime_verdict,
)
from trippostcollect.platforms.xhs.errors import (
    PlatformRuntimeError,
    XHSCreatorProfileUnavailable,
    XHSImageDownloadError,
    XHSLoginExpired,
    XHSNetworkRecoveryTimeout,
    XHSRequestFailed,
    XhsNoteRedo,
)
from trippostcollect.platforms.xhs.page_scripts import dom_overlay_state, dom_scroll_metrics, dom_visible_cards
from trippostcollect.platforms.xhs.parser import _normalized_creator_item, _xhs_image_assets
from trippostcollect.platforms.xhs.scan import SearchItem, SearchPage, XhsScanInterrupted
from trippostcollect.records.topic_relevance import topic_relevant_for_web_post
from trippostcollect.runtime.image_retry import is_runtime_blocking_image_error
from trippostcollect.runtime.page_capture import CaptureFailure, CapturedResponse, ImageWindow
from trippostcollect.runtime.visit_ledger import line_seq

logger = logging.getLogger("MediaCrawler")

FEED_RULE = XHS_FEED_RULE.name
XHS_DETAIL_CAPTURE_SECONDS = 20.0
XHS_DETAIL_ATTEMPTS = 2
XHS_OVERLAY_FAIL_STREAK = 3
XHS_OVERLAY_CLOSE_SECONDS = 5.0
XHS_OVERLAY_POLL_SECONDS = 0.25
# 浮层未出现时等 3–6 秒再点一次；浮层出现但没有 feed 时先关闭，等 4–8 秒再开。
XHS_OVERLAY_REOPEN_SECONDS = (3.0, 6.0)
XHS_OVERLAY_RECLOSE_SECONDS = (4.0, 8.0)
XHS_NOTE_REQUIRED_COUNTS = ("liked_count", "collected_count", "comment_count", "share_count")
OVERLAY_RANDOM = random.SystemRandom()


@dataclass
class NoteVisit:
    """一篇笔记本次处理的 feed 详情与访问引用。"""

    note_id: str
    note_card: dict[str, Any]
    trigger: str
    detail_visit: str
    search_visit: Optional[str]
    creator_visit: Optional[str] = None
    image_visits: list[str] = field(default_factory=list)

    @property
    def user_id(self) -> str:
        return str((self.note_card.get("user") or {}).get("user_id") or "")


@dataclass
class _NoteRun:
    """一篇笔记跨尝试共享的状态：唯一图片窗口与本篇全部 click_card 动作（序号, id）。"""

    window: ImageWindow
    clicks: list[tuple[int, str]] = field(default_factory=list)
    overlay_seen: bool = False


def _feed_note_id(item: CapturedResponse | CaptureFailure) -> str:
    request = item.request_json if isinstance(item.request_json, dict) else {}
    return str(request.get("source_note_id") or "")


def _feed_note_card(captured: CapturedResponse) -> tuple[Optional[dict[str, Any]], str]:
    """feed 成功响应里的 note_card 与其笔记 ID；形状不对时 note_card 为 None。"""
    data = (captured.json or {}).get("data") if isinstance(captured.json, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    first = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None
    note_card = first.get("note_card") if first is not None else None
    if not isinstance(note_card, dict):
        return None, ""
    return dict(note_card), str(note_card.get("note_id") or first.get("id") or "")


class XhsOverlayMixin:
    """浮层详情；作为 XiaoHongShuCrawler 的 mixin，由全量扫描对当前页的未知图文调用。"""

    async def _process_note_card(self, item: SearchItem, search_page: SearchPage, card: dict[str, Any]) -> None:
        """处理一篇并作出决定：成功写记录与 consider，候选失败记跳过，运行级事件上抛。

        单篇内的登录失效、验证完成或网络恢复后整篇重做一次；第二次再中断时关闭残留浮层并照常上抛。
        """
        run = _NoteRun(window=self._require_capture().open_image_window("primary"))
        network_state: dict[str, Any] = {}
        try:
            for redo in (False, True):
                try:
                    await self._process_note_once(item, search_page, card, run)
                    if network_state:
                        await self._finish_network_recovery(stage="note_detail", state=network_state)
                    return
                except XHSLoginExpired:
                    if redo:
                        raise
                    if not await self._wait_for_midrun_login_recovery():
                        raise PlatformRuntimeError("XHS login remained expired", code="login_required")
                except XHSRequestFailed as exc:
                    if redo:
                        raise
                    if not await self._pause_for_network_recovery(exc, stage="note_detail", state=network_state):
                        started_at = float(network_state.get("started_at") or self._popup_monotonic())
                        raise XHSNetworkRecoveryTimeout(
                            "note_detail", self._popup_monotonic() - started_at,
                        ) from exc
                except XhsNoteRedo as exc:
                    if redo:
                        await self._close_note_overlay(item.note_id)
                        raise XhsScanInterrupted(f"note_redo_interrupted_twice:{exc.reason}") from exc
                # 恢复已完成：关闭残留浮层，重新找到卡片后整篇重做。
                logger.info(f"[XiaoHongShuCrawler.overlay] Redoing note after recovery: {item.note_id}")
                await self._close_note_overlay(item.note_id)
                card = await self._card_for_redo(item.note_id)
        finally:
            run.window.close()

    async def _card_for_redo(self, note_id: str) -> dict[str, Any]:
        """恢复后重新找到这张卡片；不在可点击带内时交回扫描（本篇仍未决定，扫描会再次处理）。"""
        page = self.context_page
        metrics = await dom_scroll_metrics(page)
        height = float(metrics.get("inner_height") or 0)
        for card in await dom_visible_cards(page):
            if str(card.get("note_id") or "") == note_id and self._card_clickable(card, height):
                return card
        raise XhsScanInterrupted("note_redo_card_not_visible")

    async def _process_note_once(
        self, item: SearchItem, search_page: SearchPage, card: dict[str, Any], run: _NoteRun,
    ) -> None:
        note_id = item.note_id
        accumulator = self._scan.accumulator
        position = {
            "source_page": search_page.page,
            "source_cursor": search_page.session.search_id,
            "discovery_phase": "full_scan",
        }
        await self._check_primary_before_click()
        captured = await self._open_note_overlay(item, card, run)
        if captured is None:
            accumulator.skip_candidate_failure(
                note_id, failure_scope="post", detail="note_detail_not_observed",
                error_code="note_detail_not_observed", attempts=XHS_DETAIL_ATTEMPTS, retryable=True, **position,
            )
            return
        capture = self._require_capture()
        verdict = classify_xhs_api_response(captured)
        if verdict.kind == "verification":
            await self._wait_for_frontend_verification(
                required=True,
                data_arrived=lambda: capture.latest(
                    FEED_RULE, after_seq=captured.seq, predicate=lambda later: _feed_note_id(later) == note_id,
                ) is not None,
            )
            raise XhsNoteRedo("frontend_verification")
        raise_for_runtime_verdict(verdict, stage="note_detail")
        if verdict.kind == "note_not_found" or not verdict.ok:
            # 笔记不存在或异常为决定性候选失败；未知业务码按本次未取到详情、可重试处理。
            await self._close_note_overlay(note_id)
            final = verdict.kind == "note_not_found"
            accumulator.skip_candidate_failure(
                note_id, failure_scope="post", detail="note_detail_unavailable",
                error_code="note_not_found" if final else "detail_request_failed",
                attempts=1, retryable=not final, **position,
            )
            return
        note_card, card_note_id = _feed_note_card(captured)
        if note_card is None or card_note_id != note_id:
            await self._close_note_overlay(note_id)
            accumulator.skip_candidate_failure(
                note_id, failure_scope="post", detail="note_detail_unavailable",
                error_code="detail_parse_failed" if note_card is None else "note_id_mismatch",
                attempts=1, retryable=False, **position,
            )
            return
        trigger = max((click for click in run.clicks if click[0] < captured.seq), default=run.clicks[-1])[1]
        if self.is_video_note(note_card):
            await self._close_note_overlay(note_id)
            capture.decision(note_id, "video_skipped_from_detail", page_role="primary")
            accumulator.consider(note_id, valid=False)
            return
        note = NoteVisit(
            note_id=note_id,
            note_card=note_card,
            trigger=trigger,
            detail_visit=capture.record_visit(captured, stage="note_detail", subject_id=note_id, trigger=trigger),
            search_visit=search_page.visit_id,
        )
        assets = _xhs_image_assets(note_card) if self.settings.ENABLE_GET_MEIDAS else []
        if assets:
            run.window.expect(
                {asset["url"]: asset["source_asset_key"] for asset in assets},
                subject_id=note_id, trigger=trigger, attempt=len(run.clicks),
            )
        await self._wait_overlay(lambda state: bool(state.get("content_visible")), XHS_OVERLAY_CLOSE_SECONDS)
        await self._guarded_pause("note_detail", 4.0, 10.0)
        images = await self._collect_note_images(note_id, run.window, assets) if assets else {}

        creator: dict[str, Any] = {}
        if note.user_id:
            try:
                creator, note.creator_visit = await self._enrich_creator_via_author_tab(note)
            except XHSCreatorProfileUnavailable as exc:
                await self._close_note_overlay(note_id)
                accumulator.skip_candidate_failure(
                    note_id, failure_scope="post",
                    detail=f"creator_profile_failed:{exc.reason}" if exc.reason else "creator_profile_failed",
                    error_code="creator_profile_unavailable", attempts=exc.attempts,
                    retryable=not set(exc.reason.split(",")) & XHS_CREATOR_RUNTIME_FINAL_REASONS, **position,
                )
                return
        note_detail = {
            **note_card,
            "xsec_token": item.xsec_token,
            "xsec_source": item.xsec_source,
            "content_detail_status": "detail_observed",
            "content_detail_source": "note_detail",
        }
        if creator:
            note_detail["creator_profile"] = creator
        valid = self._note_fields_valid(note_id, note_detail)
        if valid:
            await self._maybe_run_post_interaction(note_detail)
        await self._close_note_overlay(note_id)
        if valid and assets:
            try:
                note.image_visits = await self._stage_note_images(note_id, assets, images)
            except XHSImageDownloadError as exc:
                if is_runtime_blocking_image_error(exc.code):
                    raise PlatformRuntimeError(f"XHS image {exc.code}", code=exc.code) from exc
                accumulator.skip_candidate_failure(
                    note_id, failure_scope="image", detail="image_download_failed", error_code=exc.code,
                    attempts=exc.attempts, source_index=exc.source_index,
                    **({"retryable": True} if exc.code == "image_response_not_observed" else {}),
                    **position,
                )
                return
        note_detail["content_visit_refs"] = self._build_content_visit_refs(note)
        await self.update_xhs_note(note_detail)
        target_valid = valid and topic_relevant_for_web_post("xhs", note_detail, fallback_keyword=self._scan.keyword)
        accumulator.consider(note_id, valid=target_valid)

    async def _check_primary_before_click(self) -> None:
        """§3.4 第 1 条：主页面存活；终态上抛，登录界面转轮中恢复，验证界面先被动等待完成。"""
        self._assert_primary_page_alive("note_overlay")
        state = await self._popup_checkpoint_state(self.context_page)
        self._raise_for_terminal_popup_state(state, reason="note_overlay")
        markers = set(state.get("manual_markers") or [])
        if markers == {"login_required"}:
            raise XHSLoginExpired("note_overlay")
        if markers:
            await self._wait_for_frontend_verification(required=False, data_arrived=lambda: False)

    async def _open_note_overlay(
        self, item: SearchItem, card: dict[str, Any], run: _NoteRun,
    ) -> Optional[CapturedResponse]:
        """点封面打开浮层并等本篇的 feed；最多两次尝试，都没有 feed 时返回 None（§3.4 第 3、4 条）。"""
        capture = self._require_capture()
        overlay_seen = False
        for attempt in range(1, XHS_DETAIL_ATTEMPTS + 1):
            action = capture.action(
                "click_card", page_role="primary",
                target={"note_id": item.note_id, "data_index": int(card.get("data_index", -1))},
            )
            run.clicks.append((line_seq(action), action))
            await self._click_point(card["cover"])
            captured = await capture.wait_for(
                FEED_RULE,
                after_seq=line_seq(action),
                predicate=lambda found: _feed_note_id(found) == item.note_id,
                timeout=XHS_DETAIL_CAPTURE_SECONDS,
            )
            if isinstance(captured, CaptureFailure):
                failure = XHSRequestFailed("note_detail", captured.failure_text)
                if failure.recoverable:
                    raise failure
                captured = None  # 非网络类失败（如请求被取消）按本次未取到 feed
            state = await dom_overlay_state(self.context_page)
            overlay_seen = overlay_seen or bool(state.get("visible"))
            if captured is not None:
                self._overlay_fail_streak = 0
                return captured
            if attempt == XHS_DETAIL_ATTEMPTS:
                break
            if state.get("visible"):
                await self._close_note_overlay(item.note_id)
                await self._scan_sleep(OVERLAY_RANDOM.uniform(*XHS_OVERLAY_RECLOSE_SECONDS))
            else:
                await self._scan_sleep(OVERLAY_RANDOM.uniform(*XHS_OVERLAY_REOPEN_SECONDS))
        await self._close_note_overlay(item.note_id)
        if overlay_seen:
            self._overlay_fail_streak = 0
        else:
            self._overlay_fail_streak = getattr(self, "_overlay_fail_streak", 0) + 1
            if self._overlay_fail_streak >= XHS_OVERLAY_FAIL_STREAK:
                raise PlatformRuntimeError("XHS note overlay stopped opening", code="xhs_overlay_not_opening")
        return None

    async def _wait_overlay(self, check: Any, timeout: float) -> dict[str, Any]:
        """轮询浮层状态直到 check 为真或超时，返回最后一次读数。"""
        polls = max(1, int(timeout / XHS_OVERLAY_POLL_SECONDS))
        state: dict[str, Any] = {}
        for attempt in range(polls):
            state = await dom_overlay_state(self.context_page)
            if check(state):
                return state
            if attempt + 1 < polls:
                await self._scan_sleep(XHS_OVERLAY_POLL_SECONDS)
        return state

    async def _close_note_overlay(self, note_id: str) -> None:
        """写 close_overlay 后点关闭按钮（不按 Esc），5 秒内确认浮层消失；重试一次仍失败为运行级。"""
        capture = self._require_capture()
        for _attempt in range(2):
            state = await dom_overlay_state(self.context_page)
            if not state.get("visible"):
                return
            point = state.get("close")
            if point is None:
                continue
            capture.action("close_overlay", page_role="primary", target={"note_id": note_id})
            await self._click_point(point)
            closed = await self._wait_overlay(lambda current: not current.get("visible"), XHS_OVERLAY_CLOSE_SECONDS)
            if not closed.get("visible"):
                return
        raise PlatformRuntimeError("XHS note overlay did not close", code="xhs_overlay_close_failed")

    def _note_fields_valid(self, note_id: str, note_detail: dict[str, Any]) -> bool:
        """有效条件与原详情路径一致：正文、时间、作者、图片、粉丝观察与四个互动数。"""
        user = note_detail.get("user") or {}
        interact = note_detail.get("interact_info") or {}
        creator = note_detail.get("creator_profile") or {}
        creator_item = (
            _normalized_creator_item(str(user.get("user_id") or ""), creator, current_timestamp=self.ports.current_timestamp)
            if creator
            else {}
        )
        return bool(
            note_id
            and note_detail.get("desc")
            and note_detail.get("time")
            and user.get("user_id")
            and user.get("nickname")
            and note_detail.get("image_list")
            and any(creator_item.get(key) not in (None, "") for key in ("fans_count", "fans"))
            and all(interact.get(key) not in (None, "") for key in XHS_NOTE_REQUIRED_COUNTS)
        )

    def _build_content_visit_refs(self, note: NoteVisit) -> dict[str, Any]:
        """记录的来源引用（§5.3）；图片可以引用本篇早先尝试截获的访问。"""
        return {
            "ledger_id": self._require_capture().ledger.ledger_id,
            "search_results": note.search_visit,
            "note_detail": note.detail_visit,
            "creator_profile": note.creator_visit,
            "images": list(note.image_visits),
        }
