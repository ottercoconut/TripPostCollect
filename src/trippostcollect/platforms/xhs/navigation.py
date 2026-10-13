# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/core.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

# TripPostCollect：T09 迁入根平台；来源 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303，许可见 resources/licenses/MediaCrawler-LICENSE。

"""主页面导航：带期限的 goto、可见壳等待、首页输入搜索与导航诊断写出。"""

import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

from playwright.async_api import (
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from trippostcollect.platforms.xhs.errors import (
    XHSNetworkRecoveryTimeout,
    is_recoverable_xhs_navigation_failure,
)
from trippostcollect.platforms.xhs.page_scripts import (
    dom_render_diagnostic,
    dom_search_box,
    dom_search_layout_visible,
)
from trippostcollect.runtime.page_capture import CapturedResponse
from trippostcollect.runtime.visit_ledger import line_seq

logger = logging.getLogger("MediaCrawler")

# 搜索结果页路径（V16：回车后为 /search_result_ai），只按路径精确比较，不用子串。
XHS_SEARCH_RESULT_PATHS = frozenset({"/search_result_ai", "/search_result"})
XHS_SEARCH_CAPTURE_SECONDS = 30.0
XHS_TYPE_KEY_SECONDS = (0.12, 0.26)
XHS_BEFORE_ENTER_SECONDS = (0.5, 1.5)
INPUT_RANDOM = random.SystemRandom()


def is_search_result_url(url: str) -> bool:
    """封面链接 /search_result/<note_id> 与浮层打开时的 /explore/<id> 都不是结果页。"""
    path = urlparse(str(url or "")).path
    if len(path) > 1:
        path = path.rstrip("/")
    return path in XHS_SEARCH_RESULT_PATHS


def search_request_page(item: object) -> int | None:
    request = getattr(item, "request_json", None)
    try:
        return int(request.get("page")) if isinstance(request, dict) else None
    except (TypeError, ValueError):
        return None


class XhsSearchEntryFailed(RuntimeError):
    """首页输入搜索或目标筛选失败；只允许一次受控恢复，仍失败即按 code 停止。"""

    def __init__(self, code: str):
        super().__init__(f"xhs_navigation_recovery_failed:{code}")
        self.code = code


class XhsNavigationMixin:
    """主页面导航；作为 XiaoHongShuCrawler 的 mixin，方法体逐字迁入。"""

    async def _goto_with_deadline(
        self,
        page: Page,
        url: str,
        *,
        stage: str,
        wait_until: str = "commit",
    ) -> None:
        self._install_navigation_observers(page)
        timeout_seconds = self.inputs.navigation_deadline_seconds()
        timeout_seconds = max(5.0, timeout_seconds)
        recovery_stage = f"navigation:{stage}"
        recovery_state: Dict[str, object] = {}
        while True:
            response_status: Optional[int] = None
            try:
                async with asyncio.timeout(timeout_seconds + 2.0):
                    response = await page.goto(
                        url,
                        wait_until=wait_until,
                        timeout=int(timeout_seconds * 1000),
                    )
                    response_status = (
                        response.status if response is not None else None
                    )
            except (PlaywrightTimeoutError, TimeoutError) as exc:
                await self._record_navigation_diagnostic(
                    page,
                    stage=stage,
                    outcome="navigation_network_error",
                    error=f"{type(exc).__name__}: {exc}",
                )
                if await self._pause_for_network_recovery(
                    exc,
                    stage=recovery_stage,
                    state=recovery_state,
                    page=page,
                ):
                    continue
                started_at = float(
                    recovery_state.get("started_at") or self._popup_monotonic()
                )
                raise XHSNetworkRecoveryTimeout(
                    recovery_stage,
                    self._popup_monotonic() - started_at,
                ) from exc
            except PlaywrightError as exc:
                if not is_recoverable_xhs_navigation_failure(exc):
                    await self._abort_network_recovery(
                        stage=recovery_stage,
                        state=recovery_state,
                        error=exc,
                        page=page,
                    )
                    raise
                await self._record_navigation_diagnostic(
                    page,
                    stage=stage,
                    outcome="navigation_network_error",
                    error=f"{type(exc).__name__}: {exc}",
                )
                if await self._pause_for_network_recovery(
                    exc,
                    stage=recovery_stage,
                    state=recovery_state,
                    page=page,
                ):
                    continue
                started_at = float(
                    recovery_state.get("started_at") or self._popup_monotonic()
                )
                raise XHSNetworkRecoveryTimeout(
                    recovery_stage,
                    self._popup_monotonic() - started_at,
                ) from exc

            await self._finish_network_recovery(
                stage=recovery_stage,
                state=recovery_state,
                page=page,
            )
            await self._record_navigation_diagnostic(
                page,
                stage=stage,
                outcome="navigation_committed",
                response_status=response_status,
            )
            return

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _install_navigation_observers(self, page: Page) -> None:
        page_id = id(page)
        if page_id in self._navigation_observed_pages:
            return
        on = getattr(page, "on", None)
        if not callable(on):
            return
        self._navigation_observed_pages.add(page_id)

        def record_page_error(error: object) -> None:
            self._navigation_page_errors.append(
                {
                    "at": self._utc_now(),
                    "message": str(error)[:500],
                }
            )
            self._navigation_page_errors = self._navigation_page_errors[-8:]

        def record_request_failure(request: object) -> None:
            failure = getattr(request, "failure", None)
            request_url = str(getattr(request, "url", "") or "")
            if request_url.startswith("chrome-extension://invalid"):
                return
            self._navigation_request_failures.append(
                {
                    "at": self._utc_now(),
                    "resource_type": str(getattr(request, "resource_type", "") or "")[:80],
                    "url": request_url[:500],
                    "failure": str(failure or "")[:300],
                }
            )
            self._navigation_request_failures = self._navigation_request_failures[-12:]

        on("pageerror", record_page_error)
        on("requestfailed", record_request_failure)

    def _navigation_diagnostics_path(self) -> Optional[Path]:
        evidence_path = os.environ.get(
            "TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE",
            "",
        ).strip()
        if not evidence_path:
            return None
        path = Path(evidence_path).expanduser()
        return path.with_name(f"{path.stem}.navigation.json")

    def _write_navigation_diagnostics(self) -> None:
        path = self._navigation_diagnostics_path()
        if path is None:
            return
        payload = {
            "schema_version": 1,
            "platform": "xhs",
            "updated_at": self._utc_now(),
            "events": self._navigation_diagnostics[-30:],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError as exc:
            logger.warning(
                "[XiaoHongShuCrawler] Could not persist navigation diagnostics: "
                f"{type(exc).__name__}: {exc}"
            )

    async def _page_render_diagnostic(self, page: Page) -> Dict:
        diagnostic: Dict = {
            "url": str(getattr(page, "url", "") or ""),
            "page_errors": list(self._navigation_page_errors[-5:]),
            "request_failures": list(self._navigation_request_failures[-8:]),
        }
        try:
            diagnostic.update(await dom_render_diagnostic(page))
        except Exception as exc:
            diagnostic["inspection_error"] = f"{type(exc).__name__}: {exc}"
        return diagnostic

    async def _record_navigation_diagnostic(
        self,
        page: Page,
        *,
        stage: str,
        outcome: str,
        response_status: Optional[int] = None,
        error: str = "",
    ) -> Dict:
        diagnostic = await self._page_render_diagnostic(page)
        diagnostic.update(
            {
                "at": self._utc_now(),
                "stage": stage,
                "outcome": outcome,
                "response_status": response_status,
                "error": error[:500],
            }
        )
        self._navigation_diagnostics.append(diagnostic)
        self._navigation_diagnostics = self._navigation_diagnostics[-30:]
        self._write_navigation_diagnostics()
        return diagnostic

    async def _wait_for_visible_page_shell(
        self,
        page: Page,
        *,
        stage: str,
        timeout_seconds: float,
    ) -> bool:
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        last_diagnostic: Dict = {}
        while True:
            last_diagnostic = await self._page_render_diagnostic(page)
            body_text_length = int(last_diagnostic.get("body_text_length") or 0)
            body_child_count = int(last_diagnostic.get("body_child_element_count") or 0)
            ready_state = str(last_diagnostic.get("ready_state") or "")
            if (
                ready_state in {"interactive", "complete"}
                and body_text_length > 0
                and body_child_count > 0
            ):
                await self._record_navigation_diagnostic(
                    page,
                    stage=stage,
                    outcome="visible_shell_ready",
                )
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                diagnostic = await self._record_navigation_diagnostic(
                    page,
                    stage=stage,
                    outcome="visible_shell_timeout",
                    error="visible_page_shell_not_rendered",
                )
                logger.warning(
                    "[XiaoHongShuCrawler] Visible page shell did not render: "
                    f"stage={stage}, ready_state={diagnostic.get('ready_state')}, "
                    f"body_text_length={diagnostic.get('body_text_length')}, "
                    f"body_child_element_count={diagnostic.get('body_child_element_count')}, "
                    f"url={diagnostic.get('url')}"
                )
                return False
            await asyncio.sleep(min(2.0, remaining))

    async def _enter_search_from_home(self, keyword: str, *, navigate: bool = False) -> int:
        """首页搜索框逐字输入关键词并回车，等到第 1 页搜索被截获且结果页可见；返回回车动作的序号。

        不直接导航到搜索结果页。前端的联想、onebox、筛选配套请求不截获。`navigate` 为真（受控恢复、
        会话重置）时总是重新打开首页，保证搜索框为空。
        """
        page = self.context_page
        capture = self._require_capture()
        if navigate or (urlparse(str(page.url or "")).path.rstrip("/") or "/") != "/explore":
            self._record_trigger_action("navigate", {"route": "/explore"})
            await self._goto_with_deadline(page, self.explore_url, stage="search_entry_explore")
        if not await self._wait_for_visible_page_shell(
            page,
            stage="search_entry_explore",
            timeout_seconds=self.inputs.recovery_shell_timeout_seconds(),
        ):
            raise XhsSearchEntryFailed("explore_not_rendered")
        box = await dom_search_box(page)
        if not box.get("visible"):
            raise XhsSearchEntryFailed("explore_not_rendered")
        x, y = float(box["x"]), float(box["y"])
        await page.mouse.move(x, y, steps=INPUT_RANDOM.randint(6, 14))
        await page.mouse.click(x, y, delay=INPUT_RANDOM.randint(80, 220))
        self._record_trigger_action("type_search", {"chars": len(keyword)})
        for character in keyword:
            await page.keyboard.type(character)
            await self._scan_sleep(INPUT_RANDOM.uniform(*XHS_TYPE_KEY_SECONDS))
        await self._scan_sleep(INPUT_RANDOM.uniform(*XHS_BEFORE_ENTER_SECONDS))
        enter = self._record_trigger_action("press_enter", {})
        await page.keyboard.press("Enter")
        captured = await capture.wait_for(
            "xhs.search_notes",
            after_seq=line_seq(enter),
            predicate=lambda item: isinstance(item, CapturedResponse) and search_request_page(item) == 1,
            timeout=XHS_SEARCH_CAPTURE_SECONDS,
        )
        if captured is None or not await self._search_layout_ready(page):
            await self._record_navigation_diagnostic(
                page, stage="search_entry", outcome="search_not_rendered",
            )
            raise XhsSearchEntryFailed("search_not_rendered")
        return line_seq(enter)

    def _record_trigger_action(self, kind: str, target: Dict) -> str:
        """主页面上可能引发搜索请求的动作：写账本并记下序号，供搜索访问认定 trigger。"""
        action_id = self._require_capture().action(kind, page_role="primary", target=target)
        self._trigger_actions.append((line_seq(action_id), action_id))
        del self._trigger_actions[:-64]
        return action_id

    def _trigger_before(self, seq: int) -> Optional[str]:
        for action_seq, action_id in reversed(self._trigger_actions):
            if action_seq < seq:
                return action_id
        return None

    async def _search_layout_ready(self, page: Page) -> bool:
        """回车后主页面须是搜索结果页（只比路径）且 .search-layout 可见；在搜索页壳超时内轮询。"""
        polls = max(1, int(self.inputs.search_shell_timeout_seconds() / 0.5))
        for attempt in range(polls):
            if is_search_result_url(page.url) and await dom_search_layout_visible(page):
                return True
            if attempt + 1 < polls:
                await self._scan_sleep(0.5)
        return False
