# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/browser_launcher.py
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


# TripPostCollect：迁入浏览器启动器，浏览器参数能力由调用方注入。
"""浏览器定位与启动；保留原执行器的查找顺序与判定。

xhs_window_size_argument 原位读取 TRIPPOSTCOLLECT_XHS_WINDOW_SIZE，
T09 随 XHS 迁移改为注入。既有 discover_cdp_browser_path 的
TRIPPOSTCOLLECT_CUSTOM_BROWSER_PATH/CUSTOM_BROWSER_PATH 读点保持不变。
"""

import logging
import os
import platform
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
import re


def discover_cdp_browser_path() -> str | None:
    for env_key in ("TRIPPOSTCOLLECT_CUSTOM_BROWSER_PATH", "CUSTOM_BROWSER_PATH"):
        value = os.environ.get(env_key)
        if value and Path(value).is_file():
            return value

    playwright_cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    cache_candidates = sorted(
        playwright_cache.glob(
            "chromium-*/chrome-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        ),
        key=lambda path: int(match.group(1)) if (match := re.search(r"chromium-(\d+)", str(path))) else -1,
        reverse=True,
    )
    for path in cache_candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)

    candidates = [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta"),
        Path("/Applications/Google Chrome Dev.app/Contents/MacOS/Google Chrome Dev"),
        Path("/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"),
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        Path("/Applications/Microsoft Edge Beta.app/Contents/MacOS/Microsoft Edge Beta"),
        Path("/Applications/Microsoft Edge Dev.app/Contents/MacOS/Microsoft Edge Dev"),
        Path("/Applications/Microsoft Edge Canary.app/Contents/MacOS/Microsoft Edge Canary"),
    ]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


logger = logging.getLogger("MediaCrawler")


