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

"""主页面导航：带期限的 goto、可见壳等待、/explore 回退与导航诊断写出。"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import parse_qsl, quote, urlparse

from playwright.async_api import (
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from trippostcollect.platforms.xhs.errors import (
    XHSNetworkRecoveryTimeout,
    is_recoverable_xhs_navigation_failure,
)

logger = logging.getLogger("MediaCrawler")


class XhsNavigationMixin:
    """主页面导航；作为 XiaoHongShuCrawler 的 mixin，方法体逐字迁入。"""

    @staticmethod
    def _navigation_target_matches(target_url: str, current_url: str) -> bool:
        def normalized(url: str) -> tuple[str, str, str, tuple[tuple[str, str], ...]]:
            parsed = urlparse(str(url or ""))
            return (
                parsed.scheme.casefold(),
                parsed.netloc.casefold(),
                parsed.path.rstrip("/") or "/",
                tuple(sorted(parse_qsl(parsed.query, keep_blank_values=True))),
            )

        return bool(current_url) and normalized(target_url) == normalized(current_url)

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
                try:
                    current_url = page.url or ""
                except Exception:
                    current_url = ""
                if (
                    "/search_result" in url
                    and self._navigation_target_matches(url, current_url)
                ):
                    await self._abort_network_recovery(
                        stage=recovery_stage,
                        state=recovery_state,
                        error=exc,
                        page=page,
                        outcome="network_recovery_deferred_to_visible_readiness",
                    )
                    await self._record_navigation_diagnostic(
                        page,
                        stage=stage,
                        outcome="commit_timeout_deferred",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    logger.warning(
                        "[XiaoHongShuCrawler] Navigation event timed out after URL "
                        "commit; continuing with visible readiness gate: "
                        f"stage={stage}, url={current_url}"
                    )
                    return
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
            async with asyncio.timeout(5.0):
                observed = await page.evaluate(
                    """() => ({
                        ready_state: document.readyState || '',
                        title: (document.title || '').slice(0, 160),
                        dom_length: document.documentElement
                            ? document.documentElement.outerHTML.length
                            : 0,
                        body_text_length: document.body
                            ? (document.body.innerText || '').trim().length
                            : 0,
                        body_child_element_count: document.body
                            ? document.body.childElementCount
                            : 0,
                        visibility_state: document.visibilityState || '',
                    })"""
                )
            if isinstance(observed, dict):
                diagnostic.update(observed)
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

    async def _open_behavior_search_page(self, keyword: str) -> None:
        search_url = f"{self.index_url}/search_result?keyword={quote(keyword)}"
        await self._goto_with_deadline(
            self.context_page,
            search_url,
            stage="behavior_search",
        )
        search_shell_timeout = self.inputs.search_shell_timeout_seconds()
        if await self._wait_for_visible_page_shell(
            self.context_page,
            stage="behavior_search",
            timeout_seconds=search_shell_timeout,
        ):
            return

        await self._record_navigation_diagnostic(
            self.context_page,
            stage="behavior_search_recovery",
            outcome="explore_fallback_started",
        )
        await self._goto_with_deadline(
            self.context_page,
            self.explore_url,
            stage="behavior_search_recovery_explore",
        )
        recovery_timeout = self.inputs.recovery_shell_timeout_seconds()
        if not await self._wait_for_visible_page_shell(
            self.context_page,
            stage="behavior_search_recovery_explore",
            timeout_seconds=recovery_timeout,
        ):
            raise RuntimeError("xhs_navigation_recovery_failed:explore_not_rendered")
        await self._goto_with_deadline(
            self.context_page,
            search_url,
            stage="behavior_search_recovery_search",
        )
        if not await self._wait_for_visible_page_shell(
            self.context_page,
            stage="behavior_search_recovery_search",
            timeout_seconds=recovery_timeout,
        ):
            raise RuntimeError("xhs_navigation_recovery_failed:search_not_rendered")
        await self._record_navigation_diagnostic(
            self.context_page,
            stage="behavior_search_recovery",
            outcome="explore_fallback_completed",
        )
