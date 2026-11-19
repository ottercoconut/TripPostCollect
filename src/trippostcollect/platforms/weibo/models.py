"""微博 worker 配置与错误。正文投影在 ``parser``，不在这里保存头像或移动详情状态。"""

from __future__ import annotations

from dataclasses import dataclass

from httpx import RequestError


@dataclass(frozen=True)
class WeiboConfig:
    """worker 已解析配置的只读快照；不包含 Cookie 刷新或 env 操作读取。"""

    ENABLE_CDP_MODE: bool
    CDP_HEADLESS: bool
    HEADLESS: bool
    LOGIN_TYPE: str
    COOKIES: str
    CRAWLER_TYPE: str
    KEYWORDS: str
    START_PAGE: int
    WEIBO_SEARCH_TYPE: str
    CRAWLER_MAX_SLEEP_SEC: float
    MAX_CONCURRENCY_NUM: int
    WEIBO_SPECIFIED_ID_LIST: tuple[str, ...]
    ENABLE_GET_MEIDAS: bool
    SAVE_LOGIN_STATE: bool
    PLATFORM: str
    ENABLE_WEIBO_FULL_TEXT: bool
    SAVE_DATA_OPTION: str
    SAVE_DATA_PATH: str


class DataFetchError(RequestError):
    """something error when fetch"""


class PlatformRuntimeError(DataFetchError):
    """A login or rate-limit response that must stop the current run."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class WeiboImageDownloadError(RuntimeError):
    """A post image exhausted its applicable fetch attempts."""

    def __init__(self, note_id: str, source_index: int, code: str, attempts: int = 1):
        super().__init__(
            f"Weibo image download failed: note_id={note_id}, "
            f"source_index={source_index}, code={code}"
        )
        self.note_id = note_id
        self.source_index = source_index
        self.code = code
        self.attempts = max(1, int(attempts))


class WeiboDetailFetchError(RuntimeError):
    """详情候选失败或运行级阻断。运行级阻断不得写入 seen。"""

    def __init__(
        self,
        note_id: str,
        code: str,
        attempts: int = 1,
        *,
        runtime_blocking: bool = False,
        retryable: bool = True,
    ):
        super().__init__(
            f"weibo detail failed: note_id={note_id or '<missing>'}, code={code}"
        )
        self.note_id = note_id
        self.code = code
        self.attempts = max(1, int(attempts))
        self.runtime_blocking = runtime_blocking
        self.retryable = bool(retryable) and not runtime_blocking


class WeiboRepairNotImplemented(NotImplementedError):
    """桌面 repair 链在下一段实现。不得再走移动详情。"""

    def __init__(self) -> None:
        super().__init__("weibo desktop repair is not implemented")
