#!/usr/bin/env python3
"""MediaCrawler-first structured crawl entrypoint."""

from __future__ import annotations

from trippostcollect.application.inputs import (
    load_zhihu_detail_urls as load_zhihu_detail_urls,
    load_xhs_detail_urls as load_xhs_detail_urls,
    load_xhs_repair_target_ids as load_xhs_repair_target_ids,
)
from trippostcollect.application.repair import (
    load_post_repair_fallbacks as load_post_repair_fallbacks,
    load_post_repair_targets as load_post_repair_targets,
    load_xhs_repair_report as load_xhs_repair_report,
    xhs_repair_pagination_evidence as xhs_repair_pagination_evidence,
    post_repair_pagination_evidence as post_repair_pagination_evidence,
    repair_partial_child_execution_allowed as repair_partial_child_execution_allowed,
    repair_candidate_execution_completed as repair_candidate_execution_completed,
    repair_runtime_stop_reason as _repair_runtime_stop_reason,
)

import argparse
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import json
import os
import random as random
import re
import sqlite3
import sys
import subprocess as subprocess  # 保留现有 run_command 测试与诊断的进程接缝。
import time
from collections import Counter
from datetime import datetime, timedelta as timedelta, timezone
from email.utils import parsedate_to_datetime as parsedate_to_datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterator, MutableMapping
from urllib.parse import unquote, urlparse

from playwright.async_api import async_playwright

from trippostcollect.runtime.cookies import (
    export_profile_cookies as export_profile_cookies,
    platform_cookie_url as platform_cookie_url,
    required_cookie_names as required_cookie_names,
    cookie_names_from_header as cookie_names_from_header,
    cookies_to_header as cookies_to_header,
    load_cookie_snapshot as load_cookie_snapshot,
    public_cookie_export as public_cookie_export,
)

