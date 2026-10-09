#!/usr/bin/env python3
"""MediaCrawler-first structured crawl entrypoint."""

from __future__ import annotations

from contextlib import contextmanager as contextmanager
from dataclasses import replace as replace
import fcntl as fcntl
import json as json
import sys as sys
import time as time
from collections import Counter as Counter
from hashlib import sha256 as sha256
from typing import Callable as Callable
from typing import Iterator as Iterator
from urllib.parse import unquote as unquote
from urllib.parse import urlparse as urlparse
from trippostcollect.artifacts.image_candidates import ImageCandidate as ImageCandidate
from trippostcollect.artifacts.image_candidates import source_asset_key_for_image as source_asset_key_for_image
from trippostcollect.artifacts.image_manifest import ImageManifestError as ImageManifestError
from trippostcollect.artifacts.image_manifest import parse_manifest as parse_manifest
from trippostcollect.artifacts.image_manifest import validate_post_manifest as validate_post_manifest
from trippostcollect.artifacts.image_materialization import Sha256DuplicateSource as Sha256DuplicateSource
from trippostcollect.artifacts.image_materialization import ValidatedImage as ValidatedImage
from trippostcollect.artifacts.image_materialization import promote_validated_image as promote_validated_image
from trippostcollect.artifacts.image_materialization import validate_image_file as validate_image_file
from trippostcollect.db.content import _existing_image_records as _existing_image_records
from trippostcollect.db.content import _normalize_persistence_items as _normalize_persistence_items
from trippostcollect.db.content import _prepare_image_rows as _prepare_image_rows
from trippostcollect.artifacts.image_persistence import replace_image_rows as replace_image_rows
from trippostcollect.application.policy import CrawlPolicyBlocked as CrawlPolicyBlocked
from trippostcollect.application.policy import clear_site_policy_state as clear_site_policy_state
from trippostcollect.application.policy import record_site_cooldown as record_site_cooldown
from trippostcollect.application.policy import site_request_guard as site_request_guard
from trippostcollect.application.failures import classify_attempt as classify_attempt
from trippostcollect.runtime.behavior import HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS as HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS
from trippostcollect.core.paths import FORMAL_MEDIA_PERSISTENCE_LOCK as FORMAL_MEDIA_PERSISTENCE_LOCK
from trippostcollect.core.paths import LOCAL_MEDIA_ROOT as LOCAL_MEDIA_ROOT
from trippostcollect.core.paths import ensure_dir as ensure_dir
from trippostcollect.core.paths import ensure_parent as ensure_parent
from trippostcollect.db.bootstrap import bootstrap_connection as bootstrap_connection
from trippostcollect.db.connection import connect_db as connect_db
from trippostcollect.platforms.registry import get_site as get_site
from trippostcollect.records.sanitization import sanitize_author_avatar_data as sanitize_author_avatar_data
from trippostcollect.records.topic_relevance import effective_source_keyword as effective_source_keyword
from trippostcollect.records.topic_relevance import topic_relevant_for_web_post as topic_relevant_for_web_post
from trippostcollect.scheduler.discovery import load_checkpoint as load_checkpoint
from trippostcollect.scheduler.discovery import save_checkpoint as save_checkpoint
from trippostcollect.scheduler.discovery import save_seen_candidates as save_seen_candidates
from trippostcollect.runtime.browser_runtime import XHS_WINDOW_SIZE_ENV as XHS_WINDOW_SIZE_ENV
from trippostcollect.runtime.browser_runtime import xhs_window_size_value as xhs_window_size_value

