"""B站请求异常及元数据。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any



class BilibiliArticleDetailError(RuntimeError):

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        code: int | None = None,
        attempts: int = 1,
        retry_wait_seconds: float = 0.0,
        runtime_blocking: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.attempts = attempts
        self.retry_wait_seconds = retry_wait_seconds
        self.runtime_blocking = runtime_blocking


class BilibiliFollowerFetchError(RuntimeError):

    def __init__(
        self,
        message: str,
        *,
        code: int | None,
        retryable: bool,
        runtime_blocking: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.runtime_blocking = runtime_blocking


class BilibiliRuntimeBlocked(RuntimeError):

    def __init__(self, detail: str, *, stop_reason: str = "runtime_failed") -> None:
        super().__init__(detail)
        self.detail = detail
        # 正式停止状态：login_required / captcha_detected 是登录或验证阻断，其余为 runtime_failed。
        self.stop_reason = stop_reason


@dataclass(frozen=True)
class BilibiliSearchPage:
    """一页 article 搜索响应；numPages/numResults 是判断来源耗尽的平台证据。"""

    items: list[dict[str, Any]] = field(default_factory=list)
    num_pages: int | None = None
    num_results: int | None = None
