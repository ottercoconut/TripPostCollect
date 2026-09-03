#!/usr/bin/env python3
"""MediaCrawler-first structured crawl entrypoint."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import html
import json
import os
import random
import re
import signal
import shlex
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import md5, sha256
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, MutableMapping
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from playwright.async_api import async_playwright

from trippostcollect.artifacts.image_candidates import (
    ImageCandidate,
    content_image_candidates,
    image_items_for_record,
    normalize_image_url,
    source_asset_key_for_image,
)
from trippostcollect.artifacts.image_manifest import (
    ImageManifestEntry,
    ImageManifestError,
    manifest_sha256,
    parse_manifest,
    validate_post_manifest,
    write_manifest_atomic,
)
from trippostcollect.artifacts.image_materialization import (
    DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
    ImageMaterializationError,
    MaterializedImage,
    Sha256DuplicateSource,
    SUPPORTED_IMAGE_MIME_TYPES,
    ValidatedImage,
    promote_validated_image,
    safe_platform_post_id,
    validate_image_file,
    write_staging_image,
)
from trippostcollect.artifacts.image_persistence import (
    ImagePersistenceError,
    existing_image_records as _existing_image_records,
    normalize_persistence_items as _normalize_persistence_items,
    prepare_image_rows as _prepare_image_rows,
    replace_image_rows,
)
from trippostcollect.artifacts.image_proxy import (
    RemoteImageFetchError,
    RemoteImagePreview,
    remote_image_failure_code,
    fetch_remote_image_bytes,
)
from crawl_policy import (
    CrawlPolicyBlocked,
    clear_site_policy_state,
    record_site_cooldown,
    site_request_guard,
)
from execution_state import FrozenExecutionState
from failure_classifier import classify_attempt
from human_flow import install_runtime_hints
from mediacrawler_behavior import (
    HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS,
    behavior_evidence_valid,
    run_page_behavior,
)
from trippostcollect.core.paths import (
    DEFAULT_DB,
    FORMAL_MEDIA_PERSISTENCE_LOCK,
    LOCAL_MEDIA_ROOT,
    MEDIACRAWLER_DIR,
    MEDIACRAWLER_RUNS_OUTPUT,
    PROJECT_ROOT,
    UV_CACHE_ROOT,
    ensure_dir,
    ensure_parent,
)
from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.db.connection import connect_db
from trippostcollect.platforms.registry import get_site
from trippostcollect.records.sanitization import (
    AUTHOR_AVATAR_LOG_REDACTION,
    redact_author_avatar_text,
    sanitize_author_avatar_data,
)
from trippostcollect.records.topic_relevance import (
    CONTENT_BODY_FIELDS,
    effective_source_keyword,
    is_topic_relevant,
    topic_relevant_for_web_post,
    web_post_content_text,
    web_post_title,
)
from trippostcollect.xhs.leases import (
    LEASE_DB_ENV,
    LEASE_ID_ENV,
    LEASE_OWNER_TOKEN_ENV,
    ProcessIdentity,
    SystemProcessInspector,
    mark_lease_process_exited_from_environment,
    register_lease_process_from_environment,
)
from trippostcollect.xhs.runtime import (
    RUNTIME_STATUS_AUTH_KEY_ENV,
    RUNTIME_STATUS_SCHEMA_VERSION,
    RuntimeStatusValidationError,
    runtime_session_paths,
    runtime_status_path,
    sign_runtime_status,
    write_runtime_status_atomic,
)
from trippostcollect.scheduler.discovery import (
    load_checkpoint,
    load_skipped_candidates,
    save_checkpoint,
    save_seen_candidates,
)
from browser_runtime import (
    XHS_WINDOW_SIZE_ENV,
    browser_launch_environment,
    browser_runtime_args,
    xhs_window_size_value,
)


ROOT = PROJECT_ROOT
DEFAULT_OUTPUT = MEDIACRAWLER_RUNS_OUTPUT
COOKIE_SNAPSHOT_FILENAME = "trippostcollect_cookie_snapshot.json"
XHS_OPERATOR_LOGIN_WAIT_SECONDS = 600
FORMAL_SQLITE_BUSY_TIMEOUT_MS = 60_000
PROCESS_PROGRESS_POLL_SECONDS = 5.0
PROCESS_CLEANUP_GRACE_SECONDS = 20.0
PROCESS_FINAL_REAP_SECONDS = 5.0
XHS_NETWORK_DIAGNOSTIC_MAX_AGE_SECONDS = 90.0
XHS_NETWORK_DIAGNOSTIC_MAX_BYTES = 512 * 1024
XHS_RUNTIME_STATUS_AUTH_KEY_RE = re.compile(r"[0-9a-f]{64}\Z")
XHS_RUNTIME_STATUS_RUN_ID_ENV = "TRIPPOSTCOLLECT_XHS_RUN_ID"
BILIBILI_ARTICLE_SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
BILIBILI_ARTICLE_DETAIL_URL = "https://api.bilibili.com/x/article/view"
BILIBILI_RELATION_STAT_URL = "https://api.bilibili.com/x/relation/stat"
BILIBILI_ARTICLE_PAGE_SIZE = 20
BILIBILI_DETAIL_MAX_ATTEMPTS = 3
BILIBILI_DETAIL_RETRY_DELAY_SECONDS = (4.0, 7.0)
BILIBILI_DETAIL_PACING_SECONDS = (1.5, 3.0)
BILIBILI_DETAIL_RETRYABLE_CODES = frozenset({-509, -412, -352})
BILIBILI_RUNTIME_BLOCKING_CODES = frozenset({-101, -509, -412, -352})
BILIBILI_IMAGE_MAX_ATTEMPTS = 3
BILIBILI_IMAGE_RETRY_DELAY_SECONDS = (1.0, 2.0)
RETRYABLE_IMAGE_ERROR_CODES = frozenset({"image_download_retryable"})
RUNTIME_BLOCKING_IMAGE_ERROR_CODES = frozenset(
    {"image_auth_required", "image_rate_limited"}
)
LEGACY_ZHIHU_TRANSFORM_SUFFIX_RE = re.compile(
    r"_(?:b|r|qhd|hd|xs|s|m|l|xl|xxl|original|watermark)"
    r"\.(?:avif|gif|jpe?g|png|webp)$",
    re.IGNORECASE,
)
RASTER_IMAGE_SUFFIX_RE = re.compile(
    r"\.(?:avif|gif|jpe?g|png|webp)$",
    re.IGNORECASE,
)
BILIBILI_TRUSTED_DETAIL_SOURCES = frozenset({"article_view_api"})
BILIBILI_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
BILIBILI_HTML_IMAGE_RE = re.compile(
    r"<(?:img|source)\b[^>]*?\b(?:src|data-src|data-original)\s*=\s*"
    r"(?:[\"']([^\"']+)[\"']|([^\s>]+))",
    re.I,
)
BILIBILI_WBI_MIXIN_TABLE = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)


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


def is_retryable_image_error(code: str | None) -> bool:
    return str(code or "") in RETRYABLE_IMAGE_ERROR_CODES


def is_runtime_blocking_image_error(code: str | None) -> bool:
    return str(code or "") in RUNTIME_BLOCKING_IMAGE_ERROR_CODES

PLATFORMS: dict[str, dict[str, str]] = {
    "bilibili": {"mediacrawler": "bili", "label": "B站"},
    "xhs": {"mediacrawler": "xhs", "label": "小红书"},
    "weibo": {"mediacrawler": "wb", "label": "微博"},
    "douyin": {"mediacrawler": "dy", "label": "抖音"},
    "zhihu": {"mediacrawler": "zhihu", "label": "知乎"},
}
TRUSTED_CONTENT_DETAIL_SOURCES = {
    "bilibili": BILIBILI_TRUSTED_DETAIL_SOURCES,
    "weibo": frozenset({"search_mblog_complete", "mobile_detail"}),
    "xhs": frozenset({"note_detail"}),
    "douyin": frozenset({"aweme_detail"}),
    "zhihu": frozenset({"search_content", "answer_detail", "article_detail"}),
}
FOLLOWERS_REQUIRED_PLATFORMS = frozenset(PLATFORMS)
REQUIRED_FOLLOWER_SOURCES = {
    "bilibili": frozenset({"relation_stat"}),
    "weibo": frozenset({"search_author"}),
    "xhs": frozenset({"creator_profile"}),
    "douyin": frozenset({"creator_profile"}),
    "zhihu": frozenset({"search_author"}),
}
PLATFORM_REQUIRED_METRICS: dict[str, tuple[tuple[str, ...], ...]] = {
    "bilibili": (("liked_count",), ("comment_count", "comments_count"), ("view_count", "video_play_count")),
    "weibo": (("liked_count",), ("comments_count", "comment_count"), ("shared_count", "reposts_count")),
    "xhs": (("liked_count",), ("collected_count",), ("comment_count",), ("share_count",)),
    "douyin": (("liked_count",), ("collected_count",), ("comment_count",), ("share_count",)),
    "zhihu": (("voteup_count", "liked_count"), ("comment_count", "comments_count")),
}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".svg", ".img"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
AUTHOR_FIELD_MARKERS = ("author", "user", "nickname", "avatar", "fans", "follower", "follow", "up")
VIDEO_URL_RE = re.compile(r"(?i)(?:/(?:video|share/video)/\d+|\.(?:mp4|m4v|mov|webm|flv|m3u8|mpd)(?:[?#]|$))")
CHINA_TZ = timezone(timedelta(hours=8))
TIMESTAMP_MIN = 946_684_800
TIMESTAMP_MAX = 4_102_444_800
DATETIME_TEXT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日",
)
PUBLISHED_AT_KEYS = (
    "published_at",
    "date_published",
    "datePublished",
    "publish_at",
    "publish_time",
    "publish_date",
    "pub_time",
    "pubdate",
    "created_at",
    "created_time",
    "create_date_time",
    "create_time",
    "ctime",
    "timestamp",
    "time",
)
NESTED_PUBLISHED_AT_KEYS = tuple(key for key in PUBLISHED_AT_KEYS if key not in {"time", "timestamp"})
SAMPLE_KEYS = (
    "title",
    "desc",
    "content",
    "published_at",
    "create_time",
    "publish_time",
    "time",
    "create_date_time",
    "note_id",
    "aweme_id",
    "content_id",
    "content_type",
    "video_id",
    "bvid",
    "aid",
    "note_url",
    "video_url",
    "content_url",
    "created_time",
    "updated_time",
    "user_id",
    "nickname",
    "user_nickname",
    "fans",
    "fans_count",
    "followers_count",
    "following_count",
    "aweme_count",
    "author_liked_count",
    "author_followers_source",
    "liked_count",
    "voteup_count",
    "collected_count",
    "comment_count",
    "comments_count",
    "share_count",
    "shared_count",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run MediaCrawler for supported structured social platforms.")
    parser.add_argument("--keyword", default="青岛旅游", help="Qingdao search keyword.")
    parser.add_argument(
        "--platforms",
        nargs="+",
        default=["weibo", "douyin"],
        help="bilibili weibo douyin zhihu; XHS is a low-level target selected only by xhs_runner.py",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT), help="Output root.")
    parser.add_argument(
        "--timeout-per-platform",
        type=int,
        default=180,
        help="Maximum seconds without durable MediaCrawler progress.",
    )
    parser.add_argument("--login-type", default="cookie", choices=("cookie", "qrcode", "phone"), help="MediaCrawler login type.")
    parser.add_argument("--get-media", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--download-images",
        action="store_true",
        help="Download and verify authoritative post-body images for all selected platforms. Videos remain disabled.",
    )
    parser.add_argument(
        "--media-root",
        default=str(LOCAL_MEDIA_ROOT),
        help="Immutable local image root. Overrides are restricted to the project temp directory.",
    )
    parser.add_argument("--headed", action="store_true", help="Run browser with visible UI.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path for web_posts import.")
    parser.add_argument("--required-fields-profile", default="image_post_with_followers_v1")
    parser.add_argument("--behavior-profile", default="social_high_risk")
    parser.add_argument("--xhs-account-id", help="Required isolated account id for the XHS low-level executor.")
    parser.add_argument("--xhs-profile-dir", help="Required run-scoped browser profile for XHS.")
    parser.add_argument("--xhs-discovery-target-key", help=argparse.SUPPRESS)
    parser.add_argument("--xhs-discovery-query-fingerprint", help=argparse.SUPPRESS)
    parser.add_argument(
        "--xhs-post-interaction",
        choices=("none", "comment-scroll", "like-one", "random"),
        default="none",
        help="Optional one-post visible XHS interaction selected by xhs_runner.py.",
    )
    parser.add_argument("--start-page", type=int, default=1, help="Recovery-only first platform page.")
    parser.add_argument("--start-offset", type=int, default=0, help="Saved platform offset for the discovery frontier.")
    parser.add_argument("--start-cursor", default="", help="Saved opaque platform cursor for the discovery frontier.")
    parser.add_argument("--resume-summary", help="Recovery-only prior summary whose JSONL records join this run.")
    parser.add_argument("--discovery-job-id", type=int)
    parser.add_argument("--discovery-query-fingerprint")
    parser.add_argument("--discovery-run-id")
    parser.add_argument("--top-refresh-max-pages", type=int, default=0)
    parser.add_argument("--discovery-source-exhausted", action="store_true")
    parser.add_argument("--no-checkpoint-write", action="store_true")
    parser.add_argument("--no-import", action="store_true", help="Do not import MediaCrawler JSONL records into SQLite.")
    parser.add_argument(
        "--zhihu-detail-urls-file",
        help="Diagnostic-only JSON array of Zhihu answer/article URLs to inspect via detail mode.",
    )
    parser.add_argument(
        "--xhs-detail-urls-file",
        help="Existing XHS note URLs to inspect via detail mode; only allowed with --xhs-repair.",
    )
    parser.add_argument(
        "--xhs-repair-target-ids-file",
        help="JSON array of existing XHS post IDs allowed in --xhs-repair mode.",
    )
    parser.add_argument(
        "--xhs-repair",
        action="store_true",
        help="Repair existing XHS rows from specified note detail URLs without discovery writes.",
    )
    parser.add_argument(
        "--xhs-repair-batch-size",
        type=int,
        default=5,
        help="Number of isolated XHS repair candidates processed per in-browser batch.",
    )
    parser.add_argument(
        "--post-repair",
        action="store_true",
        help=(
            "Repair an explicit allowlist of existing Douyin, Weibo, or Zhihu rows "
            "through platform detail mode without discovery writes."
        ),
    )
    parser.add_argument(
        "--repair-targets-file",
        help=(
            "Frozen JSON array of repair target objects containing platform_post_id, "
            "detail_target, and keyword; requires --post-repair."
        ),
    )
    return parser.parse_args()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f%z")


def selected_platforms(values: list[str]) -> list[str]:
    unknown = sorted(set(values) - set(PLATFORMS))
    if unknown:
        raise SystemExit(f"Unsupported MediaCrawler platform in this project: {', '.join(unknown)}")
    return values


def load_zhihu_detail_urls(path_value: str | Path) -> list[str]:
    path = Path(path_value).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid Zhihu detail URL file: {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise SystemExit("Zhihu detail URL file must contain a JSON array")

    urls: list[str] = []
    for value in payload:
        url = str(value or "").strip().split("#", 1)[0].split("?", 1)[0]
        parsed = urlparse(url)
        answer_url = bool(
            parsed.hostname in {"zhihu.com", "www.zhihu.com"}
            and re.fullmatch(r"/question/[^/]+/answer/[^/]+/?", parsed.path)
        )
        article_url = bool(
            parsed.hostname == "zhuanlan.zhihu.com"
            and re.fullmatch(r"/p/[^/]+/?", parsed.path)
        )
        if not (parsed.scheme == "https" and (answer_url or article_url)):
            raise SystemExit(f"unsupported Zhihu detail URL: {url or value!r}")
        if url not in urls:
            urls.append(url)
    if not urls:
        raise SystemExit("Zhihu detail URL file contains no answer/article URLs")
    return urls


def load_xhs_detail_urls(path_value: str | Path) -> list[str]:
    path = Path(path_value).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid XHS detail URL file: {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise SystemExit("XHS detail URL file must contain a JSON array")

    urls: list[str] = []
    for value in payload:
        raw_url = str(value or "").strip()
        parsed = urlparse(raw_url)
        if parsed.scheme != "https" or parsed.hostname not in {"xiaohongshu.com", "www.xiaohongshu.com"}:
            raise SystemExit(f"unsupported XHS detail URL: {raw_url!r}")
        if not re.fullmatch(r"/explore/[^/]+/?", parsed.path):
            raise SystemExit(f"unsupported XHS detail URL path: {raw_url!r}")
        query = dict(parse_qsl(parsed.query))
        if not query.get("xsec_token") or not query.get("xsec_source"):
            raise SystemExit(f"XHS detail URL must contain xsec_token and xsec_source: {raw_url!r}")
        normalized = parsed._replace(fragment="").geturl()
        if normalized not in urls:
            urls.append(normalized)
    if not urls:
        raise SystemExit("XHS detail URL file contains no URLs")
    return urls


def load_xhs_repair_target_ids(path_value: str | Path) -> set[str]:
    path = Path(path_value).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid XHS repair target ID file: {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise SystemExit("XHS repair target ID file must contain a JSON array")
    values = {str(value).strip() for value in payload if str(value).strip()}
    if not values:
        raise SystemExit("XHS repair target ID file contains no post IDs")
    return values


def load_post_repair_fallbacks(
    db_path: str | Path | None,
    platform_key: str,
    post_ids: set[str],
) -> dict[str, dict[str, Any]]:
    if not db_path or not post_ids:
        return {}
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        return {}
    placeholders = ",".join("?" for _ in post_ids)
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT *
                FROM web_posts
                WHERE platform_key=? AND platform_post_id IN ({placeholders})
                """,
                [platform_key, *sorted(post_ids)],
            ).fetchall()
    except sqlite3.Error:
        return {}

    fallbacks: dict[str, dict[str, Any]] = {}
    for row in rows:
        columns = set(row.keys())

        def row_value(key: str) -> Any:
            return row[key] if key in columns else None

        def available(*values: Any) -> Any:
            return next((value for value in values if value not in (None, "")), None)

        raw: dict[str, Any] = {}
        try:
            parsed = json.loads(str(row_value("raw_sample_json") or ""))
            if isinstance(parsed, dict):
                raw = parsed
        except (TypeError, json.JSONDecodeError):
            pass
        fallback = {
            key: value
            for key, value in {
                "keyword": row_value("keyword"),
                "published_at": row_value("published_at"),
                "author_followers_count": row_value("author_followers_count"),
                "author_display_name": row_value("author_display_name"),
                "author_platform_id": row_value("author_platform_id"),
                "author_profile_url": row_value("author_profile_url"),
                "author_description": row_value("author_description"),
                "created_time": raw.get("created_time"),
                "updated_time": raw.get("updated_time"),
                "creator_hash": raw.get("creator_hash"),
                "creator_url_token": raw.get("creator_url_token"),
                "user_nickname": raw.get("user_nickname"),
                "author_followers_source": raw.get("author_followers_source"),
                "followers_observed": raw.get("followers_observed"),
                "followers_count": raw.get("followers_count"),
                "liked_count": available(
                    row_value("post_likes_count"),
                    raw.get("liked_count"),
                ),
                "collected_count": available(
                    row_value("post_favorites_count"),
                    raw.get("collected_count"),
                    raw.get("favorites_count"),
                ),
                "comment_count": available(
                    row_value("post_comments_count"),
                    raw.get("comment_count"),
                    raw.get("comments_count"),
                ),
                "share_count": available(
                    row_value("post_shares_count"),
                    raw.get("share_count"),
                    raw.get("shares_count"),
                ),
            }.items()
            if value not in (None, "")
        }
        fallbacks[str(row["platform_post_id"])] = fallback
    return fallbacks


def load_post_repair_targets(
    path_value: str | Path,
    platform_key: str,
    *,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    if platform_key not in {"douyin", "weibo", "zhihu"}:
        raise SystemExit(f"unsupported post repair platform: {platform_key}")
    path = Path(path_value).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid post repair target file: {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise SystemExit("post repair target file must contain a JSON array")

    targets: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    raw_targets: list[tuple[str, str, str, dict[str, Any]]] = []
    for index, value in enumerate(payload):
        if not isinstance(value, dict):
            raise SystemExit(f"post repair target {index} must be an object")
        post_id = str(value.get("platform_post_id") or "").strip()
        detail_target = str(value.get("detail_target") or "").strip()
        keyword = str(value.get("keyword") or "").strip()
        if not post_id or not detail_target or not keyword:
            raise SystemExit(
                f"post repair target {index} requires platform_post_id, detail_target, and keyword"
            )
        if post_id in seen_ids:
            raise SystemExit(f"duplicate post repair platform_post_id: {post_id}")

        if platform_key == "weibo":
            valid = detail_target == post_id and bool(re.fullmatch(r"[0-9A-Za-z]+", post_id))
        else:
            parsed = urlparse(detail_target.split("#", 1)[0].split("?", 1)[0])
            if platform_key == "douyin":
                valid = bool(
                    parsed.scheme == "https"
                    and parsed.hostname in {"douyin.com", "www.douyin.com"}
                    and re.fullmatch(rf"/(?:video|note)/{re.escape(post_id)}/?", parsed.path)
                )
            else:
                answer = bool(
                    parsed.hostname in {"zhihu.com", "www.zhihu.com"}
                    and re.fullmatch(
                        rf"/question/[^/]+/answer/{re.escape(post_id)}/?",
                        parsed.path,
                    )
                )
                article = bool(
                    parsed.hostname == "zhuanlan.zhihu.com"
                    and re.fullmatch(rf"/p/{re.escape(post_id)}/?", parsed.path)
                )
                valid = bool(parsed.scheme == "https" and (answer or article))
        if not valid:
            raise SystemExit(
                f"post repair target does not match {platform_key} ID {post_id}: {detail_target!r}"
            )
        seen_ids.add(post_id)
        payload_fallback = value.get("repair_fallback")
        raw_targets.append(
            (
                post_id,
                detail_target,
                keyword,
                payload_fallback if isinstance(payload_fallback, dict) else {},
            )
        )
    fallbacks = load_post_repair_fallbacks(
        db_path,
        platform_key,
        {post_id for post_id, _, _, _ in raw_targets},
    )
    for post_id, detail_target, keyword, payload_fallback in raw_targets:
        value = {
            "platform_post_id": post_id,
            "detail_target": detail_target,
            "keyword": keyword,
        }
        merged_fallback = {**payload_fallback, **fallbacks.get(post_id, {})}
        if merged_fallback:
            value["repair_fallback"] = merged_fallback
        targets.append(value)
    if not targets:
        raise SystemExit("post repair target file contains no targets")
    return targets


def ensure_prerequisites() -> None:
    if not (MEDIACRAWLER_DIR / "pyproject.toml").exists():
        raise SystemExit(f"MediaCrawler is missing or incomplete: {MEDIACRAWLER_DIR}")


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


def profile_dir_for(platform_key: str) -> Path:
    code = PLATFORMS[platform_key]["mediacrawler"]
    return MEDIACRAWLER_DIR / "browser_data" / f"{code}_user_data_dir"


def cookie_snapshot_path(platform_key: str) -> Path:
    return profile_dir_for(platform_key) / COOKIE_SNAPSHOT_FILENAME


def platform_cookie_url(platform_key: str) -> str:
    return {
        "bilibili": "https://www.bilibili.com/",
        "weibo": "https://m.weibo.cn/",
        "xhs": "https://www.xiaohongshu.com/",
        "douyin": "https://www.douyin.com/",
        "zhihu": "https://www.zhihu.com/",
    }[platform_key]


def required_cookie_names(platform_key: str) -> tuple[str, ...]:
    if platform_key == "zhihu":
        return ("d_c0", "z_c0")
    return ()


def cookie_names_from_header(cookie_header: str) -> list[str]:
    names = []
    for item in cookie_header.split(";"):
        if "=" not in item:
            continue
        name = item.split("=", 1)[0].strip()
        if name:
            names.append(name)
    return sorted(set(names))


def cookies_to_header(cookies: list[dict[str, Any]]) -> str:
    pairs = []
    now = time.time()
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = item.get("value")
        if not name or value in (None, ""):
            continue
        expires = item.get("expires")
        if isinstance(expires, (int, float)) and expires > 0 and expires < now:
            continue
        pairs.append(f"{name}={value}")
    return ";".join(pairs)


def load_cookie_snapshot(platform_key: str) -> dict[str, Any] | None:
    path = cookie_snapshot_path(platform_key)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cookies = payload.get("cookies")
    if not isinstance(cookies, list):
        return None
    cookie_header = cookies_to_header(cookies)
    names = cookie_names_from_header(cookie_header)
    missing = [name for name in required_cookie_names(platform_key) if name not in names]
    if missing:
        return None
    return {
        "cookie_header": cookie_header,
        "source": "snapshot",
        "snapshot_path": str(path),
        "saved_at": payload.get("saved_at"),
        "cookie_names": names,
        "required_cookie_names": list(required_cookie_names(platform_key)),
    }


def public_cookie_export(cookie_export: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in cookie_export.items() if key != "cookie_header"}


def export_profile_cookies(platform_key: str, browser_path: str | None) -> dict[str, Any] | None:
    snapshot = load_cookie_snapshot(platform_key)
    if snapshot:
        return snapshot

    profile_dir = profile_dir_for(platform_key)
    if not profile_dir.exists():
        return None
    script = r"""
import asyncio
import json
import sys
from playwright.async_api import async_playwright

async def main() -> int:
    profile_dir = sys.argv[1]
    executable_path = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
    target_url = sys.argv[3]
    runtime_args = json.loads(sys.argv[4])
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=True,
            executable_path=executable_path,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=["--disable-dev-shm-usage", "--no-sandbox", *runtime_args],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        cookies = await context.cookies([target_url])
        await context.close()
        sys.stdout.write(";".join(f"{item['name']}={item.get('value', '')}" for item in cookies))
    return 0

raise SystemExit(asyncio.run(main()))
"""
    cmd = [sys.executable, "-c", script, str(profile_dir)]
    if browser_path:
        cmd.append(browser_path)
    else:
        cmd.append("")
    cmd.append(platform_cookie_url(platform_key))
    cmd.append(json.dumps(browser_runtime_args()))
    try:
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
            check=False,
            env=browser_launch_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    cookie_str = result.stdout.strip()
    names = cookie_names_from_header(cookie_str)
    missing = [name for name in required_cookie_names(platform_key) if name not in names]
    if result.returncode != 0 or missing:
        return None
    return {
        "cookie_header": cookie_str,
        "source": "live_profile",
        "snapshot_path": str(cookie_snapshot_path(platform_key)),
        "saved_at": None,
        "cookie_names": names,
        "required_cookie_names": list(required_cookie_names(platform_key)),
    }


def load_behavior_evidence(path: str | Path) -> dict[str, Any]:
    candidate = Path(path).expanduser()
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "missing",
            "evidence_path": str(candidate),
            "error": "behavior_evidence_missing_or_invalid",
        }
    return value if isinstance(value, dict) else {
        "status": "invalid",
        "evidence_path": str(candidate),
        "error": "behavior_evidence_not_an_object",
    }


def behavior_environment(evidence_path: Path, profile_name: str) -> dict[str, str]:
    return {
        "TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_ENABLED": "1",
        "TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE": profile_name,
        "TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE": str(evidence_path),
        "TRIPPOSTCOLLECT_PROJECT_SCRIPTS": str(ROOT / "scripts"),
        "TRIPPOSTCOLLECT_BROWSER_ARGS_JSON": json.dumps(browser_runtime_args()),
    }


async def run_bilibili_behavior_session(
    args: argparse.Namespace,
    evidence_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    browser_path = discover_cdp_browser_path()
    profile_dir = ensure_dir(profile_dir_for("bilibili"))
    stealth_script = MEDIACRAWLER_DIR / "libs" / "stealth.min.js"
    target_url = "https://search.bilibili.com/article?keyword=" + quote(args.keyword)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=not args.headed,
            executable_path=browser_path,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900},
            screen={"width": 1440, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                *browser_runtime_args(),
            ],
            env=browser_launch_environment(),
            ignore_default_args=["--enable-automation"],
        )
        try:
            if stealth_script.is_file():
                await context.add_init_script(path=str(stealth_script))
            await install_runtime_hints(context)

            async def block_video_media(route) -> None:
                request = route.request
                if request.resource_type == "media" or VIDEO_URL_RE.search(request.url):
                    await route.abort()
                    return
                await route.continue_()

            await context.route("**/*", block_video_media)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
            evidence = await run_page_behavior(
                page,
                platform_key="bilibili",
                evidence_path=evidence_path,
                profile_name="social_high_risk",
            )
            cookies = await context.cookies([platform_cookie_url("bilibili")])
        finally:
            await context.close()

    cookie_header = cookies_to_header(cookies)
    cookie_export = {
        "cookie_header": cookie_header,
        "source": "human_behavior_session",
        "snapshot_path": str(cookie_snapshot_path("bilibili")),
        "saved_at": evidence.get("finished_at"),
        "cookie_names": cookie_names_from_header(cookie_header),
        "required_cookie_names": list(required_cookie_names("bilibili")),
    }
    return cookie_export, evidence


