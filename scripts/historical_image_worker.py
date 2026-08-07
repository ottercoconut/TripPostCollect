#!/usr/bin/env python3
"""Run the frozen historical image campaign as a detached, resumable background worker."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Sequence

from trippostcollect.artifacts.historical_image_materialization import (
    HISTORICAL_PLATFORM_ORDER,
    projection_inventory,
    sha256_file,
)
from trippostcollect.core.paths import (
    DATA_ROOT,
    DEFAULT_DB,
    IMAGE_MATERIALIZATION_RUNTIME,
    MEDIACRAWLER_DIR,
    PROJECT_ROOT,
)


CAMPAIGN_ID = "historical-images-20260807-v1"
RUNTIME_ROOT = IMAGE_MATERIALIZATION_RUNTIME / CAMPAIGN_ID / "background-worker"
STATE_PATH = RUNTIME_ROOT / "state.json"
PID_PATH = RUNTIME_ROOT / "worker.pid"
LOCK_PATH = RUNTIME_ROOT / "worker.lock"
STOP_PATH = RUNTIME_ROOT / "stop.requested"
LOG_PATH = RUNTIME_ROOT / "worker.log"
DEFERRED_PATH = RUNTIME_ROOT / "deferred-posts.json"
PLATFORM_ORDER = HISTORICAL_PLATFORM_ORDER
STAGE_BY_PLATFORM = {
    "xhs": "h03",
    "bilibili": "h04",
    "weibo": "h05",
    "zhihu": "h06",
    "douyin": "h07",
}
EXPANDED_BATCH_POSTS = {
    "xhs": 25,
    "bilibili": 50,
    "weibo": 50,
    "zhihu": 20,
    "douyin": 20,
}
P95_IMAGE_BYTES = {
    "xhs": 540_648,
    "bilibili": 157_884,
    "weibo": 497_055,
    "zhihu": 720_832,
    "douyin": 961_223,
}
SAFETY_MARGIN_BYTES = 10 * 1024 * 1024 * 1024
FROZEN_MAX_STAGING_PEAK_BYTES = 355_370_176
MAX_IMAGE_ATTEMPTS = 3
MAX_TRANSIENT_BATCH_ATTEMPTS = 3
TRANSIENT_RETRY_BACKOFF_SECONDS = (30, 120)
SOURCE_FILES = (
    PROJECT_ROOT / "scripts" / "historical_image_worker.py",
    PROJECT_ROOT / "scripts" / "historical_platform_images.py",
    PROJECT_ROOT / "scripts" / "mediacrawler_crawl.py",
    PROJECT_ROOT / "scripts" / "refresh_douyin_image_urls.py",
    PROJECT_ROOT / "scripts" / "validate_historical_images.py",
    PROJECT_ROOT / "scripts" / "gc_local_images.py",
    PROJECT_ROOT / "src" / "trippostcollect" / "artifacts" / "historical_image_materialization.py",
    PROJECT_ROOT / "src" / "trippostcollect" / "artifacts" / "image_candidates.py",
    PROJECT_ROOT / "src" / "trippostcollect" / "artifacts" / "image_materialization.py",
    PROJECT_ROOT / "src" / "trippostcollect" / "artifacts" / "image_proxy.py",
    PROJECT_ROOT / "docs" / "plans" / "2026-08-07-historical-image-h00-input-freeze.json",
    PROJECT_ROOT / "docs" / "plans" / "2026-08-07-historical-image-h02-execution-gate.json",
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "start", "status", "stop"))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--runtime-root", default=str(RUNTIME_ROOT), help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _paths(runtime_root: Path) -> dict[str, Path]:
    return {
        "root": runtime_root,
        "state": runtime_root / STATE_PATH.name,
        "pid": runtime_root / PID_PATH.name,
        "lock": runtime_root / LOCK_PATH.name,
        "stop": runtime_root / STOP_PATH.name,
        "log": runtime_root / LOG_PATH.name,
        "deferred": runtime_root / DEFERRED_PATH.name,
    }


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
            file_handle.write(text)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_iso()
    _write_text_atomic(
        path,
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _read_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_pid(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _git_head(worktree: Path = PROJECT_ROOT) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("cannot resolve the project git commit")
    return result.stdout.strip()


def _source_digests() -> dict[str, str]:
    return {
        path.relative_to(PROJECT_ROOT).as_posix(): sha256_file(path)
        for path in SOURCE_FILES
    }


def _inventory(db_path: Path) -> dict[str, dict[str, int]]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        return projection_inventory(conn)


def _capacity_gate(db_path: Path, inventory: dict[str, dict[str, int]]) -> dict[str, Any]:
    remaining_estimate = sum(
        max(0, row["local_gap"]) * P95_IMAGE_BYTES[platform_key]
        for platform_key, row in inventory.items()
    )
    max_staging = FROZEN_MAX_STAGING_PEAK_BYTES
    required = 2 * (remaining_estimate + max_staging) + SAFETY_MARGIN_BYTES + db_path.stat().st_size
    actual = shutil.disk_usage(DATA_ROOT).free
    return {
        "remaining_estimate_bytes": remaining_estimate,
        "max_staging_estimate_bytes": max_staging,
        "required_free_bytes": required,
        "actual_free_bytes": actual,
        "headroom_bytes": actual - required,
        "ok": actual >= required,
    }


def _run_command(command: list[str], *, log_handle: Any) -> int:
    log_handle.write(f"[{utc_iso()}] command={json.dumps(command, ensure_ascii=False)}\n")
    log_handle.flush()
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_handle.write(f"[{utc_iso()}] returncode={result.returncode}\n")
    log_handle.flush()
    return int(result.returncode)


def _classify_failure(report: dict[str, Any]) -> str:
    text = f"{report.get('error_type', '')} {report.get('error', '')}".lower()
    auth_markers = (
        "login",
        "cookie snapshot",
        "cookie",
        "401",
        "403",
        "verify",
        "captcha",
        "风控",
        "验证码",
    )
    return "auth_required" if any(marker in text for marker in auth_markers) else "failed"


def _failed_posts(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = report.get("downloaded_posts")
    if not isinstance(rows, list):
        return []
    return [
        row
        for row in rows
        if isinstance(row, dict) and int(row.get("failed_images") or 0) > 0
    ]


def _failure_decision(report: dict[str, Any]) -> dict[str, Any]:
    classification = _classify_failure(report)
    if classification == "auth_required":
        return {"action": "stop", "status": classification, "reason": "authentication"}
    failed_posts = _failed_posts(report)
    failure_codes = sorted(
        {
            str(code)
            for post in failed_posts
            for code in (post.get("failure_codes") or [])
            if code
        }
    )
    failed_attempts = [
        int(failure.get("attempts") or 0)
        for post in failed_posts
        for failure in (post.get("failures") or [])
        if isinstance(failure, dict)
    ]
    attempts_used = max(failed_attempts, default=0)
    if failed_posts:
        retryable_only = bool(failure_codes) and set(failure_codes) == {
            "image_download_retryable"
        }
        if retryable_only and attempts_used < MAX_IMAGE_ATTEMPTS:
            return {
                "action": "retry",
                "status": "retry_wait",
                "reason": "retryable_image_failure",
                "failure_codes": failure_codes,
                "attempts_used": attempts_used,
                "failed_posts": failed_posts,
            }
        return {
            "action": "defer",
            "status": "review_required",
            "reason": (
                "image_retry_exhausted" if retryable_only else "terminal_image_failure"
            ),
            "failure_codes": failure_codes,
            "attempts_used": attempts_used,
            "failed_posts": failed_posts,
        }
    retryable_types = {
        "connectionerror",
        "oserror",
        "remoteimagefetcherror",
        "timeouterror",
        "urlerror",
    }
    error_type = str(report.get("error_type") or "").lower()
    if error_type in retryable_types:
        return {
            "action": "retry",
            "status": "retry_wait",
            "reason": "transient_batch_failure",
            "failure_codes": failure_codes,
            "attempts_used": 0,
            "failed_posts": [],
        }
    return {
        "action": "stop",
        "status": "failed",
        "reason": "non_retryable_batch_failure",
        "failure_codes": failure_codes,
        "attempts_used": attempts_used,
        "failed_posts": failed_posts,
    }


def _database_unchanged_after_failure(report: dict[str, Any]) -> bool:
    before = str(report.get("database_sha256_before") or "")
    final = str(report.get("database_sha256_final") or "")
    return bool(before and final and before == final)


def _deferred_registry(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    value = state.setdefault("deferred_posts", {})
    if isinstance(value, dict):
        return value
    state["deferred_posts"] = {}
    return state["deferred_posts"]


def _sync_deferred_registry(path: Path, state: dict[str, Any]) -> None:
    registry = _deferred_registry(state)
    platform_post_ids = {
        platform: sorted(str(post_id) for post_id in posts)
        for platform, posts in registry.items()
        if isinstance(posts, dict) and posts
    }
    _write_text_atomic(
        path,
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": CAMPAIGN_ID,
                "updated_at": utc_iso(),
                "platform_post_ids": platform_post_ids,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    state["deferred_post_count"] = sum(
        len(posts) for posts in registry.values() if isinstance(posts, dict)
    )


def _record_deferred_posts(
    state: dict[str, Any],
    *,
    platform_key: str,
    report_path: Path,
    decision: dict[str, Any],
) -> None:
    registry = _deferred_registry(state)
    platform_registry = registry.setdefault(platform_key, {})
    now = utc_iso()
    for post in decision.get("failed_posts") or []:
        post_id = str(post.get("platform_post_id") or "")
        if not post_id:
            continue
        previous = platform_registry.get(post_id) or {}
        event = {
            "recorded_at": now,
            "report": str(report_path),
            "reason": decision["reason"],
            "failure_codes": decision.get("failure_codes") or [],
            "attempts_used": decision.get("attempts_used") or 0,
            "failures": post.get("failures") or [],
        }
        history = list(previous.get("history") or [])
        history.append(event)
        platform_registry[post_id] = {
            "platform_post_id": post_id,
            "status": "review_required",
            "first_recorded_at": previous.get("first_recorded_at") or now,
            "last_recorded_at": now,
            "reason": decision["reason"],
            "failure_codes": decision.get("failure_codes") or [],
            "history": history,
        }


def _retry_delay(failed_run_count: int) -> int:
    index = min(max(0, failed_run_count - 1), len(TRANSIENT_RETRY_BACKOFF_SECONDS) - 1)
    return TRANSIENT_RETRY_BACKOFF_SECONDS[index]


def _wait_for_retry(
    delay_seconds: int,
    *,
    paths: dict[str, Path],
    state: dict[str, Any],
) -> bool:
    deadline = time.monotonic() + delay_seconds
    while time.monotonic() < deadline:
        if paths["stop"].exists():
            state.update({"status": "stopped", "stopped_at": utc_iso(), "error": None})
            _write_state(paths["state"], state)
            return False
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return True


def _login_preflight(
    platform_key: str,
    *,
    runtime_root: Path,
    log_handle: Any,
) -> bool:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "login_warmup.py"),
        "--targets",
        platform_key,
        "--timeout-seconds",
        "1",
        "--output-dir",
        str(runtime_root / "login-preflight"),
    ]
    return _run_command(command, log_handle=log_handle) == 0


def _active_standalone_batches() -> list[int]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return []
    matches: list[int] = []
    for line in result.stdout.splitlines():
        if "scripts/historical_platform_images.py" not in line:
            continue
        try:
            matches.append(int(line.strip().split(maxsplit=1)[0]))
        except (IndexError, ValueError):
            continue
    return matches


def _validate_platform(
    platform_key: str,
    *,
    db_path: Path,
    runtime_root: Path,
    log_handle: Any,
) -> tuple[bool, Path]:
    destination = runtime_root / "validation" / platform_key
    report_path = destination / "report.json"
    markdown_path = destination / "report.md"
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "validate_historical_images.py"),
        "--platform",
        platform_key,
        "--db",
        str(db_path),
        "--report",
        str(report_path),
        "--markdown-report",
        str(markdown_path),
    ]
    return _run_command(command, log_handle=log_handle) == 0, report_path


def _final_validation(
    *,
    db_path: Path,
    runtime_root: Path,
    log_handle: Any,
) -> tuple[bool, dict[str, str]]:
    destination = runtime_root / "validation" / "all"
    report_path = destination / "report.json"
    markdown_path = destination / "report.md"
    validation = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "validate_historical_images.py"),
        "--platform",
        "all",
        "--db",
        str(db_path),
        "--report",
        str(report_path),
        "--markdown-report",
        str(markdown_path),
    ]
    if _run_command(validation, log_handle=log_handle) != 0:
        return False, {"validation_report": str(report_path)}
    gc_report = destination / "gc-dry-run.json"
    gc_command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "gc_local_images.py"),
        "--db",
        str(db_path),
        "--campaign-id",
        CAMPAIGN_ID,
        "--report",
        str(gc_report),
    ]
    return _run_command(gc_command, log_handle=log_handle) == 0, {
        "validation_report": str(report_path),
        "validation_markdown": str(markdown_path),
        "gc_dry_run_report": str(gc_report),
    }


def _initial_state(db_path: Path, source_digests: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "campaign_id": CAMPAIGN_ID,
        "status": "starting",
        "historical_data_complete": False,
        "started_at": utc_iso(),
        "database": str(db_path),
        "git_commit": _git_head(),
        "mediacrawler_commit": _git_head(MEDIACRAWLER_DIR),
        "source_digests": source_digests,
        "completed_platforms": [],
        "successful_batches": 0,
        "retry_policy": {
            "max_attempts_per_image": MAX_IMAGE_ATTEMPTS,
            "max_transient_batch_attempts": MAX_TRANSIENT_BATCH_ATTEMPTS,
            "backoff_seconds": list(TRANSIENT_RETRY_BACKOFF_SECONDS),
        },
        "retry_events": [],
        "active_retry": None,
        "deferred_posts": {},
        "last_report": None,
        "preflight_completed_for": None,
        "error": None,
    }


def run_worker(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser().resolve(strict=True)
    runtime_root = Path(args.runtime_root).expanduser().resolve()
    paths = _paths(runtime_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    lock_handle = paths["lock"].open("a+")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "already_running", "state": str(paths["state"])}))
            return 3
        _write_text_atomic(paths["pid"], f"{os.getpid()}\n")
        if paths["stop"].exists():
            paths["stop"].unlink()
        source_digests = _source_digests()
        previous = _read_state(paths["state"])
        state = _initial_state(db_path, source_digests)
        state["deferred_registry"] = str(paths["deferred"])
        if previous.get("campaign_id") == CAMPAIGN_ID:
            state["resumed_from"] = previous.get("updated_at")
            state["successful_batches"] = int(previous.get("successful_batches") or 0)
            state["completed_platforms"] = list(previous.get("completed_platforms") or [])
            state["retry_events"] = list(previous.get("retry_events") or [])
            same_code = (
                previous.get("git_commit") == state["git_commit"]
                and previous.get("source_digests") == source_digests
            )
            state["active_retry"] = previous.get("active_retry") if same_code else None
            state["deferred_posts"] = dict(previous.get("deferred_posts") or {})
        _sync_deferred_registry(paths["deferred"], state)
        _write_state(paths["state"], state)

        with paths["log"].open("a", encoding="utf-8", buffering=1) as log_handle:
            log_handle.write(f"[{utc_iso()}] worker_start pid={os.getpid()} commit={state['git_commit']}\n")
            while True:
                if paths["stop"].exists():
                    state.update({"status": "stopped", "stopped_at": utc_iso(), "error": None})
                    _write_state(paths["state"], state)
                    return 0
                if (
                    _source_digests() != source_digests
                    or _git_head() != state["git_commit"]
                    or _git_head(MEDIACRAWLER_DIR) != state["mediacrawler_commit"]
                ):
                    state.update(
                        {
                            "status": "code_drift",
                            "error": "worker source or git commit changed after start",
                        }
                    )
                    _write_state(paths["state"], state)
                    return 2

                inventory = _inventory(db_path)
                capacity = _capacity_gate(db_path, inventory)
                state["inventory"] = inventory
                state["capacity"] = capacity
                if not capacity["ok"]:
                    state.update({"status": "capacity_blocked", "error": "free disk capacity gate failed"})
                    _write_state(paths["state"], state)
                    return 2

                platform_key = next(
                    (
                        platform
                        for platform in PLATFORM_ORDER
                        if inventory[platform]["local_gap"] > 0
                    ),
                    None,
                )
                for completed in PLATFORM_ORDER:
                    if inventory[completed]["local_gap"] != 0 or completed in state["completed_platforms"]:
                        continue
                    ok, report_path = _validate_platform(
                        completed,
                        db_path=db_path,
                        runtime_root=runtime_root,
                        log_handle=log_handle,
                    )
                    state["last_validation_report"] = str(report_path)
                    if not ok:
                        state.update(
                            {
                                "status": "validation_failed",
                                "current_platform": completed,
                                "error": f"platform validation failed: {completed}",
                            }
                        )
                        _write_state(paths["state"], state)
                        return 2
                    state["completed_platforms"].append(completed)
                    _write_state(paths["state"], state)

                if platform_key is None:
                    ok, reports = _final_validation(
                        db_path=db_path,
                        runtime_root=runtime_root,
                        log_handle=log_handle,
                    )
                    state.update(reports)
                    if not ok:
                        state.update({"status": "validation_failed", "error": "H-08 validation failed"})
                        _write_state(paths["state"], state)
                        return 2
                    state.update(
                        {
                            "status": "completed",
                            "historical_data_complete": True,
                            "finished_at": utc_iso(),
                            "current_platform": None,
                            "error": None,
                        }
                    )
                    _write_state(paths["state"], state)
                    return 0

                platform_inventory = inventory[platform_key]
                if state.get("preflight_completed_for") != platform_key:
                    state.update(
                        {
                            "status": "login_preflight",
                            "current_platform": platform_key,
                            "current_stage": STAGE_BY_PLATFORM[platform_key].upper(),
                            "error": None,
                        }
                    )
                    _write_state(paths["state"], state)
                    if not _login_preflight(
                        platform_key,
                        runtime_root=runtime_root,
                        log_handle=log_handle,
                    ):
                        state.update(
                            {
                                "status": "auth_required",
                                "error": (
                                    f"{platform_key} login preflight failed; run "
                                    f"scripts/login_warmup.py --targets {platform_key}, then start again"
                                ),
                            }
                        )
                        _write_state(paths["state"], state)
                        return 2
                    state["preflight_completed_for"] = platform_key
                    _write_state(paths["state"], state)
                fixed_sample = platform_inventory["existing_local_rows"] == 0
                batch_size = 10 if fixed_sample else EXPANDED_BATCH_POSTS[platform_key]
                active_retry = state.get("active_retry")
                if not isinstance(active_retry, dict) or active_retry.get("platform") != platform_key:
                    active_retry = None
                max_image_attempts = int(
                    (active_retry or {}).get("remaining_image_attempts")
                    or MAX_IMAGE_ATTEMPTS
                )
                stamp = utc_stamp()
                stage = STAGE_BY_PLATFORM[platform_key]
                retry_suffix = (
                    f"-retry{int(active_retry.get('failed_run_count') or 0) + 1}"
                    if active_retry
                    else ""
                )
                label = (
                    f"background-{'fixed10' if fixed_sample else 'expanded'}"
                    f"{retry_suffix}-{stamp}"
                )
                report_path = (
                    IMAGE_MATERIALIZATION_RUNTIME
                    / CAMPAIGN_ID
                    / stage
                    / label
                    / "report.json"
                )
                backup_dir = (
                    DATA_ROOT
                    / "backups"
                    / "historical_images"
                    / CAMPAIGN_ID
                    / stage
                    / label
                )
                state.update(
                    {
                        "status": "running",
                        "current_platform": platform_key,
                        "current_stage": stage.upper(),
                        "current_batch_size": batch_size,
                        "current_max_image_attempts": max_image_attempts,
                        "current_report": str(report_path),
                        "error": None,
                    }
                )
                _write_state(paths["state"], state)
                command = [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "historical_platform_images.py"),
                    "--platform",
                    platform_key,
                    "--batch-size",
                    str(batch_size),
                    "--report",
                    str(report_path),
                    "--backup-dir",
                    str(backup_dir),
                    "--deferred-posts-file",
                    str(paths["deferred"]),
                    "--max-image-attempts",
                    str(max_image_attempts),
                    "--apply",
                ]
                returncode = _run_command(command, log_handle=log_handle)
                report = _read_state(report_path)
                state["last_report"] = str(report_path)
                if returncode != 0 or report.get("status") != "completed":
                    decision = _failure_decision(report)
                    state["last_failure_decision"] = decision["reason"]
                    state["error"] = str(
                        report.get("error") or f"batch exited {returncode}"
                    )
                    if decision["action"] in {"retry", "defer"} and not _database_unchanged_after_failure(report):
                        state.update(
                            {
                                "status": "failed",
                                "error": "failed batch database SHA is absent or changed; automatic recovery refused",
                                "failed_at": utc_iso(),
                            }
                        )
                        _write_state(paths["state"], state)
                        return 2
                    if decision["action"] == "defer":
                        _record_deferred_posts(
                            state,
                            platform_key=platform_key,
                            report_path=report_path,
                            decision=decision,
                        )
                        state["active_retry"] = None
                        state["retry_events"].append(
                            {
                                "event": "post_deferred",
                                "recorded_at": utc_iso(),
                                "platform": platform_key,
                                "report": str(report_path),
                                "reason": decision["reason"],
                                "failure_codes": decision.get("failure_codes") or [],
                                "platform_post_ids": [
                                    str(post.get("platform_post_id") or "")
                                    for post in decision.get("failed_posts") or []
                                ],
                            }
                        )
                        _sync_deferred_registry(paths["deferred"], state)
                        state.update(
                            {
                                "status": "running_with_deferred",
                                "last_deferred_at": utc_iso(),
                            }
                        )
                        _write_state(paths["state"], state)
                        continue
                    if decision["action"] == "retry":
                        planned_ids = list(report.get("planned_platform_post_ids") or [])
                        previous_retry = active_retry or {}
                        if previous_retry and list(previous_retry.get("planned_platform_post_ids") or []) != planned_ids:
                            state.update(
                                {
                                    "status": "failed",
                                    "error": "retry plan identity changed before the safe frontier advanced",
                                    "failed_at": utc_iso(),
                                }
                            )
                            _write_state(paths["state"], state)
                            return 2
                        failed_run_count = int(previous_retry.get("failed_run_count") or 0) + 1
                        attempts_consumed = int(
                            previous_retry.get("image_attempts_consumed") or 0
                        ) + int(decision.get("attempts_used") or 0)
                        remaining_image_attempts = MAX_IMAGE_ATTEMPTS - attempts_consumed
                        if (
                            decision.get("failed_posts")
                            and remaining_image_attempts <= 0
                        ):
                            decision = {
                                **decision,
                                "action": "defer",
                                "reason": "image_retry_exhausted",
                                "attempts_used": attempts_consumed,
                            }
                            _record_deferred_posts(
                                state,
                                platform_key=platform_key,
                                report_path=report_path,
                                decision=decision,
                            )
                            state["active_retry"] = None
                            state["retry_events"].append(
                                {
                                    "event": "post_deferred",
                                    "recorded_at": utc_iso(),
                                    "platform": platform_key,
                                    "report": str(report_path),
                                    "reason": decision["reason"],
                                    "platform_post_ids": planned_ids,
                                }
                            )
                            _sync_deferred_registry(paths["deferred"], state)
                            _write_state(paths["state"], state)
                            continue
                        if failed_run_count >= MAX_TRANSIENT_BATCH_ATTEMPTS:
                            state.update(
                                {
                                    "status": "retry_exhausted",
                                    "error": (
                                        f"transient batch retry exhausted after {failed_run_count} runs: "
                                        f"{state['error']}"
                                    ),
                                    "failed_at": utc_iso(),
                                    "active_retry": None,
                                }
                            )
                            _write_state(paths["state"], state)
                            return 2
                        delay_seconds = _retry_delay(failed_run_count)
                        next_retry_at = (
                            datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
                        ).isoformat(timespec="seconds")
                        retry_state = {
                            "platform": platform_key,
                            "planned_platform_post_ids": planned_ids,
                            "failed_run_count": failed_run_count,
                            "image_attempts_consumed": attempts_consumed,
                            "remaining_image_attempts": max(
                                1, remaining_image_attempts
                            ),
                            "last_report": str(report_path),
                            "last_reason": decision["reason"],
                            "next_retry_at": next_retry_at,
                        }
                        state["active_retry"] = retry_state
                        state["retry_events"].append(
                            {
                                "event": "retry_scheduled",
                                "recorded_at": utc_iso(),
                                "platform": platform_key,
                                "report": str(report_path),
                                "failed_run_count": failed_run_count,
                                "delay_seconds": delay_seconds,
                                "next_retry_at": next_retry_at,
                                "reason": decision["reason"],
                            }
                        )
                        state.update(
                            {
                                "status": "retry_wait",
                                "next_retry_at": next_retry_at,
                            }
                        )
                        _write_state(paths["state"], state)
                        if not _wait_for_retry(
                            delay_seconds, paths=paths, state=state
                        ):
                            return 0
                        continue
                    state.update(
                        {
                            "status": decision["status"],
                            "failed_at": utc_iso(),
                            "active_retry": None,
                        }
                    )
                    _write_state(paths["state"], state)
                    return 2
                if (
                    int(report.get("planned_posts") or 0) == 0
                    and platform_inventory["local_gap"] > 0
                    and _deferred_registry(state).get(platform_key)
                ):
                    state.update(
                        {
                            "status": "review_required",
                            "historical_data_complete": False,
                            "active_retry": None,
                            "error": (
                                f"{platform_key} has no runnable posts while deferred image failures remain"
                            ),
                            "review_required_at": utc_iso(),
                        }
                    )
                    _write_state(paths["state"], state)
                    return 2
                if active_retry:
                    state["retry_events"].append(
                        {
                            "event": "retry_succeeded",
                            "recorded_at": utc_iso(),
                            "platform": platform_key,
                            "report": str(report_path),
                            "failed_run_count": active_retry.get("failed_run_count"),
                        }
                    )
                state["active_retry"] = None
                state.pop("next_retry_at", None)
                state["successful_batches"] = int(state["successful_batches"]) + 1
                state["last_completed_at"] = utc_iso()
                _write_state(paths["state"], state)
    except Exception as exc:
        state = _read_state(paths["state"])
        state.update(
            {
                "schema_version": 2,
                "campaign_id": CAMPAIGN_ID,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "failed_at": utc_iso(),
            }
        )
        _write_state(paths["state"], state)
        return 2
    finally:
        try:
            if _read_pid(paths["pid"]) == os.getpid():
                paths["pid"].unlink(missing_ok=True)
        finally:
            lock_handle.close()


def start_worker(args: argparse.Namespace) -> int:
    runtime_root = Path(args.runtime_root).expanduser().resolve()
    paths = _paths(runtime_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    existing_pid = _read_pid(paths["pid"])
    if _pid_alive(existing_pid):
        print(json.dumps({"status": "already_running", "pid": existing_pid, "state": str(paths["state"])}))
        return 0
    standalone = _active_standalone_batches()
    if standalone:
        print(
            json.dumps(
                {
                    "status": "start_blocked",
                    "reason": "standalone historical image batch is still running",
                    "pids": standalone,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    paths["stop"].unlink(missing_ok=True)
    log_handle = paths["log"].open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "run",
                "--db",
                str(Path(args.db).expanduser().resolve()),
                "--runtime-root",
                str(runtime_root),
            ],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_handle.close()
    for _ in range(50):
        time.sleep(0.1)
        pid = _read_pid(paths["pid"])
        if _pid_alive(pid):
            print(
                json.dumps(
                    {
                        "status": "started",
                        "pid": pid,
                        "launcher_pid": process.pid,
                        "state": str(paths["state"]),
                        "log": str(paths["log"]),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if process.poll() is not None:
            break
    print(json.dumps({"status": "start_failed", "log": str(paths["log"])}))
    return 2


def status_worker(args: argparse.Namespace) -> int:
    paths = _paths(Path(args.runtime_root).expanduser().resolve())
    state = _read_state(paths["state"])
    pid = _read_pid(paths["pid"])
    payload = {
        **state,
        "process_alive": _pid_alive(pid),
        "pid": pid,
        "state_path": str(paths["state"]),
        "log_path": str(paths["log"]),
        "stop_requested": paths["stop"].exists(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def stop_worker(args: argparse.Namespace) -> int:
    paths = _paths(Path(args.runtime_root).expanduser().resolve())
    paths["root"].mkdir(parents=True, exist_ok=True)
    _write_text_atomic(paths["stop"], f"{utc_iso()}\n")
    pid = _read_pid(paths["pid"])
    print(
        json.dumps(
            {
                "status": "stop_requested",
                "pid": pid,
                "process_alive": _pid_alive(pid),
                "behavior": "the worker stops after the current atomic batch",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.action == "run":
        return run_worker(args)
    if args.action == "start":
        return start_worker(args)
    if args.action == "status":
        return status_worker(args)
    return stop_worker(args)


if __name__ == "__main__":
    raise SystemExit(main())
