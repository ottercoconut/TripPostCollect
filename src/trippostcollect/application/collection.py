"""执行器正式采集编排：收集、完成门禁、图片晋升与内容提交顺序、发现提交。"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from datetime import timezone
from trippostcollect.application.contracts import ExecutorPorts
from trippostcollect.application.contracts import XhsRuntimeSupervisionError as XhsRuntimeSupervisionError
from trippostcollect.application.failures import RUNTIME_BLOCKING_FAILURE_TYPES
from trippostcollect.application.failures import apply_runtime_blocker
from trippostcollect.application.failures import classify_attempt
from trippostcollect.application.failures import latest_runtime_blocker
from trippostcollect.application.failures import runtime_blocker_from_pagination_evidence
from trippostcollect.application.failures import runtime_blocker_from_terminal_event
from trippostcollect.application.inputs import PLATFORMS as PLATFORMS
from trippostcollect.application.inputs import load_xhs_detail_urls as load_xhs_detail_urls
from trippostcollect.application.inputs import load_xhs_repair_target_ids as load_xhs_repair_target_ids
from trippostcollect.application.inputs import load_zhihu_detail_urls as load_zhihu_detail_urls
from trippostcollect.application.inputs import selected_platforms as selected_platforms
from trippostcollect.application.policy import CrawlPolicyBlocked
from trippostcollect.application.policy import clear_site_policy_state
from trippostcollect.application.policy import record_site_cooldown
from trippostcollect.application.policy import site_request_guard
from trippostcollect.application.repair import load_post_repair_fallbacks as load_post_repair_fallbacks
from trippostcollect.application.repair import load_post_repair_targets as load_post_repair_targets
from trippostcollect.application.repair import load_xhs_repair_report as load_xhs_repair_report
from trippostcollect.application.repair import post_repair_pagination_evidence as post_repair_pagination_evidence
from trippostcollect.application.repair import repair_candidate_execution_completed as repair_candidate_execution_completed
from trippostcollect.application.repair import repair_partial_child_execution_allowed as repair_partial_child_execution_allowed
from trippostcollect.application.repair import xhs_repair_pagination_evidence as xhs_repair_pagination_evidence
from trippostcollect.application.reporting import collect_behavior_validation
from trippostcollect.application.reporting import item_type_from_path
from trippostcollect.application.reporting import latest_platform_result_counts
from trippostcollect.application.reporting import summarize_output_with_progress
from trippostcollect.application.reporting import terminal_summary_envelope
from trippostcollect.application.reporting import write_json_with_progress
from trippostcollect.application.reporting import write_markdown
from trippostcollect.application.warmup import cookie_snapshot_path
from trippostcollect.artifacts.formal_images import formal_media_persistence_lock
from trippostcollect.artifacts.formal_images import materialize_formal_record_images
from trippostcollect.artifacts.formal_images import rollback_newly_promoted_images
from trippostcollect.application.contracts import ImagePersistenceError
from trippostcollect.artifacts.paths import resolve_media_root
from trippostcollect.core.paths import LOCAL_MEDIA_ROOT
from trippostcollect.core.paths import PROJECT_ROOT
from trippostcollect.core.paths import ensure_dir
from trippostcollect.core.paths import require_platform_session_migrated
from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.db.connection import connect_db
from trippostcollect.db.content import FORMAL_SQLITE_BUSY_TIMEOUT_MS
from trippostcollect.db.content import FormalImportBeforeCommitError
from trippostcollect.db.content import import_valid_records
from trippostcollect.db.discovery_read import load_existing_formal_identities as load_existing_formal_identities
from trippostcollect.platforms.registry import get_site
from trippostcollect.records.formal import formal_database_identities as formal_database_identities
from trippostcollect.records.formal import formal_record_identity as formal_record_identity
from trippostcollect.records.formal import merge_repair_fallback_metadata as merge_repair_fallback_metadata
from trippostcollect.records.formal import platform_from_path as platform_from_path
from trippostcollect.records.sanitization import sanitize_author_avatar_data
from trippostcollect.records.topic_relevance import effective_source_keyword
from trippostcollect.records.topic_relevance import topic_relevant_for_web_post
from trippostcollect.runtime.behavior import HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS
from trippostcollect.runtime.behavior import behavior_evidence_valid
from trippostcollect.runtime.behavior import load_behavior_evidence as load_behavior_evidence
from trippostcollect.runtime.browser_launcher import discover_cdp_browser_path
from trippostcollect.runtime.browser_runtime import XHS_WINDOW_SIZE_ENV
from trippostcollect.runtime.browser_runtime import xhs_window_size_value
from trippostcollect.runtime.cookies import export_profile_cookies as export_profile_cookies
from trippostcollect.runtime.cookies import public_cookie_export as public_cookie_export
from trippostcollect.runtime.cookies import required_cookie_names as required_cookie_names
from trippostcollect.runtime.helpers import _runtime_progress as _runtime_progress
from trippostcollect.runtime.helpers import _runtime_progress_if_due as _runtime_progress_if_due
from trippostcollect.runtime.helpers import utc_stamp as utc_stamp
from trippostcollect.runtime.process import NO_PROGRESS_WATCHDOG_SECONDS as NO_PROGRESS_WATCHDOG_SECONDS
from trippostcollect.runtime.process import PAGINATION_EVENT_FIELDS as PAGINATION_EVENT_FIELDS
from trippostcollect.runtime.process import RUNTIME_WATCHDOG_STOP_DETAILS as RUNTIME_WATCHDOG_STOP_DETAILS
from trippostcollect.runtime.process import append_runtime_watchdog_stop_event as append_runtime_watchdog_stop_event
from trippostcollect.runtime.process import run_command as run_command
from trippostcollect.runtime.process import runtime_watchdog_stop_detail as runtime_watchdog_stop_detail
from trippostcollect.runtime.process import skipped_command as skipped_command
from trippostcollect.scheduler.discovery import load_checkpoint
from trippostcollect.scheduler.discovery import save_checkpoint
from trippostcollect.scheduler.discovery import save_seen_candidates
from trippostcollect.xhs.operator_wait import operator_wait_diagnostics_path
from typing import Callable
from typing import TYPE_CHECKING
import argparse
import os
import sqlite3
import sys
import time

if TYPE_CHECKING:
    from trippostcollect.xhs.supervision import XhsSupervisorRuntimeReporter
    from trippostcollect.artifacts.image_materialization import MaterializedImage

import json
from pathlib import Path
from typing import Any

from trippostcollect.records import formal as _formal
from trippostcollect.artifacts.image_candidates import content_image_candidates

from trippostcollect.core.paths import PROJECT_ROOT as ROOT
from trippostcollect.runtime.browser_runtime import browser_runtime_args


def behavior_environment(evidence_path: Path, profile_name: str) -> dict[str, str]:
    return {
        "TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_ENABLED": "1",
        "TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE": profile_name,
        "TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE": str(evidence_path),
        "TRIPPOSTCOLLECT_PROJECT_SCRIPTS": str(ROOT / "scripts"),
        "TRIPPOSTCOLLECT_BROWSER_ARGS_JSON": json.dumps(browser_runtime_args()),
    }


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
XHS_OPERATOR_LOGIN_WAIT_SECONDS = 600


XHS_NETWORK_RECOVERY_WAIT_SECONDS = 600


XHS_NETWORK_RETRY_MIN_SECONDS = 2


XHS_NETWORK_RETRY_MAX_SECONDS = 30


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


def _run_platform_without_policy(
    platform_key: str,
    args: argparse.Namespace,
    batch_dir: Path,
    *,
    runtime_reporter: XhsSupervisorRuntimeReporter | None = None,
    ports: ExecutorPorts,
) -> dict[str, Any]:
    run_bilibili_article_search = ports.run_bilibili_article_search
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
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET": str(args.start_offset),
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR": str(args.start_cursor or ""),
                "TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES": str(
                    args.top_refresh_max_pages
                ),
                "TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED": (
                    "1" if args.discovery_source_exhausted else "0"
                ),
            }
        )
    if platform_key == "xhs":
        extra_env.update(
            {
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
        NO_PROGRESS_WATCHDOG_SECONDS,
        log_dir,
        extra_env=extra_env,
        progress_paths=progress_paths,
        runtime_reporter=runtime_reporter,
        network_diagnostics_path=(
            navigation_diagnostics_path if platform_key == "xhs" else None
        ),
        operator_wait_diagnostics_path=(
            operator_wait_diagnostics_path(behavior_evidence_path)
            if platform_key == "xhs"
            else None
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
            inactivity_timeout_seconds=NO_PROGRESS_WATCHDOG_SECONDS,
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
    ports: ExecutorPorts,
) -> dict[str, Any]:
    platform = PLATFORMS[platform_key]
    site = get_site(platform_key)
    log_dir = batch_dir / "logs" / platform_key
    save_path = batch_dir / platform_key / "data"
    policy_events: list[dict[str, Any]] = []
    shared_policy_disabled = platform_key == "xhs"
    policy_cleanup = clear_site_policy_state(site.key) if shared_policy_disabled else None
    try:
        if platform_key != "xhs":
            # T14 失败关闭：迁移中断留下 `.partial` 残留时不消耗站点预算、不启动浏览器或 worker。
            require_platform_session_migrated(platform_key)
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
                ports=ports,
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


def _run_main(
    args: argparse.Namespace,
    runtime_reporter: XhsSupervisorRuntimeReporter | None,
    *,
    ports: ExecutorPorts,
) -> int:
    ensure_prerequisites = ports.ensure_prerequisites
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
                ports=ports,
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


def main(*, parse_args, xhs_supervisor_runtime_reporter_from_context, _run_main) -> int:
    args = parse_args()
    runtime_reporter = xhs_supervisor_runtime_reporter_from_context(args)
    return _run_main(args, runtime_reporter)
