"""采集应用的暂存、内容出口与运行监督契约。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ImageStager(Protocol):
    """整帖图片暂存；帖子 ID 按位置传递，各站保留原关键字名称。"""

    async def store_post_images(
        self, platform_post_id: str, /, image_content_items: list[dict],
    ) -> list[dict]: ...

    async def record_failure(
        self, platform_post_id: str, /, image_content_item: dict,
    ) -> dict: ...


@runtime_checkable
class JsonlWriter(Protocol):
    """净化后逐条写出 JSONL，文件错误和取消向调用者传播。"""

    async def write_to_jsonl(self, item: dict, item_type: str) -> None: ...


@runtime_checkable
class ContentSink(Protocol):
    """只接收平台已投影的内容记录，不拥有评论或创作者写出能力。"""

    async def store_content(self, content_item: dict) -> None: ...


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
