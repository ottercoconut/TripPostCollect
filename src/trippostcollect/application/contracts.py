"""采集应用与运行监督之间的异常契约。"""

from __future__ import annotations




class XhsRuntimeSupervisionError(RuntimeError):
    """Raised when authenticated XHS runtime supervision can no longer continue."""


PLATFORMS: dict[str, dict[str, str]] = {
    "bilibili": {"mediacrawler": "bili", "label": "B站"},
    "xhs": {"mediacrawler": "xhs", "label": "小红书"},
    "weibo": {"mediacrawler": "wb", "label": "微博"},
    "douyin": {"mediacrawler": "dy", "label": "抖音"},
    "zhihu": {"mediacrawler": "zhihu", "label": "知乎"},
}


class ImageStagingError(ValueError):
    """A stable, non-sensitive image staging failure."""

    def __init__(self, code: str, message: str, *, source_index: int | None = None):
        super().__init__(message)
        self.code = code
        self.source_index = source_index