def decode_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def tail(value: str, limit: int = 4000) -> str:
    return value[-limit:] if len(value) > limit else value


def json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "无"}:
        return None
    unit = 1
    if text.endswith("万"):
        unit = 10_000
        text = text[:-1]
    elif text.endswith("亿"):
        unit = 100_000_000
        text = text[:-1]
    try:
        return int(float(text) * unit)
    except ValueError:
        return None


def first_value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def timestamp_to_iso(value: Any) -> str | None:
    parsed = parse_int(value)
    if parsed is None:
        return None
    if parsed > 10_000_000_000:
        parsed = parsed // 1000
    if parsed < TIMESTAMP_MIN or parsed > TIMESTAMP_MAX:
        return None
    try:
        return datetime.fromtimestamp(parsed, CHINA_TZ).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def parse_datetime_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text or text in {"-", "无"}:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in DATETIME_TEXT_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            parsed = None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TZ)
    return parsed.astimezone(CHINA_TZ).isoformat(timespec="seconds")


def datetime_value_to_iso(value: Any) -> str | None:
    return timestamp_to_iso(value) or parse_datetime_text(value)


def iter_nested_values_for_keys(value: Any, keys: tuple[str, ...], *, depth: int = 0) -> list[Any]:
    if depth > 4:
        return []
    found: list[Any] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and child not in (None, ""):
                found.append(child)
            found.extend(iter_nested_values_for_keys(child, keys, depth=depth + 1))
    elif isinstance(value, list):
        for child in value:
            found.extend(iter_nested_values_for_keys(child, keys, depth=depth + 1))
    return found


def published_at_for_record(record: dict[str, Any]) -> str | None:
    for key in PUBLISHED_AT_KEYS:
        value = record.get(key)
        parsed = datetime_value_to_iso(value)
        if parsed:
            return parsed
    for value in iter_nested_values_for_keys(record, NESTED_PUBLISHED_AT_KEYS):
        parsed = datetime_value_to_iso(value)
        if parsed:
            return parsed
    return None


def merge_repair_fallback_metadata(
    platform_key: str,
    record: dict[str, Any],
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    if not metadata:
        return record
    merged = dict(record)

    if not published_at_for_record(merged):
        for key in ("published_at", "created_time"):
            value = metadata.get(key)
            if value not in (None, ""):
                merged["published_at" if key == "published_at" else key] = value
                if published_at_for_record(merged):
                    break

    fallback_observed = metadata.get("followers_observed") is True
    current_observed = merged.get("followers_observed") is True
    if fallback_observed and not current_observed:
        for key in (
            "followers_count",
            "author_followers_count",
            "followers_observed",
            "author_followers_source",
        ):
            if metadata.get(key) not in (None, ""):
                merged[key] = metadata[key]
    else:
        for key in (
            "followers_count",
            "author_followers_count",
            "author_followers_source",
        ):
            if merged.get(key) in (None, "", 0) and metadata.get(key) not in (None, ""):
                merged[key] = metadata[key]

    for key in (
        "creator_hash",
        "creator_url_token",
        "user_nickname",
        "author_profile_url",
        "author_description",
        "author_display_name",
        "author_platform_id",
    ):
        if merged.get(key) in (None, "") and metadata.get(key) not in (None, ""):
            merged[key] = metadata[key]

    if platform_key == "xhs":
        metric_fallbacks: dict[str, dict[str, Any]] = {}
        for key in ("liked_count", "collected_count", "comment_count", "share_count"):
            if first_value(merged, key) is None and metadata.get(key) not in (None, ""):
                merged[key] = metadata[key]
                metric_fallbacks[key] = {
                    "source": "existing_web_posts_metric",
                    "value": metadata[key],
                }
        if metric_fallbacks:
            prior_evidence = merged.get("repair_fallback_evidence")
            evidence = dict(prior_evidence) if isinstance(prior_evidence, dict) else {}
            evidence["metrics"] = metric_fallbacks
            merged["repair_fallback_evidence"] = evidence

    return merged


def is_video_record(platform_key: str, record: dict[str, Any]) -> bool:
    if platform_key == "bilibili" and first_value(record, "video_id", "video_url", "bvid", "aid"):
        return True
    if platform_key == "zhihu":
        content_type = str(first_value(record, "content_type", "type") or "").strip().lower()
        content_url = str(first_value(record, "content_url", "url", "share_url") or "")
        if "zvideo" in content_type or content_type == "video" or "/zvideo/" in content_url:
            return True
    if platform_key == "douyin":
        note_images = first_value(record, "note_download_url", "image_list", "images")
        aweme_type = str(first_value(record, "aweme_type", "video_type", "media_type", "type") or "").strip().lower()
        if note_images or aweme_type in {"68", "note", "image", "images", "image_text", "图文"}:
            return False
        if first_value(record, "video_download_url"):
            return True
    video_type = str(first_value(record, "video_type", "media_type", "type") or "").strip().lower()
    if video_type and video_type not in {"note", "image", "images", "image_text", "图文"}:
        if "video" in video_type or "视频" in video_type:
            return True
    for key, value in record.items():
        key_lower = str(key).lower()
        if "video" not in key_lower and key_lower not in {"bvid", "aid"}:
            continue
        if value not in (None, "") and VIDEO_URL_RE.search(str(value)):
            return True
    for value in (first_value(record, "url", "share_url", "note_url", "aweme_url", "video_url"),):
        if value and VIDEO_URL_RE.search(str(value)):
            return True
    return False


_XHS_TRANSPORT_MARKER_REASONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "internet_disconnected",
        (
            "net::err_internet_disconnected",
            "network is unreachable",
            "no route to host",
        ),
    ),
    (
        "dns_unreachable",
        (
            "net::err_name_not_resolved",
            "temporary failure in name resolution",
            "name or service not known",
        ),
    ),
    ("network_changed", ("net::err_network_changed",)),
    (
        "connection_reset",
        (
            "net::err_connection_reset",
            "net::err_connection_closed",
            "connection reset by peer",
        ),
    ),
    (
        "connection_refused",
        ("net::err_connection_refused", "connection refused"),
    ),
    (
        "address_unreachable",
        ("net::err_address_unreachable",),
    ),
    (
        "proxy_unreachable",
        (
            "net::err_proxy_connection_failed",
            "net::err_tunnel_connection_failed",
        ),
    ),
    ("transport_timeout", ("net::err_timed_out",)),
)
_XHS_TRANSPORT_ERROR_TYPE_REASONS = {
    "ConnectError": "internet_disconnected",
    "ConnectTimeout": "transport_timeout",
    "NetworkError": "internet_disconnected",
    "PlaywrightTimeoutError": "transport_timeout",
    "PoolTimeout": "transport_timeout",
    "ProxyError": "proxy_unreachable",
    "ReadError": "connection_reset",
    "ReadTimeout": "transport_timeout",
    "RemoteProtocolError": "connection_reset",
    "TimeoutError": "transport_timeout",
    "WriteError": "connection_reset",
    "WriteTimeout": "transport_timeout",
}


def _diagnostic_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _fresh_diagnostic_timestamp(
    value: object,
    *,
    now: datetime,
    max_age_seconds: float,
) -> bool:
    parsed = _diagnostic_timestamp(value)
    return bool(
        parsed is not None
        and parsed <= now + timedelta(seconds=5)
        and parsed >= now - timedelta(seconds=max_age_seconds)
    )


def _sanitized_transport_reason(error: str) -> str:
    detail = error.casefold()
    for reason, markers in _XHS_TRANSPORT_MARKER_REASONS:
        if any(marker in detail for marker in markers):
            return reason
    error_type, separator, _ = error.partition(":")
    if not separator:
        return ""
    return _XHS_TRANSPORT_ERROR_TYPE_REASONS.get(error_type.strip(), "")


def xhs_network_state_from_diagnostics(
    path: str | Path | None,
    *,
    now: datetime | None = None,
    max_age_seconds: float = XHS_NETWORK_DIAGNOSTIC_MAX_AGE_SECONDS,
) -> tuple[str, str]:
    """Return only a fresh, explicitly recoverable XHS transport state."""

    if path is None or max_age_seconds <= 0:
        return "unknown", ""
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("network diagnostic reference time must be timezone-aware")
    observed_at = observed_at.astimezone(timezone.utc)
    candidate = Path(path).expanduser()
    try:
        if candidate.is_symlink():
            return "unknown", ""
        file_stat = candidate.stat()
        if not candidate.is_file() or file_stat.st_size > XHS_NETWORK_DIAGNOSTIC_MAX_BYTES:
            return "unknown", ""
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return "unknown", ""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "platform", "updated_at", "events"}
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
        or payload.get("platform") != "xhs"
        or not _fresh_diagnostic_timestamp(
            payload.get("updated_at"),
            now=observed_at,
            max_age_seconds=max_age_seconds,
        )
    ):
        return "unknown", ""
    events = payload.get("events")
    if (
        not isinstance(events, list)
        or not events
        or len(events) > 30
        or not isinstance(events[-1], dict)
    ):
        return "unknown", ""
    event = events[-1]
    if not isinstance(event.get("stage"), str) or not event["stage"].strip():
        return "unknown", ""
    if not _fresh_diagnostic_timestamp(
        event.get("at"),
        now=observed_at,
        max_age_seconds=max_age_seconds,
    ):
        return "unknown", ""
    outcome = event.get("outcome")
    error = event.get("error")
    if outcome == "network_recovered" and error in (None, ""):
        return "online", ""
    if (
        outcome != "network_paused"
        or not isinstance(error, str)
        or not error
        or len(error) > 500
    ):
        return "unknown", ""
    reason = _sanitized_transport_reason(error)
    return ("network_paused", reason) if reason else ("unknown", "")


class XhsRuntimeSupervisionError(RuntimeError):
    """Raised when authenticated XHS runtime supervision can no longer continue."""


class XhsSupervisorRuntimeReporter:
    """Synchronous, single-writer status reporter owned by this supervisor."""

    _PHASE_ORDER = {"starting": 0, "running": 1, "finalizing": 2}

    def __init__(
        self,
        *,
        run_id: str,
        account_id: str,
        lease_id: str,
        status_path: Path,
        auth_key: bytes,
        writer_identity: ProcessIdentity,
        inspector: SystemProcessInspector,
    ) -> None:
        self.run_id = run_id
        self.account_id = account_id
        self.lease_id = lease_id
        self.status_path = status_path
        self._auth_key = auth_key
        self.writer_identity = writer_identity
        self._inspector = inspector
        self._phase = "starting"
        self._sequence = 0
        self._last_written_sequence = 0
        self._write_failures = 0
        self._last_write_error = ""

    @property
    def phase(self) -> str:
        return self._phase

    def _writer_identity_is_current(self) -> bool:
        return self._inspector.identity(self.writer_identity.pid) == self.writer_identity

    def checkpoint(
        self,
        *,
        phase: str | None = None,
        network_state: str = "unknown",
        network_reason: str = "",
    ) -> bool:
        requested_phase = phase or self._phase
        if requested_phase not in self._PHASE_ORDER:
            raise ValueError(f"unsupported runtime status phase: {requested_phase}")
        if self._PHASE_ORDER[requested_phase] < self._PHASE_ORDER[self._phase]:
            raise ValueError("runtime status phase cannot move backwards")
        if not self._writer_identity_is_current():
            self._write_failures += 1
            self._last_write_error = "writer_identity_changed"
            raise XhsRuntimeSupervisionError(
                "xhs_runtime_status_writer_identity_changed"
            )
        self._phase = requested_phase
        self._sequence += 1
        identity = self.writer_identity
        unsigned = {
            "schema_version": RUNTIME_STATUS_SCHEMA_VERSION,
            "run_id": self.run_id,
            "account_id": self.account_id,
            "lease_id": self.lease_id,
            "writer_role": "mediacrawler_supervisor",
            "writer_host_id": identity.host_id,
            "writer_boot_id": identity.boot_id,
            "writer_pid": identity.pid,
            "writer_process_started_at": identity.process_started_at,
            "writer_process_start_token": identity.process_start_token,
            "writer_pgid": identity.pgid,
            "sequence": self._sequence,
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "phase": self._phase,
            "network_state": network_state,
            "network_reason": network_reason,
        }
        try:
            signed = sign_runtime_status(unsigned, auth_key=self._auth_key)
            write_runtime_status_atomic(
                self.status_path,
                signed,
                auth_key=self._auth_key,
            )
        except (OSError, RuntimeStatusValidationError) as exc:
            self._write_failures += 1
            self._last_write_error = "runtime_status_write_failed"
            raise XhsRuntimeSupervisionError(
                "xhs_runtime_status_write_failed"
            ) from exc
        self._last_written_sequence = self._sequence
        self._last_write_error = ""
        return True

    def checkpoint_from_diagnostics(
        self,
        path: str | Path | None,
        *,
        phase: str | None = None,
    ) -> bool:
        network_state, network_reason = xhs_network_state_from_diagnostics(path)
        return self.checkpoint(
            phase=phase,
            network_state=network_state,
            network_reason=network_reason,
        )

    def enter_finalizing(self) -> bool:
        return self.checkpoint(phase="finalizing")

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "status_path": str(self.status_path),
            "phase": self._phase,
            "attempted_sequence": self._sequence,
            "last_written_sequence": self._last_written_sequence,
            "write_failures": self._write_failures,
            "last_write_error": self._last_write_error,
        }