from trippostcollect.application import collection as _collection
from trippostcollect.application.collection import main as _collection_main
from trippostcollect.application.contracts import ExecutorPorts
from trippostcollect.application.reporting import item_type_from_path as item_type_from_path
from trippostcollect.application.reporting import truncate as truncate
from trippostcollect.application.reporting import extract_sample as extract_sample
from trippostcollect.application.reporting import summarize_jsonl as summarize_jsonl
from trippostcollect.application.reporting import summarize_output as summarize_output
from trippostcollect.application.reporting import summarize_output_with_progress as summarize_output_with_progress
from trippostcollect.application.reporting import write_json_with_progress as write_json_with_progress
from trippostcollect.application.reporting import terminal_summary_envelope as terminal_summary_envelope
from trippostcollect.db.content import ensure_web_schema as ensure_web_schema
from trippostcollect.application.collection import stable_douyin_search_id as stable_douyin_search_id
from trippostcollect.application.collection import effective_discovery_checkpoint_event as effective_discovery_checkpoint_event
from trippostcollect.application.collection import persist_discovery_checkpoint as persist_discovery_checkpoint
from trippostcollect.application.collection import load_pagination_evidence as load_pagination_evidence
from trippostcollect.application.collection import attach_skipped_candidate_evidence as attach_skipped_candidate_evidence
from trippostcollect.application.collection import collect_formal_records as collect_formal_records
from trippostcollect.artifacts.paths import resolve_media_root as resolve_media_root
from trippostcollect.artifacts.formal_images import _project_relative_evidence_path as _project_relative_evidence_path
from trippostcollect.artifacts.formal_images import _load_manifest_with_evidence as _load_manifest_with_evidence
from trippostcollect.artifacts.formal_images import _staging_root_for_manifest_entry as _staging_root_for_manifest_entry
from trippostcollect.artifacts.formal_images import rollback_newly_promoted_images as rollback_newly_promoted_images
from trippostcollect.artifacts.formal_images import formal_media_persistence_lock as formal_media_persistence_lock
from trippostcollect.artifacts.formal_images import _validated_manifest_rows_for_post as _validated_manifest_rows_for_post
from trippostcollect.artifacts.formal_images import materialize_formal_record_images as materialize_formal_record_images
from trippostcollect.db.content import find_existing_post as find_existing_post
from trippostcollect.db.content import upsert_web_post as upsert_web_post
from trippostcollect.db.content import FormalImportBeforeCommitError as FormalImportBeforeCommitError
from trippostcollect.db.content import commit_formal_import as commit_formal_import
from trippostcollect.db.content import import_valid_records as import_valid_records
from trippostcollect.application.collection import import_valid_records_with_media_rollback as import_valid_records_with_media_rollback
from trippostcollect.application.collection import effective_attempt_exit_code as effective_attempt_exit_code
from trippostcollect.application.reporting import collect_behavior_validation as collect_behavior_validation
from trippostcollect.application.reporting import latest_platform_result_counts as latest_platform_result_counts
from trippostcollect.application.reporting import write_markdown as write_markdown
from trippostcollect.application.collection import apply_formal_completion_gates as apply_formal_completion_gates
from trippostcollect.application.collection import formal_import_gate_met as formal_import_gate_met
from trippostcollect.application.collection import formal_image_promotion_allowed as formal_image_promotion_allowed
from trippostcollect.application.failures import runtime_blocker_stop_reason as runtime_blocker_stop_reason
from trippostcollect.application.failures import runtime_blocker_from_terminal_event as runtime_blocker_from_terminal_event
from trippostcollect.application.failures import runtime_blocker_from_pagination_evidence as runtime_blocker_from_pagination_evidence
from trippostcollect.application.failures import latest_runtime_blocker as latest_runtime_blocker
from trippostcollect.application.failures import apply_runtime_blocker as apply_runtime_blocker
from trippostcollect.db.content import FORMAL_SQLITE_BUSY_TIMEOUT_MS as FORMAL_SQLITE_BUSY_TIMEOUT_MS
from trippostcollect.application.reporting import SAMPLE_KEYS as SAMPLE_KEYS
from trippostcollect.application.reporting import AUTHOR_FIELD_MARKERS as AUTHOR_FIELD_MARKERS
from trippostcollect.application.reporting import IMAGE_SUFFIXES as IMAGE_SUFFIXES
from trippostcollect.application.reporting import VIDEO_SUFFIXES as VIDEO_SUFFIXES
from trippostcollect.application.collection import SKIPPED_CANDIDATE_EVENT_FIELDS as SKIPPED_CANDIDATE_EVENT_FIELDS
from trippostcollect.application.collection import DISCOVERY_RESEED_EVENT_FIELDS as DISCOVERY_RESEED_EVENT_FIELDS
from trippostcollect.application.collection import XHS_NETWORK_RECOVERY_WAIT_SECONDS as XHS_NETWORK_RECOVERY_WAIT_SECONDS
from trippostcollect.application.collection import XHS_NETWORK_RETRY_MIN_SECONDS as XHS_NETWORK_RETRY_MIN_SECONDS
from trippostcollect.application.collection import XHS_NETWORK_RETRY_MAX_SECONDS as XHS_NETWORK_RETRY_MAX_SECONDS
from trippostcollect.application.collection import XHS_OPERATOR_LOGIN_WAIT_SECONDS as XHS_OPERATOR_LOGIN_WAIT_SECONDS
from trippostcollect.application.failures import RUNTIME_BLOCKING_FAILURE_TYPES as RUNTIME_BLOCKING_FAILURE_TYPES
from trippostcollect.artifacts.formal_images import RASTER_IMAGE_SUFFIX_RE as RASTER_IMAGE_SUFFIX_RE
from trippostcollect.artifacts.formal_images import LEGACY_ZHIHU_TRANSFORM_SUFFIX_RE as LEGACY_ZHIHU_TRANSFORM_SUFFIX_RE

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
import os as os  # T09：小红书监督实现迁出后保留旧入口命名空间，既有测试按名 patch 或作旧基线执行环境。
import random as random
import re as re  # 同上。
import sqlite3
import subprocess as subprocess  # 保留现有 run_command 测试与诊断的进程接缝。
from datetime import datetime as datetime, timedelta as timedelta, timezone as timezone  # 同上。
from email.utils import parsedate_to_datetime as parsedate_to_datetime
from pathlib import Path
from typing import Any, MutableMapping as MutableMapping  # 同上。

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
    content_image_candidates,
    image_items_for_record,
)
from trippostcollect.artifacts.image_manifest import (
    ImageManifestEntry,
    manifest_sha256,
    write_manifest_atomic,
)
from trippostcollect.artifacts.image_materialization import (
    DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
    ImageMaterializationError,
    MaterializedImage,
    SUPPORTED_IMAGE_MIME_TYPES,
    safe_platform_post_id,
    write_staging_image,
)
from trippostcollect.artifacts.image_persistence import (
    ImagePersistenceError,
)
from trippostcollect.artifacts.evidence import write_evidence
from trippostcollect.artifacts.image_proxy import (
    RemoteImageFetchError,
    RemoteImagePreview,
    remote_image_failure_code,
    fetch_remote_image_bytes,
)
from trippostcollect.core.execution_state import FrozenExecutionState as FrozenExecutionState
from trippostcollect.runtime.human_flow import install_runtime_hints
from trippostcollect.runtime.behavior import (
    behavior_evidence_valid,
    load_behavior_evidence as load_behavior_evidence,
    run_page_behavior,
)
from trippostcollect.core.paths import (
    COOKIE_SNAPSHOT_FILENAME as COOKIE_SNAPSHOT_FILENAME,
    MEDIACRAWLER_RUNS_OUTPUT,
    PROJECT_ROOT,
)
from trippostcollect.core import paths, resources as resources
from trippostcollect.core.resources import verify_package_resources
from trippostcollect.runtime.browser_launcher import discover_cdp_browser_path
from trippostcollect.records.topic_relevance import (
    CONTENT_BODY_FIELDS as CONTENT_BODY_FIELDS,
    is_topic_relevant as is_topic_relevant,
    web_post_content_text as web_post_content_text,
    web_post_title as web_post_title,
)
from trippostcollect.xhs.leases import (
    SystemProcessInspector as SystemProcessInspector,
)
from trippostcollect.scheduler.discovery import (
    load_skipped_candidates,
)
from trippostcollect.runtime.browser_runtime import (
    browser_launch_environment,
    browser_runtime_args,
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
    run_main_with_operator_interrupt,
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



















def ensure_prerequisites() -> None:
    # T12：新 worker 只用根包与包内资源，不再以 fork 源码树存在为前置条件。
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


# T09：child 侧认证运行状态 reporter 迁入 trippostcollect.xhs.supervision；此处保留同名重导出。
from trippostcollect.xhs.supervision import (  # noqa: E402
    XHS_RUNTIME_STATUS_AUTH_KEY_RE as XHS_RUNTIME_STATUS_AUTH_KEY_RE,
    XHS_RUNTIME_STATUS_RUN_ID_ENV as XHS_RUNTIME_STATUS_RUN_ID_ENV,
    XhsSupervisorRuntimeReporter as XhsSupervisorRuntimeReporter,
    xhs_supervisor_runtime_reporter_from_context as xhs_supervisor_runtime_reporter_from_context,
)























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
    return _collection._run_platform_without_policy(
        platform_key, args, batch_dir, runtime_reporter=runtime_reporter,
        ports=ExecutorPorts(
            ensure_prerequisites=ensure_prerequisites,
            run_bilibili_article_search=run_bilibili_article_search,
        ),
    )




def run_platform(
    platform_key: str,
    args: argparse.Namespace,
    batch_dir: Path,
    *,
    runtime_reporter: XhsSupervisorRuntimeReporter | None = None,
) -> dict[str, Any]:
    return _collection.run_platform(
        platform_key, args, batch_dir, runtime_reporter=runtime_reporter,
        ports=ExecutorPorts(
            ensure_prerequisites=ensure_prerequisites,
            run_bilibili_article_search=run_bilibili_article_search,
        ),
    )




























def repair_runtime_stop_reason(records: list[dict[str, Any]], platforms: list[str]) -> str:
    return _repair_runtime_stop_reason(records, platforms, latest_runtime_blocker=latest_runtime_blocker)




def _run_main(
    args: argparse.Namespace,
    runtime_reporter: XhsSupervisorRuntimeReporter | None,
) -> int:
    return _collection._run_main(
        args, runtime_reporter,
        ports=ExecutorPorts(
            ensure_prerequisites=ensure_prerequisites,
            run_bilibili_article_search=run_bilibili_article_search,
        ),
    )


def main() -> int:
    return _collection_main(parse_args=parse_args, xhs_supervisor_runtime_reporter_from_context=xhs_supervisor_runtime_reporter_from_context, _run_main=_run_main)


if __name__ == "__main__":
    raise SystemExit(run_main_with_operator_interrupt(main))