class BrowserLauncher:
    """
    Browser launcher for detecting and launching user's Chrome/Edge browser
    Supports Windows and macOS systems
    """

    def __init__(self, *, project_browser_args: Callable[[], list[str]]):
        self._project_browser_args = project_browser_args
        self.system = platform.system()
        self.browser_process = None
        self.debug_port = None
        self.last_process_exit: Optional[Dict[str, Any]] = None
        self.last_cleanup_result: Optional[Dict[str, Any]] = None
        self._launch_in_progress = False
        self._launch_attempted = False
        self._cleanup_requested = False
        self._cleanup_request_reason = ""
        self._cleanup_in_progress = False
        # Launch and cleanup normally run on the main thread, but cleanup can
        # also be entered from a signal/atexit handler.  Keep state transitions
        # atomic without holding the lock across process.wait().
        self._state_lock = threading.RLock()

    @property
    def cleanup_requested(self) -> bool:
        with self._state_lock:
            return self._cleanup_requested

    @staticmethod
    def _process_pid(process: subprocess.Popen) -> Optional[int]:
        try:
            return int(process.pid)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _process_returncode(process: subprocess.Popen) -> Optional[int]:
        try:
            value = process.poll()
        except Exception:
            return None
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _record_process_exit(
        self,
        process: subprocess.Popen,
        *,
        reason: str,
        returncode: Optional[int] = None,
    ) -> Dict[str, Any]:
        pid = self._process_pid(process)
        if returncode is None:
            returncode = self._process_returncode(process)
        with self._state_lock:
            existing = self.last_process_exit
            if not existing or existing.get("pid") != pid:
                self.last_process_exit = {
                    "event": "browser_process_exited",
                    "reason": reason,
                    "pid": pid,
                    "returncode": returncode,
                    "observed_at": time.time(),
                }
            elif existing.get("returncode") is None and returncode is not None:
                existing["returncode"] = returncode
            return dict(self.last_process_exit)

    def process_status(self, *, reason: str = "status_check") -> Dict[str, Any]:
        """Return a stable, auditable snapshot of the owned browser process."""
        with self._state_lock:
            process = self.browser_process
            cleanup_requested = self._cleanup_requested
            last_exit = dict(self.last_process_exit or {})
            last_cleanup = dict(self.last_cleanup_result or {})
        if process is None:
            return {
                "present": False,
                "running": False,
                "pid": None,
                "returncode": None,
                "cleanup_requested": cleanup_requested,
                "last_exit": last_exit,
                "last_cleanup": last_cleanup,
            }

        returncode = self._process_returncode(process)
        if returncode is not None:
            last_exit = self._record_process_exit(
                process,
                reason=reason,
                returncode=returncode,
            )
        return {
            "present": True,
            "running": returncode is None,
            "pid": self._process_pid(process),
            "returncode": returncode,
            "cleanup_requested": cleanup_requested,
            "last_exit": last_exit,
            "last_cleanup": last_cleanup,
        }

    @staticmethod
    def xhs_window_size_argument() -> Optional[str]:
        value = os.environ.get("TRIPPOSTCOLLECT_XHS_WINDOW_SIZE", "").strip()
        if not value:
            return None
        try:
            width_text, height_text = value.split(",", 1)
            width, height = int(width_text), int(height_text)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("invalid TRIPPOSTCOLLECT_XHS_WINDOW_SIZE") from exc
        if width < 800 or height < 600:
            raise RuntimeError("TRIPPOSTCOLLECT_XHS_WINDOW_SIZE is too small")
        return f"--window-size={width},{height}"

    def detect_browser_paths(self) -> List[str]:
        """
        Detect available browser paths in system
        Returns list of browser paths sorted by priority
        """
        paths = []

        if self.system == "Windows":
            # Common Chrome/Edge installation paths on Windows
            possible_paths = [
                # Chrome paths
                os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                # Edge paths
                os.path.expandvars(r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
                # Chrome Beta/Dev/Canary
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome Beta\Application\chrome.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome Dev\Application\chrome.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome SxS\Application\chrome.exe"),
            ]
        elif self.system == "Darwin":  # macOS
            # Common Chrome/Edge installation paths on macOS
            possible_paths = [
                # Chrome paths
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
                "/Applications/Google Chrome Dev.app/Contents/MacOS/Google Chrome Dev",
                "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
                # Edge paths
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Microsoft Edge Beta.app/Contents/MacOS/Microsoft Edge Beta",
                "/Applications/Microsoft Edge Dev.app/Contents/MacOS/Microsoft Edge Dev",
                "/Applications/Microsoft Edge Canary.app/Contents/MacOS/Microsoft Edge Canary",
            ]
        else:
            # Linux and other systems
            possible_paths = [
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/google-chrome-beta",
                "/usr/bin/google-chrome-unstable",
                "/usr/bin/chromium-browser",
                "/usr/bin/chromium",
                "/snap/bin/chromium",
                "/usr/bin/microsoft-edge",
                "/usr/bin/microsoft-edge-stable",
                "/usr/bin/microsoft-edge-beta",
                "/usr/bin/microsoft-edge-dev",
            ]

        # Check if path exists and is executable
        for path in possible_paths:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                paths.append(path)

        return paths

    def find_available_port(self, start_port: int = 9222) -> int:
        """
        Find available port
        """
        port = start_port
        while port < start_port + 100:  # Try up to 100 ports
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(('localhost', port))
                    return port
            except OSError:
                port += 1

        raise RuntimeError(f"Cannot find available port, tried {start_port} to {port-1}")

    def launch_browser(self, browser_path: str, debug_port: int, headless: bool = False,
                      user_data_dir: Optional[str] = None) -> subprocess.Popen:
        """
        Launch browser process
        """
        # Basic launch arguments
        args = [
            browser_path,
            f"--remote-debugging-port={debug_port}",
            "--remote-debugging-address=0.0.0.0",  # Allow remote access
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=TranslateUI",
            "--disable-ipc-flooding-protection",
            "--disable-hang-monitor",
            "--disable-prompt-on-repost",
            "--disable-sync",
            "--disable-dev-shm-usage",  # Avoid shared memory issues
            "--no-sandbox",  # Disable sandbox in CDP mode
            # Key anti-detection arguments
            "--disable-blink-features=AutomationControlled",  # Disable automation control flag
            "--exclude-switches=enable-automation",  # Exclude automation switch
            "--disable-infobars",  # Disable info bars
            *self._project_browser_args(),
        ]

        stable_window_size = self.xhs_window_size_argument()
        if stable_window_size:
            args.append(stable_window_size)

        # Headless mode
        if headless:
            args.extend([
                "--headless=new",  # Use new headless mode
                "--disable-gpu",
            ])
        else:
            # Extra arguments for non-headless mode
            if not stable_window_size:
                args.append("--start-maximized")

        # User data directory
        if user_data_dir:
            args.append(f"--user-data-dir={user_data_dir}")

        logger.info(f"[BrowserLauncher] Launching browser: {browser_path}")
        logger.info(f"[BrowserLauncher] Debug port: {debug_port}")
        logger.info(f"[BrowserLauncher] Headless mode: {headless}")

        with self._state_lock:
            if self._cleanup_requested:
                raise RuntimeError(
                    "browser_process_cleanup_already_requested:"
                    f"reason={self._cleanup_request_reason or 'requested'}"
                )
            if self._cleanup_in_progress:
                raise RuntimeError("browser_process_cleanup_in_progress")
            if self._launch_in_progress:
                raise RuntimeError("browser_process_launch_in_progress")

            existing = self.browser_process
            if existing is not None:
                returncode = self._process_returncode(existing)
                if returncode is None:
                    raise RuntimeError(
                        "browser_process_already_running:"
                        f"pid={self._process_pid(existing)}"
                    )
                self._record_process_exit(
                    existing,
                    reason="superseded_exited_process",
                    returncode=returncode,
                )
                if self.browser_process is existing:
                    self.browser_process = None

            if self._launch_attempted:
                raise RuntimeError("browser_process_launch_already_attempted")

            # Claim the only launch slot before Popen.  This closes the window
            # where two callers could both observe browser_process == None.
            self._launch_in_progress = True
            self._launch_attempted = True

        process = None
        try:
            # On Windows, use CREATE_NEW_PROCESS_GROUP to prevent Ctrl+C from affecting subprocess
            if self.system == "Windows":
                process = subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                process = subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setsid  # Create new process group
                )

            with self._state_lock:
                self.browser_process = process
                self.debug_port = debug_port
                cleanup_requested = self._cleanup_requested
                cleanup_reason = self._cleanup_request_reason or "requested"

            if cleanup_requested:
                cleanup_result = self.cleanup(reason=cleanup_reason)
                raise RuntimeError(
                    "browser_process_cleanup_requested_during_launch:"
                    f"reason={cleanup_reason}:"
                    f"status={cleanup_result.get('status')}:"
                    f"pid={self._process_pid(process)}"
                )
            return process

        except Exception as e:
            if process is None:
                with self._state_lock:
                    if self._cleanup_requested:
                        cleanup_reason = (
                            self._cleanup_request_reason or "requested"
                        )
                        self.last_cleanup_result = {
                            "status": "launch_failed",
                            "reason": cleanup_reason,
                            "pid": None,
                            "returncode": None,
                            "error": f"{type(e).__name__}: {e}",
                        }
            logger.error(f"[BrowserLauncher] Failed to launch browser: {e}")
            raise
        finally:
            with self._state_lock:
                self._launch_in_progress = False

    def wait_for_browser_ready(self, debug_port: int, timeout: int = 30) -> bool:
        """
        Wait for browser to be ready
        """
        logger.info(f"[BrowserLauncher] Waiting for browser to be ready on port {debug_port}...")

        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout:
            status = self.process_status(reason="browser_ready_wait")
            if not status["present"]:
                raise RuntimeError("browser_process_missing_before_cdp_ready")
            if not status["running"]:
                raise RuntimeError(
                    "browser_process_exited_before_cdp_ready:"
                    f"pid={status['pid']}:returncode={status['returncode']}"
                )
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(1)
                    result = s.connect_ex(('localhost', debug_port))
                    if result == 0:
                        status = self.process_status(reason="browser_ready_confirm")
                        if not status["running"]:
                            raise RuntimeError(
                                "browser_process_exited_before_cdp_ready:"
                                f"pid={status['pid']}:returncode={status['returncode']}"
                            )
                        logger.info(f"[BrowserLauncher] Browser is ready on port {debug_port}")
                        return True
            except RuntimeError:
                raise
            except Exception:
                pass

            time.sleep(0.5)

        logger.error(f"[BrowserLauncher] Browser failed to be ready within {timeout} seconds")
        return False

    def get_browser_info(self, browser_path: str) -> Tuple[str, str]:
        """
        Get browser info (name and version)
        """
        try:
            if "chrome" in browser_path.lower():
                name = "Google Chrome"
            elif "edge" in browser_path.lower() or "msedge" in browser_path.lower():
                name = "Microsoft Edge"
            elif "chromium" in browser_path.lower():
                name = "Chromium"
            else:
                name = "Unknown Browser"

            # Try to get version info
            try:
                result = subprocess.run([browser_path, "--version"],
                                      capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=5)
                version = result.stdout.strip() if result.stdout else "Unknown Version"
            except Exception:
                version = "Unknown Version"

            return name, version

        except Exception:
            return "Unknown Browser", "Unknown Version"

    def cleanup(self, *, reason: str = "requested") -> Dict[str, Any]:
        """
        Cleanup resources, close browser process
        """
        with self._state_lock:
            if not self._cleanup_requested:
                self._cleanup_requested = True
                self._cleanup_request_reason = str(reason or "requested")[:160]
            cleanup_reason = self._cleanup_request_reason
            if self._cleanup_in_progress:
                return dict(
                    self.last_cleanup_result
                    or {
                        "status": "in_progress",
                        "reason": cleanup_reason,
                        "pid": self._process_pid(self.browser_process)
                        if self.browser_process is not None
                        else None,
                    }
                )
            if self.browser_process is None:
                if self._launch_in_progress:
                    if not self.last_cleanup_result or self.last_cleanup_result.get(
                        "status"
                    ) != "pending_launch":
                        self.last_cleanup_result = {
                            "status": "pending_launch",
                            "reason": cleanup_reason,
                            "pid": None,
                            "returncode": None,
                        }
                    return dict(self.last_cleanup_result)
                if self.last_cleanup_result is not None:
                    return dict(self.last_cleanup_result)
                self.last_cleanup_result = {
                    "status": "not_started",
                    "reason": cleanup_reason,
                    "pid": None,
                    "returncode": None,
                }
                return dict(self.last_cleanup_result)
            self._cleanup_in_progress = True
            process = self.browser_process

        pid = self._process_pid(process)
        returncode = self._process_returncode(process)
        if returncode is not None:
            exit_record = self._record_process_exit(
                process,
                reason="observed_before_cleanup",
                returncode=returncode,
            )
            result = {
                "status": "already_exited",
                "reason": cleanup_reason,
                "pid": pid,
                "returncode": returncode,
                "exit": exit_record,
            }
            with self._state_lock:
                self.last_cleanup_result = result
                if self.browser_process is process:
                    self.browser_process = None
                self._cleanup_in_progress = False
            logger.info(
                "[BrowserLauncher] Browser process already exited; "
                f"pid={pid}, returncode={returncode}, reason={cleanup_reason}"
            )
            return dict(result)

        logger.info(
            "[BrowserLauncher] Closing browser process; "
            f"pid={pid}, reason={cleanup_reason}"
        )

        status = "terminated"
        try:
            if self.system == "Windows":
                # First try normal termination
                process.terminate()
                try:
                    returncode = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    status = "killed"
                    logger.warning("[BrowserLauncher] Normal termination timeout, using taskkill to force kill")
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                        capture_output=True,
                        check=False,
                        encoding='utf-8',
                        errors='ignore'
                    )
                    returncode = process.wait(timeout=5)
            else:
                pgid = os.getpgid(process.pid)
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    returncode = self._process_returncode(process)
                    if returncode is None:
                        raise RuntimeError(
                            "browser_process_group_missing_while_process_reports_running:"
                            f"pid={pid}"
                        )
                    status = "already_exited"
                else:
                    try:
                        returncode = process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        status = "killed"
                        logger.warning("[BrowserLauncher] Graceful shutdown timeout, sending SIGKILL")
                        os.killpg(pgid, signal.SIGKILL)
                        returncode = process.wait(timeout=5)

            try:
                normalized_returncode = (
                    int(returncode) if returncode is not None else None
                )
            except (TypeError, ValueError):
                normalized_returncode = self._process_returncode(process)
            exit_record = self._record_process_exit(
                process,
                reason=f"planned_cleanup:{cleanup_reason}",
                returncode=normalized_returncode,
            )
            result = {
                "status": status,
                "reason": cleanup_reason,
                "pid": pid,
                "returncode": normalized_returncode,
                "exit": exit_record,
            }
            with self._state_lock:
                self.last_cleanup_result = result
                if self.browser_process is process:
                    self.browser_process = None
            logger.info(
                "[BrowserLauncher] Browser process closed; "
                f"pid={pid}, returncode={normalized_returncode}, "
                f"status={status}, reason={cleanup_reason}"
            )
        except Exception as e:
            result = {
                "status": "failed",
                "reason": cleanup_reason,
                "pid": pid,
                "returncode": self._process_returncode(process),
                "error": f"{type(e).__name__}: {e}",
            }
            with self._state_lock:
                self.last_cleanup_result = result
            logger.warning(
                "[BrowserLauncher] Error closing browser process; "
                f"pid={pid}, reason={cleanup_reason}, "
                f"error={type(e).__name__}: {e}"
            )
        finally:
            with self._state_lock:
                self._cleanup_in_progress = False
        return dict(result)