def xhs_supervisor_runtime_reporter_from_context(
    args: argparse.Namespace,
    *,
    environ: MutableMapping[str, str] | None = None,
    inspector: SystemProcessInspector | None = None,
) -> XhsSupervisorRuntimeReporter | None:
    """Consume the one-run auth key and construct the exact child reporter."""

    source = os.environ if environ is None else environ
    raw_auth_key = source.pop(RUNTIME_STATUS_AUTH_KEY_ENV, "")
    lease_values = {
        LEASE_DB_ENV: str(source.get(LEASE_DB_ENV) or ""),
        LEASE_ID_ENV: str(source.get(LEASE_ID_ENV) or ""),
        LEASE_OWNER_TOKEN_ENV: str(source.get(LEASE_OWNER_TOKEN_ENV) or ""),
        XHS_RUNTIME_STATUS_RUN_ID_ENV: str(
            source.get(XHS_RUNTIME_STATUS_RUN_ID_ENV) or ""
        ),
    }
    runtime_markers = [raw_auth_key, *lease_values.values()]
    if not any(runtime_markers):
        return None
    missing = [
        key
        for key, value in {
            RUNTIME_STATUS_AUTH_KEY_ENV: raw_auth_key,
            **lease_values,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeStatusValidationError(
            f"incomplete XHS runtime reporter environment: missing={sorted(missing)}"
        )
    if not XHS_RUNTIME_STATUS_AUTH_KEY_RE.fullmatch(raw_auth_key):
        raise RuntimeStatusValidationError(
            "XHS runtime status auth key must be exactly 64 lowercase hexadecimal characters"
        )
    account_id = str(getattr(args, "xhs_account_id", "") or "")
    profile_value = str(getattr(args, "xhs_profile_dir", "") or "")
    if not account_id or not profile_value:
        raise RuntimeStatusValidationError(
            "XHS runtime reporter requires account and run-scoped profile arguments"
        )
    profile = Path(profile_value).expanduser()
    if profile.name != "profile" or profile.is_symlink():
        raise RuntimeStatusValidationError(
            "XHS runtime reporter requires the exact non-symlink run profile"
        )
    profile = profile.resolve()
    run_id = profile.parent.name
    paths = runtime_session_paths(run_id)
    if (
        profile != paths["profile"].expanduser().resolve()
        or not paths["root"].is_dir()
        or paths["root"].is_symlink()
        or not profile.is_dir()
    ):
        raise RuntimeStatusValidationError(
            "XHS runtime reporter profile does not match its exact runtime session"
        )
    environment_run_id = lease_values[XHS_RUNTIME_STATUS_RUN_ID_ENV]
    if environment_run_id != run_id:
        raise RuntimeStatusValidationError(
            "XHS runtime reporter run id does not match its exact runtime session"
        )
    process_inspector = inspector or SystemProcessInspector()
    reporter = XhsSupervisorRuntimeReporter(
        run_id=run_id,
        account_id=account_id,
        lease_id=lease_values[LEASE_ID_ENV],
        status_path=runtime_status_path(run_id),
        auth_key=bytes.fromhex(raw_auth_key),
        writer_identity=process_inspector.current_identity(),
        inspector=process_inspector,
    )
    reporter.checkpoint()
    return reporter


def _runtime_progress(callback: Callable[[], object] | None) -> None:
    if callback is not None:
        callback()


def progress_path_signature(paths: Iterable[Path]) -> tuple[tuple[str, int, int], ...]:
    files: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        try:
            if path.is_dir():
                files.update(candidate for candidate in path.rglob("*.jsonl") if candidate.is_file())
            elif path.is_file():
                files.add(path)
        except OSError:
            continue
    signature: list[tuple[str, int, int]] = []
    for path in sorted(files):
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append((str(path), int(stat.st_size), int(stat.st_mtime_ns)))
    return tuple(signature)


def process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_managed_process(
    proc: subprocess.Popen[bytes],
    *,
    grace_seconds: float,
) -> tuple[bytes | None, bytes | None, bool]:
    partial_stdout: bytes | None = None
    partial_stderr: bytes | None = None
    complete_stdout: bytes | None = None
    complete_stderr: bytes | None = None
    forced = False
    deadline = time.monotonic() + max(0.0, grace_seconds)
    if proc.poll() is None:
        proc.terminate()
    remaining = max(0.01, deadline - time.monotonic())
    try:
        complete_stdout, complete_stderr = proc.communicate(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        partial_stdout = exc.stdout
        partial_stderr = exc.stderr
    while process_group_exists(proc.pid) and time.monotonic() < deadline:
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    if process_group_exists(proc.pid):
        forced = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if complete_stdout is None or complete_stderr is None:
        try:
            final_stdout, final_stderr = proc.communicate(
                timeout=PROCESS_FINAL_REAP_SECONDS
            )
        except subprocess.TimeoutExpired as exc:
            final_stdout = exc.stdout
            final_stderr = exc.stderr
        complete_stdout = final_stdout if final_stdout is not None else partial_stdout
        complete_stderr = final_stderr if final_stderr is not None else partial_stderr
    return complete_stdout, complete_stderr, forced


def append_no_progress_timeout_event(
    state_path: str | Path | None,
    *,
    platform_key: str,
    start_page: int,
    start_offset: int | None,
    start_cursor: str | None,
    inactivity_timeout_seconds: float,
    last_progress_age_seconds: float,
) -> dict[str, Any]:
    if not state_path:
        return {"skipped": True, "reason": "execution_state_unavailable"}
    state = FrozenExecutionState(state_path)
    try:
        payload = state.load()
        events = [event for event in payload.get("events") or [] if isinstance(event, dict)]
        if any(event.get("type") == "adaptive_search_stopped" for event in events):
            return {"skipped": True, "reason": "terminal_event_already_present"}
        latest_batch: dict[str, Any] = {}
        for event in events:
            if event.get("type") != "adaptive_batch_completed":
                continue
            details = event.get("details")
            if isinstance(details, dict):
                latest_batch = details
        details = {
            key: latest_batch.get(key)
            for key in PAGINATION_EVENT_FIELDS
            if key in latest_batch
        }
        details.update(
            {
                "platform": platform_key,
                "source_page": latest_batch.get("source_page", start_page),
                "source_offset": latest_batch.get("source_offset", start_offset),
                "source_cursor": latest_batch.get("source_cursor", start_cursor),
                "resume_page": latest_batch.get("resume_page", start_page),
                "resume_offset": latest_batch.get("resume_offset", start_offset),
                "resume_cursor": latest_batch.get("resume_cursor", start_cursor),
                "source_has_more": True,
                "batch_complete": False,
                "stop_reason": "runtime_failed",
                "stop_detail": "no_progress_timeout",
                "inactivity_timeout_seconds": round(inactivity_timeout_seconds, 3),
                "last_progress_age_seconds": round(last_progress_age_seconds, 3),
            }
        )
        state.append_event("adaptive_search_stopped", details)
    except Exception as exc:
        return {"skipped": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"skipped": False, "event": "adaptive_search_stopped", "details": details}


def run_command(
    cmd: list[str],
    cwd: Path,
    timeout: float,
    log_dir: Path,
    *,
    extra_env: dict[str, str] | None = None,
    progress_paths: Iterable[Path] | None = None,
    runtime_reporter: XhsSupervisorRuntimeReporter | None = None,
    network_diagnostics_path: str | Path | None = None,
    startup_grace_seconds: float = 0.0,
    poll_seconds: float = PROCESS_PROGRESS_POLL_SECONDS,
    cleanup_grace_seconds: float = PROCESS_CLEANUP_GRACE_SECONDS,
) -> dict[str, Any]:
    log_dir = ensure_dir(log_dir)
    env = browser_launch_environment()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("UV_CACHE_DIR", str(ensure_dir(UV_CACHE_ROOT)))
    if extra_env:
        env.update(extra_env)
    env.pop(RUNTIME_STATUS_AUTH_KEY_ENV, None)
    registration_env = dict(env)
    child_env = dict(registration_env)
    for private_lease_key in (LEASE_DB_ENV, LEASE_ID_ENV, LEASE_OWNER_TOKEN_ENV):
        child_env.pop(private_lease_key, None)
    started = time.monotonic()
    stdout = ""
    stderr = ""
    returncode = 0
    timed_out = False
    timeout_reason: str | None = None
    forced_termination = False
    progress_observed = False
    last_progress_at = started
    last_progress_age_seconds = 0.0
    tracked_paths = tuple(progress_paths) if progress_paths is not None else None
    if runtime_reporter is not None and tracked_paths is not None:
        runtime_status_file = runtime_reporter.status_path.expanduser().absolute()
        tracked_paths = tuple(
            path
            for path in tracked_paths
            if Path(path).expanduser().absolute() != runtime_status_file
        )
    progress_signature = (
        progress_path_signature(tracked_paths) if tracked_paths is not None else ()
    )
    proc: subprocess.Popen[bytes] | None = None
    lease_process_identity = None
    exporter_identity_mismatch = False
    lease_registration_enabled = all(
        str(registration_env.get(key) or "")
        for key in (LEASE_DB_ENV, LEASE_ID_ENV, LEASE_OWNER_TOKEN_ENV)
    )
    if runtime_reporter is not None and (
        tracked_paths is None or not lease_registration_enabled
    ):
        raise XhsRuntimeSupervisionError(
            "XHS runtime reporter requires progress tracking and lease registration"
        )
    process_inspector = (
        SystemProcessInspector()
        if lease_registration_enabled or runtime_reporter is not None
        else None
    )
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if lease_registration_enabled:
            try:
                lease_process_identity = register_lease_process_from_environment(
                    pid=proc.pid,
                    process_role="exporter",
                    environ=registration_env,
                    inspector=process_inspector,
                )
            except Exception:
                terminate_managed_process(
                    proc,
                    grace_seconds=cleanup_grace_seconds,
                )
                raise
        if runtime_reporter is not None and lease_process_identity is None:
            terminate_managed_process(
                proc,
                grace_seconds=cleanup_grace_seconds,
            )
            raise XhsRuntimeSupervisionError(
                "XHS runtime reporter requires an exact registered exporter identity"
            )
        while True:
            now = time.monotonic()
            if tracked_paths is not None:
                current_signature = progress_path_signature(tracked_paths)
                if current_signature != progress_signature:
                    progress_signature = current_signature
                    progress_observed = True
                    last_progress_at = now
                current_budget = timeout + (
                    0.0 if progress_observed else max(0.0, startup_grace_seconds)
                )
                remaining = current_budget - (now - last_progress_at)
                communicate_timeout = min(max(0.01, poll_seconds), max(0.01, remaining))
            else:
                remaining = timeout
                communicate_timeout = timeout
            if runtime_reporter is not None and remaining > 0:
                exporter_alive = proc.poll() is None
                exporter_identity_current = bool(
                    exporter_alive
                    and lease_process_identity is not None
                    and process_inspector is not None
                    and process_inspector.identity(proc.pid) == lease_process_identity
                )
                if exporter_identity_current:
                    runtime_reporter.checkpoint_from_diagnostics(
                        network_diagnostics_path,
                        phase="running",
                    )
                elif exporter_alive:
                    exporter_identity_mismatch = True
                    terminate_managed_process(
                        proc,
                        grace_seconds=cleanup_grace_seconds,
                    )
                    raise XhsRuntimeSupervisionError(
                        "xhs_exporter_process_identity_changed"
                    )
            if remaining <= 0:
                timed_out = True
                returncode = 124
                timeout_reason = (
                    "no_progress_timeout"
                    if tracked_paths is not None
                    else "wall_clock_timeout"
                )
                last_progress_age_seconds = max(0.0, now - last_progress_at)
                stdout_data, stderr_data, forced_termination = terminate_managed_process(
                    proc,
                    grace_seconds=cleanup_grace_seconds,
                )
                stdout = decode_text(stdout_data)
                stderr = decode_text(stderr_data)
                break
            try:
                stdout_data, stderr_data = proc.communicate(timeout=communicate_timeout)
            except subprocess.TimeoutExpired:
                if tracked_paths is None:
                    timed_out = True
                    returncode = 124
                    timeout_reason = "wall_clock_timeout"
                    last_progress_age_seconds = max(
                        0.0,
                        time.monotonic() - last_progress_at,
                    )
                    (
                        stdout_data,
                        stderr_data,
                        forced_termination,
                    ) = terminate_managed_process(
                        proc,
                        grace_seconds=cleanup_grace_seconds,
                    )
                    stdout = decode_text(stdout_data)
                    stderr = decode_text(stderr_data)
                    break
                continue
            stdout = decode_text(stdout_data)
            stderr = decode_text(stderr_data)
            returncode = int(proc.returncode or 0)
            last_progress_age_seconds = max(0.0, time.monotonic() - last_progress_at)
            break
    except BaseException:
        if proc is not None and getattr(proc, "poll", lambda: proc.returncode)() is None:
            try:
                terminate_managed_process(
                    proc,
                    grace_seconds=cleanup_grace_seconds,
                )
            except Exception:
                pass
        raise
    finally:
        if lease_process_identity is not None and proc is not None and proc.returncode is not None:
            mark_lease_process_exited_from_environment(
                identity=lease_process_identity,
                process_role="exporter",
                environ=registration_env,
            )

    _, avatar_output_detected = redact_author_avatar_text(f"{stdout}\n{stderr}")
    if avatar_output_detected:
        stdout = AUTHOR_AVATAR_LOG_REDACTION
        stderr = AUTHOR_AVATAR_LOG_REDACTION
    stdout_log = log_dir / "stdout.log"
    stderr_log = log_dir / "stderr.log"
    command_log = log_dir / "command.txt"
    stdout_log.write_text(stdout, encoding="utf-8")
    stderr_log.write_text(stderr, encoding="utf-8")
    command_log.write_text(shlex.join(cmd), encoding="utf-8")
    return {
        "command": cmd,
        "command_text": shlex.join(cmd),
        "returncode": returncode,
        "timed_out": timed_out,
        "timeout_reason": timeout_reason,
        "inactivity_timeout_seconds": timeout if tracked_paths is not None else None,
        "startup_grace_seconds": (
            startup_grace_seconds if tracked_paths is not None else None
        ),
        "progress_observed": progress_observed,
        "last_progress_age_seconds": round(last_progress_age_seconds, 2),
        "forced_termination": forced_termination,
        "cleanup_grace_seconds": cleanup_grace_seconds,
        "exporter_identity_mismatch": exporter_identity_mismatch,
        "runtime_status": (
            runtime_reporter.snapshot() if runtime_reporter is not None else None
        ),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "command_log": str(command_log),
        "stdout_tail": tail(stdout),
        "stderr_tail": tail(stderr),
    }


def skipped_command(cmd: list[str], log_dir: Path, reason: str) -> dict[str, Any]:
    log_dir = ensure_dir(log_dir)
    stdout_log = log_dir / "stdout.log"
    stderr_log = log_dir / "stderr.log"
    command_log = log_dir / "command.txt"
    stdout_log.write_text("", encoding="utf-8")
    stderr_log.write_text(reason, encoding="utf-8")
    command_log.write_text(shlex.join(cmd), encoding="utf-8")
    return {
        "command": cmd,
        "command_text": shlex.join(cmd),
        "returncode": 1,
        "timed_out": False,
        "elapsed_seconds": 0,
        "skipped": True,
        "reason": reason,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "command_log": str(command_log),
        "stdout_tail": "",
        "stderr_tail": reason,
    }


def item_type_from_path(path: Path) -> str:
    name = path.name
    if "_contents_" in name:
        return "contents"
    if "_comments_" in name:
        return "comments"
    if "_creators_" in name:
        return "creators"
    return "unknown"


def truncate(value: Any, limit: int = 240) -> str:
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def clean_html_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_sample(record: dict[str, Any]) -> dict[str, str]:
    sample = {key: truncate(record[key]) for key in SAMPLE_KEYS if record.get(key) not in (None, "")}
    published_at = published_at_for_record(record)
    if published_at and "published_at" not in sample:
        sample["published_at"] = published_at
    return sample or {key: truncate(value) for key, value in list(record.items())[:8]}


def summarize_jsonl(path: Path, keyword: str) -> dict[str, Any]:
    item_type = item_type_from_path(path)
    platform_key = platform_from_path(path)
    fields: set[str] = set()
    author_like_fields: set[str] = set()
    samples: list[dict[str, str]] = []
    line_count = 0
    parse_errors = 0
    keyword_hits = 0
    video_like_records = 0
    published_at_records = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            line_count += 1
            if keyword in text:
                keyword_hits += 1
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            if isinstance(record, dict):
                fields.update(record)
                author_like_fields.update(
                    key for key in record if any(marker in key.lower() for marker in AUTHOR_FIELD_MARKERS)
                )
                is_video = item_type == "contents" and is_video_record(platform_key, record)
                if is_video:
                    video_like_records += 1
                if item_type == "contents" and not is_video and published_at_for_record(record):
                    published_at_records += 1
                if item_type == "contents" and len(samples) < 3:
                    samples.append(extract_sample(record))
    return {
        "path": str(path),
        "item_type": item_type,
        "line_count": line_count,
        "keyword_hit_records": keyword_hits,
        "parse_errors": parse_errors,
        "video_like_records": video_like_records,
        "published_at_records": published_at_records,
        "top_level_fields": sorted(fields),
        "author_like_fields": sorted(author_like_fields),
        "samples": samples,
    }


def summarize_output(save_path: Path, keyword: str) -> dict[str, Any]:
    all_jsonl_files = sorted(save_path.rglob("*.jsonl")) if save_path.exists() else []
    image_manifest_paths = [path for path in all_jsonl_files if path.name == "image_manifest.jsonl"]
    jsonl_files = [path for path in all_jsonl_files if path.name != "image_manifest.jsonl"]
    jsonl = [summarize_jsonl(path, keyword) for path in jsonl_files]
    counts = {"contents": 0, "comments": 0, "creators": 0, "unknown": 0}
    fields: set[str] = set()
    author_like_fields: set[str] = set()
    samples: list[dict[str, str]] = []
    keyword_hits = 0
    parse_errors = 0
    video_like_records = 0
    published_at_records = 0
    for item in jsonl:
        counts[item["item_type"]] = counts.get(item["item_type"], 0) + item["line_count"]
        fields.update(item["top_level_fields"])
        author_like_fields.update(item["author_like_fields"])
        keyword_hits += item["keyword_hit_records"]
        parse_errors += item["parse_errors"]
        video_like_records += int(item.get("video_like_records") or 0)
        published_at_records += int(item.get("published_at_records") or 0)
        samples.extend(item["samples"])

    files = [path for path in save_path.rglob("*") if path.is_file()] if save_path.exists() else []
    image_files = [path for path in files if path.suffix.lower() in IMAGE_SUFFIXES]
    video_files = [path for path in files if path.suffix.lower() in VIDEO_SUFFIXES]
    return {
        "save_path": str(save_path),
        "jsonl_files": [str(path) for path in jsonl_files],
        "image_manifest_paths": [str(path) for path in image_manifest_paths],
        "jsonl_file_count": len(jsonl_files),
        "content_records": counts.get("contents", 0),
        "non_video_content_records": max(0, counts.get("contents", 0) - video_like_records),
        "video_like_records": video_like_records,
        "comment_records": counts.get("comments", 0),
        "creator_records": counts.get("creators", 0),
        "unknown_records": counts.get("unknown", 0),
        "total_jsonl_records": sum(counts.values()),
        "keyword_hit_records": keyword_hits,
        "parse_errors": parse_errors,
        "image_file_count": len(image_files),
        "video_file_count": len(video_files),
        "published_at_records": published_at_records,
        "top_level_fields": sorted(fields)[:120],
        "author_like_fields": sorted(author_like_fields),
        "samples": samples[:5],
        "files": jsonl,
    }


def ensure_web_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    return bootstrap_connection(conn, sync_jobs=False)


def platform_from_path(path: Path) -> str:
    parts = [part.lower() for part in path.parts]
    if "bili" in parts:
        return "bilibili"
    if "zhihu" in parts:
        return "zhihu"
    if "xhs" in parts:
        return "xhs"
    if "weibo" in parts:
        return "weibo"
    if "douyin" in parts:
        return "douyin"
    for key in PLATFORMS:
        if key in parts:
            return key
    return "unknown"


def post_id_for_record(platform_key: str, record: dict[str, Any]) -> str | None:
    value = first_value(record, "note_id", "aweme_id", "content_id", "video_id", "bvid", "aid", "id")
    return str(value) if value not in (None, "") else None


def canonical_url_for_record(platform_key: str, record: dict[str, Any]) -> str | None:
    value = first_value(record, "note_url", "aweme_url", "content_url", "video_url", "url", "share_url")
    if value:
        return str(value)
    post_id = post_id_for_record(platform_key, record)
    if not post_id:
        return None
    if platform_key == "weibo":
        return f"https://m.weibo.cn/detail/{post_id}"
    if platform_key == "douyin":
        return f"https://www.douyin.com/video/{post_id}"
    if platform_key == "bilibili":
        content_type = str(first_value(record, "content_type", "type") or "").strip().lower()
        if content_type in {"article", "read", "opus", "dynamic"}:
            return f"https://www.bilibili.com/read/cv{post_id}/"
        return f"https://www.bilibili.com/video/av{post_id}"
    if platform_key == "xhs":
        return f"https://www.xiaohongshu.com/explore/{post_id}"
    if platform_key == "zhihu":
        content_type = str(first_value(record, "content_type", "type") or "").strip().lower()
        question_id = first_value(record, "question_id")
        if content_type == "article":
            return f"https://zhuanlan.zhihu.com/p/{post_id}"
        if content_type == "answer" and question_id:
            return f"https://www.zhihu.com/question/{question_id}/answer/{post_id}"
    return None


def content_body_for_record(platform_key: str, record: dict[str, Any]) -> str:
    fields = CONTENT_BODY_FIELDS.get(platform_key, ("content_text", "content"))
    return str(first_value(record, *fields) or "").strip()


def content_text_for_record(platform_key: str, record: dict[str, Any]) -> str:
    return web_post_content_text(platform_key, record)


def inject_materialized_images(
    image_items: list[dict[str, Any]],
    materialized_images: list[MaterializedImage],
) -> list[dict[str, Any]]:

    def source_identity(
        *,
        platform_key: str,
        platform_post_id: str,
        source_index: int,
        source_key: str,
        source_asset_key: str,
        source_url: str,
    ) -> tuple[str, str, int, str, str, str]:
        return (
            platform_key,
            platform_post_id,
            source_index,
            source_key,
            source_asset_key,
            source_url,
        )

    materialized: dict[tuple[str, str, int, str, str, str], MaterializedImage] = {}
    duplicate_identities: set[tuple[str, str, int, str, str, str]] = set()
    for local in materialized_images:
        if local.manifest_source_index is None:
            raise ImagePersistenceError("materialized image lacks manifest source index")
        identity = source_identity(
            platform_key=local.platform_key,
            platform_post_id=local.platform_post_id,
            source_index=local.manifest_source_index,
            source_key=local.source_key,
            source_asset_key=local.source_asset_key,
            source_url=local.source_url,
        )
        if identity in materialized:
            raise ImagePersistenceError("duplicate materialized image identity")
        materialized[identity] = local
        for duplicate in local.sha256_duplicate_sources:
            duplicate_identity = source_identity(
                platform_key=local.platform_key,
                platform_post_id=local.platform_post_id,
                source_index=duplicate.source_index,
                source_key=duplicate.source_key,
                source_asset_key=duplicate.source_asset_key,
                source_url=duplicate.source_url,
            )
            if duplicate_identity in materialized or duplicate_identity in duplicate_identities:
                raise ImagePersistenceError("duplicate SHA-256 source identity")
            duplicate_identities.add(duplicate_identity)
    content_items = [item for item in image_items if item.get("role") == "content"]
    if len(materialized) + len(duplicate_identities) != len(content_items):
        raise ImagePersistenceError(
            "materialized and SHA-256 duplicate image count does not match source candidates"
        )

    result: list[dict[str, Any]] = []
    for raw_item in image_items:
        item = dict(raw_item)
        if item.get("role") != "content":
            result.append(item)
            continue
        identity = source_identity(
            platform_key=str(item.get("platform_key") or ""),
            platform_post_id=str(item.get("platform_post_id") or ""),
            source_index=int(item["source_index"]),
            source_key=str(item.get("source_key") or ""),
            source_asset_key=str(item.get("source_asset_key") or ""),
            source_url=str(item.get("url") or ""),
        )
        local = materialized.get(identity)
        if local is None:
            if identity in duplicate_identities:
                continue
            raise ImagePersistenceError(f"materialized image identity mismatch: {identity}")
        local_file = {
            "source": "formal_image_materialization_v1",
            "source_url": local.source_url,
            "size_bytes": local.size_bytes,
            "manifest_source_index": local.manifest_source_index,
        }
        if local.manifest_path:
            local_file["manifest_path"] = local.manifest_path
        if local.manifest_line is not None:
            local_file["manifest_line"] = local.manifest_line
        if local.sha256_duplicate_sources:
            local_file["sha256_duplicate_sources"] = [
                {
                    "source_index": duplicate.source_index,
                    "source_key": duplicate.source_key,
                    "source_asset_key": duplicate.source_asset_key,
                    "source_url": duplicate.source_url,
                    "manifest_path": duplicate.manifest_path,
                    "manifest_line": duplicate.manifest_line,
                }
                for duplicate in local.sha256_duplicate_sources
            ]
        item.update(
            {
                "source_index": local.source_index,
                "local_path": local.local_path,
                "width": local.width,
                "height": local.height,
                "mime_type": local.mime_type,
                "sha256": local.sha256,
                "local_file": local_file,
            }
        )
        result.append(item)
    retained_content = [item for item in result if item.get("role") == "content"]
    if len(retained_content) != len(materialized_images):
        raise ImagePersistenceError("retained materialized image count mismatch")
    if [int(item["source_index"]) for item in retained_content] != list(
        range(len(retained_content))
    ):
        raise ImagePersistenceError("retained materialized image indices are not continuous")
    return result


def row_for_record(
    platform_key: str,
    record: dict[str, Any],
    *,
    artifact_dir: str,
    captured_at: str,
    keyword: str,
    materialized_images: list[MaterializedImage] | None = None,
) -> dict[str, Any]:
    sanitized_record = sanitize_author_avatar_data(record).value
    if not isinstance(sanitized_record, dict):
        raise ValueError("sanitized record must remain an object")
    record = sanitized_record
    content_text = content_text_for_record(platform_key, record)
    canonical_url = canonical_url_for_record(platform_key, record)
    image_items = image_items_for_record(platform_key, record)
    if materialized_images is not None:
        image_items = inject_materialized_images(image_items, materialized_images)
    keyword_value = effective_source_keyword(record, keyword)
    title = web_post_title(platform_key, record)
    metrics = {
        "liked_count": parse_int(first_value(record, "liked_count", "voteup_count")),
        "favorites_count": parse_int(first_value(record, "collected_count", "video_favorite_count")),
        "comments_count": parse_int(first_value(record, "comment_count", "comments_count", "video_comment")),
        "shares_count": parse_int(first_value(record, "share_count", "shared_count", "video_share_count")),
        "reposts_count": parse_int(first_value(record, "reposts_count", "repost_count")),
        "views_count": parse_int(first_value(record, "video_play_count", "view_count", "play_count")),
    }
    author = {
        "nickname": first_value(record, "nickname", "user_nickname", "user_name", "author_name"),
        "creator_hash": first_value(record, "user_id", "creator_id", "creator_hash", "author_id"),
        "followers_count": parse_int(
            first_value(
                record,
                "author_followers_count",
                "followers_count",
                "follower_count",
                "fans_count",
                "fans",
                "followers",
            )
        ),
        "following_count": parse_int(first_value(record, "author_following_count", "following_count", "follow_count", "follows")),
        "posts_count": parse_int(first_value(record, "author_posts_count", "aweme_count", "posts_count", "note_count", "notes_count", "video_count")),
        "liked_count": parse_int(first_value(record, "author_liked_count", "total_favorited", "favorited_count")),
        "followers_observed": record.get("followers_observed") is True,
        "followers_source": first_value(record, "author_followers_source", "followers_source"),
    }
    return {
        "platform_key": platform_key,
        "platform_post_id": post_id_for_record(platform_key, record),
        "source_type": "mediacrawler_search",
        "source_url": canonical_url or "",
        "canonical_url": canonical_url,
        "title": title,
        "author_display_name": author["nickname"],
        "author_platform_id": author["creator_hash"],
        "author_profile_url": first_value(record, "author_profile_url", "user_link", "profile_url", "user_url"),
        "author_description": first_value(record, "author_desc", "user_desc"),
        "author_followers_count": author["followers_count"],
        "author_following_count": author["following_count"],
        "author_posts_count": author["posts_count"],
        "author_platform_level": first_value(record, "level", "user_level"),
        "author_verified": None,
        "author_verified_text": first_value(record, "verified_text", "verify_info"),
        "published_at": published_at_for_record(record),
        "captured_at": captured_at,
        "keyword": keyword_value,
        "topic_relevant": int(
            is_topic_relevant(
                title=title,
                content_text=content_text,
                keyword=keyword_value,
            )
        ),
        "content_text": content_text,
        "content_length": len(content_text),
        "post_likes_count": metrics["liked_count"],
        "post_favorites_count": metrics["favorites_count"],
        "post_comments_count": metrics["comments_count"],
        "post_shares_count": metrics["shares_count"],
        "post_reposts_count": metrics["reposts_count"],
        "post_views_count": metrics["views_count"],
        "post_images_count": len([item for item in image_items if item["role"] == "content"]),
        "metrics_json": json_dump(metrics),
        "author_json": json_dump(author),
        "raw_sample_json": json_dump(record),
        "artifact_dir": artifact_dir,
        "capture_method": "import",
        "status": "captured" if content_text else "partial",
        "_image_items": image_items,
    }


def formal_record_identity(platform_key: str, record: dict[str, Any]) -> str:
    post_id = post_id_for_record(platform_key, record)
    if post_id:
        return f"{platform_key}:id:{post_id}"
    canonical_url = canonical_url_for_record(platform_key, record)
    return f"{platform_key}:url:{canonical_url}" if canonical_url else ""


def formal_database_identities(platform_key: str, record: dict[str, Any]) -> set[str]:
    identities: set[str] = set()
    post_id = post_id_for_record(platform_key, record)
    canonical_url = canonical_url_for_record(platform_key, record)
    if post_id:
        identities.add(f"{platform_key}:id:{post_id}")
    if canonical_url:
        identities.add(f"{platform_key}:url:{canonical_url}")
    return identities


def validate_formal_record(
    platform_key: str,
    record: dict[str, Any],
    seen: set[str],
    *,
    allow_xhs_title_image_only: bool = False,
) -> dict[str, Any]:
    identity = formal_record_identity(platform_key, record)
    reasons: list[str] = []
    if not identity:
        reasons.append("missing_identity")
    elif identity in seen:
        reasons.append("duplicate_identity")
    if is_video_record(platform_key, record):
        reasons.append("video_record")
    detail_status = str(record.get("content_detail_status") or "")
    detail_source = str(record.get("content_detail_source") or "")
    xhs_title_image_only = bool(
        allow_xhs_title_image_only
        and platform_key == "xhs"
        and str(first_value(record, "title") or "").strip()
        and detail_status == "detail_observed"
        and detail_source == "note_detail"
        and content_image_candidates(platform_key, record)
    )
    if not content_body_for_record(platform_key, record) and not xhs_title_image_only:
        reasons.append("missing_content")
    if detail_status != "detail_observed":
        reasons.append("content_detail_unobserved")
    if detail_source not in TRUSTED_CONTENT_DETAIL_SOURCES.get(platform_key, frozenset()):
        reasons.append("untrusted_content_detail_source")
    if platform_key == "bilibili":
        image_detail_status = str(record.get("content_images_detail_status") or "")
        if image_detail_status != "detail_observed":
            reasons.append("content_images_detail_unobserved")
    if not published_at_for_record(record):
        reasons.append("missing_published_at")
    if not first_value(record, "user_id", "creator_id", "creator_hash", "author_id", "mid"):
        reasons.append("missing_author_id")
    if not first_value(record, "nickname", "user_nickname", "user_name", "author_name", "author"):
        reasons.append("missing_author_name")
    content_images = content_image_candidates(platform_key, record)
    if not content_images:
        detail_status = str(record.get("content_detail_status") or "")
        if (
            platform_key == "zhihu"
            and str(record.get("content_type") or "") in {"answer", "article"}
            and detail_status != "detail_observed"
        ):
            reasons.append("content_detail_unobserved")
        else:
            reasons.append("missing_content_image")

    followers_count = parse_int(
        first_value(
            record,
            "author_followers_count",
            "followers_count",
            "follower_count",
            "fans_count",
            "fans",
            "followers",
        )
    )
    followers_observed = record.get("followers_observed") is True
    followers_source = str(first_value(record, "author_followers_source", "followers_source") or "")
    if platform_key in FOLLOWERS_REQUIRED_PLATFORMS:
        if followers_count is None:
            reasons.append("missing_followers_count")
        if not followers_observed:
            reasons.append("followers_not_observed")
        if not followers_source or followers_source == "missing":
            reasons.append("missing_followers_source")
        elif followers_source not in REQUIRED_FOLLOWER_SOURCES.get(platform_key, frozenset()):
            reasons.append("untrusted_followers_source")

    missing_metric_groups = [
        "/".join(keys)
        for keys in PLATFORM_REQUIRED_METRICS.get(platform_key, ())
        if first_value(record, *keys) is None
    ]
    if missing_metric_groups:
        reasons.append("missing_metrics:" + ",".join(missing_metric_groups))
    return {
        "valid": not reasons,
        "identity": identity,
        "reasons": reasons,
        "followers_count": followers_count,
        "followers_observed": followers_observed,
        "followers_source": followers_source,
        "content_image_count": len(content_images),
    }


PAGINATION_EVENT_FIELDS = (
    "platform",
    "batch_no",
    "candidate_count",
    "valid_new_count",
    "valid_existing_count",
    "batch_new_count",
    "batch_candidate_identity_count",
    "source_page",
    "source_offset",
    "source_cursor",
    "next_cursor",
    "resume_page",
    "resume_offset",
    "resume_cursor",
    "batch_complete",
    "discovery_phase",
    "source_has_more",
    "raw_batch_count",
    "raw_response_count",
    "stagnant_batches",
    "stagnation_basis",
    "stop_reason",
    "stop_detail",
    "candidate_identities",
    "skipped_candidate_count",
    "skipped_candidate_failures",
)

SKIPPED_CANDIDATE_EVENT_FIELDS = (
    "platform",
    "identity",
    "platform_post_id",
    "failure_scope",
    "detail",
    "error_code",
    "retryable",
    "attempts",
    "source_index",
    "source_page",
    "source_offset",
    "source_cursor",
    "discovery_phase",
)

DISCOVERY_RESEED_EVENT_FIELDS = (
    "platform",
    "reason",
    "saved_resume_page",
    "saved_resume_offset",
    "saved_resume_cursor",
    "resume_page",
    "resume_offset",
    "resume_cursor",
    "refresh_new_candidate_count",
)


def stable_douyin_search_id(pagination_evidence: dict[str, Any]) -> str:
    for batch in pagination_evidence.get("batches") or []:
        if not isinstance(batch, dict) or batch.get("platform") != "douyin":
            continue
        if batch.get("discovery_phase") not in (None, "frontier"):
            continue
        source_cursor = str(batch.get("source_cursor") or "")
        next_cursor = str(batch.get("next_cursor") or "")
        if source_cursor:
            return source_cursor
        if batch.get("source_offset") in (None, 0, "0") and next_cursor:
            return next_cursor
    return ""


def effective_discovery_checkpoint_event(
    pagination_evidence: dict[str, Any],
) -> dict[str, Any] | None:
    event = pagination_evidence.get("stop_event") or (
        (pagination_evidence.get("batches") or [None])[-1]
    )
    if not isinstance(event, dict):
        return None
    return dict(event)


def persist_discovery_checkpoint(
    args: argparse.Namespace,
    platform_key: str,
    pagination_evidence: dict[str, Any],
) -> dict[str, Any]:
    if args.discovery_job_id is None:
        return {"skipped": True, "reason": "not_scheduler_managed"}
    if args.no_checkpoint_write:
        return {"skipped": True, "reason": "checkpoint_write_disabled"}
    event = effective_discovery_checkpoint_event(pagination_evidence)
    if event is None:
        return {"skipped": True, "reason": "no_frontier_batch_evidence"}
    refresh_only = event.get("discovery_phase") == "refresh"
    if refresh_only:
        resume_page = int(args.start_page)
        resume_offset = (
            int(args.start_offset)
            if platform_key == "douyin" and args.start_offset is not None
            else None
        )
        resume_cursor = (
            (str(args.start_cursor or "") or None)
            if platform_key == "douyin"
            else None
        )
        source_has_more_value = False if args.discovery_source_exhausted else None
    else:
        resume_page = int(
            event.get("resume_page") or event.get("source_page") or args.start_page
        )
        resume_offset_value = event.get("resume_offset")
        resume_offset = (
            int(resume_offset_value)
            if resume_offset_value not in (None, "")
            else None
        )
        resume_cursor = str(event.get("resume_cursor") or "") or None
        if platform_key == "douyin":
            resume_cursor = stable_douyin_search_id(pagination_evidence) or resume_cursor
        source_has_more_value = event.get("source_has_more")
    source_has_more = (
        None if source_has_more_value is None else bool(source_has_more_value)
    )
    stop_detail = str(event.get("stop_detail") or "")
    with connect_db(
        Path(args.db).expanduser(),
        busy_timeout_ms=FORMAL_SQLITE_BUSY_TIMEOUT_MS,
    ) as conn:
        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        existing_checkpoint = load_checkpoint(
            conn,
            job_id=int(args.discovery_job_id),
            query_fingerprint_value=str(args.discovery_query_fingerprint),
        )
        if (
            platform_key == "douyin"
            and stop_detail == "saved_source_exhausted"
            and source_has_more is False
            and resume_page == 1
            and resume_offset in (None, 0)
            and not resume_cursor
            and existing_checkpoint
            and existing_checkpoint.get("last_stop_detail")
            == "verified_empty_first_page"
        ):
            stop_detail = "verified_empty_first_page"
        save_checkpoint(
            conn,
            job_id=int(args.discovery_job_id),
            platform_key=platform_key,
            keyword=args.keyword,
            query_fingerprint_value=str(args.discovery_query_fingerprint),
            resume_page=resume_page,
            resume_offset=resume_offset,
            resume_cursor=resume_cursor,
            source_has_more=source_has_more,
            last_batch_complete=bool(event.get("batch_complete")),
            last_stop_reason=str(event.get("stop_reason") or "continue"),
            last_run_id=str(args.discovery_run_id),
            last_stop_detail=stop_detail,
        )
        seen_candidate_count = save_seen_candidates(
            conn,
            job_id=int(args.discovery_job_id),
            platform_key=platform_key,
            query_fingerprint_value=str(args.discovery_query_fingerprint),
            platform_post_ids=list(event.get("candidate_identities") or []),
            run_id=str(args.discovery_run_id),
        )
        conn.commit()
    return {
        "skipped": False,
        "resume_page": resume_page,
        "resume_offset": resume_offset,
        "resume_cursor": resume_cursor,
        "source_has_more": source_has_more,
        "last_stop_reason": str(event.get("stop_reason") or "continue"),
        "last_stop_detail": stop_detail,
        "refresh_only": refresh_only,
        "seen_candidate_count": seen_candidate_count,
    }


def load_existing_formal_identities(db_path: str | Path | None) -> set[str]:
    if not db_path:
        return set()
    try:
        with sqlite3.connect(Path(db_path).expanduser()) as conn:
            rows = conn.execute(
                """
                SELECT platform_key, platform_post_id, canonical_url
                FROM web_posts
                """
            ).fetchall()
    except (OSError, sqlite3.Error):
        return set()
    identities: set[str] = set()
    for platform_key, platform_post_id, canonical_url in rows:
        if platform_post_id:
            identities.add(f"{platform_key}:id:{platform_post_id}")
        if canonical_url:
            identities.add(f"{platform_key}:url:{canonical_url}")
    return identities


def load_pagination_evidence(state_path: str | Path | None) -> dict[str, Any]:
    if not state_path:
        return {"available": False, "stopped": False, "batches": []}
    path = Path(state_path).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {
            "available": False,
            "stopped": False,
            "state_path": str(path),
            "batches": [],
        }

    batches = []
    stopped_details: dict[str, Any] | None = None
    frontier_reseeds: list[dict[str, Any]] = []
    skipped_candidate_failures: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        details = event.get("details") or {}
        if not isinstance(details, dict):
            continue
        selected = {key: details.get(key) for key in PAGINATION_EVENT_FIELDS if key in details}
        if event.get("type") == "adaptive_batch_completed":
            batches.append(selected)
        elif event.get("type") == "adaptive_search_stopped":
            stopped_details = selected
        elif event.get("type") == "discovery_frontier_reseeded":
            frontier_reseeds.append(
                {
                    key: details.get(key)
                    for key in DISCOVERY_RESEED_EVENT_FIELDS
                    if key in details
                }
            )
        elif event.get("type") == "candidate_skipped":
            skipped_candidate_failures.append(
                {
                    key: details.get(key)
                    for key in SKIPPED_CANDIDATE_EVENT_FIELDS
                    if key in details
                }
            )

    latest = stopped_details or (batches[-1] if batches else {})
    return {
        "available": bool(batches or stopped_details),
        "state_path": str(path),
        "batch_count": len(batches),
        "candidate_count": int(latest.get("candidate_count") or 0),
        "stopped": stopped_details is not None,
        "stop_reason": str((stopped_details or {}).get("stop_reason") or ""),
        "stop_detail": str((stopped_details or {}).get("stop_detail") or ""),
        "batches": batches,
        "frontier_reseeds": frontier_reseeds,
        "skipped_candidate_count": len(skipped_candidate_failures),
        "skipped_candidate_failures": skipped_candidate_failures,
        "stop_event": stopped_details,
    }


def load_xhs_repair_report(path_value: str | Path | None) -> dict[str, Any]:
    if not path_value:
        return {}
    path = Path(path_value).expanduser()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def xhs_repair_pagination_evidence(
    records: list[dict[str, Any]],
    *,
    target_count: int,
) -> dict[str, Any]:
    reports = [
        record.get("repair_report")
        for record in records
        if isinstance(record, dict)
        and record.get("platform") == "xhs"
        and isinstance(record.get("repair_report"), dict)
    ]
    batches: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    successful_ids: set[str] = set()
    for report in reports:
        batches.extend(
            value for value in report.get("batches") or [] if isinstance(value, dict)
        )
        failures.extend(
            value
            for value in report.get("candidate_failures") or []
            if isinstance(value, dict)
        )
        successful_ids.update(
            str(value) for value in report.get("successful_ids") or [] if str(value)
        )
    return {
        "available": True,
        "stopped": True,
        "stop_reason": "repair_targets_processed",
        "stop_detail": "specified_detail_targets",
        "candidate_count": target_count,
        "batches": batches,
        "successful_candidate_count": len(successful_ids),
        "skipped_candidate_count": len(failures),
        "skipped_candidate_failures": failures,
    }


def post_repair_pagination_evidence(
    targets: list[dict[str, Any]],
    *,
    platform: str,
    successful_identities: set[str],
    materialization_failures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    target_ids = {
        str(target.get("platform_post_id") or "")
        for target in targets
        if str(target.get("platform_post_id") or "")
    }
    successful_ids = {
        identity.rsplit(":id:", 1)[-1]
        for identity in successful_identities
        if identity.startswith(f"{platform}:id:")
    } & target_ids
    failure_by_id: dict[str, dict[str, Any]] = {}
    for failure in materialization_failures or []:
        identity = str(failure.get("identity") or "")
        post_id = identity.rsplit(":id:", 1)[-1] if ":id:" in identity else identity
        if post_id:
            failure_by_id[post_id] = failure

    failures: list[dict[str, Any]] = []
    for target in targets:
        post_id = str(target.get("platform_post_id") or "")
        if not post_id or post_id in successful_ids:
            continue
        materialization_failure = failure_by_id.get(post_id) or {}
        error_code = str(
            materialization_failure.get("error_code")
            or materialization_failure.get("code")
            or "repair_target_no_valid_output"
        )
        detail = str(
            materialization_failure.get("detail")
            or materialization_failure.get("message")
            or "explicit repair target produced no formally valid persisted detail record"
        )
        failure_scope = str(
            materialization_failure.get("failure_scope")
            or ("image" if materialization_failure else "post")
        )
        failures.append(
            {
                "platform": platform,
                "identity": f"{platform}:id:{post_id}",
                "platform_post_id": post_id,
                "failure_scope": failure_scope,
                "detail": detail,
                "error_code": error_code,
                "retryable": bool(materialization_failure.get("retryable", False)),
                "attempts": max(1, int(materialization_failure.get("attempts") or 1)),
                "source_index": materialization_failure.get("source_index"),
                "terminal_for_run": True,
                "evidence_source": (
                    "image_materialization"
                    if materialization_failure
                    else "repair_target_output_difference"
                ),
            }
        )
    return {
        "available": True,
        "stopped": True,
        "stop_reason": "repair_targets_processed",
        "stop_detail": "specified_detail_targets",
        "candidate_count": len(targets),
        "batches": [],
        "successful_candidate_count": len(successful_ids),
        "skipped_candidate_count": len(failures),
        "skipped_candidate_failures": failures,
    }


def attach_skipped_candidate_evidence(
    image_materialization: dict[str, Any],
    pagination_evidence: dict[str, Any],
) -> dict[str, Any]:
    stop_event = pagination_evidence.get("stop_event") or {}
    failures = list(
        stop_event.get("skipped_candidate_failures")
        or pagination_evidence.get("skipped_candidate_failures")
        or []
    )
    result = dict(image_materialization)
    result["skipped_candidate_count"] = max(
        int(stop_event.get("skipped_candidate_count") or 0),
        int(pagination_evidence.get("skipped_candidate_count") or 0),
        len(failures),
    )
    result["skipped_candidate_failures"] = failures
    image_failures = [
        failure for failure in failures if failure.get("failure_scope") == "image"
    ]
    result["retryable_failures"] = int(result.get("retryable_failures") or 0) + sum(
        bool(failure.get("retryable"))
        or failure.get("error_code") == "image_download_retryable"
        for failure in image_failures
    )
    result["terminal_failures"] = int(result.get("terminal_failures") or 0) + sum(
        not (
            bool(failure.get("retryable"))
            or failure.get("error_code") == "image_download_retryable"
        )
        for failure in image_failures
    )
    return result


def collect_formal_records(
    summary: dict[str, Any],
    *,
    db_path: str | Path | None,
    pagination_evidence: dict[str, Any] | None = None,
    require_local_images: bool = False,
    localized_identities: set[str] | None = None,
    materialized_images_by_identity: dict[str, list[MaterializedImage]] | None = None,
    allowed_identities: set[str] | None = None,
    repair_metadata_by_identity: dict[str, dict[str, Any]] | None = None,
    repair_mode: bool = False,
    progress_callback: Callable[[], object] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _runtime_progress(progress_callback)
    localized = localized_identities or set()
    materialized = materialized_images_by_identity or {}
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    existing_identities = load_existing_formal_identities(db_path)
    valid_new_count = 0
    valid_existing_count = 0
    topic_relevant_new_count = 0
    topic_relevant_existing_count = 0
    topic_irrelevant_new_count = 0
    topic_irrelevant_existing_count = 0
    reason_counts: Counter[str] = Counter()
    candidate_count = 0
    parse_errors = 0
    local_image_failure_count = 0
    for platform_record in summary.get("records") or []:
        output = platform_record.get("output") if isinstance(platform_record, dict) else {}
        for path_value in (output or {}).get("jsonl_files") or []:
            path = Path(path_value)
            if item_type_from_path(path) != "contents":
                continue
            platform_key = platform_from_path(path)
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, start=1):
                    text = line.strip()
                    if not text:
                        continue
                    candidate_count += 1
                    if candidate_count % 64 == 1:
                        _runtime_progress(progress_callback)
                    try:
                        record = json.loads(text)
                    except json.JSONDecodeError:
                        parse_errors += 1
                        reason_counts["invalid_json"] += 1
                        continue
                    if not isinstance(record, dict):
                        reason_counts["invalid_record_type"] += 1
                        continue
                    sanitized_record = sanitize_author_avatar_data(record).value
                    if not isinstance(sanitized_record, dict):
                        reason_counts["invalid_record_type"] += 1
                        continue
                    record = sanitized_record
                    repair_identity = formal_record_identity(platform_key, record)
                    repair_metadata = (
                        (repair_metadata_by_identity or {}).get(repair_identity) or {}
                    )
                    if repair_mode and repair_metadata:
                        record = merge_repair_fallback_metadata(
                            platform_key,
                            record,
                            repair_metadata,
                        )
                        record["source_keyword"] = str(
                            repair_metadata.get("keyword") or record.get("source_keyword") or ""
                        )
                    validation = validate_formal_record(
                        platform_key,
                        record,
                        seen,
                        allow_xhs_title_image_only=repair_mode,
                    )
                    if not validation["valid"]:
                        reason_counts.update(validation["reasons"])
                        continue
                    identity = str(validation["identity"])
                    if allowed_identities is not None and identity not in allowed_identities:
                        reason_counts["repair_target_not_allowed"] += 1
                        continue
                    if require_local_images and identity not in localized:
                        reason_counts["local_images_incomplete"] += 1
                        local_image_failure_count += 1
                        continue
                    seen.add(str(validation["identity"]))
                    is_existing = bool(
                        formal_database_identities(platform_key, record)
                        & existing_identities
                    )
                    effective_keyword = effective_source_keyword(
                        record,
                        summary.get("keyword"),
                    )
                    topic_relevant = topic_relevant_for_web_post(
                        platform_key,
                        record,
                        fallback_keyword=effective_keyword,
                    )
                    if topic_relevant:
                        valid_existing_count += int(is_existing)
                        valid_new_count += int(not is_existing)
                        topic_relevant_existing_count += int(is_existing)
                        topic_relevant_new_count += int(not is_existing)
                    else:
                        topic_irrelevant_existing_count += int(is_existing)
                        topic_irrelevant_new_count += int(not is_existing)
                    selected.append(
                        {
                            "platform": platform_key,
                            "record": record,
                            "identity": validation["identity"],
                            "source_path": str(path),
                            "line_number": line_number,
                            "is_new": not is_existing,
                            "topic_relevant": topic_relevant,
                            "manifest_paths": list((output or {}).get("image_manifest_paths") or []),
                            "materialized_images": materialized.get(identity),
                        }
                    )
            _runtime_progress(progress_callback)

    output_record_count = candidate_count
    pagination_evidence = pagination_evidence or {}
    run_candidate_count = int(pagination_evidence.get("candidate_count") or 0)
    candidate_count = max(candidate_count, run_candidate_count)
    stop_event = pagination_evidence.get("stop_event") or {}
    skipped_candidate_count = max(
        int(pagination_evidence.get("skipped_candidate_count") or 0),
        int(stop_event.get("skipped_candidate_count") or 0),
        len(stop_event.get("skipped_candidate_failures") or []),
    )
    unverified_douyin_first_page_empty = bool(
        stop_event.get("platform") == "douyin"
        and stop_event.get("stop_reason") == "source_exhausted"
        and stop_event.get("stop_detail") in {"empty_page", "has_more_false"}
        and stop_event.get("source_page") in (1, "1")
        and stop_event.get("source_offset") in (None, "", 0, "0")
        and not str(stop_event.get("source_cursor") or "").strip()
        and stop_event.get("raw_batch_count") in (None, "", 0, "0")
    )
    source_exhausted_met = bool(
        pagination_evidence.get("stopped")
        and pagination_evidence.get("stop_reason") == "source_exhausted"
        and not unverified_douyin_first_page_empty
    )
    pagination_stop_reason = str(pagination_evidence.get("stop_reason") or "")
    pagination_incomplete = bool(
        pagination_evidence.get("available")
        and not pagination_evidence.get("stopped")
    )
    pagination_runtime_blocked = bool(
        pagination_stop_reason
        in {"runtime_failed", "login_required", "captcha_detected"}
    )
    completion_met = (
        source_exhausted_met
        and not pagination_runtime_blocked
        and not pagination_incomplete
    )
    repair_import_met = False
    if repair_mode:
        repair_import_met = (
            bool(selected)
            and not pagination_runtime_blocked
            and not pagination_incomplete
        )
        completion_met = bool(
            repair_import_met
            and run_candidate_count > 0
            and len(selected) == run_candidate_count
            and skipped_candidate_count == 0
        )
    if unverified_douyin_first_page_empty:
        stop_reason = "runtime_failed"
    elif pagination_runtime_blocked or pagination_incomplete:
        stop_reason = pagination_stop_reason
        if not stop_reason:
            stop_reason = "runtime_failed"
    elif repair_mode and completion_met:
        stop_reason = "repair_targets_processed"
    elif repair_mode and selected:
        stop_reason = "repair_targets_partially_processed"
    elif repair_mode:
        stop_reason = "repair_no_valid_detail"
    elif pagination_evidence.get("stopped"):
        stop_reason = str(pagination_evidence.get("stop_reason") or "runtime_failed")
    else:
        stop_reason = "runtime_failed"
    validation_summary = {
        "completion_mode": "source-exhausted",
        "repair_mode": repair_mode,
        "candidate_count": candidate_count,
        "run_candidate_count": run_candidate_count,
        "output_record_count": output_record_count,
        "valid_new_count": valid_new_count,
        "valid_existing_count": valid_existing_count,
        "valid_total_count": len(selected),
        "topic_relevant_new_count": topic_relevant_new_count,
        "topic_relevant_existing_count": topic_relevant_existing_count,
        "topic_irrelevant_new_count": topic_irrelevant_new_count,
        "topic_irrelevant_existing_count": topic_irrelevant_existing_count,
        "source_exhausted_met": source_exhausted_met,
        "pagination_runtime_blocked": pagination_runtime_blocked,
        "pagination_incomplete": pagination_incomplete,
        "skipped_candidate_count": skipped_candidate_count,
        "skipped_candidate_failures": list(
            pagination_evidence.get("skipped_candidate_failures") or []
        ),
        "repair_import_met": repair_import_met,
        "all_repair_targets_valid": completion_met if repair_mode else None,
        "completion_met": completion_met,
        "local_images_required": require_local_images,
        "local_images_complete": (
            not require_local_images or local_image_failure_count == 0
        ),
        "local_image_failure_count": local_image_failure_count,
        "stop_reason": stop_reason,
        "stop_detail": (
            "unverified_empty_first_page"
            if unverified_douyin_first_page_empty
            else str(pagination_evidence.get("stop_detail") or "")
        ),
        "pagination_evidence": pagination_evidence,
        "parse_errors": parse_errors,
        "invalid_reason_counts": dict(sorted(reason_counts.items())),
        "new_identities": [item["identity"] for item in selected if item["is_new"]],
        "existing_identities": [item["identity"] for item in selected if not item["is_new"]],
        "topic_relevant_new_identities": [
            item["identity"]
            for item in selected
            if item["is_new"] and item["topic_relevant"]
        ],
        "topic_irrelevant_new_identities": [
            item["identity"]
            for item in selected
            if item["is_new"] and not item["topic_relevant"]
        ],
        "valid_new_samples": [
            {
                "identity": item["identity"],
                "source_path": item["source_path"],
                "line_number": item["line_number"],
            }
            for item in selected
            if item["is_new"] and item["topic_relevant"]
        ][:5],
        "valid_existing_samples": [
            {
                "identity": item["identity"],
                "source_path": item["source_path"],
                "line_number": item["line_number"],
            }
            for item in selected
            if not item["is_new"] and item["topic_relevant"]
        ][:5],
    }
    _runtime_progress(progress_callback)
    return validation_summary, selected


def resolve_media_root(
    value: str | Path,
    *,
    project_root: str | Path = PROJECT_ROOT,
    default_media_root: str | Path = LOCAL_MEDIA_ROOT,
) -> Path:
    root = Path(project_root).expanduser().resolve(strict=True)
    media_root = Path(value).expanduser().resolve()
    formal_root = Path(default_media_root).expanduser().resolve()
    temp_root = (root / "temp").resolve()
    if media_root != formal_root and media_root != temp_root and temp_root not in media_root.parents:
        raise ImagePersistenceError(
            "--media-root must be LOCAL_MEDIA_ROOT or a directory below project temp/"
        )
    if media_root != root and root not in media_root.parents:
        raise ImagePersistenceError("media root escapes project root")
    return media_root


def _project_relative_evidence_path(path: Path, project_root: Path) -> str:
    resolved = path.expanduser().resolve(strict=True)
    if resolved != project_root and project_root not in resolved.parents:
        raise ImageMaterializationError(
            "image_path_escape",
            f"image evidence escapes project root: {resolved}",
        )
    return resolved.relative_to(project_root).as_posix()


def _load_manifest_with_evidence(
    path_value: str | Path,
    *,
    project_root: Path,
) -> tuple[Path, tuple[ImageManifestEntry, ...], dict[tuple[str, str, int], int]]:
    path = Path(path_value).expanduser().resolve(strict=True)
    _project_relative_evidence_path(path, project_root)
    payload = path.read_bytes()
    entries = parse_manifest(payload)
    line_numbers: dict[tuple[str, str, int], int] = {}
    entry_offset = 0
    for line_number, line in enumerate(payload.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        entry = entries[entry_offset]
        entry_offset += 1
        line_numbers[(entry.platform_key, entry.platform_post_id, entry.source_index)] = line_number
    return path, entries, line_numbers


def _staging_root_for_manifest_entry(
    manifest_path: Path,
    entry: ImageManifestEntry,
) -> Path:
    staging_path = Path(str(entry.staging_path))
    if staging_path.parts and staging_path.parts[0] == manifest_path.parent.name:
        return manifest_path.parent.parent
    return manifest_path.parent


def rollback_newly_promoted_images(
    materialized_by_identity: dict[str, list[MaterializedImage]],
    *,
    project_root: str | Path = PROJECT_ROOT,
    media_root: str | Path = LOCAL_MEDIA_ROOT,
    db_path: str | Path | None = None,
    progress_callback: Callable[[], object] | None = None,
) -> int:
    _runtime_progress(progress_callback)
    root = Path(project_root).expanduser().resolve(strict=True)
    media = Path(media_root).expanduser().resolve()
    if media != root and root not in media.parents:
        raise ImagePersistenceError("media root escapes project root")
    referenced_paths: set[str] = set()
    if db_path is not None:
        resolved_db = Path(db_path).expanduser().resolve()
        if resolved_db.is_file():
            try:
                with sqlite3.connect(f"file:{resolved_db}?mode=ro", uri=True) as conn:
                    referenced_paths = {
                        str(row[0])
                        for row in conn.execute(
                            """
                            SELECT DISTINCT local_path
                            FROM web_post_images
                            WHERE local_path IS NOT NULL AND local_path != ''
                            """
                        )
                    }
            except sqlite3.Error as exc:
                print(
                    "[image_promotion_rollback_skipped] "
                    f"reason=sqlite_reference_check_failed error={type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                return 0
    removed = 0
    candidate_dirs: set[Path] = set()
    for images in materialized_by_identity.values():
        _runtime_progress(progress_callback)
        for item in images:
            if item.reused:
                continue
            if item.local_path in referenced_paths:
                continue
            path = (root / item.local_path).resolve()
            if path != media and media not in path.parents:
                raise ImagePersistenceError("promoted image escapes media root")
            candidate_dirs.add(path.parent)
            if path.is_file():
                path.unlink()
                removed += 1
    for directory in sorted(candidate_dirs, key=lambda value: len(value.parts), reverse=True):
        _runtime_progress(progress_callback)
        current = directory
        while current != media and media in current.parents:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent
    return removed


@contextmanager
def formal_media_persistence_lock(
    *,
    enabled: bool,
    lock_path: str | Path = FORMAL_MEDIA_PERSISTENCE_LOCK,
    progress_callback: Callable[[], object] | None = None,
) -> Iterator[None]:
    if not enabled:
        yield
        return
    resolved_lock = ensure_parent(lock_path)
    with resolved_lock.open("a+b") as handle:
        if progress_callback is None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        else:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    _runtime_progress(progress_callback)
                    time.sleep(min(1.0, PROCESS_PROGRESS_POLL_SECONDS))
            _runtime_progress(progress_callback)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validated_manifest_rows_for_post(
    post_manifest_rows: list[tuple[ImageManifestEntry, Path, int]],
    candidates: list[ImageCandidate],
    *,
    project_root: Path,
) -> tuple[
    list[tuple[ImageCandidate, ImageManifestEntry, Path, int]],
    list[dict[str, Any]],
    list[tuple[ImageManifestEntry, Path, int]],
]:
    entries = [row[0] for row in post_manifest_rows]
    try:
        ordered_entries = validate_post_manifest(
            entries,
            candidates,
            require_downloaded=True,
        )
    except ImageManifestError as original_error:
        if (
            not candidates
            or any(candidate.platform_key != "zhihu" for candidate in candidates)
            or original_error.code != "image_manifest_count_mismatch"
        ):
            raise

        if len({manifest_path.resolve() for _, manifest_path, _ in post_manifest_rows}) != 1:
            raise original_error

        def is_real_zhimg_url(source_url: str) -> bool:
            hostname = (urlparse(source_url).hostname or "").lower()
            return hostname == "zhimg.com" or hostname.endswith(".zhimg.com")

        def legacy_zhihu_asset_key(source_url: str) -> str:
            path = urlparse(source_url).path
            logical_path = LEGACY_ZHIHU_TRANSFORM_SUFFIX_RE.sub("", path)
            legacy_identity = RASTER_IMAGE_SUFFIX_RE.sub("", logical_path)
            digest = sha256(legacy_identity.encode("utf-8")).hexdigest()
            return f"zhihu:urlsha256:{digest}"

        if any(not is_real_zhimg_url(candidate.source_url) for candidate in candidates):
            raise original_error

        evidence_by_entry_id = {
            id(entry): (manifest_path, line_number)
            for entry, manifest_path, line_number in post_manifest_rows
        }
        expected_by_asset = {
            candidate.source_asset_key: candidate for candidate in candidates
        }
        if len(expected_by_asset) != len(candidates):
            raise original_error

        entries_by_asset: dict[str, list[ImageManifestEntry]] = {}
        for entry in entries:
            if (
                entry.platform_key != "zhihu"
                or entry.image_role != "content"
                or entry.fetch_status != "downloaded"
                or entry.sha256 is None
                or not is_real_zhimg_url(entry.source_url)
            ):
                raise original_error
            canonical_asset_key = source_asset_key_for_image(
                "zhihu",
                entry.source_url,
            )
            if entry.source_asset_key not in {
                canonical_asset_key,
                legacy_zhihu_asset_key(entry.source_url),
            }:
                raise original_error
            entries_by_asset.setdefault(canonical_asset_key, []).append(entry)
        if set(entries_by_asset) != set(expected_by_asset):
            raise original_error

        matched_rows: list[tuple[ImageCandidate, ImageManifestEntry, Path, int]] = []
        reconciliations: list[dict[str, Any]] = []
        reconciliation_rows: list[tuple[ImageManifestEntry, Path, int]] = []
        for candidate in candidates:
            grouped_entries = entries_by_asset[candidate.source_asset_key]
            if any(
                entry.platform_post_id != candidate.platform_post_id
                or entry.source_key != candidate.source_key
                or source_asset_key_for_image("zhihu", entry.source_url)
                != candidate.source_asset_key
                for entry in grouped_entries
            ):
                raise original_error
            sha256_values = {entry.sha256 for entry in grouped_entries}
            if len(sha256_values) != 1 or len(
                {entry.source_url for entry in grouped_entries}
            ) != len(grouped_entries):
                raise original_error
            retained_entry = min(
                grouped_entries,
                key=lambda entry: (
                    entry.source_url != candidate.source_url,
                    entry.source_index,
                ),
            )
            manifest_path, line_number = evidence_by_entry_id[id(retained_entry)]
            matched_rows.append(
                (candidate, retained_entry, manifest_path, line_number)
            )
            for duplicate_entry in grouped_entries:
                if duplicate_entry is retained_entry:
                    continue
                duplicate_path, duplicate_line = evidence_by_entry_id[id(duplicate_entry)]
                reconciliation_rows.append(
                    (duplicate_entry, duplicate_path, duplicate_line)
                )
                reconciliations.append(
                    {
                        "identity": (
                            f"{candidate.platform_key}:id:{candidate.platform_post_id}"
                        ),
                        "sha256": str(retained_entry.sha256),
                        "retained_source_index": retained_entry.source_index,
                        "retained_source_url": retained_entry.source_url,
                        "retained_manifest_path": _project_relative_evidence_path(
                            manifest_path,
                            project_root,
                        ),
                        "retained_manifest_line": line_number,
                        "duplicate_source_index": duplicate_entry.source_index,
                        "duplicate_source_url": duplicate_entry.source_url,
                        "duplicate_manifest_path": _project_relative_evidence_path(
                            duplicate_path,
                            project_root,
                        ),
                        "duplicate_manifest_line": duplicate_line,
                    }
                )
        return matched_rows, reconciliations, reconciliation_rows

    evidence_by_entry_id = {
        id(entry): (manifest_path, line_number)
        for entry, manifest_path, line_number in post_manifest_rows
    }
    matched_rows = []
    for candidate, entry in zip(candidates, ordered_entries, strict=True):
        manifest_path, line_number = evidence_by_entry_id[id(entry)]
        matched_rows.append((candidate, entry, manifest_path, line_number))
    return matched_rows, [], []


def materialize_formal_record_images(
    selected: list[dict[str, Any]],
    *,
    project_root: str | Path = PROJECT_ROOT,
    media_root: str | Path = LOCAL_MEDIA_ROOT,
    promote: bool,
    progress_callback: Callable[[], object] | None = None,
) -> tuple[dict[str, Any], dict[str, list[MaterializedImage]], set[str]]:
    _runtime_progress(progress_callback)
    root = Path(project_root).expanduser().resolve(strict=True)
    resolved_media_root = Path(media_root).expanduser().resolve()
    if resolved_media_root != root and root not in resolved_media_root.parents:
        raise ImagePersistenceError("media root escapes project root")

    cache: dict[
        Path,
        tuple[
            tuple[ImageManifestEntry, ...],
            dict[tuple[str, str, int], int],
            str,
        ],
    ] = {}
    materialized_by_identity: dict[str, list[MaterializedImage]] = {}
    complete_identities: set[str] = set()
    manifest_evidence: dict[str, str] = {}
    failures: list[dict[str, Any]] = []
    expected_images = 0
    downloaded_images = 0
    validated_images = 0
    unique_images = 0
    sha256_duplicate_images = 0
    promoted_images = 0
    reused_images = 0
    retryable_failures = 0
    terminal_failures = 0
    sha256_duplicates: list[dict[str, Any]] = []
    legacy_manifest_reconciliations: list[dict[str, Any]] = []
    rolled_back_images = 0

    for item in selected:
        _runtime_progress(progress_callback)
        identity = str(item.get("identity") or "")
        platform_key = str(item.get("platform") or "")
        record = item.get("record") if isinstance(item.get("record"), dict) else {}
        candidates = content_image_candidates(platform_key, record)
        expected_images += len(candidates)
        post_id = post_id_for_record(platform_key, record)
        post_manifest_rows: list[
            tuple[ImageManifestEntry, Path, int]
        ] = []
        try:
            manifest_values: list[Path] = []
            seen_manifest_paths: set[Path] = set()
            for path_value in item.get("manifest_paths") or []:
                unresolved_path = Path(path_value).expanduser()
                resolved_path = (
                    unresolved_path.resolve()
                    if unresolved_path.is_absolute()
                    else (root / unresolved_path).resolve()
                )
                if resolved_path in seen_manifest_paths:
                    continue
                seen_manifest_paths.add(resolved_path)
                manifest_values.append(resolved_path)
            if not manifest_values:
                raise ImageManifestError(
                    "missing_image_manifest",
                    f"no image manifest is associated with {identity}",
                )
            for path_value in manifest_values:
                unresolved_path = Path(path_value).expanduser()
                cache_key = unresolved_path.resolve()
                cached = cache.get(cache_key)
                if cached is None:
                    path, entries, line_numbers = _load_manifest_with_evidence(
                        unresolved_path,
                        project_root=root,
                    )
                    payload_sha256 = sha256(path.read_bytes()).hexdigest()
                    cached = (entries, line_numbers, payload_sha256)
                    cache[path] = cached
                    manifest_evidence[_project_relative_evidence_path(path, root)] = payload_sha256
                else:
                    path = cache_key
                entries, line_numbers, _ = cached
                for entry in entries:
                    if entry.platform_key == platform_key and entry.platform_post_id == post_id:
                        post_manifest_rows.append(
                            (
                                entry,
                                path,
                                line_numbers[
                                    (entry.platform_key, entry.platform_post_id, entry.source_index)
                                ],
                            )
                        )

            (
                matched_manifest_rows,
                post_reconciliations,
                reconciliation_rows,
            ) = _validated_manifest_rows_for_post(
                post_manifest_rows,
                candidates,
                project_root=root,
            )
            downloaded_images += sum(
                entry.fetch_status == "downloaded"
                for _, entry, _, _ in matched_manifest_rows
            )
            validated_rows: list[
                tuple[ImageCandidate, ValidatedImage, Path, int, Path]
            ] = []
            for candidate, entry, manifest_path, manifest_line in matched_manifest_rows:
                _runtime_progress(progress_callback)
                staging_root = _staging_root_for_manifest_entry(manifest_path, entry)
                staged_path = staging_root / str(entry.staging_path)
                validated = validate_image_file(
                    staged_path,
                    allowed_root=staging_root,
                    expected_sha256=entry.sha256,
                    max_bytes=DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
                    require_suffix_match=True,
                )
                if any(
                    (
                        validated.size_bytes != entry.size_bytes,
                        validated.mime_type != entry.mime_type,
                        validated.width != entry.width,
                        validated.height != entry.height,
                    )
                ):
                    raise ImageMaterializationError(
                        "image_manifest_metadata_mismatch",
                        f"manifest byte metadata does not match staging file for {identity}",
                    )
                validated_images += 1
                validated_rows.append(
                    (candidate, validated, manifest_path, manifest_line, staging_root)
                )

            for entry, manifest_path, _ in reconciliation_rows:
                _runtime_progress(progress_callback)
                staging_root = _staging_root_for_manifest_entry(manifest_path, entry)
                staged_path = staging_root / str(entry.staging_path)
                validated = validate_image_file(
                    staged_path,
                    allowed_root=staging_root,
                    expected_sha256=entry.sha256,
                    max_bytes=DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
                    require_suffix_match=True,
                )
                if any(
                    (
                        validated.size_bytes != entry.size_bytes,
                        validated.mime_type != entry.mime_type,
                        validated.width != entry.width,
                        validated.height != entry.height,
                    )
                ):
                    raise ImageMaterializationError(
                        "image_manifest_metadata_mismatch",
                        "legacy manifest byte metadata does not match staging "
                        f"file for {identity}",
                    )
            legacy_manifest_reconciliations.extend(post_reconciliations)

            retained_rows: list[
                tuple[ImageCandidate, ValidatedImage, Path, int, Path]
            ] = []
            retained_by_sha256: dict[str, int] = {}
            duplicate_sources_by_retained: dict[int, list[Sha256DuplicateSource]] = {}
            for (
                candidate,
                validated,
                manifest_path,
                manifest_line,
                staging_root,
            ) in validated_rows:
                retained_position = retained_by_sha256.get(validated.sha256)
                if retained_position is None:
                    retained_by_sha256[validated.sha256] = len(retained_rows)
                    retained_rows.append(
                        (
                            candidate,
                            validated,
                            manifest_path,
                            manifest_line,
                            staging_root,
                        )
                    )
                    continue
                (
                    retained_candidate,
                    _,
                    retained_manifest_path,
                    retained_manifest_line,
                    _,
                ) = retained_rows[retained_position]
                duplicate = Sha256DuplicateSource(
                    source_index=candidate.source_index,
                    source_key=candidate.source_key,
                    source_asset_key=candidate.source_asset_key,
                    source_url=candidate.source_url,
                    manifest_path=_project_relative_evidence_path(manifest_path, root),
                    manifest_line=manifest_line,
                )
                duplicate_sources_by_retained.setdefault(retained_position, []).append(
                    duplicate
                )
                sha256_duplicate_images += 1
                sha256_duplicates.append(
                    {
                        "identity": identity,
                        "sha256": validated.sha256,
                        "retained_source_index": retained_candidate.source_index,
                        "retained_source_url": retained_candidate.source_url,
                        "retained_manifest_path": _project_relative_evidence_path(
                            retained_manifest_path,
                            root,
                        ),
                        "retained_manifest_line": retained_manifest_line,
                        "duplicate_source_index": candidate.source_index,
                        "duplicate_source_url": candidate.source_url,
                        "duplicate_manifest_path": duplicate.manifest_path,
                        "duplicate_manifest_line": duplicate.manifest_line,
                    }
                )
            unique_images += len(retained_rows)

            post_materialized: list[MaterializedImage] = []
            if not promote:
                complete_identities.add(identity)
                continue
            materialized_by_identity[identity] = post_materialized
            for retained_index, (
                candidate,
                validated,
                manifest_path,
                manifest_line,
                staging_root,
            ) in enumerate(retained_rows):
                _runtime_progress(progress_callback)
                persistence_candidate = replace(candidate, source_index=retained_index)
                promoted = promote_validated_image(
                    validated,
                    persistence_candidate,
                    staging_root=staging_root,
                    media_root=resolved_media_root,
                    project_root=root,
                )
                promoted = replace(
                    promoted,
                    manifest_source_index=candidate.source_index,
                    manifest_path=_project_relative_evidence_path(manifest_path, root),
                    manifest_line=manifest_line,
                    sha256_duplicate_sources=tuple(
                        duplicate_sources_by_retained.get(retained_index, [])
                    ),
                )
                post_materialized.append(promoted)
                reused_images += int(promoted.reused)
                promoted_images += int(not promoted.reused)
            if len(post_materialized) != len(retained_rows):
                raise ImageMaterializationError(
                    "image_manifest_count_mismatch",
                    f"promoted image count does not match SHA-256 unique images for {identity}",
                )
            complete_identities.add(identity)
        except BaseException as exc:
            expected_failure = isinstance(
                exc,
                (ImageManifestError, ImageMaterializationError, OSError, UnicodeError),
            )
            if not expected_failure:
                if promote:
                    rolled_back_images += rollback_newly_promoted_images(
                        materialized_by_identity,
                        project_root=root,
                        media_root=resolved_media_root,
                        progress_callback=progress_callback,
                    )
                raise
            code = getattr(exc, "code", "missing_image_manifest")
            failed_rows = [row for row in post_manifest_rows if row[0].fetch_status == "failed"]
            retryable_count = sum(
                row[0].error_code == "image_download_retryable" for row in failed_rows
            )
            failure_count = max(1, len(failed_rows), len(candidates) - len(post_manifest_rows))
            if code == "image_download_retryable":
                retryable_count = max(retryable_count, failure_count)
            retryable_failures += retryable_count
            terminal_failures += max(0, failure_count - retryable_count)
            failures.append(
                {
                    "identity": identity,
                    "code": str(code),
                    "message": str(exc),
                    "source_path": str(item.get("source_path") or ""),
                    "line_number": item.get("line_number"),
                }
            )

    manifest_items = [
        {"path": path, "sha256": digest}
        for path, digest in sorted(manifest_evidence.items())
    ]
    aggregate_manifest_sha256 = sha256(
        json.dumps(manifest_items, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    complete = (
        len(complete_identities) == len(selected)
        and validated_images == expected_images
        and not failures
    )
    if promote and not complete:
        rolled_back_images = rollback_newly_promoted_images(
            materialized_by_identity,
            project_root=root,
            media_root=resolved_media_root,
            progress_callback=progress_callback,
        )
        materialized_by_identity = {}
        complete_identities = set()
        promoted_images = 0
        reused_images = 0
    report = {
        "required": True,
        "promotion_required": promote,
        "candidate_posts": len(selected),
        "complete_posts": len(complete_identities),
        "expected_images": expected_images,
        "downloaded_images": downloaded_images,
        "validated_images": validated_images,
        "unique_images": unique_images,
        "sha256_duplicate_images": sha256_duplicate_images,
        "sha256_duplicates": sha256_duplicates,
        "legacy_manifest_reconciled_images": len(legacy_manifest_reconciliations),
        "legacy_manifest_reconciliations": legacy_manifest_reconciliations,
        "reused_images": reused_images,
        "promoted_images": promoted_images,
        "rolled_back_images": rolled_back_images,
        "retryable_failures": retryable_failures,
        "terminal_failures": terminal_failures,
        "complete": complete,
        "manifest_paths": [item["path"] for item in manifest_items],
        "manifest_sha256": aggregate_manifest_sha256,
        "manifest_evidence": manifest_items,
        "failures": failures,
    }
    _runtime_progress(progress_callback)
    return report, materialized_by_identity, complete_identities


def find_existing_post(conn: sqlite3.Connection, row: dict[str, Any]) -> int | None:
    if row.get("platform_post_id"):
        found = conn.execute(
            "SELECT id FROM web_posts WHERE platform_key=? AND platform_post_id=?",
            (row["platform_key"], row["platform_post_id"]),
        ).fetchone()
        if found:
            return int(found[0])
    if row.get("canonical_url"):
        found = conn.execute(
            "SELECT id FROM web_posts WHERE platform_key=? AND canonical_url=?",
            (row["platform_key"], row["canonical_url"]),
        ).fetchone()
        if found:
            return int(found[0])
    return None


def upsert_web_post(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    *,
    project_root: str | Path = PROJECT_ROOT,
    media_root: str | Path = LOCAL_MEDIA_ROOT,
    require_local_images: bool = False,
) -> tuple[int, bool]:
    post_row = dict(row)
    image_items = _normalize_persistence_items(list(post_row.pop("_image_items", [])))
    content_count = sum(item["role"] == "content" for item in image_items)
    if post_row.get("post_images_count") is not None and int(post_row["post_images_count"]) != content_count:
        raise ImagePersistenceError("post_images_count does not match projected content images")
    existing_id = find_existing_post(conn, post_row)
    platform_key = str(post_row.get("platform_key") or "")
    existing_images = _existing_image_records(conn, existing_id) if existing_id else []
    resolved_project_root = Path(project_root).expanduser().resolve(strict=True)
    resolved_media_root = Path(media_root).expanduser().resolve()
    if resolved_media_root != resolved_project_root and resolved_project_root not in resolved_media_root.parents:
        raise ImagePersistenceError("media root escapes project root")
    prepared_images = _prepare_image_rows(
        platform_key,
        image_items,
        existing_images,
        project_root=resolved_project_root,
        media_root=resolved_media_root,
        require_local_images=require_local_images,
    )

    conn.execute("SAVEPOINT trippostcollect_web_post_upsert")
    try:
        columns = list(post_row)
        if existing_id:
            updates = ", ".join(f"{column}=:{column}" for column in columns)
            conn.execute(
                f"UPDATE web_posts SET {updates}, updated_at=datetime('now') WHERE id=:id",
                {**post_row, "id": existing_id},
            )
            post_id = existing_id
        else:
            placeholders = ", ".join(f":{column}" for column in columns)
            conn.execute(
                f"INSERT INTO web_posts ({', '.join(columns)}) VALUES ({placeholders})",
                post_row,
            )
            post_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        replace_image_rows(conn, post_id, prepared_images)
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT trippostcollect_web_post_upsert")
        conn.execute("RELEASE SAVEPOINT trippostcollect_web_post_upsert")
        raise
    conn.execute("RELEASE SAVEPOINT trippostcollect_web_post_upsert")
    return post_id, existing_id is None


class FormalImportBeforeCommitError(RuntimeError):

    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cause = cause


def commit_formal_import(conn: sqlite3.Connection) -> None:
    conn.commit()


def import_valid_records(
    summary: dict[str, Any],
    selected: list[dict[str, Any]],
    db_path: Path,
    *,
    project_root: str | Path = PROJECT_ROOT,
    media_root: str | Path = LOCAL_MEDIA_ROOT,
    require_local_images: bool = False,
    progress_callback: Callable[[], object] | None = None,
) -> dict[str, Any]:
    _runtime_progress(progress_callback)
    db_path = ensure_parent(db_path)
    captured_at = str(summary.get("captured_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"))
    keyword = str(summary.get("keyword") or "")
    processed = inserted = updated = 0
    relevant_inserted = relevant_updated = 0
    irrelevant_inserted = irrelevant_updated = 0
    conn = connect_db(db_path, busy_timeout_ms=FORMAL_SQLITE_BUSY_TIMEOUT_MS)
    commit_started = False
    try:
        db_sync = ensure_web_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        for item in selected:
            _runtime_progress(progress_callback)
            record = item["record"]
            platform_key = str(item["platform"])
            row = row_for_record(
                platform_key,
                record,
                artifact_dir=str(Path(str(summary.get("batch_dir") or "")).resolve()),
                captured_at=captured_at,
                keyword=keyword,
                materialized_images=item.get("materialized_images"),
            )
            _, was_inserted = upsert_web_post(
                conn,
                row,
                project_root=project_root,
                media_root=media_root,
                require_local_images=require_local_images,
            )
            processed += 1
            inserted += int(was_inserted)
            updated += int(not was_inserted)
            relevant = bool(row["topic_relevant"])
            relevant_inserted += int(relevant and was_inserted)
            relevant_updated += int(relevant and not was_inserted)
            irrelevant_inserted += int(not relevant and was_inserted)
            irrelevant_updated += int(not relevant and not was_inserted)
        commit_started = True
        _runtime_progress(progress_callback)
        commit_formal_import(conn)
    except XhsRuntimeSupervisionError:
        conn.rollback()
        raise
    except BaseException as exc:
        if commit_started and not conn.in_transaction:
            raise
        conn.rollback()
        raise FormalImportBeforeCommitError(exc) from exc
    finally:
        conn.close()
    return {
        "db": str(db_path),
        "db_sync": db_sync,
        "processed_rows": processed,
        "inserted_rows": inserted,
        "updated_rows": updated,
        "topic_relevant_inserted_rows": relevant_inserted,
        "topic_relevant_updated_rows": relevant_updated,
        "topic_irrelevant_inserted_rows": irrelevant_inserted,
        "topic_irrelevant_updated_rows": irrelevant_updated,
        "skipped": 0,
        "skipped_video": 0,
        "parse_errors": 0,
    }


def import_valid_records_with_media_rollback(
    summary: dict[str, Any],
    selected: list[dict[str, Any]],
    db_path: Path,
    *,
    materialized_images_by_identity: dict[str, list[MaterializedImage]],
    image_materialization: dict[str, Any],
    project_root: str | Path = PROJECT_ROOT,
    media_root: str | Path = LOCAL_MEDIA_ROOT,
    progress_callback: Callable[[], object] | None = None,
) -> dict[str, Any]:
    try:
        return import_valid_records(
            summary,
            selected,
            db_path,
            project_root=project_root,
            media_root=media_root,
            require_local_images=True,
            progress_callback=progress_callback,
        )
    except FormalImportBeforeCommitError as exc:
        rolled_back = rollback_newly_promoted_images(
            materialized_images_by_identity,
            project_root=project_root,
            media_root=media_root,
            db_path=db_path,
            progress_callback=progress_callback,
        )
        image_materialization["rolled_back_images"] = int(
            image_materialization.get("rolled_back_images") or 0
        ) + rolled_back
        image_materialization["promoted_images"] = 0
        print(
            "[image_promotion_rollback] "
            f"reason=sqlite_import_failed removed_new_files={rolled_back}",
            file=sys.stderr,
            flush=True,
        )
        return {
            "db": str(db_path),
            "processed_rows": 0,
            "inserted_rows": 0,
            "updated_rows": 0,
            "topic_relevant_inserted_rows": 0,
            "topic_relevant_updated_rows": 0,
            "topic_irrelevant_inserted_rows": 0,
            "topic_irrelevant_updated_rows": 0,
            "skipped": len(selected),
            "skipped_video": 0,
            "parse_errors": 0,
            "reason": "sqlite_import_failed",
            "error": str(exc),
            "rolled_back_images": rolled_back,
        }


def normalize_bilibili_article_record(item: dict[str, Any], keyword: str) -> dict[str, Any] | None:
    post_id = str(item.get("id") or "").strip()
    if not post_id:
        return None
    title = clean_html_text(item.get("title"))
    desc = clean_html_text(item.get("desc"))
    if not title and not desc:
        return None
    content_url = str(item.get("arcurl") or item.get("url") or f"https://www.bilibili.com/read/cv{post_id}/")
    search_preview_urls = item.get("image_urls") if isinstance(item.get("image_urls"), list) else []
    record = dict(item)
    record.update(
        {
            "id": post_id,
            "content_id": post_id,
            "content_type": "article",
            "title": title,
            "desc": desc,
            "search_desc": desc,
            "search_excerpt_length": len(desc),
            "search_preview_urls": search_preview_urls,
            "search_preview_count": len(search_preview_urls),
            "content_text": "",
            "content_detail_status": "search_only",
            "content_detail_source": "article_search_api",
            "content_images_detail_status": "search_only",
            "content_url": content_url,
            "image_urls": [],
            "source_keyword": keyword,
            "created_time": item.get("pubdate") or item.get("pub_time"),
            "published_at": item.get("pubdate") or item.get("pub_time"),
            "liked_count": item.get("like"),
            "comment_count": item.get("reply"),
            "view_count": item.get("view"),
            "nickname": item.get("author"),
            "user_id": item.get("mid"),
            "raw_bilibili_type": item.get("type"),
        }
    )
    return record


def clean_bilibili_article_body(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(?:p|div|li|blockquote|h[1-6]|section|article)\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_bilibili_detail_image_url(value: Any) -> str | None:
    url = normalize_image_url(value)
    if url and url.startswith("http://"):
        return "https://" + url.removeprefix("http://")
    return url


def extract_bilibili_detail_images(detail: dict[str, Any]) -> tuple[list[str], list[str]]:
    images: list[str] = []
    sources: list[str] = []
    seen: set[str] = set()

    def add(value: Any, source: str) -> None:
        url = normalize_bilibili_detail_image_url(value)
        if not url or url in seen:
            return
        seen.add(url)
        images.append(url)
        sources.append(source)

    opus = detail.get("opus") if isinstance(detail.get("opus"), dict) else {}
    opus_content = opus.get("content") if isinstance(opus.get("content"), dict) else {}
    paragraphs = opus_content.get("paragraphs") if isinstance(opus_content.get("paragraphs"), list) else []
    for paragraph in paragraphs:
        if not isinstance(paragraph, dict):
            continue
        pic = paragraph.get("pic") if isinstance(paragraph.get("pic"), dict) else {}
        pics = pic.get("pics") if isinstance(pic.get("pics"), list) else []
        for item in pics:
            if isinstance(item, dict):
                add(item.get("url") or item.get("src"), "opus_paragraph_pic")
            else:
                add(item, "opus_paragraph_pic")

    content = str(detail.get("content") or "")
    for match in BILIBILI_HTML_IMAGE_RE.finditer(content):
        add(match.group(1) or match.group(2), "content_html_img")

    for key in ("content_pic_list",):
        values = detail.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, dict):
                add(
                    item.get("url") or item.get("src") or item.get("image_url"),
                    f"detail_{key}",
                )
            else:
                add(item, f"detail_{key}")

    if not images:
        for key in ("origin_image_urls", "image_urls"):
            values = detail.get(key)
            if not isinstance(values, list):
                continue
            for item in values:
                if isinstance(item, dict):
                    add(
                        item.get("url") or item.get("src") or item.get("image_url"),
                        f"detail_{key}",
                    )
                else:
                    add(item, f"detail_{key}")
    return images, sources


def bilibili_detail_headers(post_id: str, cookie_header: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": BILIBILI_BROWSER_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://www.bilibili.com",
        "Referer": f"https://www.bilibili.com/read/cv{post_id}/",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def bilibili_image_headers(post_id: str, cookie_header: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": BILIBILI_BROWSER_USER_AGENT,
        "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif;q=0.9",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": f"https://www.bilibili.com/read/cv{post_id}/",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def fetch_bilibili_image_bytes(
    source_url: str,
    post_id: str,
    cookie_header: str = "",
) -> RemoteImagePreview:
    return fetch_remote_image_bytes(
        source_url,
        headers=bilibili_image_headers(post_id, cookie_header),
        max_bytes=DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
        timeout_seconds=30,
        allowed_media_types=SUPPORTED_IMAGE_MIME_TYPES,
    )


def download_bilibili_record_images(
    record: dict[str, Any],
    *,
    cookie_header: str,
    platform_data_root: Path,
    fetcher: Any | None = None,
    sleep_fn: Any | None = None,
    log_fn: Any | None = None,
    max_attempts: int = BILIBILI_IMAGE_MAX_ATTEMPTS,
) -> list[ImageManifestEntry]:
    if not 1 <= max_attempts <= BILIBILI_IMAGE_MAX_ATTEMPTS:
        raise ValueError(
            f"max_attempts must be between 1 and {BILIBILI_IMAGE_MAX_ATTEMPTS}"
        )
    fetch = fetcher or fetch_bilibili_image_bytes
    wait = sleep_fn or time.sleep
    def default_log(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    emit = log_fn or default_log
    root = platform_data_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    entries: list[ImageManifestEntry] = []
    for candidate in content_image_candidates("bilibili", record):
        attempts = 0
        http_status: int | None = None
        error_code = "image_download_retryable"
        while attempts < max_attempts:
            attempts += 1
            try:
                response = fetch(candidate.source_url, candidate.platform_post_id, cookie_header)
                http_status = response.http_status
                if not response.content:
                    raise RemoteImageFetchError(
                        "Bilibili image returned an empty response",
                        http_status=http_status,
                        retryable=True,
                    )
                staged = write_staging_image(
                    [response.content],
                    staging_root=root,
                    relative_stem=(
                        f"images/{safe_platform_post_id(candidate.platform_post_id)}/"
                        f"{candidate.source_index:03d}"
                    ),
                    content_type=response.media_type,
                    content_length=len(response.content),
                    source_url=response.final_url,
                )
                entries.append(
                    ImageManifestEntry(
                        schema_version=1,
                        platform_key=candidate.platform_key,
                        platform_post_id=candidate.platform_post_id,
                        image_role=candidate.image_role,
                        source_index=candidate.source_index,
                        source_key=candidate.source_key,
                        source_asset_key=candidate.source_asset_key,
                        source_url=candidate.source_url,
                        fetch_status="downloaded",
                        attempts=attempts,
                        http_status=http_status,
                        staging_path=staged.path.relative_to(root).as_posix(),
                        size_bytes=staged.size_bytes,
                        mime_type=staged.mime_type,
                        width=staged.width,
                        height=staged.height,
                        sha256=staged.sha256,
                        error_code=None,
                    )
                )
                if attempts > 1:
                    emit(
                        "[image_download_retry_recovered] "
                        f"platform=bilibili post_id={candidate.platform_post_id} "
                        f"source_index={candidate.source_index}, attempts={attempts}"
                    )
                break
            except RemoteImageFetchError as exc:
                http_status = exc.http_status
                error_code = remote_image_failure_code(exc)
                if exc.retryable and attempts < max_attempts:
                    delay = random.uniform(*BILIBILI_IMAGE_RETRY_DELAY_SECONDS) * (
                        2 ** (attempts - 1)
                    )
                    emit(
                        "[image_download_retry] "
                        f"platform=bilibili post_id={candidate.platform_post_id} "
                        f"source_index={candidate.source_index}, "
                        f"attempt={attempts}/{max_attempts}, "
                        f"next_delay_seconds={delay:.3f}"
                    )
                    wait(delay)
                    continue
            except ImageMaterializationError as exc:
                error_code = exc.code
            except (OSError, TimeoutError):
                error_code = "image_download_retryable"
                if attempts < max_attempts:
                    delay = random.uniform(*BILIBILI_IMAGE_RETRY_DELAY_SECONDS) * (
                        2 ** (attempts - 1)
                    )
                    emit(
                        "[image_download_retry] "
                        f"platform=bilibili post_id={candidate.platform_post_id} "
                        f"source_index={candidate.source_index}, "
                        f"attempt={attempts}/{max_attempts}, "
                        f"next_delay_seconds={delay:.3f}"
                    )
                    wait(delay)
                    continue
            if is_retryable_image_error(error_code):
                emit(
                    "[image_download_retry_exhausted] "
                    f"platform=bilibili post_id={candidate.platform_post_id} "
                    f"source_index={candidate.source_index}, attempts={attempts}"
                )
            else:
                emit(
                    "[image_download_terminal] "
                    f"platform=bilibili post_id={candidate.platform_post_id} "
                    f"source_index={candidate.source_index}, attempts={attempts}, "
                    f"error_code={error_code}"
                )
            entries.append(
                ImageManifestEntry(
                    schema_version=1,
                    platform_key=candidate.platform_key,
                    platform_post_id=candidate.platform_post_id,
                    image_role=candidate.image_role,
                    source_index=candidate.source_index,
                    source_key=candidate.source_key,
                    source_asset_key=candidate.source_asset_key,
                    source_url=candidate.source_url,
                    fetch_status="failed",
                    attempts=attempts,
                    http_status=http_status,
                    staging_path=None,
                    size_bytes=None,
                    mime_type=None,
                    width=None,
                    height=None,
                    sha256=None,
                    error_code=error_code,
                )
            )
            return entries
    return entries


def fetch_bilibili_article_detail(post_id: str, cookie_header: str = "") -> dict[str, Any]:
    request = Request(
        BILIBILI_ARTICLE_DETAIL_URL + "?" + urlencode({"id": post_id}),
        headers=bilibili_detail_headers(post_id, cookie_header),
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        raise BilibiliArticleDetailError(
            f"bilibili article detail HTTP {exc.code}",
            retryable=exc.code == 429 or exc.code >= 500,
            code=exc.code,
            runtime_blocking=exc.code in {401, 403, 429},
        ) from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise BilibiliArticleDetailError(
            f"bilibili article detail transport/parse failure: {type(exc).__name__}",
            retryable=True,
        ) from exc

    if not isinstance(payload, dict):
        raise BilibiliArticleDetailError(
            "bilibili article detail payload is not an object",
            retryable=True,
        )
    try:
        code = int(payload.get("code"))
    except (TypeError, ValueError):
        code = None
    if code != 0:
        raise BilibiliArticleDetailError(
            f"bilibili article detail failed: {code} {payload.get('message')}",
            retryable=code in BILIBILI_DETAIL_RETRYABLE_CODES,
            code=code,
            runtime_blocking=code in BILIBILI_RUNTIME_BLOCKING_CODES,
        )
    detail = payload.get("data")
    if not isinstance(detail, dict):
        raise BilibiliArticleDetailError(
            "bilibili article detail missing data object",
            retryable=True,
            code=code,
        )
    if not clean_bilibili_article_body(detail.get("content")):
        raise BilibiliArticleDetailError(
            "bilibili article detail has no parseable body",
            retryable=True,
            code=code,
        )
    return detail


def fetch_bilibili_article_detail_with_retry(
    post_id: str,
    cookie_header: str = "",
) -> tuple[dict[str, Any], int, float]:
    last_error: BilibiliArticleDetailError | None = None
    retry_wait_seconds = 0.0
    for attempt in range(1, BILIBILI_DETAIL_MAX_ATTEMPTS + 1):
        try:
            return (
                fetch_bilibili_article_detail(post_id, cookie_header),
                attempt,
                round(retry_wait_seconds, 3),
            )
        except BilibiliArticleDetailError as exc:
            last_error = exc
            if not exc.retryable or attempt >= BILIBILI_DETAIL_MAX_ATTEMPTS:
                exc.attempts = attempt
                exc.retry_wait_seconds = round(retry_wait_seconds, 3)
                raise
            delay = random.uniform(*BILIBILI_DETAIL_RETRY_DELAY_SECONDS) * attempt
            retry_wait_seconds += delay
            time.sleep(delay)
    assert last_error is not None
    last_error.attempts = BILIBILI_DETAIL_MAX_ATTEMPTS
    last_error.retry_wait_seconds = round(retry_wait_seconds, 3)
    raise last_error


def hydrate_bilibili_article_record(
    search_record: dict[str, Any],
    detail: dict[str, Any],
    *,
    attempts: int,
    retry_wait_seconds: float = 0.0,
    pacing_wait_seconds: float = 0.0,
) -> dict[str, Any]:
    post_id = str(search_record.get("content_id") or search_record.get("id") or "")
    body = clean_bilibili_article_body(detail.get("content"))
    if not body:
        raise BilibiliArticleDetailError(
            f"bilibili article detail {post_id} has no body",
            retryable=True,
            code=0,
            attempts=attempts,
        )
    image_urls, image_sources = extract_bilibili_detail_images(detail)
    opus = detail.get("opus") if isinstance(detail.get("opus"), dict) else {}
    opus_content = opus.get("content") if isinstance(opus.get("content"), dict) else {}
    paragraphs = opus_content.get("paragraphs") if isinstance(opus_content.get("paragraphs"), list) else []
    hydrated = dict(search_record)
    hydrated.update(
        {
            "title": clean_html_text(detail.get("title")) or search_record.get("title"),
            "content_text": body,
            "content_length": len(body),
            "content_detail_status": "detail_observed",
            "content_detail_source": "article_view_api",
            "content_detail_attempts": attempts,
            "content_detail_retry_wait_seconds": round(retry_wait_seconds, 3),
            "content_detail_pacing_wait_seconds": round(pacing_wait_seconds, 3),
            "content_images_detail_status": "detail_observed",
            "detail_image_urls": image_urls,
            "image_urls": image_urls,
            "detail_image_count": len(image_urls),
            "detail_image_sources": image_sources,
            "detail_opus_observed": bool(opus),
            "detail_opus_paragraph_count": len(paragraphs),
            "detail_content_image_token_count": body.count("图片"),
            "content_detail_evidence": {
                "source": "article_view_api",
                "attempts": attempts,
                "retry_wait_seconds": round(retry_wait_seconds, 3),
                "pacing_wait_seconds": round(pacing_wait_seconds, 3),
                "content_length": len(body),
                "image_count": len(image_urls),
                "image_sources": image_sources,
                "opus_observed": bool(opus),
                "opus_paragraph_count": len(paragraphs),
            },
        }
    )
    return hydrated


def fetch_bilibili_wbi_keys(cookie_header: str = "") -> tuple[str, str]:
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = Request(
        "https://api.bilibili.com/x/web-interface/nav",
        headers=headers,
    )
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    data = payload.get("data") or {}
    wbi_img = data.get("wbi_img") or {}
    img_url = str(wbi_img.get("img_url") or "")
    sub_url = str(wbi_img.get("sub_url") or "")
    if not img_url or not sub_url:
        raise RuntimeError("bilibili nav response missing WBI keys")
    return Path(img_url).stem, Path(sub_url).stem


def sign_bilibili_wbi_params(params: dict[str, Any], img_key: str, sub_key: str) -> dict[str, str]:
    mixin_key = img_key + sub_key
    salt = "".join(mixin_key[index] for index in BILIBILI_WBI_MIXIN_TABLE)[:32]
    signed = {**params, "wts": int(time.time())}
    filtered = {
        key: "".join(character for character in str(value) if character not in "!'()*")
        for key, value in sorted(signed.items())
    }
    query = urlencode(filtered)
    filtered["w_rid"] = md5((query + salt).encode("utf-8")).hexdigest()
    return filtered


def fetch_bilibili_article_page(
    keyword: str,
    page: int,
    *,
    wbi_keys: tuple[str, str] | None = None,
    cookie_header: str = "",
) -> list[dict[str, Any]]:
    params = {
        "keyword": keyword,
        "page": page,
        "page_size": BILIBILI_ARTICLE_PAGE_SIZE,
        "search_type": "article",
    }
    img_key, sub_key = wbi_keys or fetch_bilibili_wbi_keys(cookie_header)
    signed_params = sign_bilibili_wbi_params(params, img_key, sub_key)
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://search.bilibili.com/article?keyword=" + quote(keyword),
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = Request(BILIBILI_ARTICLE_SEARCH_URL + "?" + urlencode(signed_params), headers=headers)
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    if payload.get("code") != 0:
        raise RuntimeError(f"bilibili article search failed: {payload.get('code')} {payload.get('message')}")
    result = (payload.get("data") or {}).get("result") or []
    return [item for item in result if isinstance(item, dict)]


def fetch_bilibili_follower_count(creator_id: str, cookie_header: str = "") -> int | None:
    if not creator_id:
        return None
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": f"https://space.bilibili.com/{creator_id}",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = Request(
        BILIBILI_RELATION_STAT_URL + "?" + urlencode({"vmid": creator_id}),
        headers=headers,
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        raise BilibiliFollowerFetchError(
            f"bilibili relation stat HTTP {exc.code}",
            code=exc.code,
            retryable=exc.code == 429 or exc.code >= 500,
            runtime_blocking=exc.code in {401, 403, 429},
        ) from exc
    try:
        code = int(payload.get("code"))
    except (TypeError, ValueError):
        code = None
    if code != 0:
        raise BilibiliFollowerFetchError(
            f"bilibili relation stat failed: {code} {payload.get('message')}",
            code=code,
            retryable=code in BILIBILI_DETAIL_RETRYABLE_CODES,
            runtime_blocking=code in BILIBILI_RUNTIME_BLOCKING_CODES,
        )
    value = (payload.get("data") or {}).get("follower")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def run_bilibili_article_search(args: argparse.Namespace, batch_dir: Path) -> dict[str, Any]:
    platform_key = "bilibili"
    platform = PLATFORMS[platform_key]
    platform_data_root = ensure_dir(
        batch_dir / platform_key / "data" / platform["mediacrawler"]
    )
    save_path = ensure_dir(platform_data_root / "jsonl")
    manifest_path = platform_data_root / "image_manifest.jsonl"
    download_images = bool(getattr(args, "download_images", False))
    log_dir = ensure_dir(batch_dir / "logs" / platform_key)
    jsonl_path = save_path / f"search_contents_{datetime.now(CHINA_TZ).date().isoformat()}.jsonl"
    stdout_log = log_dir / "stdout.log"
    stderr_log = log_dir / "stderr.log"
    command_log = log_dir / "command.txt"
    behavior_evidence_path = log_dir / "behavior_evidence.json"
    command = [
        "bilibili_article_search",
        "--keyword",
        args.keyword,
    ]
    if download_images:
        command.append("--download-images")

    started = time.monotonic()
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    follower_cache: dict[str, int | None] = {}
    follower_attempts: dict[str, int] = {}
    valid_seen: set[str] = set()
    valid_new_count = 0
    valid_existing_count = 0
    candidate_count = 0
    stagnant_pages = 0
    last_detail_request_at: float | None = None
    detail_request_pacing_events: list[dict[str, Any]] = []
    image_manifest_entries: list[ImageManifestEntry] = []
    image_candidate_posts: set[str] = set()
    skipped_candidate_ids: set[str] = set()
    skipped_candidate_failures: list[dict[str, Any]] = []
    state_path = os.environ.get("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", "").strip()
    stderr = ""
    returncode = 0
    behavior_evidence: dict[str, Any] = load_behavior_evidence(behavior_evidence_path)
    try:
        if download_images:
            write_manifest_atomic(manifest_path, image_manifest_entries)
        existing_identities = load_existing_formal_identities(args.db)
        known_post_ids = {
            identity.split(":id:", 1)[1]
            for identity in existing_identities
            if identity.startswith("bilibili:id:")
        }
        discovery_job_id = getattr(args, "discovery_job_id", None)
        discovery_fingerprint = str(
            getattr(args, "discovery_query_fingerprint", "") or ""
        ).strip()
        if discovery_job_id is not None and discovery_fingerprint:
            with sqlite3.connect(Path(args.db).expanduser()) as conn:
                known_post_ids.update(
                    load_skipped_candidates(
                        conn,
                        job_id=int(discovery_job_id),
                        platform_key=platform_key,
                        query_fingerprint_value=discovery_fingerprint,
                    )
                )
        if args.resume_identities_path:
            try:
                resume_values = json.loads(
                    Path(args.resume_identities_path).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError, TypeError):
                resume_values = []
            known_post_ids.update(str(value) for value in resume_values if value)
        cookie_export, behavior_evidence = asyncio.run(
            run_bilibili_behavior_session(args, behavior_evidence_path)
        )
        cookie_header = str((cookie_export or {}).get("cookie_header") or "")
        wbi_keys = fetch_bilibili_wbi_keys(cookie_header)
        frontier_start = max(1, int(args.start_page))
        phases: list[tuple[str, int, int | None]] = []
        if frontier_start > 1 and args.top_refresh_max_pages > 0:
            phases.append(
                ("refresh", 1, min(frontier_start - 1, args.top_refresh_max_pages))
            )
        if not args.discovery_source_exhausted:
            phases.append(("frontier", frontier_start, None))

        for discovery_phase, phase_start, phase_end in phases:
            page = phase_start
            while phase_end is None or page <= phase_end:
                page_items = fetch_bilibili_article_page(
                    args.keyword,
                    page,
                    wbi_keys=wbi_keys,
                    cookie_header=cookie_header,
                )
                if not page_items:
                    if discovery_phase == "frontier" and state_path:
                        FrozenExecutionState(state_path).append_event(
                            "adaptive_search_stopped",
                            {
                                "platform": platform_key,
                                "candidate_count": candidate_count,
                                "valid_new_count": valid_new_count,
                                "valid_existing_count": valid_existing_count,
                                "stagnant_batches": stagnant_pages,
                                "stop_reason": "source_exhausted",
                                "stop_detail": "empty_page",
                                "source_page": page,
                                "resume_page": page,
                                "source_has_more": False,
                                "batch_complete": True,
                                "discovery_phase": discovery_phase,
                                "raw_batch_count": 0,
                                "raw_response_count": 0,
                                "stagnation_basis": "candidate_identity",
                                "candidate_identities": sorted(seen_ids),
                                "skipped_candidate_count": len(
                                    skipped_candidate_failures
                                ),
                                "skipped_candidate_failures": list(
                                    skipped_candidate_failures
                                ),
                            },
                        )
                    break
                new_before = valid_new_count
                seen_before = len(seen_ids)
                processed_in_batch = 0
                batch_complete = True
                for item in page_items:
                    post_id = str(item.get("id") or "").strip()
                    if post_id and (
                        post_id in known_post_ids
                        or post_id in seen_ids
                        or post_id in skipped_candidate_ids
                    ):
                        continue
                    candidate_count += 1
                    processed_in_batch += 1
                    normalized = normalize_bilibili_article_record(item, args.keyword)
                    if not normalized:
                        continue
                    post_id = str(normalized.get("content_id") or "")
                    if post_id in seen_ids:
                        continue
                    applied_pacing_delay = 0.0
                    if last_detail_request_at is not None:
                        pacing_delay = random.uniform(*BILIBILI_DETAIL_PACING_SECONDS)
                        elapsed_since_detail = time.monotonic() - last_detail_request_at
                        if elapsed_since_detail < pacing_delay:
                            applied_pacing_delay = pacing_delay - elapsed_since_detail
                            time.sleep(applied_pacing_delay)
                    try:
                        detail_result = fetch_bilibili_article_detail_with_retry(
                            post_id,
                            cookie_header,
                        )
                        detail, detail_attempts, retry_wait_seconds = detail_result
                        last_detail_request_at = time.monotonic()
                        normalized = hydrate_bilibili_article_record(
                            normalized,
                            detail,
                            attempts=detail_attempts,
                            retry_wait_seconds=retry_wait_seconds,
                            pacing_wait_seconds=applied_pacing_delay,
                        )
                        detail_request_pacing_events.append(
                            {
                                "stage": "article_detail",
                                "post_id": post_id,
                                "seconds": round(
                                    applied_pacing_delay + retry_wait_seconds,
                                    3,
                                ),
                                "pacing_wait_seconds": round(applied_pacing_delay, 3),
                                "retry_wait_seconds": round(retry_wait_seconds, 3),
                                "attempts": detail_attempts,
                                "status": "completed",
                                "finished_at": datetime.now(timezone.utc).isoformat(
                                    timespec="seconds"
                                ),
                            }
                        )
                    except BilibiliArticleDetailError as exc:
                        last_detail_request_at = time.monotonic()
                        detail_request_pacing_events.append(
                            {
                                "stage": "article_detail",
                                "post_id": post_id,
                                "seconds": round(
                                    applied_pacing_delay + exc.retry_wait_seconds,
                                    3,
                                ),
                                "pacing_wait_seconds": round(applied_pacing_delay, 3),
                                "retry_wait_seconds": exc.retry_wait_seconds,
                                "attempts": exc.attempts,
                                "status": "failed",
                                "code": exc.code,
                                "finished_at": datetime.now(timezone.utc).isoformat(
                                    timespec="seconds"
                                ),
                            }
                        )
                        if exc.runtime_blocking:
                            raise BilibiliRuntimeBlocked(
                                f"bilibili_article_detail_blocked:{exc.code}"
                            ) from exc
                        failure = {
                            "platform": platform_key,
                            "identity": post_id,
                            "failure_scope": "post",
                            "detail": "bilibili_article_detail_failed",
                            "error_code": str(exc.code or "detail_request_failed"),
                            "attempts": max(1, int(exc.attempts or 1)),
                            "retryable": bool(exc.retryable),
                            "source_index": None,
                            "source_page": page,
                            "source_offset": None,
                            "source_cursor": None,
                            "discovery_phase": discovery_phase,
                        }
                        skipped_candidate_failures.append(failure)
                        skipped_candidate_ids.add(post_id)
                        seen_ids.add(post_id)
                        known_post_ids.add(post_id)
                        if state_path:
                            FrozenExecutionState(state_path).append_event(
                                "candidate_skipped",
                                failure,
                            )
                        continue
                    if download_images:
                        image_candidate_posts.add(post_id)
                        post_image_entries = download_bilibili_record_images(
                            normalized,
                            cookie_header=cookie_header,
                            platform_data_root=platform_data_root,
                        )
                        image_manifest_entries.extend(post_image_entries)
                        write_manifest_atomic(manifest_path, image_manifest_entries)
                        failed_image = next(
                            (
                                entry
                                for entry in post_image_entries
                                if entry.fetch_status != "downloaded"
                            ),
                            None,
                        )
                        if failed_image is not None:
                            failure = {
                                "platform": platform_key,
                                "identity": post_id,
                                "failure_scope": "image",
                                "detail": "image_download_failed",
                                "error_code": str(
                                    failed_image.error_code
                                    or "image_download_retryable"
                                ),
                                "attempts": max(1, int(failed_image.attempts or 1)),
                                "source_index": failed_image.source_index,
                                "source_page": page,
                                "source_offset": None,
                                "source_cursor": None,
                                "discovery_phase": discovery_phase,
                            }
                            failure["retryable"] = is_retryable_image_error(
                                failure["error_code"]
                            )
                            if is_runtime_blocking_image_error(
                                failure["error_code"]
                            ):
                                raise BilibiliRuntimeBlocked(
                                    str(failure["error_code"])
                                )
                            skipped_candidate_failures.append(failure)
                            skipped_candidate_ids.add(post_id)
                            seen_ids.add(post_id)
                            known_post_ids.add(post_id)
                            if state_path:
                                FrozenExecutionState(state_path).append_event(
                                    "candidate_skipped",
                                    failure,
                                )
                            continue
                    creator_id = str(normalized.get("user_id") or "")
                    if creator_id:
                        if creator_id not in follower_cache:
                            follower_count: int | None = None
                            follower_error: BilibiliFollowerFetchError | None = None
                            for follower_attempt in range(
                                1,
                                BILIBILI_DETAIL_MAX_ATTEMPTS + 1,
                            ):
                                follower_attempts[creator_id] = follower_attempt
                                try:
                                    follower_count = fetch_bilibili_follower_count(
                                        creator_id,
                                        cookie_header,
                                    )
                                except BilibiliFollowerFetchError as exc:
                                    follower_error = exc
                                    follower_count = None
                                    if not exc.retryable:
                                        break
                                except Exception:
                                    follower_error = None
                                    follower_count = None
                                if follower_count is not None:
                                    break
                                if follower_attempt < BILIBILI_DETAIL_MAX_ATTEMPTS:
                                    time.sleep(
                                        random.uniform(
                                            *BILIBILI_DETAIL_RETRY_DELAY_SECONDS
                                        )
                                        * follower_attempt
                                    )
                            if follower_error and follower_error.runtime_blocking:
                                raise BilibiliRuntimeBlocked(
                                    f"bilibili_relation_stat_blocked:{follower_error.code}"
                                ) from follower_error
                            follower_cache[creator_id] = follower_count
                            time.sleep(0.15)
                        follower_count = follower_cache[creator_id]
                        if follower_count is None:
                            failure = {
                                "platform": platform_key,
                                "identity": post_id,
                                "failure_scope": "post",
                                "detail": "creator_profile_failed",
                                "error_code": "relation_stat_unavailable",
                                "attempts": follower_attempts.get(
                                    creator_id,
                                    BILIBILI_DETAIL_MAX_ATTEMPTS,
                                ),
                                "retryable": True,
                                "source_index": None,
                                "source_page": page,
                                "source_offset": None,
                                "source_cursor": None,
                                "discovery_phase": discovery_phase,
                            }
                            skipped_candidate_failures.append(failure)
                            skipped_candidate_ids.add(post_id)
                            seen_ids.add(post_id)
                            known_post_ids.add(post_id)
                            if state_path:
                                FrozenExecutionState(state_path).append_event(
                                    "candidate_skipped",
                                    failure,
                                )
                            continue
                        normalized["followers_observed"] = follower_count is not None
                        normalized["author_followers_source"] = (
                            "relation_stat" if follower_count is not None else "missing"
                        )
                        if follower_count is not None:
                            normalized["followers_count"] = follower_count
                            normalized["author_followers_count"] = follower_count
                    seen_ids.add(post_id)
                    known_post_ids.add(post_id)
                    sanitized_record = sanitize_author_avatar_data(normalized).value
                    if not isinstance(sanitized_record, dict):
                        raise RuntimeError("sanitized Bilibili record must remain an object")
                    normalized = sanitized_record
                    records.append(normalized)
                    validation = validate_formal_record(platform_key, normalized, valid_seen)
                    if validation["valid"]:
                        identity = str(validation["identity"])
                        valid_seen.add(identity)
                        topic_relevant = topic_relevant_for_web_post(
                            platform_key,
                            normalized,
                            fallback_keyword=args.keyword,
                        )
                        if topic_relevant:
                            if formal_database_identities(platform_key, normalized) & existing_identities:
                                valid_existing_count += 1
                            else:
                                valid_new_count += 1
                candidate_identities_added = len(seen_ids) - seen_before
                if discovery_phase == "frontier":
                    stagnant_pages = stagnant_pages + 1 if candidate_identities_added == 0 else 0
                batch_stop_reason = "continue"
                resume_page = page + 1 if batch_complete else page
                event_details = {
                    "platform": platform_key,
                    "batch_no": page,
                    "candidate_count": candidate_count,
                    "valid_new_count": valid_new_count,
                    "valid_existing_count": valid_existing_count,
                    "batch_new_count": valid_new_count - new_before,
                    "batch_candidate_identity_count": candidate_identities_added,
                    "stagnant_batches": stagnant_pages,
                    "stagnation_basis": "candidate_identity",
                    "stop_reason": batch_stop_reason,
                    "stop_detail": None,
                    "source_page": page,
                    "resume_page": resume_page,
                    "source_has_more": None,
                    "batch_complete": batch_complete,
                    "discovery_phase": discovery_phase,
                    "raw_batch_count": processed_in_batch,
                    "raw_response_count": len(page_items),
                    "candidate_identities": sorted(seen_ids),
                    "skipped_candidate_count": len(
                        skipped_candidate_failures
                    ),
                    "skipped_candidate_failures": list(
                        skipped_candidate_failures
                    ),
                }
                if state_path:
                    frozen_state = FrozenExecutionState(state_path)
                    frozen_state.append_event("adaptive_batch_completed", event_details)
                page += 1

        if (not phases or args.discovery_source_exhausted) and state_path:
            FrozenExecutionState(state_path).append_event(
                "adaptive_search_stopped",
                {
                    "platform": platform_key,
                    "candidate_count": candidate_count,
                    "valid_new_count": valid_new_count,
                    "valid_existing_count": valid_existing_count,
                    "stagnant_batches": stagnant_pages,
                    "stop_reason": "source_exhausted",
                    "stop_detail": "saved_source_exhausted",
                    "source_page": frontier_start,
                    "resume_page": frontier_start,
                    "source_has_more": False,
                    "batch_complete": True,
                    "discovery_phase": "frontier",
                    "raw_batch_count": 0,
                    "raw_response_count": 0,
                    "stagnation_basis": "candidate_identity",
                    "candidate_identities": sorted(seen_ids),
                    "skipped_candidate_count": len(
                        skipped_candidate_failures
                    ),
                    "skipped_candidate_failures": list(
                        skipped_candidate_failures
                    ),
                },
            )
    except Exception as exc:
        returncode = 1
        stderr = repr(exc)
        behavior_evidence = load_behavior_evidence(behavior_evidence_path)
        if state_path:
            FrozenExecutionState(state_path).append_event(
                "adaptive_search_stopped",
                {
                    "platform": platform_key,
                    "candidate_count": candidate_count,
                    "valid_new_count": valid_new_count,
                    "valid_existing_count": valid_existing_count,
                    "stagnant_batches": stagnant_pages,
                    "stop_reason": "runtime_failed",
                    "stop_detail": (
                        exc.detail
                        if isinstance(exc, BilibiliRuntimeBlocked)
                        else type(exc).__name__
                    ),
                    "source_page": locals().get("page"),
                    "resume_page": locals().get("page"),
                    "source_has_more": True,
                    "batch_complete": False,
                    "discovery_phase": locals().get("discovery_phase"),
                    "raw_batch_count": locals().get("processed_in_batch", 0),
                    "raw_response_count": len(locals().get("page_items", [])),
                    "stagnation_basis": "candidate_identity",
                    "candidate_identities": sorted(seen_ids),
                },
            )

    if detail_request_pacing_events:
        prior_pacing_events = behavior_evidence.get("request_pacing_events")
        if not isinstance(prior_pacing_events, list):
            prior_pacing_events = []
        behavior_evidence["request_pacing_events"] = (
            prior_pacing_events + detail_request_pacing_events
        )[-200:]
        behavior_evidence_path.write_text(
            json.dumps(behavior_evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    stdout = json.dumps(
        {
            "keyword": args.keyword,
            "jsonl": str(jsonl_path),
            "records": len(records),
        },
        ensure_ascii=False,
        indent=2,
    )
    stdout_log.write_text(stdout, encoding="utf-8")
    stderr_log.write_text(stderr, encoding="utf-8")
    command_log.write_text(shlex.join(command), encoding="utf-8")
    output = summarize_output(batch_dir / platform_key / "data", args.keyword)
    active_image_manifest_entries = [
        entry
        for entry in image_manifest_entries
        if entry.platform_post_id not in skipped_candidate_ids
    ]
    successful_expected_image_count = sum(
        len(content_image_candidates(platform_key, record)) for record in records
    )
    downloaded_image_count = sum(
        entry.fetch_status == "downloaded"
        for entry in active_image_manifest_entries
    )
    failed_image_count = len(active_image_manifest_entries) - downloaded_image_count
    images_complete = (
        not download_images
        or (
            len(active_image_manifest_entries) == successful_expected_image_count
            and failed_image_count == 0
        )
    )
    status = (
        "completed"
        if returncode == 0
        and behavior_evidence_valid(behavior_evidence)
        and images_complete
        and (bool(records) or returncode == 0)
        else "failed"
    )
    return {
        "platform": platform_key,
        "label": platform["label"],
        "status": status,
        "ok": status == "completed",
        "media_enabled": download_images,
        "video_enabled": False,
        "login_state": None,
        "image_materialization": {
            "required": download_images,
            "candidate_posts": len(records),
            "attempted_candidate_posts": len(image_candidate_posts),
            "expected_images": successful_expected_image_count,
            "downloaded_images": downloaded_image_count,
            "retryable_failures": sum(
                entry.error_code == "image_download_retryable"
                for entry in image_manifest_entries
            ),
            "terminal_failures": sum(
                entry.fetch_status == "failed"
                and entry.error_code != "image_download_retryable"
                for entry in image_manifest_entries
            ),
            "complete": images_complete,
            "skipped_candidate_count": len(skipped_candidate_failures),
            "skipped_candidate_failures": skipped_candidate_failures,
            "manifest_paths": [str(manifest_path)] if download_images else [],
            "manifest_sha256": (
                manifest_sha256(image_manifest_entries) if download_images else None
            ),
        },
        "behavior_evidence": behavior_evidence,
        "run": {
            "command": command,
            "command_text": shlex.join(command),
            "returncode": returncode,
            "timed_out": False,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log),
            "command_log": str(command_log),
            "stdout_tail": tail(stdout),
            "stderr_tail": tail(stderr),
        },
        "output": output,
    }


def _run_platform_without_policy(
    platform_key: str,
    args: argparse.Namespace,
    batch_dir: Path,
    *,
    runtime_reporter: XhsSupervisorRuntimeReporter | None = None,
) -> dict[str, Any]:
    if platform_key == "bilibili":
        return run_bilibili_article_search(args, batch_dir)

    platform = PLATFORMS[platform_key]
    save_path = batch_dir / platform_key / "data"
    log_dir = batch_dir / "logs" / platform_key
    behavior_evidence_path = log_dir / "behavior_evidence.json"
    navigation_diagnostics_path = behavior_evidence_path.with_name(
        f"{behavior_evidence_path.stem}.navigation.json"
    )
    image_download_enabled = bool(args.download_images)
    specified_detail_urls = list(getattr(args, "zhihu_detail_urls", [])) + list(
        getattr(args, "xhs_detail_urls", [])
    ) + list(getattr(args, "post_repair_detail_targets", []))
    cmd = [
        "uv",
        "run",
        "python",
        str(ROOT / "scripts" / "mediacrawler_export_entrypoint.py"),
        "--platform",
        platform["mediacrawler"],
        "--lt",
        args.login_type,
        "--type",
        "detail"
        if (
            getattr(args, "zhihu_detail_urls", [])
            or getattr(args, "xhs_detail_urls", [])
            or getattr(args, "post_repair_detail_targets", [])
        )
        else "search",
        "--keywords",
        args.keyword,
        "--get_comment",
        "false",
        "--get_sub_comment",
        "false",
        "--get_media",
        "true" if image_download_enabled else "false",
        "--headless",
        "false" if args.headed else "true",
        "--save_data_option",
        "jsonl",
        "--save_data_path",
        str(save_path),
        "--start",
        str(args.start_page),
        "--max_concurrency_num",
        "1",
        "--enable_ip_proxy",
        "false",
    ]
    if specified_detail_urls:
        cmd.extend(["--specified_id", ",".join(specified_detail_urls)])
    extra_env: dict[str, str] = {
        "TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS": "1",
        "TRIPPOSTCOLLECT_POST_REPAIR": "1" if getattr(args, "post_repair", False) else "0",
        "TRIPPOSTCOLLECT_DB_PATH": str(Path(args.db).expanduser().resolve()),
        **behavior_environment(behavior_evidence_path, args.behavior_profile),
    }
    if args.discovery_job_id is not None:
        extra_env.update(
            {
                "TRIPPOSTCOLLECT_DISCOVERY_JOB_ID": str(args.discovery_job_id),
                "TRIPPOSTCOLLECT_DISCOVERY_QUERY_FINGERPRINT": str(
                    args.discovery_query_fingerprint or ""
                ),
                "TRIPPOSTCOLLECT_DISCOVERY_RUN_ID": str(args.discovery_run_id or ""),
                "TRIPPOSTCOLLECT_DISCOVERY_PLATFORM": platform_key,
                "TRIPPOSTCOLLECT_DISCOVERY_KEYWORD": args.keyword,
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_PAGE": str(args.start_page),
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET": str(args.start_offset),
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR": str(args.start_cursor or ""),
                "TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES": str(
                    args.top_refresh_max_pages
                ),
                "TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED": (
                    "1" if args.discovery_source_exhausted else "0"
                ),
                "TRIPPOSTCOLLECT_DISCOVERY_CHECKPOINT_WRITE_DISABLED": (
                    "1" if args.no_checkpoint_write else "0"
                ),
            }
        )
    if platform_key == "xhs":
        extra_env.update(
            {
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_PAGE": str(args.start_page),
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR": str(args.start_cursor or ""),
                "TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES": str(
                    args.top_refresh_max_pages
                ),
                "TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED": (
                    "1" if args.discovery_source_exhausted else "0"
                ),
            }
        )
    if args.resume_identities_path:
        extra_env["TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH"] = args.resume_identities_path
    login_state: dict[str, Any] | None = None
    if platform_key == "xhs":
        cmd.extend(["--enable_cdp_mode", "true"])
        extra_env.update(
            {
                "TRIPPOSTCOLLECT_XHS_ENRICH_CREATORS": "1",
                "TRIPPOSTCOLLECT_XHS_KEEP_AUTHOR_DETAIL": "1",
                "TRIPPOSTCOLLECT_SHARE_CDP_PROFILE": "1",
                "TRIPPOSTCOLLECT_XHS_PROFILE_DIR": str(Path(args.xhs_profile_dir).expanduser().resolve()),
                "TRIPPOSTCOLLECT_XHS_ACCOUNT_ID": str(args.xhs_account_id),
                "TRIPPOSTCOLLECT_XHS_RUN_SCOPED_LOGIN": "1",
                XHS_WINDOW_SIZE_ENV: xhs_window_size_value(),
                "TRIPPOSTCOLLECT_XHS_DISCOVERY_TARGET_KEY": str(
                    args.xhs_discovery_target_key
                ),
                "TRIPPOSTCOLLECT_XHS_DISCOVERY_QUERY_FINGERPRINT": str(
                    args.xhs_discovery_query_fingerprint
                ),
                "TRIPPOSTCOLLECT_XHS_POST_INTERACTION": str(args.xhs_post_interaction),
                "TRIPPOSTCOLLECT_XHS_REPAIR": "1" if getattr(args, "xhs_repair", False) else "0",
                "TRIPPOSTCOLLECT_XHS_REPAIR_BATCH_SIZE": str(args.xhs_repair_batch_size),
                "TRIPPOSTCOLLECT_XHS_REPAIR_REPORT_PATH": str(
                    log_dir / "repair_report.json"
                ),
                "TRIPPOSTCOLLECT_XHS_INITIAL_SETTLE_SECONDS": "12",
                "TRIPPOSTCOLLECT_XHS_LOGIN_WAIT_SECONDS": (
                    str(XHS_OPERATOR_LOGIN_WAIT_SECONDS) if args.headed else "0"
                ),
                "TRIPPOSTCOLLECT_XHS_NAVIGATION_DEADLINE_SECONDS": "60",
                "TRIPPOSTCOLLECT_XHS_CREATOR_VERIFY_WAIT_SECONDS": "600",
                "TRIPPOSTCOLLECT_XHS_CREATOR_VERIFY_POLL_SECONDS": "2",
                "TRIPPOSTCOLLECT_XHS_QR_REFRESH_SECONDS": "180",
            }
        )
    elif platform_key == "zhihu":
        cmd.extend(["--enable_cdp_mode", "true"])
        extra_env.update(
            {
                "TRIPPOSTCOLLECT_SHARE_CDP_PROFILE": "1",
                "TRIPPOSTCOLLECT_CLEAN_BROWSER_TABS": "1",
                "TRIPPOSTCOLLECT_ZHIHU_INITIAL_SETTLE_SECONDS": "8",
            }
        )
    elif platform_key == "douyin":
        extra_env.update(
            {
                "TRIPPOSTCOLLECT_DOUYIN_ENRICH_CREATORS": "1",
                "TRIPPOSTCOLLECT_DOUYIN_ENRICH_ONLY_IMAGES": "1",
                "TRIPPOSTCOLLECT_DOUYIN_MAX_CREATOR_ENRICH": "-1",
                "TRIPPOSTCOLLECT_DOUYIN_CREATOR_SLEEP_SECONDS": "0.25",
                "TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_FALLBACK": (
                    "1" if getattr(args, "post_repair", False) else "0"
                ),
                "TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_TIMEOUT_MS": "30000",
            }
        )
    if platform_key in {"xhs", "zhihu"}:
        browser_path = discover_cdp_browser_path()
        if browser_path:
            extra_env.setdefault("TRIPPOSTCOLLECT_CUSTOM_BROWSER_PATH", browser_path)
        if platform_key == "zhihu":
            cookie_export = export_profile_cookies(platform_key, browser_path)
            if not cookie_export:
                reason = (
                    "missing_zhihu_login_cookies: run "
                    ".venv/bin/python scripts/mediacrawler_login_warmup.py --platforms zhihu "
                    "until d_c0/z_c0 are verified and snapshotted"
                )
                run = skipped_command(cmd, log_dir, reason)
                output = summarize_output(save_path, args.keyword)
                return {
                    "platform": platform_key,
                    "label": platform["label"],
                    "status": "failed",
                    "ok": False,
                    "media_enabled": image_download_enabled,
                    "video_enabled": False,
                    "login_state": {
                        "ok": False,
                        "reason": "missing_required_cookies",
                        "required_cookie_names": list(required_cookie_names(platform_key)),
                        "snapshot_path": str(cookie_snapshot_path(platform_key)),
                    },
                    "run": run,
                    "output": output,
                }
            extra_env["TRIPPOSTCOLLECT_COOKIES"] = str(cookie_export["cookie_header"])
            login_state = {"ok": True, **public_cookie_export(cookie_export)}
    timeout = args.timeout_per_platform
    if platform_key == "xhs" and (args.login_type == "qrcode" or args.headed):
        timeout = max(timeout, 420)
    if platform_key == "zhihu":
        timeout = max(timeout, 300)
    execution_state_path = os.environ.get(
        "TRIPPOSTCOLLECT_EXECUTION_STATE_PATH",
        "",
    ).strip()
    progress_paths = [save_path, behavior_evidence_path]
    if execution_state_path:
        progress_paths.append(Path(execution_state_path).expanduser())
    run = run_command(
        cmd,
        MEDIACRAWLER_DIR,
        timeout,
        log_dir,
        extra_env=extra_env,
        progress_paths=progress_paths,
        runtime_reporter=runtime_reporter,
        network_diagnostics_path=(
            navigation_diagnostics_path if platform_key == "xhs" else None
        ),
        startup_grace_seconds=HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS,
    )
    if runtime_reporter is not None:
        runtime_reporter.enter_finalizing()
    if run.get("timeout_reason") == "no_progress_timeout":
        run["timeout_state_event"] = append_no_progress_timeout_event(
            execution_state_path,
            platform_key=platform_key,
            start_page=int(args.start_page),
            start_offset=(
                int(args.start_offset) if args.start_offset is not None else None
            ),
            start_cursor=str(args.start_cursor or "") or None,
            inactivity_timeout_seconds=float(timeout),
            last_progress_age_seconds=float(
                run.get("last_progress_age_seconds") or 0.0
            ),
        )
    behavior_evidence = load_behavior_evidence(behavior_evidence_path)
    repair_report = (
        load_xhs_repair_report(log_dir / "repair_report.json")
        if platform_key == "xhs" and getattr(args, "xhs_repair", False)
        else {}
    )
    output = summarize_output(save_path, args.keyword)
    status = (
        "completed"
        if output["parse_errors"] == 0
        and (
            output["non_video_content_records"] > 0
            or run.get("returncode") == 0
        )
        else "failed"
    )
    if output["content_records"] > 0 and output["non_video_content_records"] == 0 and output["video_like_records"] > 0:
        status = "skipped_video_only"
    if (
        output["content_records"] == 0
        and platform_key in {"xhs", "douyin"}
        and "skip video" in str(run.get("stderr_tail") or "").lower()
    ):
        status = "skipped_video_only"
    if run["timed_out"]:
        status = "runtime_failed"
    elif int(run.get("returncode") or 0) != 0:
        status = "runtime_failed"
    if not behavior_evidence_valid(behavior_evidence):
        status = "behavior_failed"
    result = {
        "platform": platform_key,
        "label": platform["label"],
        "status": status,
        "ok": status in {"completed", "skipped_video_only"},
        "media_enabled": image_download_enabled,
        "video_enabled": False,
        "login_state": login_state,
        "behavior_evidence": behavior_evidence,
        "run": run,
        "output": output,
    }
    if repair_report:
        result["repair_report"] = repair_report
    return result


def effective_attempt_exit_code(record: dict[str, Any]) -> int:
    run = record.get("run") or {}
    raw_returncode = run.get("returncode")
    if raw_returncode is not None:
        return int(raw_returncode)
    return 0 if record.get("ok") else 1


def run_platform(
    platform_key: str,
    args: argparse.Namespace,
    batch_dir: Path,
    *,
    runtime_reporter: XhsSupervisorRuntimeReporter | None = None,
) -> dict[str, Any]:
    platform = PLATFORMS[platform_key]
    site = get_site(platform_key)
    log_dir = batch_dir / "logs" / platform_key
    save_path = batch_dir / platform_key / "data"
    policy_events: list[dict[str, Any]] = []
    shared_policy_disabled = platform_key == "xhs"
    policy_cleanup = clear_site_policy_state(site.key) if shared_policy_disabled else None
    try:
        with site_request_guard(
            site,
            label="mediacrawler:formal_platform_session",
            disabled=shared_policy_disabled,
        ) as event:
            if policy_cleanup:
                event["obsolete_policy_state_cleared"] = policy_cleanup
            policy_events.append(event)
            record = _run_platform_without_policy(
                platform_key,
                args,
                batch_dir,
                runtime_reporter=runtime_reporter,
            )
    except XhsRuntimeSupervisionError:
        raise
    except CrawlPolicyBlocked as exc:
        policy_events.append(exc.event)
        reason = json.dumps(exc.event, ensure_ascii=False, sort_keys=True)
        return {
            "platform": platform_key,
            "label": platform["label"],
            "status": "policy_blocked",
            "ok": False,
            "media_enabled": False,
            "video_enabled": False,
            "login_state": None,
            "behavior_evidence": {
                "status": "skipped",
                "reason": "policy_blocked_before_behavior",
            },
            "policy_events": policy_events,
            "failure_classification": {
                "status": "retry_wait",
                "failure_type": "policy_blocked",
                "retryable": True,
                "wait_seconds": int(exc.event.get("wait_seconds") or 0),
                "reason": str(exc.event.get("reason") or "policy_blocked"),
            },
            "run": skipped_command(["crawl_policy", platform_key], log_dir, reason),
            "output": summarize_output(save_path, args.keyword),
        }
    except Exception as exc:
        reason = f"platform_session_failed:{type(exc).__name__}:{exc}"
        record = {
            "platform": platform_key,
            "label": platform["label"],
            "status": "failed",
            "ok": False,
            "media_enabled": False,
            "video_enabled": False,
            "login_state": None,
            "behavior_evidence": load_behavior_evidence(log_dir / "behavior_evidence.json"),
            "run": skipped_command(["mediacrawler", platform_key], log_dir, reason),
            "output": summarize_output(save_path, args.keyword),
        }

    record["policy_events"] = policy_events
    run = record.get("run") or {}
    evidence = record.get("behavior_evidence") or {}
    effective_exit_code = effective_attempt_exit_code(record)
    structured_markers = {
        **(evidence.get("initial_visible_markers") or {}),
        **(evidence.get("visible_markers") or {}),
    }
    classification = classify_attempt(
        exit_code=effective_exit_code,
        stdout=str(run.get("stdout_tail") or ""),
        stderr=str(run.get("stderr_tail") or ""),
        meta={"platform": platform_key, "structured_markers": structured_markers},
    )
    record["failure_classification"] = classification
    if platform_key != "xhs" and classification.get("failure_type") in {
        "captcha_detected",
        "rate_limited",
        "blocked_or_forbidden",
    }:
        record["cooldown_event"] = record_site_cooldown(
            site,
            reason=str(classification.get("failure_type")),
            evidence=[str(classification.get("reason") or "")],
        )
    return record


def collect_behavior_validation(
    records: list[dict[str, Any]],
    platforms: list[str],
    keyword: str,
    xhs_post_interaction: str = "none",
    repair_mode: bool = False,
) -> dict[str, Any]:
    latest_by_platform: dict[str, dict[str, Any]] = {}
    for record in reversed(records):
        platform_key = str(record.get("platform") or "")
        if platform_key in platforms and platform_key not in latest_by_platform:
            latest_by_platform[platform_key] = record

    platform_results: dict[str, Any] = {}
    for platform_key in platforms:
        record = latest_by_platform.get(platform_key) or {}
        evidence = record.get("behavior_evidence") or {}
        behavior_profile = str(evidence.get("profile") or "")
        expected_profile = "xhs_guarded" if platform_key == "xhs" else "social_high_risk"
        profile_ok = behavior_profile == expected_profile
        pacing_events = [
            item
            for item in evidence.get("request_pacing_events") or []
            if isinstance(item, dict)
        ]
        pacing_stages = {str(item.get("stage") or "") for item in pacing_events}
        continuity_events = [
            item
            for item in evidence.get("continuity_events") or []
            if isinstance(item, dict)
        ]
        continuity_stages = {
            str(item.get("stage") or "")
            for item in continuity_events
            if item.get("status") == "completed"
        }
        required_pacing_stages = set()
        if platform_key == "xhs":
            required_pacing_stages = (
                {"note_detail", "creator_profile"}
                if repair_mode
                else {"search_results", "note_detail", "creator_profile"}
            )
        pacing_ok = required_pacing_stages.issubset(pacing_stages)
        continuity_ok = (
            platform_key != "xhs"
            or repair_mode
            or "search_results" in continuity_stages
        )
        post_interactions = [
            item
            for item in evidence.get("post_interactions") or []
            if isinstance(item, dict)
        ]
        interaction_requested = platform_key == "xhs" and xhs_post_interaction != "none"
        interaction_ok = (not interaction_requested) or any(
            item.get("requested_mode") == xhs_post_interaction and item.get("status") == "completed"
            for item in post_interactions
        )
        policy_events = record.get("policy_events") or []
        policy_allowed = any(
            isinstance(event, dict)
            and event.get("allowed") is True
            and (platform_key == "xhs" or event.get("disabled") is not True)
            for event in policy_events
        )
        behavior_url = str(evidence.get("url") or "")
        target_url_ok = bool(keyword) and keyword in unquote(behavior_url)
        platform_results[platform_key] = {
            "behavior_ok": (
                behavior_evidence_valid(evidence)
                and target_url_ok
                and profile_ok
                and pacing_ok
                and continuity_ok
            ),
            "behavior_status": str(evidence.get("status") or "missing"),
            "behavior_profile": behavior_profile,
            "behavior_profile_ok": profile_ok,
            "behavior_event_count": len(evidence.get("events") or []),
            "request_pacing_event_count": len(pacing_events),
            "request_pacing_stages": sorted(pacing_stages),
            "request_pacing_ok": pacing_ok,
            "continuity_event_count": len(continuity_events),
            "continuity_stages": sorted(continuity_stages),
            "continuity_ok": continuity_ok,
            "post_interaction_requested": interaction_requested,
            "post_interaction_mode": xhs_post_interaction if platform_key == "xhs" else "none",
            "post_interaction_ok": interaction_ok,
            "post_interactions": post_interactions,
            "behavior_url": behavior_url,
            "target_url_ok": target_url_ok,
            "policy_allowed": policy_allowed,
            "policy_event_count": len(policy_events),
            "evidence_path": str(evidence.get("evidence_path") or ""),
        }

    behavior_ok = bool(platform_results) and all(item["behavior_ok"] for item in platform_results.values())
    policy_ok = bool(platform_results) and all(item["policy_allowed"] for item in platform_results.values())
    return {
        "required": True,
        "ok": behavior_ok and policy_ok,
        "behavior_ok": behavior_ok,
        "policy_ok": policy_ok,
        "required_platforms": platforms,
        "platforms": platform_results,
    }


def latest_platform_result_counts(
    records: list[dict[str, Any]],
    platforms: list[str],
) -> dict[str, int]:
    latest_by_platform: dict[str, dict[str, Any]] = {}
    for record in records:
        platform_key = str(record.get("platform") or "")
        if platform_key in platforms:
            latest_by_platform[platform_key] = record
    latest_records = list(latest_by_platform.values())
    return {
        "ok_count": sum(1 for record in latest_records if record.get("ok")),
        "skipped_video_only_count": sum(
            1 for record in latest_records if record.get("status") == "skipped_video_only"
        ),
        "failed_count": sum(1 for record in latest_records if not record.get("ok")),
    }


def write_markdown(summary: dict[str, Any], path: Path) -> None:
    lines = [
        "# MediaCrawler 结构化抓取摘要",
        "",
        f"- 时间：`{summary['captured_at']}`",
        f"- 关键词：`{summary['keyword']}`",
        f"- 输出目录：`{summary['batch_dir']}`",
        "",
        "| 平台 | 状态 | 图文内容记录 | 发帖时间记录 | 跳过视频记录 | 图片文件 | 视频文件(应为0) | 作者字段 |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for record in summary["records"]:
        output = record["output"]
        lines.append(
            "| {label} | {status} | {contents} | {published_at} | {skipped_videos} | {images} | {videos} | {fields} |".format(
                label=record["label"],
                status=record["status"],
                contents=output["non_video_content_records"],
                published_at=output["published_at_records"],
                skipped_videos=output["video_like_records"],
                images=output["image_file_count"],
                videos=output["video_file_count"],
                fields=", ".join(output["author_like_fields"]) or "无",
            )
        )
    lines.extend(["", "## 样本", ""])
    for record in summary["records"]:
        lines.append(f"### {record['label']}")
        samples = record["output"].get("samples") or []
        if not samples:
            lines.append("")
            lines.append("无")
            lines.append("")
            continue
        for sample in samples[:2]:
            lines.append(f"- `{json.dumps(sample, ensure_ascii=False)}`")
        lines.append("")
    validation = summary.get("formal_validation") or {}
    if validation:
        pagination = validation.get("pagination_evidence") or {}
        stop_event = pagination.get("stop_event") or {}
        lines.extend(
            [
                "## 正式校验",
                "",
                "- 完成策略：`source-exhausted`（唯一正式策略）",
                f"- 实际候选：`{validation.get('candidate_count', 0)}`",
                f"- 主题相关有效新增图文：`{validation.get('valid_new_count', 0)}`",
                f"- 主题相关有效旧记录：`{validation.get('valid_existing_count', 0)}`（更新统计）",
                f"- 主题不相关结构有效新增：`{validation.get('topic_irrelevant_new_count', 0)}`（入库审计）",
                f"- 主题不相关结构有效旧记录：`{validation.get('topic_irrelevant_existing_count', 0)}`",
                f"- 来源耗尽达成：`{validation.get('source_exhausted_met', False)}`",
                f"- 修复成功子集可入库：`{validation.get('repair_import_met', False)}`",
                f"- 修复选中目标全部有效：`{validation.get('all_repair_targets_valid')}`",
                f"- 本轮完成门禁达成：`{validation.get('completion_met', False)}`",
                f"- 停止原因：`{validation.get('stop_reason', '')}`",
                f"- 停止细节：`{validation.get('stop_detail', '')}`",
                f"- 已处理分页批次：`{pagination.get('batch_count', 0)}`",
                f"- 正常停止事件：`{pagination.get('stopped', False)}`",
                f"- 已记录跳过候选：`{stop_event.get('skipped_candidate_count', 0)}`",
                f"- 无效原因计数：`{json.dumps(validation.get('invalid_reason_counts') or {}, ensure_ascii=False, sort_keys=True)}`",
                "",
            ]
        )
    behavior_validation = summary.get("behavior_validation") or {}
    if behavior_validation:
        lines.extend(
            [
                "## 行为与策略门禁",
                "",
                f"- 总体通过：`{behavior_validation.get('ok', False)}`",
                f"- 人类行为证据通过：`{behavior_validation.get('behavior_ok', False)}`",
                f"- 请求预算与冷却门禁通过：`{behavior_validation.get('policy_ok', False)}`",
                f"- 平台证据：`{json.dumps(behavior_validation.get('platforms') or {}, ensure_ascii=False, sort_keys=True)}`",
                "",
            ]
        )
    image_materialization = summary.get("image_materialization") or {}
    if image_materialization:
        lines.extend(
            [
                "## 图片本地化",
                "",
                f"- 正式要求：`{image_materialization.get('required', False)}`",
                f"- 候选帖子：`{image_materialization.get('candidate_posts', 0)}`",
                f"- 预期正文图：`{image_materialization.get('expected_images', 0)}`",
                f"- staging 下载：`{image_materialization.get('downloaded_images', 0)}`",
                f"- 根项目字节复验：`{image_materialization.get('validated_images', 0)}`",
                f"- SHA-256 唯一正文图：`{image_materialization.get('unique_images', 0)}`",
                f"- SHA-256 重复来源：`{image_materialization.get('sha256_duplicate_images', 0)}`",
                f"- 新晋升文件：`{image_materialization.get('promoted_images', 0)}`",
                f"- 失败回滚文件：`{image_materialization.get('rolled_back_images', 0)}`",
                f"- 复用文件：`{image_materialization.get('reused_images', 0)}`",
                f"- 可恢复失败：`{image_materialization.get('retryable_failures', 0)}`",
                f"- 终态失败：`{image_materialization.get('terminal_failures', 0)}`",
                f"- 完整：`{image_materialization.get('complete', False)}`",
                "",
            ]
        )
    import_result = summary.get("import_result") or {}
    if import_result:
        db_sync = import_result.get("db_sync") or {}
        db_value = import_result.get("db") or db_sync.get("db", "")
        lines.extend(
            [
                "## 入库",
                "",
                f"- 数据库：`{db_value}`",
                f"- JSONL 文件数：`{import_result.get('jsonl_files', 0)}`",
                f"- 处理行：`{import_result.get('processed_rows', 0)}`",
                f"- 新增行：`{import_result.get('inserted_rows', 0)}`",
                f"- 更新行：`{import_result.get('updated_rows', 0)}`",
                f"- 相关新增/更新：`{import_result.get('topic_relevant_inserted_rows', 0)}` / `{import_result.get('topic_relevant_updated_rows', 0)}`",
                f"- 不相关新增/更新：`{import_result.get('topic_irrelevant_inserted_rows', 0)}` / `{import_result.get('topic_irrelevant_updated_rows', 0)}`",
                f"- 跳过记录：`{import_result.get('skipped', 0)}`",
                f"- 跳过视频记录：`{import_result.get('skipped_video', 0)}`",
                f"- 解析错误：`{import_result.get('parse_errors', 0)}`",
                f"- 同步任务：`{db_sync.get('synced_jobs', '')}`",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def apply_formal_completion_gates(
    validation: dict[str, Any],
    *,
    content_validation: dict[str, Any],
    image_materialization: dict[str, Any],
    behavior_validation: dict[str, Any],
    download_images: bool,
    child_execution_ok: bool = True,
) -> dict[str, Any]:
    gated = dict(validation)
    gated["content_completion_met"] = bool(content_validation.get("completion_met"))
    gated["content_repair_import_met"] = bool(
        content_validation.get("repair_import_met")
    )
    gated["image_materialization_complete"] = bool(image_materialization.get("complete"))
    gated["behavior_evidence_ok"] = bool(behavior_validation.get("behavior_ok"))
    gated["policy_evidence_ok"] = bool(behavior_validation.get("policy_ok"))
    gated["child_execution_ok"] = child_execution_ok
    runtime_stop_reason = (
        str(gated.get("stop_reason"))
        if str(gated.get("stop_reason") or "")
        in {
            "runtime_failed",
            "login_required",
            "captcha_detected",
            "sms_verification_terminal",
            "rate_limited",
            "platform_security_limit",
            "policy_blocked",
            "blocked_or_forbidden",
            "runtime_permission_error",
            "browser_launch_failed",
            "browser_target_closed",
        }
        else ""
    )
    if download_images and not image_materialization.get("complete"):
        gated["completion_met"] = False
        gated["repair_import_met"] = False
        gated["stop_reason"] = "image_materialization_incomplete"
    if not behavior_validation.get("ok"):
        gated["completion_met"] = False
        gated["repair_import_met"] = False
        gated["stop_reason"] = (
            "behavior_evidence_failed"
            if not behavior_validation.get("behavior_ok")
            else "crawl_policy_evidence_failed"
        )
    if runtime_stop_reason or not child_execution_ok:
        gated["completion_met"] = False
        gated["repair_import_met"] = False
        gated["stop_reason"] = runtime_stop_reason or "runtime_failed"
    return gated


def formal_import_gate_met(validation: dict[str, Any]) -> bool:
    if validation.get("repair_mode"):
        return bool(validation.get("repair_import_met"))
    return bool(validation.get("completion_met"))


def formal_image_promotion_allowed(
    *,
    download_images: bool,
    no_import: bool,
    validation: dict[str, Any],
) -> bool:
    return bool(download_images and not no_import and formal_import_gate_met(validation))


def repair_partial_child_execution_allowed(
    *,
    repair_mode: bool,
    child_execution_ok: bool,
    runtime_blocked: bool = False,
    validation: dict[str, Any],
    image_materialization: dict[str, Any],
    behavior_validation: dict[str, Any],
) -> bool:
    if runtime_blocked:
        return False
    if child_execution_ok:
        return True
    return bool(
        repair_mode
        and int(validation.get("valid_total_count") or 0) > 0
        and bool(image_materialization.get("complete"))
        and bool(behavior_validation.get("ok"))
    )


def repair_candidate_execution_completed(
    records: list[dict[str, Any]],
    platforms: list[str],
) -> bool:
    repair_records = [
        record
        for record in records
        if isinstance(record, dict) and record.get("platform") in platforms
    ]

    def clean_process(record: dict[str, Any]) -> bool:
        run = record.get("run")
        return bool(
            isinstance(run, dict)
            and run.get("returncode") == 0
            and not bool(run.get("timed_out"))
            and str(
                (record.get("failure_classification") or {}).get("failure_type")
                or ""
            )
            == "success"
        )

    return bool(
        len(repair_records) == len(platforms)
        and all(clean_process(record) for record in repair_records)
    )


def repair_runtime_stop_reason(
    records: list[dict[str, Any]],
    platforms: list[str],
) -> str:
    blocking_types = {
        "policy_blocked",
        "platform_security_limit",
        "sms_verification_terminal",
        "captcha_detected",
        "login_required",
        "rate_limited",
        "blocked_or_forbidden",
        "runtime_permission_error",
        "browser_launch_failed",
        "browser_target_closed",
        "browser_runtime_failed",
        "verification_timeout",
        "ip_blocked",
    }
    for record in records:
        if not isinstance(record, dict) or record.get("platform") not in platforms:
            continue
        failure_type = str(
            (record.get("failure_classification") or {}).get("failure_type") or ""
        )
        if failure_type in blocking_types:
            return failure_type
        repair_report = record.get("repair_report") or {}
        runtime_blocker = repair_report.get("runtime_blocker") or {}
        blocker_code = str(runtime_blocker.get("error_code") or "")
        if blocker_code in blocking_types or blocker_code == "runtime_failed":
            return blocker_code
    return ""


def _run_main(
    args: argparse.Namespace,
    runtime_reporter: XhsSupervisorRuntimeReporter | None,
) -> int:
    progress_callback = (
        runtime_reporter.checkpoint if runtime_reporter is not None else None
    )
    _runtime_progress(progress_callback)
    if args.xhs_repair and args.post_repair:
        raise SystemExit("--xhs-repair and --post-repair are mutually exclusive")
    repair_mode = bool(args.xhs_repair or args.post_repair)
    if args.xhs_repair_batch_size <= 0:
        raise SystemExit("--xhs-repair-batch-size must be positive")
    if args.required_fields_profile != "image_post_with_followers_v1":
        raise SystemExit(f"unsupported required fields profile: {args.required_fields_profile}")
    if args.start_page <= 0:
        raise SystemExit("--start-page must be positive")
    if args.start_offset < 0 or args.top_refresh_max_pages < 0:
        raise SystemExit("--start-offset and --top-refresh-max-pages cannot be negative")
    discovery_values = (
        args.discovery_job_id,
        args.discovery_query_fingerprint,
        args.discovery_run_id,
    )
    if any(value not in (None, "") for value in discovery_values) and not all(
        value not in (None, "") for value in discovery_values
    ):
        raise SystemExit("discovery job id, query fingerprint and run id must be supplied together")
    if args.no_import:
        args.no_checkpoint_write = True
    if repair_mode:
        args.no_checkpoint_write = True
    xhs_managed_resume = bool(args.xhs_account_id and args.start_cursor)
    if (
        args.start_page > 1
        and not args.resume_summary
        and args.discovery_job_id is None
        and not xhs_managed_resume
    ):
        raise SystemExit("--start-page greater than 1 requires --resume-summary")
    if args.get_media:
        raise SystemExit(
            "--get-media 已禁用：请使用 --download-images 启用项目正文图片模式；视频始终禁用。"
        )
    ensure_prerequisites()
    platforms = selected_platforms(args.platforms)
    _runtime_progress(progress_callback)
    if not args.no_import and not args.download_images:
        raise SystemExit("正式入库模式必须显式启用 --download-images")
    try:
        media_root = resolve_media_root(args.media_root)
    except ImagePersistenceError as exc:
        raise SystemExit(str(exc)) from exc
    args.zhihu_detail_urls = []
    if args.zhihu_detail_urls_file:
        if platforms != ["zhihu"]:
            raise SystemExit("--zhihu-detail-urls-file requires --platforms zhihu only")
        if not args.no_import:
            raise SystemExit("--zhihu-detail-urls-file is diagnostic-only and requires --no-import")
        if args.resume_summary or args.start_page != 1 or args.discovery_job_id is not None:
            raise SystemExit("Zhihu detail diagnosis cannot use discovery resume arguments")
        args.zhihu_detail_urls = load_zhihu_detail_urls(args.zhihu_detail_urls_file)
    args.xhs_detail_urls = []
    args.xhs_repair_target_ids = set()
    if args.xhs_detail_urls_file:
        if platforms != ["xhs"] or not args.xhs_repair:
            raise SystemExit("--xhs-detail-urls-file requires --platforms xhs --xhs-repair")
        if args.resume_summary or args.start_page != 1 or args.discovery_job_id is not None:
            raise SystemExit("XHS repair cannot use discovery resume arguments")
        if not args.xhs_repair_target_ids_file:
            raise SystemExit("XHS repair requires --xhs-repair-target-ids-file")
        args.xhs_detail_urls = load_xhs_detail_urls(args.xhs_detail_urls_file)
        args.xhs_repair_target_ids = load_xhs_repair_target_ids(args.xhs_repair_target_ids_file)
        if len(args.xhs_detail_urls) != len(args.xhs_repair_target_ids):
            raise SystemExit("XHS repair URL and target ID files must contain the same number of items")
    elif args.xhs_repair or args.xhs_repair_target_ids_file:
        raise SystemExit("XHS repair requires both detail URL and target ID files")
    args.post_repair_targets = []
    args.post_repair_detail_targets = []
    args.post_repair_target_ids = set()
    args.post_repair_metadata_by_identity = {}
    if args.post_repair:
        if len(platforms) != 1 or platforms[0] not in {"douyin", "weibo", "zhihu"}:
            raise SystemExit(
                "--post-repair requires exactly one of --platforms douyin, weibo, or zhihu"
            )
        if not args.repair_targets_file:
            raise SystemExit("--post-repair requires --repair-targets-file")
        if args.zhihu_detail_urls_file or args.xhs_detail_urls_file:
            raise SystemExit("--post-repair cannot be combined with another detail target mode")
        if (
            args.resume_summary
            or args.start_page != 1
            or args.start_offset != 0
            or args.start_cursor
            or args.discovery_job_id is not None
            or args.top_refresh_max_pages != 0
            or args.discovery_source_exhausted
        ):
            raise SystemExit("post repair cannot use discovery or resume arguments")
        platform_key = platforms[0]
        args.post_repair_targets = load_post_repair_targets(
            args.repair_targets_file,
            platform_key,
            db_path=args.db,
        )
        args.post_repair_detail_targets = [
            item["detail_target"] for item in args.post_repair_targets
        ]
        args.post_repair_target_ids = {
            item["platform_post_id"] for item in args.post_repair_targets
        }
        args.post_repair_metadata_by_identity = {
            f"{platform_key}:id:{item['platform_post_id']}": {
                "keyword": item["keyword"],
                **(item.get("repair_fallback") or {}),
            }
            for item in args.post_repair_targets
        }
    elif args.repair_targets_file:
        raise SystemExit("--repair-targets-file requires --post-repair")
    if "douyin" in platforms and args.start_page > 1 and not args.start_cursor:
        raise SystemExit(
            "Douyin continuation requires --start-cursor together with --start-page"
        )
    if "xhs" in platforms:
        if len(platforms) != 1:
            raise SystemExit("XHS must run alone through scripts/xhs_runner.py")
        if not args.xhs_account_id or not args.xhs_profile_dir:
            raise SystemExit("XHS requires --xhs-account-id and --xhs-profile-dir")
        if args.login_type != "qrcode":
            raise SystemExit("XHS requires --login-type qrcode for per-run login")
        if not args.xhs_repair and (
            not args.xhs_discovery_target_key or not args.xhs_discovery_query_fingerprint
        ):
            raise SystemExit("XHS requires runner-managed discovery target and query fingerprint")
        if args.behavior_profile != "xhs_guarded":
            raise SystemExit("XHS requires --behavior-profile xhs_guarded")
        if not args.xhs_repair and args.start_page > 1 and not args.start_cursor:
            raise SystemExit("XHS continuation requires --start-cursor together with --start-page")
        if not Path(args.xhs_profile_dir).expanduser().is_dir():
            raise SystemExit("XHS isolated profile directory does not exist")
    elif args.xhs_post_interaction != "none":
        raise SystemExit("--xhs-post-interaction is only supported for XHS")
    elif args.behavior_profile != "social_high_risk":
        raise SystemExit("generic MediaCrawler platforms require --behavior-profile social_high_risk")
    resume_records: list[dict[str, Any]] = []
    resume_info: dict[str, Any] | None = None
    resume_identity_values: list[str] = []
    if args.resume_summary:
        resume_path = Path(args.resume_summary).expanduser().resolve()
        try:
            resume_summary = json.loads(resume_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"cannot load --resume-summary: {exc}") from exc
        resume_records = [
            record
            for record in (resume_summary.get("records") or [])
            if isinstance(record, dict) and record.get("platform") in platforms
        ]
        if not resume_records:
            raise SystemExit("--resume-summary has no records for the selected platform")
        resume_validation, _ = collect_formal_records(
            {"records": resume_records},
            db_path=args.db,
            progress_callback=progress_callback,
        )
        prior_new_count = int(resume_validation.get("valid_new_count") or 0)
        resume_identity_values = [
            identity.split(":id:", 1)[1]
            for identity in (
                list(resume_validation.get("new_identities") or [])
                + list(resume_validation.get("existing_identities") or [])
            )
            if ":id:" in identity
        ]
        previous_candidate_count = int(
            (resume_summary.get("formal_validation") or {}).get("candidate_count") or 0
        )
        consumed_candidates = max(
            int(resume_validation.get("candidate_count") or 0),
            previous_candidate_count,
        )
        resume_info = {
            "summary_path": str(resume_path),
            "valid_new_count": prior_new_count,
            "candidate_count": consumed_candidates,
            "completion_mode": "source-exhausted",
            "start_page": args.start_page,
        }

    repair_allowed_identities: set[str] | None = None
    repair_metadata_by_identity: dict[str, dict[str, Any]] = {}
    repair_target_count = 0
    if args.xhs_repair:
        repair_allowed_identities = {
            f"xhs:id:{value}" for value in args.xhs_repair_target_ids
        }
        xhs_repair_fallbacks = load_post_repair_fallbacks(
            args.db,
            "xhs",
            args.xhs_repair_target_ids,
        )
        repair_metadata_by_identity = {
            f"xhs:id:{post_id}": metadata
            for post_id, metadata in xhs_repair_fallbacks.items()
        }
        repair_target_count = len(args.xhs_detail_urls)
    elif args.post_repair:
        repair_allowed_identities = {
            f"{platforms[0]}:id:{value}" for value in args.post_repair_target_ids
        }
        repair_metadata_by_identity = dict(args.post_repair_metadata_by_identity)
        repair_target_count = len(args.post_repair_targets)

    batch_dir = ensure_dir(Path(args.output_dir).expanduser() / utc_stamp()).resolve()
    _runtime_progress(progress_callback)
    args.resume_identities_path = None
    if resume_identity_values:
        resume_identities_path = batch_dir / "resume_identities.json"
        resume_identities_path.write_text(
            json.dumps(sorted(set(resume_identity_values)), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        args.resume_identities_path = str(resume_identities_path)

    records = list(resume_records)
    for platform_key in platforms:
        print(f"[mediacrawler] {platform_key}", flush=True)
        records.append(
            run_platform(
                platform_key,
                args,
                batch_dir,
                runtime_reporter=runtime_reporter,
            )
        )

    if runtime_reporter is not None:
        runtime_reporter.enter_finalizing()

    result_counts = latest_platform_result_counts(records, platforms)
    child_execution_ok = bool(
        result_counts["failed_count"] == 0
        and result_counts["ok_count"] == len(platforms)
    )
    repair_runtime_reason = (
        repair_runtime_stop_reason(records, platforms) if repair_mode else ""
    )
    repair_runtime_blocked = bool(repair_runtime_reason)
    if repair_mode and not repair_runtime_blocked and not child_execution_ok:
        child_execution_ok = repair_candidate_execution_completed(records, platforms)
    summary = {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "keyword": args.keyword,
        "batch_dir": str(batch_dir),
        "records": records,
        **result_counts,
    }
    behavior_validation = collect_behavior_validation(
        records,
        platforms,
        args.keyword,
        args.xhs_post_interaction,
        repair_mode=repair_mode,
    )
    _runtime_progress(progress_callback)
    summary["behavior_validation"] = behavior_validation
    if resume_info:
        summary["resume"] = resume_info
    if repair_mode:
        pagination_evidence = (
            xhs_repair_pagination_evidence(
                records,
                target_count=repair_target_count,
            )
            if args.xhs_repair
            else post_repair_pagination_evidence(
                args.post_repair_targets,
                platform=platforms[0],
                successful_identities=set(),
            )
        )
    else:
        pagination_evidence = load_pagination_evidence(
            os.environ.get("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", "").strip()
        )
    summary["pagination_evidence"] = pagination_evidence
    content_validation, content_valid_records = collect_formal_records(
        summary,
        db_path=args.db,
        pagination_evidence=pagination_evidence,
        allowed_identities=repair_allowed_identities,
        repair_metadata_by_identity=repair_metadata_by_identity,
        repair_mode=repair_mode,
        progress_callback=progress_callback,
    )
    if args.post_repair:
        pagination_evidence = post_repair_pagination_evidence(
            args.post_repair_targets,
            platform=platforms[0],
            successful_identities=set(content_validation.get("existing_identities") or []),
        )
        summary["pagination_evidence"] = pagination_evidence
        content_validation, content_valid_records = collect_formal_records(
            summary,
            db_path=args.db,
            pagination_evidence=pagination_evidence,
            allowed_identities=repair_allowed_identities,
            repair_metadata_by_identity=repair_metadata_by_identity,
            repair_mode=True,
            progress_callback=progress_callback,
        )
    materialized_images_by_identity: dict[str, list[MaterializedImage]] = {}
    if args.download_images:
        (
            image_materialization,
            _,
            localized_identities,
        ) = materialize_formal_record_images(
            content_valid_records,
            project_root=PROJECT_ROOT,
            media_root=media_root,
            promote=False,
            progress_callback=progress_callback,
        )
        image_materialization = attach_skipped_candidate_evidence(
            image_materialization,
            pagination_evidence,
        )
        validation, valid_records = collect_formal_records(
            summary,
            db_path=args.db,
            pagination_evidence=pagination_evidence,
            require_local_images=True,
            localized_identities=localized_identities,
            allowed_identities=repair_allowed_identities,
            repair_metadata_by_identity=repair_metadata_by_identity,
            repair_mode=repair_mode,
            progress_callback=progress_callback,
        )
    else:
        image_materialization = {
            "required": False,
            "promotion_required": False,
            "candidate_posts": len(content_valid_records),
            "complete_posts": 0,
            "expected_images": sum(
                len(content_image_candidates(str(item["platform"]), item["record"]))
                for item in content_valid_records
            ),
            "downloaded_images": 0,
            "validated_images": 0,
            "unique_images": 0,
            "sha256_duplicate_images": 0,
            "sha256_duplicates": [],
            "reused_images": 0,
            "promoted_images": 0,
            "rolled_back_images": 0,
            "retryable_failures": 0,
            "terminal_failures": 0,
            "complete": True,
            "manifest_paths": [],
            "manifest_sha256": None,
            "manifest_evidence": [],
            "failures": [],
        }
        image_materialization = attach_skipped_candidate_evidence(
            image_materialization,
            pagination_evidence,
        )
        validation, valid_records = content_validation, content_valid_records
    if args.post_repair:
        pagination_evidence = post_repair_pagination_evidence(
            args.post_repair_targets,
            platform=platforms[0],
            successful_identities=set(validation.get("existing_identities") or []),
            materialization_failures=list(image_materialization.get("failures") or []),
        )
        summary["pagination_evidence"] = pagination_evidence
        image_materialization = attach_skipped_candidate_evidence(
            image_materialization,
            pagination_evidence,
        )
        validation, valid_records = collect_formal_records(
            summary,
            db_path=args.db,
            pagination_evidence=pagination_evidence,
            require_local_images=args.download_images,
            localized_identities=(localized_identities if args.download_images else None),
            allowed_identities=repair_allowed_identities,
            repair_metadata_by_identity=repair_metadata_by_identity,
            repair_mode=True,
            progress_callback=progress_callback,
        )
    if repair_runtime_reason:
        validation = {**validation, "stop_reason": repair_runtime_reason}
    validation = apply_formal_completion_gates(
        validation,
        content_validation=content_validation,
        image_materialization=image_materialization,
        behavior_validation=behavior_validation,
        download_images=args.download_images,
        child_execution_ok=repair_partial_child_execution_allowed(
            repair_mode=repair_mode,
            child_execution_ok=child_execution_ok,
            runtime_blocked=repair_runtime_blocked,
            validation=validation,
            image_materialization=image_materialization,
            behavior_validation=behavior_validation,
        ),
    )
    promotion_allowed = formal_image_promotion_allowed(
        download_images=args.download_images,
        no_import=args.no_import,
        validation=validation,
    )
    with formal_media_persistence_lock(
        enabled=promotion_allowed,
        progress_callback=progress_callback,
    ):
        if promotion_allowed:
            (
                image_materialization,
                materialized_images_by_identity,
                localized_identities,
            ) = materialize_formal_record_images(
                content_valid_records,
                project_root=PROJECT_ROOT,
                media_root=media_root,
                promote=True,
                progress_callback=progress_callback,
            )
            image_materialization = attach_skipped_candidate_evidence(
                image_materialization,
                pagination_evidence,
            )
            validation, valid_records = collect_formal_records(
                summary,
                db_path=args.db,
                pagination_evidence=pagination_evidence,
                require_local_images=True,
                localized_identities=localized_identities,
                materialized_images_by_identity=materialized_images_by_identity,
                allowed_identities=repair_allowed_identities,
                repair_metadata_by_identity=repair_metadata_by_identity,
                repair_mode=repair_mode,
                progress_callback=progress_callback,
            )
            validation = apply_formal_completion_gates(
                validation,
                content_validation=content_validation,
                image_materialization=image_materialization,
                behavior_validation=behavior_validation,
                download_images=True,
                child_execution_ok=repair_partial_child_execution_allowed(
                    repair_mode=repair_mode,
                    child_execution_ok=child_execution_ok,
                    runtime_blocked=repair_runtime_blocked,
                    validation=validation,
                    image_materialization=image_materialization,
                    behavior_validation=behavior_validation,
                ),
            )
            if not formal_import_gate_met(validation):
                rolled_back = rollback_newly_promoted_images(
                    materialized_images_by_identity,
                    project_root=PROJECT_ROOT,
                    media_root=media_root,
                    db_path=args.db,
                    progress_callback=progress_callback,
                )
                image_materialization["rolled_back_images"] = int(
                    image_materialization.get("rolled_back_images") or 0
                ) + rolled_back
                image_materialization["promoted_images"] = 0
                materialized_images_by_identity = {}
                valid_records = []
        elif args.download_images and not args.no_import:
            image_materialization["promotion_deferred"] = True
            image_materialization["promotion_deferred_reason"] = validation["stop_reason"]
        summary["image_materialization"] = image_materialization
        summary["required_fields_profile"] = args.required_fields_profile
        summary["formal_validation"] = validation
        if args.no_import:
            summary["import_result"] = {"skipped": True, "reason": "no_import"}
        elif not formal_import_gate_met(validation):
            summary["import_result"] = {
                "skipped": True,
                "reason": validation["stop_reason"],
                "processed_rows": 0,
                "inserted_rows": 0,
                "updated_rows": 0,
                "topic_relevant_inserted_rows": 0,
                "topic_relevant_updated_rows": 0,
                "topic_irrelevant_inserted_rows": 0,
                "topic_irrelevant_updated_rows": 0,
            }
        else:
            summary["import_result"] = import_valid_records_with_media_rollback(
                summary,
                valid_records,
                Path(args.db).expanduser(),
                materialized_images_by_identity=materialized_images_by_identity,
                image_materialization=image_materialization,
                project_root=PROJECT_ROOT,
                media_root=media_root,
                progress_callback=progress_callback,
            )
    summary["completion_mode"] = "source-exhausted"
    import_result_value = summary.get("import_result") or {}
    import_performed = all(
        key in import_result_value
        for key in ("processed_rows", "inserted_rows", "updated_rows")
    ) and not bool(import_result_value.get("reason"))
    summary["import_completion_met"] = bool(
        formal_import_gate_met(validation)
        and (not image_materialization["required"] or image_materialization["complete"])
        and (
            args.no_import
            or import_performed
        )
    )
    if not summary["import_completion_met"]:
        summary["failure_reason"] = (
            "import_completion_not_met: "
            "completion_mode=source-exhausted "
            f"valid_new={validation['valid_new_count']} "
            f"stop_reason={validation['stop_reason']} "
            f"behavior_ok={behavior_validation['behavior_ok']} "
            f"policy_ok={behavior_validation['policy_ok']}"
        )
    summary_path = batch_dir / "summary.json"
    report_path = batch_dir / "summary.md"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    import_failed = import_result_value.get("reason") == "sqlite_import_failed"
    checkpoint_ok = not import_failed
    if import_failed:
        summary["discovery_checkpoint"] = {
            "skipped": True,
            "reason": "sqlite_import_failed",
        }
        summary["failure_reason"] = "sqlite_import_failed"
    else:
        try:
            with formal_media_persistence_lock(
                enabled=True,
                progress_callback=progress_callback,
            ):
                _runtime_progress(progress_callback)
                summary["discovery_checkpoint"] = persist_discovery_checkpoint(
                    args,
                    platforms[0],
                    pagination_evidence,
                )
        except (OSError, sqlite3.Error, ValueError) as exc:
            checkpoint_ok = False
            summary["discovery_checkpoint"] = {
                "skipped": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            summary["failure_reason"] = "discovery_checkpoint_write_failed"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _runtime_progress(progress_callback)
    write_markdown(summary, report_path)
    _runtime_progress(progress_callback)
    print(json.dumps({"summary": str(summary_path), "report": str(report_path), "batch_dir": str(batch_dir), **summary}, ensure_ascii=False, indent=2))
    partial_repair_import_ok = bool(
        repair_mode
        and summary["import_completion_met"]
        and int((summary.get("formal_validation") or {}).get("valid_total_count") or 0) > 0
    )
    return (
        0
        if (
            (summary["failed_count"] == 0 and summary["import_completion_met"] and checkpoint_ok)
            or (partial_repair_import_ok and checkpoint_ok)
        )
        else 2
    )


def main() -> int:
    args = parse_args()
    runtime_reporter = xhs_supervisor_runtime_reporter_from_context(args)
    return _run_main(args, runtime_reporter)


if __name__ == "__main__":
    raise SystemExit(main())
