"""B站请求异常及元数据。"""

from __future__ import annotations



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

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

