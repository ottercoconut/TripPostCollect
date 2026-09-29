"""采集应用的异常契约、暂存与内容出口、平台能力端口及运行监督契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Collection, Protocol, runtime_checkable


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


class RemoteImageResponse(Protocol):
    """图片下载返回的字节及响应元数据。"""

    content: bytes
    media_type: str
    final_url: str
    http_status: int


class ImageManifestRecord(Protocol):
    """平台编排使用的图片清单视图；具体序列化仍由产物层负责。"""

    platform_post_id: str
    fetch_status: str
    error_code: str | None
    attempts: int
    source_index: int


@dataclass(frozen=True)
class BilibiliBehaviorPorts:
    """执行器显式装配的 B站能力端口；创建时不执行 IO。"""

    discover_cdp_browser_path: Callable[..., Any]
    profile_dir_for: Callable[..., Any]
    async_playwright: Callable[..., Any]
    browser_runtime_args: Callable[..., Any]
    browser_launch_environment: Callable[..., Any]
    install_runtime_hints: Callable[..., Any]
    run_page_behavior: Callable[..., Any]
    write_evidence: Callable[..., Any]
    platform_cookie_url: Callable[..., Any]
    cookies_to_header: Callable[..., Any]
    cookie_snapshot_path: Callable[..., Any]
    cookie_names_from_header: Callable[..., Any]
    required_cookie_names: Callable[..., Any]


@dataclass(frozen=True)
class BilibiliImageFetchPorts:
    """执行器显式装配的 B站能力端口；创建时不执行 IO。"""

    fetch_remote_image_bytes: Callable[..., Any]
    DEFAULT_ARCHIVE_IMAGE_MAX_BYTES: int
    SUPPORTED_IMAGE_MIME_TYPES: Collection[str]


@dataclass(frozen=True)
class BilibiliImagePorts:
    """执行器显式装配的 B站能力端口；创建时不执行 IO。"""

    ImageManifestEntry: Callable[..., Any]
    ImageMaterializationError: type[Exception]
    RemoteImageFetchError: type[Exception]
    content_image_candidates: Callable[..., Any]
    fetch_bilibili_image_bytes: Callable[..., Any]
    is_retryable_image_error: Callable[..., Any]
    remote_image_failure_code: Callable[..., Any]
    safe_platform_post_id: Callable[..., Any]
    write_staging_image: Callable[..., Any]


@dataclass(frozen=True)
class BilibiliSearchPorts:
    """执行器显式装配的 B站能力端口；创建时不执行 IO。"""

    load_behavior_evidence: Callable[..., Any]
    behavior_evidence_valid: Callable[..., Any]
    load_existing_formal_identities: Callable[..., Any]
    load_skipped_candidates: Callable[..., Any]
    connect_database: Callable[..., Any]
    run_bilibili_behavior_session: Callable[..., Any]
    download_bilibili_record_images: Callable[..., Any]
    content_image_candidates: Callable[..., Any]
    write_manifest_atomic: Callable[..., Any]
    manifest_sha256: Callable[..., Any]
    is_retryable_image_error: Callable[..., Any]
    is_runtime_blocking_image_error: Callable[..., Any]
    formal_database_identities: Callable[..., Any]
    summarize_output: Callable[..., Any]
    tail: Callable[..., Any]
    validate_formal_record: Callable[..., Any]
