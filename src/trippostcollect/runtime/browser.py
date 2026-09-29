# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/cdp_browser.py
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


# TripPostCollect：迁入 CDP 生命周期，配置快照与浏览器参数显式注入。
"""CDP 浏览器生命周期与进程所有权。

保留环境变量读取的原位与时点，T09 随 XHS 迁移改为注入：
_clean_session_restore_tabs 读取 TRIPPOSTCOLLECT_CLEAN_BROWSER_TABS；
_launch_browser 读取 TRIPPOSTCOLLECT_XHS_PROFILE_DIR 和
TRIPPOSTCOLLECT_SHARE_CDP_PROFILE。PROFILE_BASE_DIR 保留 fork 原位置，T14 再迁。
"""

import asyncio
import atexit
import logging
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
import os
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from playwright.async_api import Browser, BrowserContext, Playwright

from trippostcollect.core import resources
from trippostcollect.core.paths import MEDIACRAWLER_DIR
from trippostcollect.runtime.browser_launcher import BrowserLauncher


PROFILE_BASE_DIR = MEDIACRAWLER_DIR / "browser_data"
logger = logging.getLogger("MediaCrawler")


@dataclass(frozen=True)
class CDPBrowserSettings:
    """调用方完成 CLI 配置后，在构造 manager 时冻结的浏览器配置。"""

    PLATFORM: str
    CDP_CONNECT_EXISTING: bool
    CDP_DEBUG_PORT: int
    BROWSER_LAUNCH_TIMEOUT: int
    CUSTOM_BROWSER_PATH: str
    SAVE_LOGIN_STATE: bool
    USER_DATA_DIR: str
    AUTO_CLOSE_BROWSER: bool


class CDPBrowserLifecycleError(RuntimeError):
    """The one browser session disappeared outside a planned cleanup."""

    def __init__(self, event: Dict[str, Any], *, stage: str):
        self.event = dict(event)
        self.stage = stage
        code = str(event.get("code") or "xhs_cdp_lifecycle_failure")
        details = [f"stage={stage}"]
        for key in ("pid", "returncode", "debug_port", "detail"):
            value = event.get(key)
            if value is not None and value != "":
                details.append(f"{key}={value}")
        super().__init__(f"{code}:" + ":".join(details))


