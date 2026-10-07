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

"""小红书平台异常、可恢复判定与请求失败解释；分类优先级保持原实现。"""


from httpx import RequestError
from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
from tenacity import RetryError

from trippostcollect.runtime.browser import CDPBrowserLifecycleError


class DataFetchError(RequestError):
    """something error when fetch"""


class IPBlockError(RequestError):
    """fetch so fast that the server block us ip"""


class PlatformRuntimeError(RequestError):
    """A login or rate-limit response that must stop the current run."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class NoteNotFoundError(RequestError):
    """Note does not exist or is abnormal"""


_XHS_RECOVERABLE_NAVIGATION_ERROR_MARKERS = (
    "net::err_internet_disconnected",
    "net::err_network_changed",
    "net::err_name_not_resolved",
    "net::err_connection_reset",
    "net::err_connection_closed",
    "net::err_connection_refused",
    "net::err_timed_out",
    "net::err_address_unreachable",
    "net::err_proxy_connection_failed",
    "net::err_tunnel_connection_failed",
)
_XHS_CDP_LIFECYCLE_STOP_DETAILS = {
    "xhs_browser_process_exited": "browser_process_exited",
    "xhs_cdp_disconnected_unexpected": "cdp_disconnected",
    "xhs_browser_context_closed_unexpected": "browser_context_closed",
}


def xhs_cdp_lifecycle_stop_detail(exc: CDPBrowserLifecycleError) -> str:
    return _XHS_CDP_LIFECYCLE_STOP_DETAILS.get(
        str(exc.event.get("code") or ""),
        "browser_runtime_failed",
    )


def is_recoverable_xhs_navigation_failure(exc: BaseException) -> bool:
    detail = str(exc).casefold()
    if exc.__class__.__name__ == "TargetClosedError" or any(
        marker in detail
        for marker in (
            "target page, context or browser has been closed",
            "context or browser has been closed",
            "browser has been closed",
            "browser has disconnected",
            "browser disconnected",
            "playwright connection closed",
            "connection closed while reading from the driver",
        )
    ):
        return False
    if isinstance(exc, (PlaywrightTimeoutError, TimeoutError)):
        return True
    if not isinstance(exc, PlaywrightError):
        return False
    return any(
        marker in detail for marker in _XHS_RECOVERABLE_NAVIGATION_ERROR_MARKERS
    )


class XHSImageDownloadError(RuntimeError):
    """An XHS post image exhausted its applicable fetch attempts."""

    def __init__(self, note_id: str, source_index: int, code: str, attempts: int = 1):
        super().__init__(
            f"XHS image download failed: note_id={note_id}, "
            f"source_index={source_index}, code={code}"
        )
        self.note_id = note_id
        self.source_index = source_index
        self.code = code
        self.attempts = max(1, int(attempts))


class XHSNoteDetailUnavailable(RuntimeError):
    """An XHS note detail remained unavailable after bounded attempts."""

    def __init__(self, note_id: str, code: str, attempts: int = 1):
        super().__init__(
            f"XHS note detail unavailable: note_id={note_id or '<missing>'}, code={code}"
        )
        self.note_id = note_id
        self.code = code
        self.attempts = max(1, int(attempts))


class XHSCreatorProfileUnavailable(RuntimeError):
    """An XHS creator profile remained unavailable after observed attempts."""

    def __init__(self, user_id: str, attempts: int):
        super().__init__(
            "creator_profile_unavailable_after_retry:"
            f"user_id={user_id or '<missing>'}:attempts={attempts}"
        )
        self.user_id = user_id
        self.attempts = max(1, int(attempts))


class XHSNetworkRecoveryTimeout(RuntimeError):
    """A recoverable transport outage outlived the configured same-run wait."""

    def __init__(self, stage: str, elapsed_seconds: float):
        super().__init__(
            "xhs_network_recovery_timeout:"
            f"stage={stage}:elapsed={max(0.0, elapsed_seconds):.1f}s"
        )
        self.stage = stage
        self.elapsed_seconds = max(0.0, elapsed_seconds)


class XhsErrorsMixin:
    """请求失败解释；作为 XiaoHongShuCrawler 的 mixin，方法体逐字保留。"""

    @staticmethod
    def _request_failure_exception(exc: BaseException) -> BaseException:
        """Return the request exception hidden by tenacity, when available."""
        if not isinstance(exc, RetryError):
            return exc
        try:
            nested = exc.last_attempt.exception()
        except Exception:
            nested = None
        return nested if isinstance(nested, BaseException) else exc

    @staticmethod
    def _request_failure_attempts(exc: BaseException) -> int:
        """Return the observed tenacity attempt count without inventing retries."""

        if not isinstance(exc, RetryError):
            return 1
        return max(1, int(getattr(exc.last_attempt, "attempt_number", 1) or 1))

    @classmethod
    def _is_login_expired_failure(cls, exc: BaseException) -> bool:
        nested = cls._request_failure_exception(exc)
        text = str(nested).lower()
        return isinstance(nested, DataFetchError) and any(
            marker in text
            for marker in (
                "登录已过期",
                "登录状态已失效",
                "登录失效",
                "login expired",
                "login has expired",
                "session expired",
            )
        )
