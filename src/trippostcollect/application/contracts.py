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


@dataclass(frozen=True)
class WeiboClientPorts:
    """微博请求边界；HTTP 工厂仍在每次请求时读取 TLS 设置。"""

    make_async_client: Callable[..., Any]
    convert_browser_context_cookies: Callable[..., Any]
    image_error: type[Exception]
    classified_http_image_error: Callable[..., Any]
    image_max_bytes: int
    detail_timeout: Callable[[], int]


@dataclass(frozen=True)
class WeiboLoginPorts:
    """微博登录所借用的 Cookie 与二维码能力。"""

    convert_cookies: Callable[..., Any]
    convert_str_cookie_to_dict: Callable[..., Any]
    find_login_qrcode: Callable[..., Any]
    show_qrcode: Callable[..., Any]


class CandidateDecisions(Protocol):
    """抖音消费的已有候选累积器；前沿纪元判定仍由应用层负责。"""

    seen_candidate_identities: set[str]
    stop_reason: str

    @property
    def can_continue(self) -> bool: ...

    def begin_batch(self) -> None: ...

    def is_known(self, identity: str) -> bool: ...

    def consider(self, identity: str, *, valid: bool) -> bool: ...

    def skip_candidate_failure(self, identity: str, **details: Any) -> bool: ...

    def finish_batch(self, **position: Any) -> bool: ...

    def mark_source_exhausted(self, detail: str, **position: Any) -> None: ...

    def mark_runtime_failed(self, detail: str, **position: Any) -> None: ...

    def should_reseed_frontier(
        self, *, saved_source_exhausted: bool,
        refresh_has_more: bool | int | None, refresh_next_cursor: str | None,
        refresh_new_candidate_count: int,
    ) -> bool: ...


@dataclass(frozen=True)
class DouyinSettings:
    """抖音 crawler 构造时从已解析配置提取的不可变切片。"""

    PLATFORM: str
    LOGIN_TYPE: str
    COOKIES: str
    CRAWLER_TYPE: str
    KEYWORDS: str
    START_PAGE: int
    PUBLISH_TIME_TYPE: int
    DY_SPECIFIED_ID_LIST: tuple[str, ...]
    MAX_CONCURRENCY_NUM: int
    CRAWLER_MAX_SLEEP_SEC: float
    ENABLE_CDP_MODE: bool
    CDP_HEADLESS: bool
    HEADLESS: bool
    SAVE_LOGIN_STATE: bool
    USER_DATA_DIR: str
    ENABLE_GET_MEIDAS: bool
    SAVE_DATA_OPTION: str
    SAVE_DATA_PATH: str
    DISABLE_SSL_VERIFY: bool


@dataclass(frozen=True)
class DouyinReaders:
    """只绑定读取点；值仍在关键词、作者或详情操作开始时读取。"""

    refresh_max_pages: Callable[[], int]
    source_exhausted: Callable[[], str | None]
    resume_offset: Callable[[], int]
    resume_cursor: Callable[[], str]
    enrich_creators: Callable[[], str | None]
    enrich_only_images: Callable[[], str]
    max_creator_enrich: Callable[[], str]
    creator_sleep_seconds: Callable[[], str]
    browser_detail_timeout: Callable[[], str]


@dataclass(frozen=True)
class DouyinClientPorts:
    """HTTP 客户端与当前浏览器 Cookie 的原调用接缝。"""

    make_async_client: Callable[..., Any]
    convert_browser_context_cookies: Callable[..., Any]
    random: Callable[[], float]


@dataclass(frozen=True)
class DouyinLoginPorts:
    """二维码读取和系统展示；不在平台导入时执行。"""

    find_login_qrcode: Callable[..., Any]
    show_qrcode: Callable[..., Any]


@dataclass(frozen=True)
class WeiboPorts:
    """微博流程的进程内端口；不持有数据库连接或全平台工厂。"""

    client: WeiboClientPorts
    login: WeiboLoginPorts
    accumulator: Callable[[], Any]
    refresh_max_pages: Callable[[], int]
    source_exhausted: Callable[[], bool]
    post_repair: bool
    store_factory: Callable[[], ContentSink]
    image_stager: Callable[[], ImageStager]
    current_timestamp: Callable[[], int]
    fetch_image_bytes_with_retry: Callable[..., Any]
    image_error: type[Exception]
    is_runtime_blocking_image_error: Callable[..., bool]
    browser_manager: Callable[..., Any]
    project_browser_args: Callable[..., Any]
    run_required_human_behavior: Callable[..., Any]


@dataclass(frozen=True)
class DouyinCrawlerPorts:
    """抖音流程借用的进程内能力，不改变 HTTP 和写出顺序。"""

    client: DouyinClientPorts
    login: DouyinLoginPorts
    browser_detail_fallback: bool
    async_playwright: Callable[..., Any]
    cdp_manager: Callable[..., Any]
    project_browser_args: Callable[[], list[str]]
    run_required_human_behavior: Callable[..., Any]
    candidates: Callable[[], CandidateDecisions]
    append_execution_event: Callable[..., None]
    current_timestamp: Callable[[], int]
    content_sink: Callable[[str], ContentSink]
    image_stager: Callable[[], ImageStager]
    fetch_image_bytes_with_retry: Callable[..., Any]


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


@dataclass(frozen=True)
class ZhihuSettings:
    """worker 解析完成后、crawler 构造时一次性冻结的本站配置。"""

    CDP_HEADLESS: bool
    CRAWLER_MAX_SLEEP_SEC: float
    CRAWLER_TYPE: str
    ENABLE_CDP_MODE: bool
    ENABLE_GET_MEIDAS: bool
    HEADLESS: bool
    KEYWORDS: str
    LOGIN_TYPE: str
    MAX_CONCURRENCY_NUM: int
    PLATFORM: str
    SAVE_LOGIN_STATE: bool
    START_PAGE: int
    USER_DATA_DIR: str
    ZHIHU_SPECIFIED_ID_LIST: tuple[str, ...]


@dataclass(frozen=True)
class ZhihuClientPorts:
    """借用 HTTPX 工厂和原 BrowserContext Cookie 读取能力。"""

    make_async_client: Callable[..., Any]
    convert_browser_context_cookies: Callable[..., Any]


@dataclass(frozen=True)
class ZhihuLoginPorts:
    """二维码提取与显示仍由运行时拥有。"""

    find_qrcode_img_from_canvas: Callable[..., Any]
    show_qrcode: Callable[..., Any]


@dataclass(frozen=True)
class ZhihuPorts:
    """知乎编排借用的 IO 能力与操作起点 reader；装配不执行 IO。"""

    async_playwright: Callable[..., Any]
    browser_manager_factory: Callable[..., Any]
    project_browser_args: Callable[[], list[str]]
    run_required_human_behavior: Callable[..., Any]
    client_factory: Callable[..., Any]
    login_factory: Callable[..., Any]
    convert_browser_context_cookies: Callable[..., Any]
    fetch_image_bytes_with_retry: Callable[..., Any]
    accumulator_factory: Callable[..., Any]
    refresh_max_pages: Callable[[], int]
    source_exhausted: Callable[[], bool]
    initial_settle_seconds: Callable[[], float]
    initial_cookies: Callable[[], str]
    current_timestamp: Callable[[], int]
    content_sink_factory: Callable[[], ContentSink]
    image_stager_factory: Callable[[], ImageStager]