class CDPBrowserManager:
    """
    CDP browser manager, responsible for launching and managing browsers connected via CDP
    """

    def __init__(self, settings: CDPBrowserSettings, *, project_browser_args: Callable[[], list[str]]):
        self.settings = settings
        self.launcher = BrowserLauncher(project_browser_args=project_browser_args)
        self.browser: Optional[Browser] = None
        self.browser_context: Optional[BrowserContext] = None
        self.debug_port: Optional[int] = None
        self._cleanup_registered = False
        self._launch_started = False
        self._connection_established = False
        self._owns_browser_process = False
        self._planned_cleanup_reason = ""
        self._cleanup_in_progress = False
        self._cleanup_complete = False
        self._observed_browser: Optional[Browser] = None
        self._observed_browser_context: Optional[BrowserContext] = None
        self._unexpected_lifecycle_event: Optional[Dict[str, Any]] = None
        self.last_cleanup_result: Optional[Dict[str, Any]] = None
        self._state_lock = threading.RLock()

    def mark_planned_cleanup(self, reason: str) -> None:
        normalized = str(reason or "requested")[:160]
        with self._state_lock:
            if not self._planned_cleanup_reason:
                self._planned_cleanup_reason = normalized

    def _planned_close_reason(self) -> str:
        with self._state_lock:
            reason = self._planned_cleanup_reason
        if reason:
            return reason
        if self.launcher.cleanup_requested:
            return "launcher_cleanup"
        return ""

    def lifecycle_snapshot(self) -> Dict[str, Any]:
        with self._state_lock:
            unexpected = dict(self._unexpected_lifecycle_event or {})
            cleanup = dict(self.last_cleanup_result or {})
            planned_reason = self._planned_cleanup_reason
            connection_established = self._connection_established
        return {
            "planned_cleanup_reason": planned_reason,
            "connection_established": connection_established,
            "unexpected": unexpected,
            "cleanup": cleanup,
            "process": self.launcher.process_status(reason="lifecycle_snapshot"),
        }

    def _capture_lifecycle_event(self, kind: str, *, detail: str = "") -> None:
        planned_reason = self._planned_close_reason()
        if planned_reason:
            logger.info(
                "[CDPBrowserManager] Planned browser lifecycle event; "
                f"kind={kind}, reason={planned_reason}"
            )
            return

        process = self.launcher.process_status(reason=kind)
        if kind == "browser_process_exited" or (
            process.get("present") and not process.get("running")
        ):
            code = "xhs_browser_process_exited"
        elif kind == "browser_disconnected":
            code = "xhs_cdp_disconnected_unexpected"
        elif kind == "context_closed":
            code = "xhs_browser_context_closed_unexpected"
        else:
            code = "xhs_cdp_lifecycle_failure"
        event = {
            "code": code,
            "kind": kind,
            "pid": process.get("pid"),
            "returncode": process.get("returncode"),
            "debug_port": self.debug_port,
            "detail": str(detail or "")[:300],
            "observed_at": time.time(),
        }
        with self._state_lock:
            if self._planned_cleanup_reason or self.launcher.cleanup_requested:
                return
            if self._unexpected_lifecycle_event is None:
                self._unexpected_lifecycle_event = event
            else:
                event = dict(self._unexpected_lifecycle_event)
        logger.error(
            "[CDPBrowserManager] Unexpected browser lifecycle event: "
            + str(CDPBrowserLifecycleError(event, stage="event"))
        )

    def _observe_browser(self, browser: Browser) -> None:
        with self._state_lock:
            if self._observed_browser is browser:
                return
            self._observed_browser = browser
        on = getattr(browser, "on", None)
        if not callable(on):
            return
        try:
            on(
                "disconnected",
                lambda *_args: self._capture_lifecycle_event(
                    "browser_disconnected"
                ),
            )
        except Exception as exc:
            logger.warning(
                "[CDPBrowserManager] Could not install browser disconnect observer: "
                f"{type(exc).__name__}: {exc}"
            )

    def _observe_browser_context(self, context: BrowserContext) -> None:
        with self._state_lock:
            if self._observed_browser_context is context:
                return
            self._observed_browser_context = context
        on = getattr(context, "on", None)
        if not callable(on):
            return
        try:
            on(
                "close",
                lambda *_args: self._capture_lifecycle_event("context_closed"),
            )
        except Exception as exc:
            logger.warning(
                "[CDPBrowserManager] Could not install context close observer: "
                f"{type(exc).__name__}: {exc}"
            )

    def assert_alive(self, stage: str) -> None:
        """Fail with stable evidence if the current CDP session disappeared."""
        with self._state_lock:
            unexpected = dict(self._unexpected_lifecycle_event or {})
            established = self._connection_established
            owns_process = self._owns_browser_process
        if unexpected:
            raise CDPBrowserLifecycleError(unexpected, stage=stage)

        planned_reason = self._planned_close_reason()
        if planned_reason:
            raise RuntimeError(
                "xhs_cdp_session_closing:"
                f"stage={stage}:reason={planned_reason}"
            )

        process = self.launcher.process_status(reason=f"assert_alive:{stage}")
        if owns_process and (
            not process.get("present") or not process.get("running")
        ):
            self._capture_lifecycle_event("browser_process_exited")
        elif established:
            browser = self.browser
            if browser is None:
                self._capture_lifecycle_event(
                    "browser_disconnected",
                    detail="browser reference missing",
                )
            else:
                try:
                    connected = bool(browser.is_connected())
                except Exception as exc:
                    connected = False
                    self._capture_lifecycle_event(
                        "browser_disconnected",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                if not connected:
                    self._capture_lifecycle_event("browser_disconnected")

            context = self.browser_context
            if context is None:
                self._capture_lifecycle_event(
                    "context_closed",
                    detail="browser context reference missing",
                )
            else:
                try:
                    context.pages
                except Exception as exc:
                    self._capture_lifecycle_event(
                        "context_closed",
                        detail=f"{type(exc).__name__}: {exc}",
                    )

        with self._state_lock:
            unexpected = dict(self._unexpected_lifecycle_event or {})
        if unexpected:
            raise CDPBrowserLifecycleError(unexpected, stage=stage)

    def _register_cleanup_handlers(self):
        """
        Register cleanup handlers to ensure browser process cleanup on program exit
        """
        if self._cleanup_registered:
            return

        def sync_cleanup():
            """Synchronous cleanup function for atexit"""
            if self.launcher and self.launcher.browser_process:
                self.mark_planned_cleanup("atexit")
                logger.info("[CDPBrowserManager] atexit: Cleaning up browser process")
                self.launcher.cleanup(reason="atexit")

        # Register atexit cleanup
        atexit.register(sync_cleanup)

        # Register signal handlers (only when no custom handlers exist, to avoid overriding main entry signal handling logic)
        prev_sigint = signal.getsignal(signal.SIGINT)
        prev_sigterm = signal.getsignal(signal.SIGTERM)

        def signal_handler(signum, frame):
            """Signal handler"""
            logger.info(f"[CDPBrowserManager] Received signal {signum}, cleaning up browser process")
            if self.launcher and self.launcher.browser_process:
                self.mark_planned_cleanup(f"signal_{signum}")
                self.launcher.cleanup(reason=f"signal_{signum}")

            if signum == signal.SIGINT:
                if prev_sigint == signal.default_int_handler:
                    return prev_sigint(signum, frame)
                raise KeyboardInterrupt

            raise SystemExit(0)

        install_sigint = prev_sigint in (signal.default_int_handler, signal.SIG_DFL)
        install_sigterm = prev_sigterm == signal.SIG_DFL

        # Register SIGINT (Ctrl+C) and SIGTERM
        if install_sigint:
            signal.signal(signal.SIGINT, signal_handler)
        else:
            logger.info("[CDPBrowserManager] SIGINT handler already exists, skipping registration to avoid override")

        if install_sigterm:
            signal.signal(signal.SIGTERM, signal_handler)
        else:
            logger.info("[CDPBrowserManager] SIGTERM handler already exists, skipping registration to avoid override")

        self._cleanup_registered = True
        logger.info("[CDPBrowserManager] Cleanup handlers registered")

    async def launch_and_connect(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict] = None,
        user_agent: Optional[str] = None,
        headless: bool = False,
    ) -> BrowserContext:
        """
        Launch browser and connect via CDP
        """
        with self._state_lock:
            if self._launch_started:
                raise RuntimeError("cdp_browser_manager_already_started")
            self._launch_started = True
            self._owns_browser_process = not self.settings.CDP_CONNECT_EXISTING
        try:
            if self.settings.PLATFORM == "xhs" and self.settings.CDP_CONNECT_EXISTING:
                raise RuntimeError("xhs_cdp_connect_existing_forbidden")
            if self.settings.CDP_CONNECT_EXISTING:
                # Connect to an existing browser that already has remote debugging enabled
                return await self._connect_existing_browser(playwright, playwright_proxy, user_agent)

            # 1. Detect browser path
            browser_path = await self._get_browser_path()

            # 2. Get available port
            self.debug_port = self.launcher.find_available_port(self.settings.CDP_DEBUG_PORT)

            # 3. Launch browser
            await self._launch_browser(browser_path, headless)

            # 4. Register cleanup handlers (ensure cleanup on abnormal exit)
            self._register_cleanup_handlers()

            # 5. Connect via CDP
            await self._connect_via_cdp(playwright)

            # 6. Create browser context
            browser_context = await self._create_browser_context(
                playwright_proxy, user_agent
            )

            self.browser_context = browser_context
            self._observe_browser_context(browser_context)
            return browser_context

        except Exception as e:
            logger.error(f"[CDPBrowserManager] CDP browser launch failed: {e}")
            # This manager owns the one browser launch attempt. Force cleanup
            # here so callers never need a second cleanup or fallback launch.
            self.mark_planned_cleanup("launch_failure")
            await self.cleanup(force=True)
            raise

    async def _connect_existing_browser(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict] = None,
        user_agent: Optional[str] = None,
    ) -> BrowserContext:
        """
        Connect to an existing browser that already has remote debugging enabled.
        User needs to enable remote debugging via chrome://inspect/#remote-debugging
        or launch Chrome with --remote-debugging-port flag.
        """
        self.debug_port = self.settings.CDP_DEBUG_PORT
        logger.info(
            f"[CDPBrowserManager] Connecting to existing browser on port {self.debug_port}..."
        )
        logger.info(
            "[CDPBrowserManager] Make sure remote debugging is enabled in your browser: "
            "chrome://inspect/#remote-debugging"
        )

        # Wait for the browser's CDP port to become available
        # The user may need time to enable remote debugging or confirm the connection dialog
        timeout = self.settings.BROWSER_LAUNCH_TIMEOUT
        logger.info(
            f"[CDPBrowserManager] Waiting up to {timeout}s for browser CDP connection..."
        )
        connected = False
        for i in range(timeout):
            if await self._test_cdp_connection(self.debug_port):
                connected = True
                break
            if i % 5 == 0 and i > 0:
                logger.info(
                    f"[CDPBrowserManager] Still waiting for browser... ({i}s elapsed) "
                    "Please enable remote debugging: chrome://inspect/#remote-debugging"
                )
            await asyncio.sleep(1)

        if not connected:
            raise RuntimeError(
                f"Cannot connect to existing browser on port {self.debug_port} "
                f"after waiting {timeout}s. Please ensure:\n"
                "  1. Your browser is running\n"
                "  2. Remote debugging is enabled (chrome://inspect/#remote-debugging)\n"
                f"  3. The debug port is {self.debug_port} (configure via CDP_DEBUG_PORT)"
            )

        # Connect via CDP (reuse existing method)
        await self._connect_via_cdp(playwright)

        # Create browser context (reuse existing method, will prefer existing context)
        browser_context = await self._create_browser_context(playwright_proxy, user_agent)
        self.browser_context = browser_context
        self._observe_browser_context(browser_context)

        logger.info("[CDPBrowserManager] Successfully connected to existing browser")
        return browser_context

    async def _get_browser_path(self) -> str:
        """
        Get browser path
        """
        # Prefer user-defined path
        if self.settings.CUSTOM_BROWSER_PATH and os.path.isfile(self.settings.CUSTOM_BROWSER_PATH):
            logger.info(
                f"[CDPBrowserManager] Using custom browser path: {self.settings.CUSTOM_BROWSER_PATH}"
            )
            return self.settings.CUSTOM_BROWSER_PATH

        # Auto-detect browser path
        browser_paths = self.launcher.detect_browser_paths()

        if not browser_paths:
            raise RuntimeError(
                "No available browser found. Please ensure Chrome or Edge browser is installed, "
                "or set CUSTOM_BROWSER_PATH in config file to specify browser path."
            )

        browser_path = browser_paths[0]  # Use the first browser found
        browser_name, browser_version = self.launcher.get_browser_info(browser_path)

        logger.info(
            f"[CDPBrowserManager] Detected browser: {browser_name} ({browser_version})"
        )
        logger.info(f"[CDPBrowserManager] Browser path: {browser_path}")

        return browser_path

    async def _test_cdp_connection(self, debug_port: int) -> bool:
        """
        Test if CDP connection is available
        """
        try:
            # Simple socket connection test
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                result = s.connect_ex(("localhost", debug_port))
                if result == 0:
                    logger.info(
                        f"[CDPBrowserManager] CDP port {debug_port} is accessible"
                    )
                    return True
                else:
                    logger.warning(
                        f"[CDPBrowserManager] CDP port {debug_port} is not accessible"
                    )
                    return False
        except Exception as e:
            logger.warning(f"[CDPBrowserManager] CDP connection test failed: {e}")
            return False

    def _clean_session_restore_tabs(self, user_data_dir: str) -> None:
        """
        Remove Chromium session restore files without touching login cookies/storage.
        """
        if not os.environ.get("TRIPPOSTCOLLECT_CLEAN_BROWSER_TABS"):
            return

        profile_root = Path(user_data_dir)
        default_profile = profile_root / "Default"
        candidates = [
            default_profile / "Current Session",
            default_profile / "Current Tabs",
            default_profile / "Last Session",
            default_profile / "Last Tabs",
        ]
        sessions_dir = default_profile / "Sessions"
        if sessions_dir.exists():
            candidates.extend(sessions_dir.glob("Session_*"))
            candidates.extend(sessions_dir.glob("Tabs_*"))

        removed = 0
        for path in candidates:
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError as exc:
                logger.warning(
                    f"[CDPBrowserManager] Failed to remove stale session tab file {path}: {exc}"
                )

        if removed:
            logger.info(
                f"[CDPBrowserManager] Removed {removed} stale session tab files from {user_data_dir}"
            )

    async def _launch_browser(self, browser_path: str, headless: bool):
        """
        Launch browser process
        """
        # XHS formal runs always use the run-scoped empty profile supplied by
        # xhs_runner.  SAVE_LOGIN_STATE is a generic cross-run persistence
        # switch and must never be allowed to detach XHS from that profile.
        if self.settings.PLATFORM == "xhs":
            explicit_profile = os.environ.get("TRIPPOSTCOLLECT_XHS_PROFILE_DIR", "").strip()
            if not explicit_profile:
                raise RuntimeError("XHS requires TRIPPOSTCOLLECT_XHS_PROFILE_DIR from xhs_runner.py")
            user_data_dir = os.path.abspath(os.path.expanduser(explicit_profile))
        elif self.settings.SAVE_LOGIN_STATE:
            profile_name = self.settings.USER_DATA_DIR % self.settings.PLATFORM
            if not os.environ.get("TRIPPOSTCOLLECT_SHARE_CDP_PROFILE"):
                profile_name = f"cdp_{profile_name}"
            user_data_dir = os.path.join(
                PROFILE_BASE_DIR,
                profile_name,
            )
        else:
            user_data_dir = None

        if user_data_dir is not None:
            os.makedirs(user_data_dir, exist_ok=True)
            logger.info(f"[CDPBrowserManager] User data directory: {user_data_dir}")
            self._clean_session_restore_tabs(user_data_dir)

        # Launch browser
        self.launcher.browser_process = self.launcher.launch_browser(
            browser_path=browser_path,
            debug_port=self.debug_port,
            headless=headless,
            user_data_dir=user_data_dir,
        )

        # Wait for browser to be ready
        if not self.launcher.wait_for_browser_ready(
            self.debug_port, self.settings.BROWSER_LAUNCH_TIMEOUT
        ):
            raise RuntimeError(f"Browser failed to start within {self.settings.BROWSER_LAUNCH_TIMEOUT} seconds")

        # Extra wait for CDP service to fully start
        await asyncio.sleep(1)

        # Test CDP connection
        if not await self._test_cdp_connection(self.debug_port):
            logger.warning(
                "[CDPBrowserManager] CDP connection test failed, but will continue to try connecting"
            )

    async def _get_browser_websocket_url(self, debug_port: int) -> str:
        """
        Get browser WebSocket connection URL
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"http://localhost:{debug_port}/json/version", timeout=10
                )
                if response.status_code == 200:
                    data = response.json()
                    ws_url = data.get("webSocketDebuggerUrl")
                    if ws_url:
                        logger.info(
                            f"[CDPBrowserManager] Got browser WebSocket URL: {ws_url}"
                        )
                        return ws_url
                    else:
                        raise RuntimeError("webSocketDebuggerUrl not found")
                else:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
        except Exception as e:
            logger.error(f"[CDPBrowserManager] Failed to get WebSocket URL: {e}")
            raise

    async def _connect_via_cdp(self, playwright: Playwright):
        """
        Connect to browser via CDP
        """
        try:
            if self.settings.CDP_CONNECT_EXISTING:
                # Existing browser remote debugging in Chrome 136+ does not expose
                # /json/version. Connect directly and wait for user confirmation.
                ws_url = f"ws://localhost:{self.debug_port}/devtools/browser"
                logger.info(f"[CDPBrowserManager] Connecting to existing browser via CDP: {ws_url}")
                logger.info(
                    "[CDPBrowserManager] Please check your browser for a confirmation dialog and accept it"
                )
                try:
                    self.browser = await playwright.chromium.connect_over_cdp(
                        ws_url, timeout=self.settings.BROWSER_LAUNCH_TIMEOUT * 1000
                    )
                except Exception as direct_error:
                    logger.warning(
                        "[CDPBrowserManager] Direct existing-browser CDP connection failed: "
                        f"{direct_error}. Trying /json/version discovery..."
                    )
                    ws_url = await self._get_browser_websocket_url(self.debug_port)
                    logger.info(
                        f"[CDPBrowserManager] Connecting to existing browser via discovered CDP: {ws_url}"
                    )
                    self.browser = await playwright.chromium.connect_over_cdp(
                        ws_url, timeout=self.settings.BROWSER_LAUNCH_TIMEOUT * 1000
                    )
            else:
                # For launched browser, get WebSocket URL first
                ws_url = await self._get_browser_websocket_url(self.debug_port)
                logger.info(f"[CDPBrowserManager] Connecting to browser via CDP: {ws_url}")
                self.browser = await playwright.chromium.connect_over_cdp(ws_url)

            self._observe_browser(self.browser)
            if self.browser.is_connected():
                with self._state_lock:
                    self._connection_established = True
                logger.info("[CDPBrowserManager] Successfully connected to browser")
                logger.info(
                    f"[CDPBrowserManager] Browser contexts count: {len(self.browser.contexts)}"
                )
            else:
                raise RuntimeError("CDP connection failed")

        except Exception as e:
            logger.error(f"[CDPBrowserManager] CDP connection failed: {e}")
            raise

    async def _create_browser_context(
        self, playwright_proxy: Optional[Dict] = None, user_agent: Optional[str] = None
    ) -> BrowserContext:
        """
        Create or get browser context
        """
        if not self.browser:
            raise RuntimeError("Browser not connected")

        # Get existing context or create new context
        contexts = self.browser.contexts

        if contexts:
            # Use existing first context
            browser_context = contexts[0]
            logger.info("[CDPBrowserManager] Using existing browser context")
        else:
            # Create new context
            context_options = {
                "viewport": {"width": 1920, "height": 1080},
                "accept_downloads": True,
            }

            # Set user agent
            if user_agent:
                context_options["user_agent"] = user_agent
                logger.info(f"[CDPBrowserManager] Setting user agent: {user_agent}")

            # Note: Proxy settings may not work in CDP mode since browser is already launched
            if playwright_proxy:
                logger.warning(
                    "[CDPBrowserManager] Warning: Proxy settings may not work in CDP mode, "
                    "recommend configuring system proxy or browser proxy extension before launching browser"
                )

            browser_context = await self.browser.new_context(**context_options)
            logger.info("[CDPBrowserManager] Created new browser context")

        return browser_context

    async def add_stealth_script(self, script_path: str | None = None):
        """从包资源或显式路径注入脚本；资源缺失时保持跳过。"""
        with ExitStack() as resource_paths:
            if script_path is None:
                try:
                    script_path = str(resource_paths.enter_context(resources.path("js/stealth.min.js")))
                except FileNotFoundError:
                    return
            if self.browser_context and os.path.exists(script_path):
                try:
                    await self.browser_context.add_init_script(path=script_path)
                    logger.info(
                        f"[CDPBrowserManager] Added anti-detection script: {script_path}"
                    )
                except Exception as e:
                    logger.warning(f"[CDPBrowserManager] Failed to add anti-detection script: {e}")

    async def add_cookies(self, cookies: list):
        """
        Add cookies
        """
        if self.browser_context:
            try:
                await self.browser_context.add_cookies(cookies)
                logger.info(f"[CDPBrowserManager] Added {len(cookies)} cookies")
            except Exception as e:
                logger.warning(f"[CDPBrowserManager] Failed to add cookies: {e}")

    async def get_cookies(self) -> list:
        """
        Get current cookies
        """
        if self.browser_context:
            try:
                cookies = await self.browser_context.cookies()
                return cookies
            except Exception as e:
                logger.warning(f"[CDPBrowserManager] Failed to get cookies: {e}")
                return []
        return []

    def _record_cancelled_cleanup(
        self,
        *,
        reason: str,
        stage: str,
        context_status: str,
        browser_status: str,
        errors: list[str],
        error: asyncio.CancelledError,
    ) -> None:
        if self._owns_browser_process:
            process_result = {
                "status": "not_started",
                "reason": reason,
                **self.launcher.process_status(reason="cleanup_interrupted"),
            }
        else:
            process_result = {
                "status": "not_owned",
                "reason": reason,
            }
        detail = str(error) or "no detail"
        result = {
            "status": "interrupted",
            "reason": reason,
            "interrupted_at": stage,
            "context": context_status,
            "browser": browser_status,
            "process": process_result,
            "errors": [
                *errors,
                f"{stage}:{type(error).__name__}:{detail}",
            ],
        }
        with self._state_lock:
            self.last_cleanup_result = result
            self._cleanup_complete = False
            self._cleanup_in_progress = False
        logger.error(
            "[CDPBrowserManager] Resource cleanup interrupted; "
            f"reason={reason}, stage={stage}, "
            f"error={type(error).__name__}: {detail}"
        )

    async def cleanup(
        self,
        force: bool = False,
        *,
        reason: str = "requested",
    ) -> Dict[str, Any]:
        """
        Cleanup resources

        Args:
            force: Whether to force cleanup browser process (ignoring AUTO_CLOSE_BROWSER config)
        """
        self.mark_planned_cleanup(reason)
        with self._state_lock:
            if self._cleanup_complete:
                return dict(self.last_cleanup_result or {})
            if self._cleanup_in_progress:
                return dict(
                    self.last_cleanup_result
                    or {
                        "status": "in_progress",
                        "reason": self._planned_cleanup_reason,
                    }
                )
            self._cleanup_in_progress = True
            cleanup_reason = self._planned_cleanup_reason

        errors = []
        context_status = "not_present"
        browser_status = "not_present"
        process_result: Dict[str, Any] = {
            "status": "not_owned",
            "reason": cleanup_reason,
        }

        context = self.browser_context
        if context is not None:
            try:
                await context.close()
            except asyncio.CancelledError as exc:
                self._record_cancelled_cleanup(
                    reason=cleanup_reason,
                    stage="context_close",
                    context_status="interrupted",
                    browser_status=(
                        "not_started"
                        if self.browser is not None
                        else "not_present"
                    ),
                    errors=errors,
                    error=exc,
                )
                raise
            except Exception as exc:
                error_msg = str(exc).casefold()
                if "closed" in error_msg or "disconnected" in error_msg:
                    context_status = "already_closed"
                    self.browser_context = None
                else:
                    context_status = "failed"
                    errors.append(f"context:{type(exc).__name__}:{exc}")
            else:
                context_status = "closed"
                self.browser_context = None

        browser = self.browser
        if browser is not None:
            try:
                if browser.is_connected():
                    await browser.close()
                    browser_status = "closed"
                else:
                    browser_status = "already_disconnected"
                self.browser = None
            except asyncio.CancelledError as exc:
                self._record_cancelled_cleanup(
                    reason=cleanup_reason,
                    stage="browser_close",
                    context_status=context_status,
                    browser_status="interrupted",
                    errors=errors,
                    error=exc,
                )
                raise
            except Exception as exc:
                error_msg = str(exc).casefold()
                if "closed" in error_msg or "disconnected" in error_msg:
                    browser_status = "already_disconnected"
                    self.browser = None
                else:
                    browser_status = "failed"
                    errors.append(f"browser:{type(exc).__name__}:{exc}")

        if self._owns_browser_process:
            if force or self.settings.AUTO_CLOSE_BROWSER:
                try:
                    process_result = self.launcher.cleanup(reason=cleanup_reason)
                except Exception as exc:
                    process_result = {
                        "status": "failed",
                        "reason": cleanup_reason,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                if process_result.get("status") == "failed":
                    errors.append(
                        "process:" + str(process_result.get("error") or "cleanup failed")
                    )
            else:
                process_result = {
                    "status": "kept_running",
                    "reason": cleanup_reason,
                    **self.launcher.process_status(reason="cleanup_skipped"),
                }
                logger.info(
                    "[CDPBrowserManager] Browser process kept running "
                    "(AUTO_CLOSE_BROWSER=False)"
                )
        elif self.settings.CDP_CONNECT_EXISTING:
            logger.info(
                "[CDPBrowserManager] Connected to existing browser, skipping process cleanup"
            )

        result = {
            "status": "failed" if errors else "completed",
            "reason": cleanup_reason,
            "context": context_status,
            "browser": browser_status,
            "process": process_result,
            "errors": errors,
        }
        with self._state_lock:
            self.last_cleanup_result = result
            self._cleanup_complete = (
                not errors and process_result.get("status") != "kept_running"
            )
            self._cleanup_in_progress = False

        if errors:
            logger.error(
                "[CDPBrowserManager] Resource cleanup incomplete; "
                f"reason={cleanup_reason}, errors={errors}"
            )
        else:
            logger.info(
                "[CDPBrowserManager] Planned cleanup completed; "
                f"reason={cleanup_reason}"
            )
        return dict(result)

    def is_connected(self) -> bool:
        """
        Check if connected to browser
        """
        return self.browser is not None and self.browser.is_connected()

    async def get_browser_info(self) -> Dict[str, Any]:
        """
        Get browser info
        """
        if not self.browser:
            return {}

        try:
            version = self.browser.version
            contexts_count = len(self.browser.contexts)

            return {
                "version": version,
                "contexts_count": contexts_count,
                "debug_port": self.debug_port,
                "is_connected": self.is_connected(),
            }
        except Exception as e:
            logger.warning(f"[CDPBrowserManager] Failed to get browser info: {e}")
            return {}
