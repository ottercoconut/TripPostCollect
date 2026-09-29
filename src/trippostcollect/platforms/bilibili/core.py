"""B站图文流程与正文图片编排。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trippostcollect.application.contracts import (
    BilibiliImagePorts,
    BilibiliSearchPorts,
    ImageManifestRecord as ImageManifestEntry,
    PLATFORMS,
)
from trippostcollect.core.execution_state import FrozenExecutionState
from trippostcollect.core.paths import ensure_dir
from trippostcollect.records.formal import CHINA_TZ
from trippostcollect.records.sanitization import sanitize_author_avatar_data
from trippostcollect.records.topic_relevance import topic_relevant_for_web_post
from .models import BilibiliArticleDetailError, BilibiliFollowerFetchError, BilibiliRuntimeBlocked
from .parser import hydrate_bilibili_article_record, normalize_bilibili_article_record
from .client import (
    BILIBILI_DETAIL_MAX_ATTEMPTS,
    BILIBILI_DETAIL_RETRY_DELAY_SECONDS,
    fetch_bilibili_wbi_keys,
    fetch_bilibili_article_page,
    fetch_bilibili_article_detail_with_retry,
    fetch_bilibili_follower_count,
)


BILIBILI_DETAIL_PACING_SECONDS = (1.5, 3.0)


BILIBILI_IMAGE_MAX_ATTEMPTS = 3


BILIBILI_IMAGE_RETRY_DELAY_SECONDS = (1.0, 2.0)


def download_bilibili_record_images(
    record: dict[str, Any],
    *,
    cookie_header: str,
    platform_data_root: Path,
    fetcher: Any | None = None,
    sleep_fn: Any | None = None,
    log_fn: Any | None = None,
    max_attempts: int = BILIBILI_IMAGE_MAX_ATTEMPTS,
    ports: BilibiliImagePorts,
) -> list[ImageManifestEntry]:
    ImageManifestEntry = ports.ImageManifestEntry
    ImageMaterializationError = ports.ImageMaterializationError
    RemoteImageFetchError = ports.RemoteImageFetchError
    content_image_candidates = ports.content_image_candidates
    fetch_bilibili_image_bytes = ports.fetch_bilibili_image_bytes
    is_retryable_image_error = ports.is_retryable_image_error
    remote_image_failure_code = ports.remote_image_failure_code
    safe_platform_post_id = ports.safe_platform_post_id
    write_staging_image = ports.write_staging_image
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


def run_bilibili_article_search(
    args: argparse.Namespace,
    batch_dir: Path,
    *,
    ports: BilibiliSearchPorts,
) -> dict[str, Any]:
    load_behavior_evidence = ports.load_behavior_evidence
    behavior_evidence_valid = ports.behavior_evidence_valid
    load_existing_formal_identities = ports.load_existing_formal_identities
    load_skipped_candidates = ports.load_skipped_candidates
    connect_database = ports.connect_database
    run_bilibili_behavior_session = ports.run_bilibili_behavior_session
    download_bilibili_record_images = ports.download_bilibili_record_images
    content_image_candidates = ports.content_image_candidates
    write_manifest_atomic = ports.write_manifest_atomic
    manifest_sha256 = ports.manifest_sha256
    is_retryable_image_error = ports.is_retryable_image_error
    is_runtime_blocking_image_error = ports.is_runtime_blocking_image_error
    formal_database_identities = ports.formal_database_identities
    summarize_output = ports.summarize_output
    tail = ports.tail
    validate_formal_record = ports.validate_formal_record
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
            with connect_database(Path(args.db).expanduser()) as conn:
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