from trippostcollect.artifacts.image_candidates import (
    ImageCandidate,
    content_image_candidates,
    image_items_for_record,
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
from trippostcollect.artifacts.evidence import write_evidence
from trippostcollect.artifacts.image_proxy import (
    RemoteImageFetchError,
    RemoteImagePreview,
    remote_image_failure_code,
    fetch_remote_image_bytes,
)
from trippostcollect.application.policy import (
    CrawlPolicyBlocked,
    clear_site_policy_state,
    record_site_cooldown,
    site_request_guard,
)
from execution_state import FrozenExecutionState as FrozenExecutionState
from trippostcollect.application.failures import classify_attempt
from trippostcollect.runtime.human_flow import install_runtime_hints
from trippostcollect.runtime.behavior import (
    HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS,
    behavior_evidence_valid,
    load_behavior_evidence as load_behavior_evidence,
    run_page_behavior,
)
from trippostcollect.core.paths import (
    COOKIE_SNAPSHOT_FILENAME as COOKIE_SNAPSHOT_FILENAME,
    FORMAL_MEDIA_PERSISTENCE_LOCK,
    LOCAL_MEDIA_ROOT,
    MEDIACRAWLER_DIR,
    MEDIACRAWLER_RUNS_OUTPUT,
    PROJECT_ROOT,
    ensure_dir,
    ensure_parent,
)
from trippostcollect.core import paths, resources as resources
from trippostcollect.core.resources import verify_package_resources
from trippostcollect.runtime.browser_launcher import discover_cdp_browser_path
from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.db.connection import connect_db
from trippostcollect.platforms.registry import get_site
from trippostcollect.records.sanitization import (
    sanitize_author_avatar_data,
)
from trippostcollect.records.topic_relevance import (
    CONTENT_BODY_FIELDS as CONTENT_BODY_FIELDS,
    effective_source_keyword,
    is_topic_relevant as is_topic_relevant,
    topic_relevant_for_web_post,
    web_post_content_text as web_post_content_text,
    web_post_title as web_post_title,
)
from trippostcollect.xhs.leases import (
    LEASE_DB_ENV,
    LEASE_ID_ENV,
    LEASE_OWNER_TOKEN_ENV,
    ProcessIdentity,
    SystemProcessInspector,
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
from trippostcollect.runtime.browser_runtime import (
    XHS_WINDOW_SIZE_ENV,
    browser_launch_environment,
    browser_runtime_args,
    xhs_window_size_value,
)


from trippostcollect.application.contracts import (
    XhsRuntimeSupervisionError as XhsRuntimeSupervisionError,
)

from trippostcollect.runtime.helpers import (
    utc_stamp as utc_stamp,
    _runtime_progress as _runtime_progress,
    _runtime_progress_if_due as _runtime_progress_if_due,
)

from trippostcollect.runtime.process import (
    XhsParentNetworkPauseClock as XhsParentNetworkPauseClock,
    decode_text as decode_text,
    tail as tail,
    progress_path_signature as progress_path_signature,
    process_group_exists as process_group_exists,
    terminate_managed_process as terminate_managed_process,
    runtime_watchdog_stop_detail as runtime_watchdog_stop_detail,
    append_runtime_watchdog_stop_event as append_runtime_watchdog_stop_event,
    run_command as run_command,
    skipped_command as skipped_command,
    xhs_network_state_from_diagnostics as xhs_network_state_from_diagnostics,
    _diagnostic_timestamp as _diagnostic_timestamp,
    _fresh_diagnostic_timestamp as _fresh_diagnostic_timestamp,
    _sanitized_transport_reason as _sanitized_transport_reason,
    PROCESS_PROGRESS_POLL_SECONDS as PROCESS_PROGRESS_POLL_SECONDS,
    PROCESS_CLEANUP_GRACE_SECONDS as PROCESS_CLEANUP_GRACE_SECONDS,
    PROCESS_FINAL_REAP_SECONDS as PROCESS_FINAL_REAP_SECONDS,
    XHS_NETWORK_DIAGNOSTIC_MAX_AGE_SECONDS as XHS_NETWORK_DIAGNOSTIC_MAX_AGE_SECONDS,
    XHS_NETWORK_DIAGNOSTIC_MAX_BYTES as XHS_NETWORK_DIAGNOSTIC_MAX_BYTES,
    XHS_PARENT_NETWORK_PAUSE_CEILING_SECONDS as XHS_PARENT_NETWORK_PAUSE_CEILING_SECONDS,
    XHS_CHILD_NETWORK_TERMINAL_GRACE_SECONDS as XHS_CHILD_NETWORK_TERMINAL_GRACE_SECONDS,
    SUPERVISOR_RUNTIME_TIMEOUT_REASONS as SUPERVISOR_RUNTIME_TIMEOUT_REASONS,
    RUNTIME_WATCHDOG_STOP_DETAILS as RUNTIME_WATCHDOG_STOP_DETAILS,
    PAGINATION_EVENT_FIELDS as PAGINATION_EVENT_FIELDS,
    _XHS_TRANSPORT_MARKER_REASONS as _XHS_TRANSPORT_MARKER_REASONS,
    _XHS_TRANSPORT_ERROR_TYPE_REASONS as _XHS_TRANSPORT_ERROR_TYPE_REASONS,
)

from trippostcollect.application.inputs import (
    parse_args as parse_args,
    selected_platforms as selected_platforms,
    PLATFORMS as PLATFORMS,
)

from trippostcollect.application.collection import (
    behavior_environment as behavior_environment,
)

from trippostcollect.records import formal as _formal
from trippostcollect.records.formal import (
    json_dump as json_dump,
    parse_int as parse_int,
    first_value as first_value,
    timestamp_to_iso as timestamp_to_iso,
    parse_datetime_text as parse_datetime_text,
    datetime_value_to_iso as datetime_value_to_iso,
    iter_nested_values_for_keys as iter_nested_values_for_keys,
    published_at_for_record as published_at_for_record,
    merge_repair_fallback_metadata as merge_repair_fallback_metadata,
    is_video_record as is_video_record,
    platform_from_path as platform_from_path,
    post_id_for_record as post_id_for_record,
    canonical_url_for_record as canonical_url_for_record,
    content_body_for_record as content_body_for_record,
    content_text_for_record as content_text_for_record,
    formal_record_identity as formal_record_identity,
    formal_database_identities as formal_database_identities,
    BILIBILI_TRUSTED_DETAIL_SOURCES as BILIBILI_TRUSTED_DETAIL_SOURCES,
    TRUSTED_CONTENT_DETAIL_SOURCES as TRUSTED_CONTENT_DETAIL_SOURCES,
    FOLLOWERS_REQUIRED_PLATFORMS as FOLLOWERS_REQUIRED_PLATFORMS,
    REQUIRED_FOLLOWER_SOURCES as REQUIRED_FOLLOWER_SOURCES,
    PLATFORM_REQUIRED_METRICS as PLATFORM_REQUIRED_METRICS,
    VIDEO_URL_RE as VIDEO_URL_RE,
    CHINA_TZ as CHINA_TZ,
    TIMESTAMP_MIN as TIMESTAMP_MIN,
    TIMESTAMP_MAX as TIMESTAMP_MAX,
    DATETIME_TEXT_FORMATS as DATETIME_TEXT_FORMATS,
    PUBLISHED_AT_KEYS as PUBLISHED_AT_KEYS,
    NESTED_PUBLISHED_AT_KEYS as NESTED_PUBLISHED_AT_KEYS,
)

from trippostcollect.runtime.image_retry import (
    is_retryable_image_error as is_retryable_image_error,
    is_runtime_blocking_image_error as is_runtime_blocking_image_error,
    RETRYABLE_IMAGE_ERROR_CODES as RETRYABLE_IMAGE_ERROR_CODES,
    RUNTIME_BLOCKING_IMAGE_ERROR_CODES as RUNTIME_BLOCKING_IMAGE_ERROR_CODES,
)


from trippostcollect.platforms.bilibili import (
    core as _bilibili_core,
    client as _bilibili_client,
    login as _bilibili_login,
)
from trippostcollect.application.contracts import (
    BilibiliBehaviorPorts,
    BilibiliImageFetchPorts,
    BilibiliImagePorts,
    BilibiliSearchPorts,
)
from trippostcollect.db.discovery_read import load_existing_formal_identities as load_existing_formal_identities
from trippostcollect.platforms.bilibili.models import (
    BilibiliArticleDetailError as BilibiliArticleDetailError,
    BilibiliFollowerFetchError as BilibiliFollowerFetchError,
    BilibiliRuntimeBlocked as BilibiliRuntimeBlocked,
)
from trippostcollect.platforms.bilibili.parser import (
    clean_html_text as clean_html_text,
    normalize_bilibili_article_record as normalize_bilibili_article_record,
    clean_bilibili_article_body as clean_bilibili_article_body,
    normalize_bilibili_detail_image_url as normalize_bilibili_detail_image_url,
    extract_bilibili_detail_images as extract_bilibili_detail_images,
    hydrate_bilibili_article_record as hydrate_bilibili_article_record,
    BILIBILI_HTML_IMAGE_RE as BILIBILI_HTML_IMAGE_RE,
)
from trippostcollect.platforms.bilibili.client import (
    bilibili_detail_headers as bilibili_detail_headers,
    bilibili_image_headers as bilibili_image_headers,
    fetch_bilibili_article_detail as fetch_bilibili_article_detail,
    fetch_bilibili_article_detail_with_retry as fetch_bilibili_article_detail_with_retry,
    fetch_bilibili_wbi_keys as fetch_bilibili_wbi_keys,
    fetch_bilibili_article_page as fetch_bilibili_article_page,
    fetch_bilibili_follower_count as fetch_bilibili_follower_count,
    BILIBILI_ARTICLE_SEARCH_URL as BILIBILI_ARTICLE_SEARCH_URL,
    BILIBILI_ARTICLE_DETAIL_URL as BILIBILI_ARTICLE_DETAIL_URL,
    BILIBILI_RELATION_STAT_URL as BILIBILI_RELATION_STAT_URL,
    BILIBILI_ARTICLE_PAGE_SIZE as BILIBILI_ARTICLE_PAGE_SIZE,
    BILIBILI_DETAIL_MAX_ATTEMPTS as BILIBILI_DETAIL_MAX_ATTEMPTS,
    BILIBILI_DETAIL_RETRY_DELAY_SECONDS as BILIBILI_DETAIL_RETRY_DELAY_SECONDS,
    BILIBILI_DETAIL_RETRYABLE_CODES as BILIBILI_DETAIL_RETRYABLE_CODES,
    BILIBILI_RUNTIME_BLOCKING_CODES as BILIBILI_RUNTIME_BLOCKING_CODES,
    BILIBILI_BROWSER_USER_AGENT as BILIBILI_BROWSER_USER_AGENT,
)
from trippostcollect.platforms.bilibili.core import (
    BILIBILI_DETAIL_PACING_SECONDS as BILIBILI_DETAIL_PACING_SECONDS,
    BILIBILI_IMAGE_MAX_ATTEMPTS as BILIBILI_IMAGE_MAX_ATTEMPTS,
    BILIBILI_IMAGE_RETRY_DELAY_SECONDS as BILIBILI_IMAGE_RETRY_DELAY_SECONDS,
)
from trippostcollect.platforms.bilibili.signer import (
    sign_bilibili_wbi_params as sign_bilibili_wbi_params,
    BILIBILI_WBI_MIXIN_TABLE as BILIBILI_WBI_MIXIN_TABLE,
)

ROOT = PROJECT_ROOT
DEFAULT_OUTPUT = MEDIACRAWLER_RUNS_OUTPUT
XHS_OPERATOR_LOGIN_WAIT_SECONDS = 600
XHS_NETWORK_RECOVERY_WAIT_SECONDS = 600
XHS_NETWORK_RETRY_MIN_SECONDS = 2
XHS_NETWORK_RETRY_MAX_SECONDS = 30
FORMAL_SQLITE_BUSY_TIMEOUT_MS = 60_000
XHS_RUNTIME_STATUS_AUTH_KEY_RE = re.compile(r"[0-9a-f]{64}\Z")
XHS_RUNTIME_STATUS_RUN_ID_ENV = "TRIPPOSTCOLLECT_XHS_RUN_ID"


LEGACY_ZHIHU_TRANSFORM_SUFFIX_RE = re.compile(
    r"_(?:b|r|qhd|hd|xs|s|m|l|xl|xxl|original|watermark)"
    r"\.(?:avif|gif|jpe?g|png|webp)$",
    re.IGNORECASE,
)
RASTER_IMAGE_SUFFIX_RE = re.compile(
    r"\.(?:avif|gif|jpe?g|png|webp)$",
    re.IGNORECASE,
)



IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".svg", ".img"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
AUTHOR_FIELD_MARKERS = ("author", "user", "nickname", "avatar", "fans", "follower", "follow", "up")


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












def ensure_prerequisites() -> None:
    if not (MEDIACRAWLER_DIR / "pyproject.toml").exists():
        raise SystemExit(f"MediaCrawler is missing or incomplete: {MEDIACRAWLER_DIR}")
    verify_package_resources()


def profile_dir_for(platform_key: str) -> Path:
    return paths.platform_profile_dir(platform_key)


def cookie_snapshot_path(platform_key: str) -> Path:
    return paths.platform_cookie_snapshot_path(platform_key)




async def run_bilibili_behavior_session(
    args: argparse.Namespace,
    evidence_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return await _bilibili_login.run_bilibili_behavior_session(
        args, evidence_path,
        ports=BilibiliBehaviorPorts(
            discover_cdp_browser_path=discover_cdp_browser_path,
            profile_dir_for=profile_dir_for,
            async_playwright=async_playwright,
            browser_runtime_args=browser_runtime_args,
            browser_launch_environment=browser_launch_environment,
            install_runtime_hints=install_runtime_hints,
            run_page_behavior=run_page_behavior,
            write_evidence=write_evidence,
            platform_cookie_url=platform_cookie_url,
            cookies_to_header=cookies_to_header,
            cookie_snapshot_path=cookie_snapshot_path,
            cookie_names_from_header=cookie_names_from_header,
            required_cookie_names=required_cookie_names,
        ),
    )


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



def extract_sample(record: dict[str, Any]) -> dict[str, str]:
    sample = {key: truncate(record[key]) for key in SAMPLE_KEYS if record.get(key) not in (None, "")}
    published_at = published_at_for_record(record)
    if published_at and "published_at" not in sample:
        sample["published_at"] = published_at
    return sample or {key: truncate(value) for key, value in list(record.items())[:8]}


def summarize_jsonl(
    path: Path,
    keyword: str,
    *,
    progress_callback: Callable[[], object] | None = None,
) -> dict[str, Any]:
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
    _runtime_progress(progress_callback)
    last_checkpoint_at = time.monotonic()
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            last_checkpoint_at = _runtime_progress_if_due(
                progress_callback,
                last_checkpoint_at,
            )
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
    _runtime_progress(progress_callback)
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


def summarize_output(
    save_path: Path,
    keyword: str,
    *,
    progress_callback: Callable[[], object] | None = None,
) -> dict[str, Any]:
    _runtime_progress(progress_callback)
    last_checkpoint_at = time.monotonic()
    files: list[Path] = []
    if save_path.exists():
        for path in save_path.rglob("*"):
            last_checkpoint_at = _runtime_progress_if_due(
                progress_callback,
                last_checkpoint_at,
            )
            if path.is_file():
                files.append(path)
    files.sort()
    all_jsonl_files = [path for path in files if path.suffix.lower() == ".jsonl"]
    image_manifest_paths = [path for path in all_jsonl_files if path.name == "image_manifest.jsonl"]
    jsonl_files = [path for path in all_jsonl_files if path.name != "image_manifest.jsonl"]
    jsonl = [
        summarize_jsonl(
            path,
            keyword,
            progress_callback=progress_callback,
        )
        for path in jsonl_files
    ]
    counts = {"contents": 0, "comments": 0, "creators": 0, "unknown": 0}
    fields: set[str] = set()
    author_like_fields: set[str] = set()
    samples: list[dict[str, str]] = []
    keyword_hits = 0
    parse_errors = 0
    video_like_records = 0
    published_at_records = 0
    for item in jsonl:
        last_checkpoint_at = _runtime_progress_if_due(
            progress_callback,
            last_checkpoint_at,
        )
        counts[item["item_type"]] = counts.get(item["item_type"], 0) + item["line_count"]
        fields.update(item["top_level_fields"])
        author_like_fields.update(item["author_like_fields"])
        keyword_hits += item["keyword_hit_records"]
        parse_errors += item["parse_errors"]
        video_like_records += int(item.get("video_like_records") or 0)
        published_at_records += int(item.get("published_at_records") or 0)
        samples.extend(item["samples"])

    image_files = [path for path in files if path.suffix.lower() in IMAGE_SUFFIXES]
    video_files = [path for path in files if path.suffix.lower() in VIDEO_SUFFIXES]
    _runtime_progress(progress_callback)
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


def summarize_output_with_progress(
    save_path: Path,
    keyword: str,
    progress_callback: Callable[[], object] | None,
) -> dict[str, Any]:
    if progress_callback is None:
        return summarize_output(save_path, keyword)
    return summarize_output(
        save_path,
        keyword,
        progress_callback=progress_callback,
    )


def write_json_with_progress(
    path: Path,
    value: Any,
    *,
    progress_callback: Callable[[], object] | None = None,
    trailing_newline: bool = False,
) -> None:
    """Atomically stream JSON while keeping synchronous supervision alive."""

    ensure_parent(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoder = json.JSONEncoder(ensure_ascii=False, indent=2)
    _runtime_progress(progress_callback)
    last_checkpoint_at = time.monotonic()
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            for chunk in encoder.iterencode(value):
                for start in range(0, len(chunk), 1024 * 1024):
                    handle.write(chunk[start : start + 1024 * 1024])
                    last_checkpoint_at = _runtime_progress_if_due(
                        progress_callback,
                        last_checkpoint_at,
                    )
            if trailing_newline:
                handle.write("\n")
        _runtime_progress(progress_callback)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def terminal_summary_envelope(
    *,
    summary_path: Path,
    report_path: Path,
    batch_dir: Path,
    summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "summary": str(summary_path),
        "report": str(report_path),
        "batch_dir": str(batch_dir),
        "status": "completed" if summary["import_completion_met"] else "failed",
        "import_completion_met": summary["import_completion_met"],
        "failure_reason": summary.get("failure_reason"),
    }


def ensure_web_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    return bootstrap_connection(conn, sync_jobs=False)


def inject_materialized_images(
    image_items: list[dict[str, Any]],
    materialized_images: list[MaterializedImage],
) -> list[dict[str, Any]]:
    return _formal.inject_materialized_images(
        image_items, materialized_images, ImagePersistenceError=ImagePersistenceError,
    )


def row_for_record(
    platform_key: str,
    record: dict[str, Any],
    *,
    artifact_dir: str,
    captured_at: str,
    keyword: str,
    materialized_images: list[MaterializedImage] | None = None,
) -> dict[str, Any]:
    return _formal.row_for_record(
        platform_key, record, artifact_dir=artifact_dir, captured_at=captured_at,
        keyword=keyword, materialized_images=materialized_images,
        image_items_for_record=image_items_for_record, ImagePersistenceError=ImagePersistenceError,
    )


def validate_formal_record(
    platform_key: str,
    record: dict[str, Any],
    seen: set[str],
    *,
    allow_xhs_title_image_only: bool = False,
) -> dict[str, Any]:
    return _formal.validate_formal_record(
        platform_key, record, seen, allow_xhs_title_image_only=allow_xhs_title_image_only,
        content_image_candidates=content_image_candidates,
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



def load_pagination_evidence(
    state_path: str | Path | None,
    *,
    progress_callback: Callable[[], object] | None = None,
) -> dict[str, Any]:
    if not state_path:
        return {"available": False, "stopped": False, "batches": []}
    path = Path(state_path).expanduser()
    try:
        _runtime_progress(progress_callback)
        chunks: list[str] = []
        last_checkpoint_at = time.monotonic()
        with path.open("r", encoding="utf-8") as handle:
            while chunk := handle.read(1024 * 1024):
                chunks.append(chunk)
                last_checkpoint_at = _runtime_progress_if_due(
                    progress_callback,
                    last_checkpoint_at,
                )
        payload = json.loads("".join(chunks))
        _runtime_progress(progress_callback)
    except (OSError, json.JSONDecodeError, TypeError):
        return {
            "available": False,
            "stopped": False,
            "state_path": str(path),
            "batches": [],
        }

    batches = []
    stopped_details: dict[str, Any] | None = None
    runtime_terminal: dict[str, Any] | None = None
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
        elif event.get("type") == "xhs_runtime_terminal":
            runtime_terminal = {
                key: details.get(key)
                for key in (
                    "phase",
                    "failure_type",
                    "stop_reason",
                    "stop_detail",
                    "checkpoint_kind",
                    "manual_progress_observed",
                    "matched_markers",
                    "retryable",
                )
                if key in details
            }

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
        "runtime_terminal": runtime_terminal,
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



def fetch_bilibili_image_bytes(
    source_url: str,
    post_id: str,
    cookie_header: str = "",
) -> RemoteImagePreview:
    return _bilibili_client.fetch_bilibili_image_bytes(
        source_url, post_id, cookie_header,
        ports=BilibiliImageFetchPorts(
            fetch_remote_image_bytes=fetch_remote_image_bytes,
            DEFAULT_ARCHIVE_IMAGE_MAX_BYTES=DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
            SUPPORTED_IMAGE_MIME_TYPES=SUPPORTED_IMAGE_MIME_TYPES,
        ),
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
    return _bilibili_core.download_bilibili_record_images(
        record, cookie_header=cookie_header, platform_data_root=platform_data_root,
        fetcher=fetcher, sleep_fn=sleep_fn, log_fn=log_fn, max_attempts=max_attempts,
        ports=BilibiliImagePorts(
            ImageManifestEntry=ImageManifestEntry,
            ImageMaterializationError=ImageMaterializationError,
            RemoteImageFetchError=RemoteImageFetchError,
            content_image_candidates=content_image_candidates,
            fetch_bilibili_image_bytes=fetch_bilibili_image_bytes,
            is_retryable_image_error=is_retryable_image_error,
            remote_image_failure_code=remote_image_failure_code,
            safe_platform_post_id=safe_platform_post_id,
            write_staging_image=write_staging_image,
        ),
    )



def run_bilibili_article_search(args: argparse.Namespace, batch_dir: Path) -> dict[str, Any]:
    return _bilibili_core.run_bilibili_article_search(
        args, batch_dir,
        ports=BilibiliSearchPorts(
            load_behavior_evidence=load_behavior_evidence,
            behavior_evidence_valid=behavior_evidence_valid,
            load_existing_formal_identities=load_existing_formal_identities,
            load_skipped_candidates=load_skipped_candidates,
            connect_database=sqlite3.connect,
            run_bilibili_behavior_session=run_bilibili_behavior_session,
            download_bilibili_record_images=download_bilibili_record_images,
            content_image_candidates=content_image_candidates,
            write_manifest_atomic=write_manifest_atomic,
            manifest_sha256=manifest_sha256,
            is_retryable_image_error=is_retryable_image_error,
            is_runtime_blocking_image_error=is_runtime_blocking_image_error,
            formal_database_identities=formal_database_identities,
            summarize_output=summarize_output,
            tail=tail,
            validate_formal_record=validate_formal_record,
        ),
    )


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
        sys.executable,
        "-P",
        "-m",
        "trippostcollect.platforms.entry",
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
        from trippostcollect.xhs.batch_checkpoint import DATA_ROOT_ENV, ENABLED_ENV, RESUME_ENV

        extra_env[DATA_ROOT_ENV] = str(save_path.resolve())
        extra_env[RESUME_ENV] = str(getattr(args, "resume_summary", None) or "")
        if any(getattr(args, key, False) for key in ("no_import", "xhs_repair", "post_repair")):
            extra_env[ENABLED_ENV] = "0"
        extra_env.update(
            {
                "TRIPPOSTCOLLECT_XHS_ENRICH_CREATORS": "1",
                "TRIPPOSTCOLLECT_XHS_KEEP_AUTHOR_DETAIL": "1",
                "TRIPPOSTCOLLECT_SHARE_CDP_PROFILE": "1",
                "TRIPPOSTCOLLECT_XHS_PROFILE_DIR": str(Path(args.xhs_profile_dir).expanduser().resolve()),
                "TRIPPOSTCOLLECT_XHS_ACCOUNT_ID": str(args.xhs_account_id),
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
                "TRIPPOSTCOLLECT_XHS_NETWORK_WAIT_SECONDS": str(
                    XHS_NETWORK_RECOVERY_WAIT_SECONDS
                ),
                "TRIPPOSTCOLLECT_XHS_NETWORK_RETRY_MIN_SECONDS": str(
                    XHS_NETWORK_RETRY_MIN_SECONDS
                ),
                "TRIPPOSTCOLLECT_XHS_NETWORK_RETRY_MAX_SECONDS": str(
                    XHS_NETWORK_RETRY_MAX_SECONDS
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
                output = summarize_output_with_progress(
                    save_path,
                    args.keyword,
                    runtime_reporter.checkpoint if runtime_reporter is not None else None,
                )
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
        ROOT,
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
    runtime_stop_detail = runtime_watchdog_stop_detail(run)
    if runtime_stop_detail in RUNTIME_WATCHDOG_STOP_DETAILS:
        run["timeout_state_event"] = append_runtime_watchdog_stop_event(
            execution_state_path,
            timeout_reason=runtime_stop_detail,
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
            network_pause_total_seconds=float(
                run.get("network_pause_total_seconds") or 0.0
            ),
            network_pause_ceiling_seconds=(
                float(run["network_pause_ceiling_seconds"])
                if run.get("network_pause_ceiling_seconds") is not None
                else None
            ),
            network_terminal_grace_seconds=(
                float(run["network_terminal_grace_seconds"])
                if run.get("network_terminal_grace_seconds") is not None
                else None
            ),
        )
    behavior_evidence = load_behavior_evidence(behavior_evidence_path)
    repair_report = (
        load_xhs_repair_report(log_dir / "repair_report.json")
        if platform_key == "xhs" and getattr(args, "xhs_repair", False)
        else {}
    )
    output = summarize_output_with_progress(
        save_path,
        args.keyword,
        runtime_reporter.checkpoint if runtime_reporter is not None else None,
    )
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
            "output": summarize_output_with_progress(
                save_path,
                args.keyword,
                runtime_reporter.checkpoint if runtime_reporter is not None else None,
            ),
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
            "output": summarize_output_with_progress(
                save_path,
                args.keyword,
                runtime_reporter.checkpoint if runtime_reporter is not None else None,
            ),
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
                label=record.get("label") or PLATFORMS[record["platform"]]["label"],
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
        label = record.get("label") or PLATFORMS[record["platform"]]["label"]
        lines.append(f"### {label}")
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
    raw_runtime_stop_reason = str(gated.get("stop_reason") or "")
    if raw_runtime_stop_reason in {"login_required", "captcha_detected"}:
        runtime_stop_reason = raw_runtime_stop_reason
    elif raw_runtime_stop_reason in RUNTIME_BLOCKING_FAILURE_TYPES:
        runtime_stop_reason = "runtime_failed"
    else:
        runtime_stop_reason = ""
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






RUNTIME_BLOCKING_FAILURE_TYPES = frozenset(
    {
        "runtime_failed",
        "policy_blocked",
        "platform_security_limit",
        "sms_verification_terminal",
        "manual_checkpoint_timeout",
        "captcha_detected",
        "login_required",
        "rate_limited",
        "blocked_or_forbidden",
        "runtime_permission_error",
        "browser_launch_failed",
        "browser_target_closed",
        "browser_runtime_failed",
        "login_runtime_error",
        "verification_timeout",
        "ip_blocked",
    }
)


def runtime_blocker_stop_reason(failure_type: str) -> str:
    """Map a diagnostic failure family onto the formal stop-state contract."""

    if failure_type in {"login_required", "captcha_detected"}:
        return failure_type
    return "runtime_failed"


def runtime_blocker_from_terminal_event(event: Any) -> dict[str, Any]:
    """Normalize the safe child event written before login exceptions escape."""

    if not isinstance(event, dict):
        return {}
    failure_type = str(event.get("failure_type") or "")
    stop_detail = str(event.get("stop_detail") or "")
    if failure_type not in RUNTIME_BLOCKING_FAILURE_TYPES or not stop_detail:
        return {}
    stop_reason = str(event.get("stop_reason") or "")
    if stop_reason not in {"runtime_failed", "login_required", "captcha_detected"}:
        stop_reason = runtime_blocker_stop_reason(failure_type)
    return {
        "platform": "xhs",
        "status": "blocked",
        "failure_type": failure_type,
        "stop_reason": stop_reason,
        "reason": stop_detail,
        "retryable": bool(event.get("retryable", False)),
        "source": "xhs_runtime_terminal",
    }


def runtime_blocker_from_pagination_evidence(
    pagination_evidence: Any,
    platforms: list[str],
) -> dict[str, Any]:
    """Recover a run blocker from the current adaptive stop event."""

    if not isinstance(pagination_evidence, dict):
        return {}
    stop_event = pagination_evidence.get("stop_event") or {}
    if not isinstance(stop_event, dict):
        return {}
    stop_reason = str(stop_event.get("stop_reason") or "")
    if stop_reason not in {"runtime_failed", "login_required", "captcha_detected"}:
        return {}
    stop_detail = str(stop_event.get("stop_detail") or stop_reason)
    platform = str(stop_event.get("platform") or "")
    if platform not in platforms:
        platform = platforms[0] if len(platforms) == 1 else ""
    if not platform:
        return {}
    classification = classify_attempt(
        exit_code=1,
        stderr=stop_detail,
        meta={"platform": platform},
    )
    failure_type = str(classification.get("failure_type") or "")
    if failure_type not in RUNTIME_BLOCKING_FAILURE_TYPES:
        failure_type = (
            stop_reason
            if stop_reason in {"login_required", "captcha_detected"}
            else "runtime_failed"
        )
    return {
        "platform": platform,
        "status": "blocked",
        "failure_type": failure_type,
        "stop_reason": stop_reason,
        "reason": stop_detail,
        "retryable": False,
        "source": "adaptive_search_stopped",
    }


def latest_runtime_blocker(
    records: list[dict[str, Any]],
    platforms: list[str],
) -> dict[str, Any]:
    """Return only the current attempt's structured blocker per platform.

    ``records`` can start with resumed campaign records.  Reading each
    platform from the end prevents a historical blocker from contaminating a
    newer attempt.
    """

    latest_by_platform: dict[str, dict[str, Any]] = {}
    for record in reversed(records):
        if not isinstance(record, dict):
            continue
        platform = str(record.get("platform") or "")
        if platform in platforms and platform not in latest_by_platform:
            latest_by_platform[platform] = record

    for platform in platforms:
        record = latest_by_platform.get(platform) or {}
        repair_report = record.get("repair_report") or {}
        repair_runtime_blocker = repair_report.get("runtime_blocker") or {}
        blocker_code = str(repair_runtime_blocker.get("error_code") or "")
        if blocker_code in RUNTIME_BLOCKING_FAILURE_TYPES:
            return {
                "platform": platform,
                "status": "blocked",
                "failure_type": blocker_code,
                "stop_reason": runtime_blocker_stop_reason(blocker_code),
                "reason": str(
                    repair_runtime_blocker.get("reason") or blocker_code
                ),
                "retryable": bool(
                    repair_runtime_blocker.get("retryable", False)
                ),
            }
        classification = record.get("failure_classification") or {}
        failure_type = str(
            classification.get("failure_type") or ""
        )
        if failure_type in RUNTIME_BLOCKING_FAILURE_TYPES:
            return {
                "platform": platform,
                "status": str(classification.get("status") or "blocked"),
                "failure_type": failure_type,
                "stop_reason": runtime_blocker_stop_reason(failure_type),
                "reason": str(classification.get("reason") or failure_type),
                "retryable": bool(classification.get("retryable", False)),
            }
    return {}


def repair_runtime_stop_reason(records: list[dict[str, Any]], platforms: list[str]) -> str:
    return _repair_runtime_stop_reason(records, platforms, latest_runtime_blocker=latest_runtime_blocker)


def apply_runtime_blocker(
    validation: dict[str, Any],
    runtime_blocker: dict[str, Any],
) -> dict[str, Any]:
    """Project the current attempt's blocker without losing its subtype."""

    if not runtime_blocker:
        return validation
    return {
        **validation,
        "pagination_runtime_blocked": True,
        "stop_reason": str(runtime_blocker.get("stop_reason") or "")
        or runtime_blocker_stop_reason(str(runtime_blocker["failure_type"])),
        "stop_detail": str(runtime_blocker["reason"]),
        "runtime_blocker": runtime_blocker,
    }


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
        write_json_with_progress(
            resume_identities_path,
            sorted(set(resume_identity_values)),
            progress_callback=progress_callback,
            trailing_newline=True,
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
    runtime_blocker = latest_runtime_blocker(records, platforms)
    runtime_blocked = bool(runtime_blocker)
    if repair_mode and not runtime_blocked and not child_execution_ok:
        child_execution_ok = repair_candidate_execution_completed(records, platforms)
    summary = {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "keyword": args.keyword,
        "batch_dir": str(batch_dir),
        "records": records,
        **result_counts,
    }
    if runtime_blocker:
        summary["runtime_blocker"] = runtime_blocker
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
            os.environ.get("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", "").strip(),
            progress_callback=progress_callback,
        )
    summary["pagination_evidence"] = pagination_evidence
    runtime_terminal = pagination_evidence.get("runtime_terminal") or {}
    if not runtime_terminal and repair_mode:
        runtime_terminal = (
            load_pagination_evidence(
                os.environ.get("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", "").strip(),
                progress_callback=progress_callback,
            ).get("runtime_terminal")
            or {}
        )
    event_runtime_blocker = runtime_blocker_from_terminal_event(runtime_terminal)
    pagination_runtime_blocker = runtime_blocker_from_pagination_evidence(
        pagination_evidence,
        platforms,
    )
    authoritative_runtime_blocker = (
        event_runtime_blocker or pagination_runtime_blocker
    )
    if authoritative_runtime_blocker:
        runtime_blocker = authoritative_runtime_blocker
        runtime_blocked = True
        if event_runtime_blocker:
            summary["runtime_terminal"] = runtime_terminal
        summary["runtime_blocker"] = runtime_blocker
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
    validation = apply_runtime_blocker(validation, runtime_blocker)
    validation = apply_formal_completion_gates(
        validation,
        content_validation=content_validation,
        image_materialization=image_materialization,
        behavior_validation=behavior_validation,
        download_images=args.download_images,
        child_execution_ok=repair_partial_child_execution_allowed(
            repair_mode=repair_mode,
            child_execution_ok=child_execution_ok,
            runtime_blocked=runtime_blocked,
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
                    runtime_blocked=runtime_blocked,
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
    write_json_with_progress(
        summary_path,
        summary,
        progress_callback=progress_callback,
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
    write_json_with_progress(
        summary_path,
        summary,
        progress_callback=progress_callback,
    )
    _runtime_progress(progress_callback)
    write_markdown(summary, report_path)
    _runtime_progress(progress_callback)
    print(
        json.dumps(
            terminal_summary_envelope(
                summary_path=summary_path,
                report_path=report_path,
                batch_dir=batch_dir,
                summary=summary,
            ),
            ensure_ascii=False,
        ),
        flush=True,
    )
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
