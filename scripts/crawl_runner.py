#!/usr/bin/env python3
"""Deterministic long-running crawl runner controlled by config and SQLite state."""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import re
import signal
import sqlite3
import subprocess
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.db.connection import connect_db
from trippostcollect.artifacts.image_completion import (
    verify_image_artifacts,
    verify_image_persistence,
)
from trippostcollect.scheduler.discovery import (
    clear_campaign,
    load_checkpoint,
    query_fingerprint,
    update_campaign,
)
from execution_state import FrozenExecutionState
from trippostcollect.application.failures import classify_attempt, extract_stdout_json
from trippostcollect.runtime.process import (
    PROCESS_CLEANUP_GRACE_SECONDS,
    PROCESS_FINAL_REAP_SECONDS,
    decode_text,
    drain_exited_process,
    kill_recorded_child_process_groups,
    operator_interrupt_error,
    terminate_managed_process,
    write_command_logs,
)
from trippostcollect.xhs.leases import DeferredTerminationSignals
from trippostcollect.core.paths import (
    CRAWL_EXECUTION_STATE_ROOT,
    CRAWL_RUNNER_RUNTIME,
    DEFAULT_CONFIG,
    DEFAULT_DB,
    FORMAL_CRAWL_CONTRACT,
    LOCAL_MEDIA_ROOT,
    PROJECT_ROOT,
    ensure_dir,
    ensure_parent,
)


ROOT = PROJECT_ROOT
DEFAULT_RUN_ROOT = CRAWL_RUNNER_RUNTIME
RANDOM = random.SystemRandom()
CRAWL_CONFIG_SCHEMA_VERSION = 2
DEFAULT_MAX_PARALLEL_PLATFORMS = 4
RUNNER_SQLITE_BUSY_TIMEOUT_MS = 60_000
REMOVED_FORMAL_QUANTITY_FIELDS = frozenset(
    {"candidate_hard_limit", "max_stagnant_batches", "target_new_posts"}
)
RUNNER_CHILD_POLL_SECONDS = 0.5
RUNNER_LANE_WAIT_SECONDS = 0.5
# SIGHUP（终端挂断）与 SIGINT/SIGTERM 同样锁存，避免独立会话中的 child 成为孤儿；SIGQUIT 不处理。
RUNNER_INTERRUPT_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
# 中间层收到唯一一次 SIGTERM 后，最多用 PROCESS_CLEANUP_GRACE_SECONDS 等 worker 进程组退出，
# 再用 PROCESS_FINAL_REAP_SECONDS 回收输出；runner 额外留余量后才对中间层进程组 SIGKILL。
RUNNER_CHILD_INTERRUPT_GRACE_SECONDS = (
    PROCESS_CLEANUP_GRACE_SECONDS + PROCESS_FINAL_REAP_SECONDS + 15.0
)

JobRow = Mapping[str, Any]


@dataclass(frozen=True)
class PreparedJob:

    selection_index: int
    row: JobRow
    command: list[str]
    discovery_plan: dict[str, Any] | None
    state_path: Path
    lane_key: str


class JobLeaseConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ChildRun:
    """中间层一次执行；completed 只含清洗后可落盘的输出，raw_* 仅供内存内解析与分类。"""

    completed: subprocess.CompletedProcess[str]
    raw_stdout: str
    raw_stderr: str
    logs: dict[str, str]
    interrupt_signum: int | None
    launched: bool
    forced_termination: bool
    killed_child_process_groups: list[dict[str, Any]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run configured crawl jobs with deterministic scheduling.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Crawl target config JSON.")
    parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT), help="Directory for run summaries.")
    parser.add_argument(
        "--execution-state-root",
        default=str(CRAWL_EXECUTION_STATE_ROOT),
        help="Directory for frozen per-job execution states.",
    )
    parser.add_argument("--max-jobs", type=int, default=3, help="Maximum due jobs to run in this invocation.")
    parser.add_argument(
        "--max-parallel-platforms",
        type=int,
        default=DEFAULT_MAX_PARALLEL_PLATFORMS,
        help=(
            "Maximum platform lanes to execute concurrently; jobs in the same platform lane remain serial."
        ),
    )
    parser.add_argument("--site", help="Optional site_key filter.")
    parser.add_argument("--kind", help="Optional job_kind filter.")
    parser.add_argument("--job-key", help="Run one specific job key.")
    parser.add_argument("--start-page", type=int, help="Recovery-only platform page to start from.")
    parser.add_argument("--resume-summary", help="Recovery-only prior MediaCrawler summary to include and freeze.")
    parser.add_argument("--recovery-keyword", help="Recovery-only alternative keyword used for the continuation.")
    parser.add_argument("--sync-only", action="store_true", help="Only sync config into crawl_jobs.")
    parser.add_argument("--no-sync-config", action="store_true", help="Do not sync config before selecting jobs.")
    parser.add_argument("--dry-run", action="store_true", help="Plan jobs and commands without executing them.")
    parser.add_argument("--headless", action="store_true", help="Pass headless mode to browser jobs.")
    parser.add_argument("--headful", action="store_true", help="Pass headed mode to browser jobs when supported.")
    parser.add_argument("--no-throttle", action="store_true", help="Forward --no-throttle to child scripts.")
    parser.add_argument("--no-import", action="store_true", help="Do not import capture results after successful jobs.")
    return parser.parse_args()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_stamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%S%f%z")


def iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_crawl_config(config: Any, path: Path) -> dict[str, Any]:
    if not isinstance(config, dict) or config.get("schema_version") != CRAWL_CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported crawl config schema in {path}; "
            f"expected {CRAWL_CONFIG_SCHEMA_VERSION}"
        )
    for item in config.get("jobs") or []:
        if not isinstance(item, dict) or item.get("job_kind") != "mediacrawler_search":
            continue
        params = item.get("params") or {}
        stale_fields = sorted(REMOVED_FORMAL_QUANTITY_FIELDS & params.keys())
        if stale_fields:
            raise ValueError(
                f"removed quantity fields remain in job {item.get('job_key')}: "
                f"{', '.join(stale_fields)}"
            )
    return config


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def tail(text: str, limit: int = 6000) -> str:
    return text[-limit:] if len(text) > limit else text


def select_due_jobs(conn: sqlite3.Connection, args: argparse.Namespace) -> list[JobRow]:
    clauses = ["enabled = 1"]
    params: list[Any] = []
    if args.job_key:
        clauses.append("job_key = ?")
        params.append(args.job_key)
    else:
        clauses.append("status IN ('pending', 'completed', 'retry_wait')")
        clauses.append("next_run_at <= ?")
        params.append(iso())
        if args.site:
            clauses.append("site_key = ?")
            params.append(args.site)
        if args.kind:
            clauses.append("job_kind = ?")
            params.append(args.kind)
    query = f"""
        SELECT * FROM crawl_jobs
        WHERE {" AND ".join(clauses)}
        ORDER BY priority ASC, next_run_at ASC, id ASC
        LIMIT ?
    """
    params.append(max(1, args.max_jobs))
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def behavior_profile_name(row: JobRow) -> str:
    data = json.loads(row["behavior_profile_json"] or "{}")
    return str(data.get("name") or row["site_key"] or "conservative")


def params_for(row: JobRow) -> dict[str, Any]:
    return json.loads(row["params_json"] or "{}")


def platform_lane_key(row: JobRow) -> str:
    params = params_for(row)
    if row["job_kind"] == "mediacrawler_search":
        return str(params.get("platform") or row["site_key"])
    return str(row["site_key"])


def is_unverified_douyin_first_page_checkpoint(
    checkpoint: dict[str, Any] | None,
) -> bool:
    if not checkpoint:
        return False
    return bool(
        checkpoint.get("platform_key") == "douyin"
        and checkpoint.get("status") == "exhausted"
        and checkpoint.get("last_stop_reason") == "source_exhausted"
        and checkpoint.get("last_stop_detail") != "verified_empty_first_page"
        and checkpoint.get("resume_page") in (1, "1")
        and checkpoint.get("resume_offset") in (None, "", 0, "0")
        and not str(checkpoint.get("resume_cursor") or "").strip()
    )


def stable_douyin_search_id_from_summary(summary: dict[str, Any]) -> str:
    pagination = summary.get("pagination_evidence") or {}
    for batch in pagination.get("batches") or []:
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


def resolve_discovery_args(
    conn: sqlite3.Connection,
    row: JobRow,
    args: argparse.Namespace,
    *,
    run_id: str,
) -> tuple[argparse.Namespace, dict[str, Any] | None]:
    job_args = copy.copy(args)
    if row["job_kind"] != "mediacrawler_search":
        return job_args, None

    params = params_for(row)
    platform_key = str(params.get("platform") or row["site_key"])
    keyword = str(args.recovery_keyword or params.get("keyword") or "青岛旅游")
    fingerprint = query_fingerprint(platform_key, keyword, params)
    checkpoint = load_checkpoint(
        conn,
        job_id=int(row["id"]),
        query_fingerprint_value=fingerprint,
    )
    explicit_recovery = bool(args.start_page is not None or args.resume_summary or args.recovery_keyword)

    job_args.discovery_job_id = int(row["id"])
    job_args.discovery_query_fingerprint = fingerprint
    job_args.discovery_run_id = run_id
    job_args.discovery_source_exhausted = False
    job_args.start_offset = None
    job_args.start_cursor = None
    job_args.top_refresh_max_pages = 0
    job_args.auto_resume = False
    resume_cursor_corrected_from_summary = False
    if checkpoint and not explicit_recovery:
        unverified_first_page = is_unverified_douyin_first_page_checkpoint(checkpoint)
        summary_value = str(checkpoint.get("last_summary_path") or "")
        summary_path = Path(summary_value).expanduser() if summary_value else None
        job_args.start_page = max(1, int(checkpoint.get("resume_page") or 1))
        job_args.start_offset = checkpoint.get("resume_offset")
        job_args.start_cursor = checkpoint.get("resume_cursor")
        job_args.discovery_source_exhausted = bool(
            checkpoint.get("status") == "exhausted" and not unverified_first_page
        )
        job_args.top_refresh_max_pages = max(
            0,
            int(params.get("top_refresh_max_pages") or 3),
        )
        if summary_path:
            if not summary_path.is_file():
                raise RuntimeError(
                    "checkpoint campaign summary is missing: "
                    f"job={row['job_key']} path={summary_path}"
                )
            job_args.resume_summary = str(summary_path.resolve())
            if platform_key == "douyin":
                prior_summary = load_json(summary_path)
                stable_search_id = stable_douyin_search_id_from_summary(prior_summary)
                if stable_search_id and stable_search_id != str(job_args.start_cursor or ""):
                    job_args.start_cursor = stable_search_id
                    resume_cursor_corrected_from_summary = True
        job_args.auto_resume = True

    plan = {
        "job_id": int(row["id"]),
        "platform_key": platform_key,
        "keyword": keyword,
        "query_fingerprint": fingerprint,
        "checkpoint_found": checkpoint is not None,
        "auto_resume": bool(job_args.auto_resume),
        "resume_page": int(job_args.start_page or 1),
        "resume_offset": job_args.start_offset,
        "resume_cursor": job_args.start_cursor,
        "resume_cursor_corrected_from_summary": resume_cursor_corrected_from_summary,
        "source_exhausted": bool(job_args.discovery_source_exhausted),
        "unverified_first_page_checkpoint_rejected": bool(
            checkpoint
            and not explicit_recovery
            and is_unverified_douyin_first_page_checkpoint(checkpoint)
        ),
        "top_refresh_max_pages": int(job_args.top_refresh_max_pages),
        "campaign_summary_path": str(job_args.resume_summary or ""),
        "checkpoint_before": checkpoint,
        "local_image_storage_required": True,
        "media_root": str(LOCAL_MEDIA_ROOT.resolve()),
    }
    return job_args, plan


def add_flag(command: list[str], flag: str, value: Any | None = None) -> None:
    command.append(flag)
    if value is not None:
        command.append(str(value))


def build_command(row: JobRow, args: argparse.Namespace) -> list[str]:
    params = params_for(row)
    site = row["site_key"]
    url = row["target_url"]
    kind = row["job_kind"]
    profile = behavior_profile_name(row)

    if kind == "mediacrawler_search":
        platform = str(params.get("platform") or site)
        if platform == "xhs":
            raise ValueError("XHS formal jobs must use scripts/xhs_runner.py")
        if profile != "social_high_risk":
            raise ValueError(
                f"Generic MediaCrawler formal job must use social_high_risk: {row['job_key']}"
            )
        stale_fields = sorted(REMOVED_FORMAL_QUANTITY_FIELDS & params.keys())
        if stale_fields:
            raise ValueError(
                f"Removed quantity fields remain in job {row['job_key']}: "
                f"{', '.join(stale_fields)}"
            )
        required_fields_profile = str(params.get("required_fields_profile") or "")
        followers_policy = str(params.get("followers_policy") or "")
        if required_fields_profile != "image_post_with_followers_v1":
            raise ValueError(f"Unsupported required_fields_profile for job {row['job_key']}")
        if followers_policy != "required":
            raise ValueError(f"Structured platform must require followers for job {row['job_key']}")
        if params.get("get_media"):
            raise ValueError(f"Generic media/video downloads are disabled for job {row['job_key']}")
        if "download_images" in params:
            raise ValueError(f"removed download_images option remains in job {row['job_key']}")
        command = [sys.executable, str(ROOT / "scripts" / "mediacrawler_crawl.py"), "--platforms", platform]
        add_flag(command, "--keyword", args.recovery_keyword or params.get("keyword", "青岛旅游"))
        add_flag(command, "--timeout-per-platform", params.get("timeout_per_platform", 180))
        add_flag(command, "--required-fields-profile", required_fields_profile)
        add_flag(command, "--behavior-profile", profile)
        add_flag(command, "--login-type", params.get("login_type", "cookie"))
        add_flag(command, "--db", args.db)
        command.append("--download-images")
        add_flag(command, "--media-root", LOCAL_MEDIA_ROOT.resolve())
        if args.start_page is not None:
            add_flag(command, "--start-page", args.start_page)
        if args.resume_summary:
            add_flag(command, "--resume-summary", args.resume_summary)
        if getattr(args, "start_offset", None) is not None:
            add_flag(command, "--start-offset", args.start_offset)
        if getattr(args, "start_cursor", None):
            add_flag(command, "--start-cursor", args.start_cursor)
        add_flag(command, "--discovery-job-id", getattr(args, "discovery_job_id", row["id"]))
        add_flag(
            command,
            "--discovery-query-fingerprint",
            getattr(args, "discovery_query_fingerprint", ""),
        )
        add_flag(command, "--discovery-run-id", getattr(args, "discovery_run_id", ""))
        add_flag(
            command,
            "--top-refresh-max-pages",
            getattr(args, "top_refresh_max_pages", 0),
        )
        if getattr(args, "discovery_source_exhausted", False):
            command.append("--discovery-source-exhausted")
        if args.headful or (params.get("headless") is False and not args.headless):
            command.append("--headed")
        if args.no_import:
            command.append("--no-import")
            command.append("--no-checkpoint-write")
    elif kind == "ctf_resource_crawl":
        if url:
            command = [
                sys.executable,
                str(ROOT / "scripts" / "ctf_resource_crawl.py"),
                "--urls",
                url,
                "--site-label",
                site,
                "--configured-site-urls",
            ]
        else:
            command = [sys.executable, str(ROOT / "scripts" / "ctf_resource_crawl.py"), "--sites", site]
        add_flag(command, "--keyword", params.get("keyword", ""))
        add_flag(command, "--scrapling-preflight", params.get("scrapling_preflight", "auto"))
        add_flag(command, "--max-scrolls", params.get("max_scrolls", 4))
        add_flag(command, "--behavior-profile", profile)
        if args.headless or params.get("headless", False):
            command.append("--headless")
    else:
        raise ValueError(f"Unsupported or retired job_kind: {kind}")

    if args.no_throttle:
        command.append("--no-throttle")
    return command


def latest_attempt_no(conn: sqlite3.Connection, job_id: int) -> int:
    row = conn.execute("SELECT max(attempt_no) FROM crawl_attempts WHERE job_id = ?", (job_id,)).fetchone()
    return int(row[0] or 0) + 1


def insert_attempt(conn: sqlite3.Connection, row: JobRow, run_id: str, command: list[str]) -> int:
    if row["status"] == "leased":
        raise JobLeaseConflict(f"scheduler job is already leased: {row['job_key']}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        leased = conn.execute(
            """
            UPDATE crawl_jobs
            SET status='leased', updated_at=datetime('now')
            WHERE id=? AND enabled=1 AND status=? AND last_attempt_id IS ?
            """,
            (row["id"], row["status"], row["last_attempt_id"]),
        )
        if leased.rowcount != 1:
            raise JobLeaseConflict(
                f"scheduler job changed, is already leased, or was disabled: {row['job_key']}"
            )
        attempt_no = latest_attempt_no(conn, int(row["id"]))
        started_at = iso()
        cur = conn.execute(
            """
            INSERT INTO crawl_attempts (
                job_id, run_id, attempt_no, status, command_json, started_at
            )
            VALUES (?, ?, ?, 'running', ?, ?)
            """,
            (row["id"], run_id, attempt_no, json_dump(command), started_at),
        )
        attempt_id = cur.lastrowid
        if attempt_id is None:
            raise RuntimeError(f"scheduler attempt ID was not returned: {row['job_key']}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return int(attempt_id)


def find_artifact_paths(stdout: str, row: JobRow) -> tuple[str, list[str], str | None]:
    stdout_json = extract_stdout_json(stdout)
    artifact_dirs: list[str] = []
    summary_path: str | None = None
    for key in ("artifact_dir", "batch_dir"):
        value = stdout_json.get(key)
        if value:
            artifact_dirs.append(str(value))
    summary_value = stdout_json.get("summary")
    if summary_value:
        candidate_summary = Path(str(summary_value))
        if candidate_summary.is_file():
            summary_path = str(candidate_summary)
    for record in stdout_json.get("records") or []:
        if isinstance(record, dict):
            for key in ("artifact_dir", "capture_dir"):
                if record.get(key):
                    artifact_dirs.append(str(record[key]))
    match = re.search(r"Summary:\s*(.+)", stdout)
    if match:
        summary_path = match.group(1).strip()
    if summary_path:
        try:
            summary = load_json(Path(summary_path))
            if summary.get("batch_dir"):
                artifact_dirs.append(str(summary["batch_dir"]))
            for record in summary.get("records") or []:
                if isinstance(record, dict) and record.get("artifact_dir"):
                    artifact_dirs.append(str(record["artifact_dir"]))
        except Exception:
            pass

    for match in re.finditer(r"Saved artifacts to:\s*(.+)", stdout):
        artifact_dirs.append(match.group(1).strip())

    capture_meta_paths: list[str] = []
    seen: set[str] = set()
    for artifact in artifact_dirs:
        path = Path(artifact)
        candidates = [path / "capture_meta.json"] if path.is_dir() else []
        if path.is_dir():
            candidates.extend(path.glob("*/capture_meta.json"))
        for candidate in candidates:
            resolved = str(candidate)
            if candidate.exists() and resolved not in seen:
                seen.add(resolved)
                capture_meta_paths.append(resolved)

    artifact_dir = artifact_dirs[0] if artifact_dirs else ""
    return artifact_dir, capture_meta_paths, summary_path


def load_first_meta(paths: list[str]) -> dict[str, Any]:
    if not paths:
        return {}
    try:
        return load_json(Path(paths[0]))
    except Exception:
        return {}


def import_capture_results(paths: list[str], db_path: Path) -> dict[str, Any]:
    if not paths:
        return {"skipped": True, "reason": "no_capture_meta_paths"}
    command = [
        sys.executable,
        str(ROOT / "scripts" / "import_ctf_captures.py"),
        "--db",
        str(db_path),
        "--capture-meta",
        *paths,
    ]
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    payload = extract_stdout_json(completed.stdout)
    return {
        "command": command,
        "exit_code": completed.returncode,
        "stdout_tail": tail(completed.stdout),
        "stderr_tail": tail(completed.stderr),
        "ok": completed.returncode == 0,
        **payload,
    }


def next_run_time(row: JobRow, classification: dict[str, Any], config: dict[str, Any]) -> str | None:
    status = classification["status"]
    if status in {"blocked", "login_required", "captcha_detected", "failed_final"}:
        return None
    wait_seconds = int(classification.get("wait_seconds") or 0)
    if status == "retry_wait" and not classification.get("checkpoint_progress"):
        return iso(utc_now() + timedelta(seconds=max(60, wait_seconds)))
    schedule_seconds = int(row["schedule_seconds"] or 86400)
    jitter_ratio = float((config.get("defaults") or {}).get("schedule_jitter_ratio") or 0.0)
    jitter = int(schedule_seconds * jitter_ratio)
    offset = schedule_seconds + (RANDOM.randint(-jitter, jitter) if jitter > 0 else 0)
    return iso(utc_now() + timedelta(seconds=max(60, offset)))


def finalize_attempt(
    conn: sqlite3.Connection,
    *,
    row: JobRow,
    attempt_id: int,
    completed: subprocess.CompletedProcess[str] | None,
    classification: dict[str, Any],
    artifact_dir: str,
    capture_meta_paths: list[str],
    import_result: dict[str, Any],
    config: dict[str, Any],
) -> None:
    exit_code = completed.returncode if completed is not None else None
    stdout_tail = tail(completed.stdout if completed is not None else "")
    stderr_tail = tail(completed.stderr if completed is not None else "")
    conn.execute(
        """
        UPDATE crawl_attempts
        SET status=?, failure_type=?, retryable=?, wait_seconds=?, finished_at=?, exit_code=?,
            artifact_dir=?, capture_meta_paths_json=?, import_result_json=?,
            stdout_tail=?, stderr_tail=?, classification_json=?
        WHERE id=?
        """,
        (
            classification["status"],
            classification.get("failure_type"),
            1 if classification.get("retryable") else 0,
            int(classification.get("wait_seconds") or 0),
            iso(),
            exit_code,
            artifact_dir,
            json_dump(capture_meta_paths),
            json_dump(import_result),
            stdout_tail,
            stderr_tail,
            json_dump(classification),
            attempt_id,
        ),
    )
    made_discovery_progress = bool(classification.get("checkpoint_progress"))
    previous_failures = int(row["consecutive_failures"] or 0)
    if classification["status"] == "completed" or made_discovery_progress:
        failures = 0
    elif classification.get("interrupt"):
        # 操作人中断不是任务失败，不累计重试次数，也不因此升级为 failed_final。
        failures = previous_failures
    else:
        failures = previous_failures + 1
    status = classification["status"]
    if (
        status == "retry_wait"
        and not classification.get("interrupt")
        and failures >= int(row["max_attempts"])
    ):
        status = "failed_final"
        classification["status"] = status
    next_at = next_run_time(row, classification, config)
    conn.execute(
        """
        UPDATE crawl_jobs
        SET status=?, consecutive_failures=?, last_attempt_id=?, last_status=?, last_failure_type=?,
            next_run_at=COALESCE(?, next_run_at), updated_at=datetime('now')
        WHERE id=?
        """,
        (
            status,
            failures,
            attempt_id,
            status,
            classification.get("failure_type"),
            next_at,
            row["id"],
        ),
    )
    conn.commit()


def markdown_report(summary: dict[str, Any]) -> str:
    scheduling = summary.get("scheduling") or {}
    lines = [
        "# 抓取运行摘要",
        "",
        f"- 运行 ID：`{summary['run_id']}`",
        f"- 开始时间：`{summary['started_at']}`",
        f"- 结束时间：`{summary['finished_at']}`",
        f"- 选中任务：`{summary['jobs_selected']}`",
        f"- 完成：`{summary['completed_count']}`",
        f"- 失败：`{summary['failed_count']}`",
        f"- 阻断/需人工：`{summary['blocked_count']}`",
        f"- 平台执行通道：`{scheduling.get('platform_lane_count', 0)}`",
        f"- 计划并行 worker：`{scheduling.get('planned_workers', 0)}`",
        f"- 有效并行 worker：`{scheduling.get('effective_workers', 0)}`",
        f"- 并行执行：`{str(bool(scheduling.get('parallel_execution'))).lower()}`",
    ]
    interrupt = summary.get("interrupt")
    if interrupt:
        lines.append(
            f"- 操作人中断：`{interrupt['signal']}`（`{interrupt['error']}`，"
            f"退出码 `{interrupt['exit_code']}`；未派发与被收束任务均记为 runtime_failed）"
        )
    lines += [
        "",
        "## 任务结果",
        "",
        "| job_key | site | kind | status | failure_type | state | artifact |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in summary["records"]:
        lines.append(
            "| {job_key} | {site_key} | {job_kind} | {status} | {failure_type} | {execution_state} | {artifact_dir} |".format(
                **{
                    key: str(item.get(key, ""))
                    for key in (
                        "job_key",
                        "site_key",
                        "job_kind",
                        "status",
                        "failure_type",
                        "execution_state",
                        "artifact_dir",
                    )
                }
            )
        )
    lines.append("")
    return "\n".join(lines)


def create_run_report(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO crawl_run_reports(run_id, started_at, status) VALUES (?, ?, 'running')",
        (run_id, iso()),
    )
    conn.commit()


def finish_run_report(conn: sqlite3.Connection, run_id: str, summary: dict[str, Any], report_path: Path) -> None:
    run_status = (
        "failed"
        if summary["failed_count"] or summary.get("interrupt")
        else ("blocked" if summary["blocked_count"] else "completed")
    )
    conn.execute(
        """
        UPDATE crawl_run_reports
        SET finished_at=?, status=?, jobs_selected=?, completed_count=?, failed_count=?, blocked_count=?,
            report_json=?, report_path=?, updated_at=datetime('now')
        WHERE run_id=?
        """,
        (
            summary["finished_at"],
            run_status,
            summary["jobs_selected"],
            summary["completed_count"],
            summary["failed_count"],
            summary["blocked_count"],
            json_dump(summary),
            str(report_path),
            run_id,
        ),
    )
    conn.commit()


def prepare_job(
    conn: sqlite3.Connection,
    row: JobRow,
    args: argparse.Namespace,
    *,
    selection_index: int,
    run_id: str,
    config_path: Path,
    db_path: Path,
    state_dir: Path,
    contract_path: Path,
) -> PreparedJob:
    job_args, discovery_plan = resolve_discovery_args(conn, row, args, run_id=run_id)
    command = build_command(row, job_args)
    frozen_inputs = [config_path, contract_path]
    if getattr(job_args, "resume_summary", None):
        resume_path = Path(job_args.resume_summary).expanduser().resolve()
        resume_summary = load_json(resume_path)
        frozen_inputs.append(resume_path)
        for record in resume_summary.get("records") or []:
            output = record.get("output") if isinstance(record, dict) else {}
            for path_value in (output or {}).get("jsonl_files") or []:
                frozen_inputs.append(Path(path_value).expanduser().resolve())
    lane_key = platform_lane_key(row)
    state_path = state_dir / f"{row['job_key']}.json"
    FrozenExecutionState.create(
        state_path,
        run_id=run_id,
        job_key=str(row["job_key"]),
        site_key=str(row["site_key"]),
        job_kind=str(row["job_kind"]),
        plan={
            "contract_path": str(contract_path.resolve()),
            "config_path": str(config_path.resolve()),
            "database_path": str(db_path.resolve()),
            "job_params": params_for(row),
            "completion_policy": "source_exhausted",
            "scheduling": {
                "mode": "parallel_platform_lanes",
                "lane_key": lane_key,
                "max_parallel_platforms": int(args.max_parallel_platforms),
            },
            "local_image_storage_required": row["job_kind"] == "mediacrawler_search",
            "media_root": (
                str(LOCAL_MEDIA_ROOT.resolve())
                if row["job_kind"] == "mediacrawler_search"
                else None
            ),
            "command": command,
            "discovery": discovery_plan,
            "no_import": bool(args.no_import),
            "dry_run": bool(args.dry_run),
        },
        frozen_inputs=frozen_inputs,
        dry_run=args.dry_run,
    )
    return PreparedJob(
        selection_index=selection_index,
        row=row,
        command=command,
        discovery_plan=discovery_plan,
        state_path=state_path,
        lane_key=lane_key,
    )


def build_result_record(
    job: PreparedJob,
    classification: Mapping[str, Any],
    *,
    completed: subprocess.CompletedProcess[str] | None = None,
    artifact_dir: str = "",
    capture_meta_paths: Sequence[str] = (),
    import_result: Mapping[str, Any] | None = None,
    child_logs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "job_key": job.row["job_key"],
        "site_key": job.row["site_key"],
        "job_kind": job.row["job_kind"],
        "platform_lane": job.lane_key,
        "status": classification["status"],
        "failure_type": classification.get("failure_type", ""),
        "retryable": bool(classification.get("retryable")),
        "reason": classification.get("reason", ""),
        "exit_code": completed.returncode if completed is not None else None,
        "stdout_tail": tail(completed.stdout, 2000) if completed is not None else "",
        "stderr_tail": tail(completed.stderr, 2000) if completed is not None else "",
        "artifact_dir": artifact_dir,
        "capture_meta_paths": list(capture_meta_paths),
        "command": job.command,
        "execution_state": str(job.state_path),
        "child_logs": {
            key: value
            for key, value in (child_logs or {}).items()
            if key in {"stdout_log", "stderr_log", "command_log"}
        },
        "interrupt": dict(classification.get("interrupt") or {}) or None,
        "import_result": dict(import_result or {}),
    }


def planned_record(job: PreparedJob) -> dict[str, Any]:
    return build_result_record(
        job,
        {"status": "planned", "failure_type": "", "retryable": False},
        import_result={"skipped": True, "reason": "dry_run"},
    )


def fail_open_execution_step(
    state: FrozenExecutionState,
    *,
    error: str,
    evidence: Mapping[str, Any],
) -> str | None:
    try:
        payload = state.load()
        for step_name in (
            "command_executed",
            "artifacts_verified",
            "persistence_verified",
            "task_finalized",
        ):
            status = str((payload.get("steps") or {}).get(step_name, {}).get("status") or "")
            if status in {"pending", "in_progress"}:
                state.fail(step_name, error=error, evidence=dict(evidence))
                return None
            if status == "failed":
                return None
    except Exception as exc:
        return repr(exc)
    return None


def operator_interrupt_details(signum: int) -> dict[str, Any]:
    return {
        "reason": "operator_interrupt",
        "signum": int(signum),
        "signal": signal.Signals(int(signum)).name,
        "exit_code": 128 + int(signum),
        "error": operator_interrupt_error(signum),
    }


def operator_interrupt_classification(signum: int) -> dict[str, Any]:
    return {
        "status": "retry_wait",
        "failure_type": "runtime_failed",
        "retryable": True,
        "wait_seconds": 0,
        "reason": "operator_interrupt",
        "stop_reason": "runtime_failed",
        "stop_detail": "operator_interrupt",
        "interrupt": operator_interrupt_details(signum),
    }


def terminalize_interrupted_state(
    state: FrozenExecutionState,
    *,
    error: str,
    evidence: Mapping[str, Any],
) -> str | None:
    state_error = fail_open_execution_step(state, error=error, evidence=evidence)
    if state_error:
        return state_error
    try:
        state.finalize_failure(error=error, evidence=dict(evidence))
    except Exception as exc:
        return repr(exc)
    return None


def interrupted_before_dispatch(job: PreparedJob, signum: int) -> dict[str, Any]:
    """首信号后不再派发的 job：不租约、不启动 child，只把冻结状态写成中断终态。"""

    classification = operator_interrupt_classification(signum)
    state_error = terminalize_interrupted_state(
        FrozenExecutionState(job.state_path),
        error=operator_interrupt_error(signum),
        evidence={"interrupt": classification["interrupt"], "dispatched": False},
    )
    if state_error:
        classification["reason"] = f"operator_interrupt; state_update_failed={state_error}"
    return build_result_record(
        job,
        classification,
        import_result={"skipped": True, "reason": "operator_interrupt"},
    )


def _universal_newlines(value: str) -> str:
    # 与原 subprocess.run(text=True) 的通用换行一致，摘要定位与分类不受 CRLF 影响。
    return value.replace("\r\n", "\n").replace("\r", "\n")


def run_child_command(
    command: list[str],
    *,
    env: Mapping[str, str],
    log_dir: Path,
    state_path: Path,
    interrupt_signal: Callable[[], int | None],
) -> ChildRun:
    """在独立会话中运行中间层；首信号只向它发一次 SIGTERM，超时才强杀中间层与其 worker 组。"""

    def finish(
        returncode: int | None,
        stdout_data: bytes | None,
        stderr_data: bytes | None,
        *,
        interrupt_signum: int | None,
        launched: bool = True,
        forced_termination: bool = False,
        killed_groups: list[dict[str, Any]] | None = None,
    ) -> ChildRun:
        raw_stdout = _universal_newlines(decode_text(stdout_data))
        raw_stderr = _universal_newlines(decode_text(stderr_data))
        logs = write_command_logs(command, log_dir, raw_stdout, raw_stderr)
        return ChildRun(
            completed=subprocess.CompletedProcess(
                command,
                returncode,  # type: ignore[arg-type]
                logs["stdout"],
                logs["stderr"],
            ),
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            logs=logs,
            interrupt_signum=interrupt_signum,
            launched=launched,
            forced_termination=forced_termination,
            killed_child_process_groups=list(killed_groups or []),
        )

    signum = interrupt_signal()
    if signum is not None:
        # 租约与状态已建立但信号先到：不再启动 child。
        return finish(None, b"", b"", interrupt_signum=signum, launched=False)
    try:
        proc = subprocess.Popen(
            command,
            cwd=ROOT,
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return finish(
            127,
            b"",
            f"subprocess_launch_failed: {exc!r}".encode("utf-8"),
            interrupt_signum=None,
            launched=False,
        )
    interrupt_signum: int | None = None
    forced_termination = False
    killed_groups: list[dict[str, Any]] = []
    try:
        while True:
            try:
                stdout_data, stderr_data = proc.communicate(timeout=RUNNER_CHILD_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                signum = interrupt_signal()
                if signum is None:
                    continue
                interrupt_signum = signum
                stdout_data, stderr_data, forced_termination = terminate_managed_process(
                    proc,
                    grace_seconds=RUNNER_CHILD_INTERRUPT_GRACE_SECONDS,
                )
                if forced_termination:
                    # 中间层被强杀后无法再收束它启动的 worker 组，由 runner 按登记身份兜底。
                    killed_groups = kill_recorded_child_process_groups(state_path)
                break
            # 完成边界再核对一次：首信号若早于这次观察到的退出，按中断处理，不走正常摘要路径。
            interrupt_signum = interrupt_signal()
            break
    except BaseException:
        collected: tuple[bytes | None, bytes | None] = (None, None)
        try:
            if proc.poll() is None:
                collected = terminate_managed_process(
                    proc,
                    grace_seconds=RUNNER_CHILD_INTERRUPT_GRACE_SECONDS,
                )[:2]
            else:
                collected = drain_exited_process(proc, timeout=PROCESS_FINAL_REAP_SECONDS)
        except Exception:
            pass
        try:
            write_command_logs(
                command,
                log_dir,
                _universal_newlines(decode_text(collected[0])),
                _universal_newlines(decode_text(collected[1])),
            )
        except Exception:
            pass
        raise
    return finish(
        proc.returncode,
        stdout_data,
        stderr_data,
        interrupt_signum=interrupt_signum,
        forced_termination=forced_termination,
        killed_groups=killed_groups,
    )


def execute_prepared_job(
    job: PreparedJob,
    *,
    args: argparse.Namespace,
    db_path: Path,
    config: dict[str, Any],
    run_id: str,
    run_dir: Path,
    interrupt_signal: Callable[[], int | None],
) -> dict[str, Any]:
    row = job.row
    state = FrozenExecutionState(job.state_path)
    attempt_id: int | None = None
    completed: subprocess.CompletedProcess[str] | None = None
    child_logs: dict[str, str] = {}
    interrupt_signum: int | None = None
    artifact_dir = ""
    capture_meta_paths: list[str] = []
    import_result: dict[str, Any] = {"skipped": True, "reason": "command_not_completed"}
    try:
        with connect_db(db_path, busy_timeout_ms=RUNNER_SQLITE_BUSY_TIMEOUT_MS) as conn:
            attempt_id = insert_attempt(conn, row, run_id, job.command)
            state.begin("command_executed")
            child_env = os.environ.copy()
            child_env["TRIPPOSTCOLLECT_EXECUTION_STATE_PATH"] = str(job.state_path)
            child = run_child_command(
                job.command,
                env=child_env,
                log_dir=run_dir / "jobs" / str(row["job_key"]),
                state_path=job.state_path,
                interrupt_signal=interrupt_signal,
            )
            completed = child.completed
            child_logs = child.logs
            if child.interrupt_signum is not None:
                # 被中断的 child 不读取摘要、不推进 campaign，也不写来源耗尽；只记录首个信号。
                interrupt_signum = child.interrupt_signum
                classification = operator_interrupt_classification(interrupt_signum)
                import_result = {"skipped": True, "reason": "operator_interrupt"}
                state_error = terminalize_interrupted_state(
                    state,
                    error=operator_interrupt_error(interrupt_signum),
                    evidence={
                        "interrupt": classification["interrupt"],
                        "dispatched": True,
                        "launched": child.launched,
                        "exit_code": completed.returncode,
                        "forced_termination": child.forced_termination,
                        "killed_child_process_groups": child.killed_child_process_groups,
                        "child_logs": {
                            key: child_logs[key]
                            for key in ("stdout_log", "stderr_log", "command_log")
                        },
                        "stderr_tail": tail(completed.stderr, 2000),
                    },
                )
                if state_error:
                    classification["reason"] = (
                        f"operator_interrupt; state_update_failed={state_error}"
                    )
                classification["forced_termination"] = child.forced_termination
                finalize_attempt(
                    conn,
                    row=row,
                    attempt_id=attempt_id,
                    completed=completed,
                    classification=classification,
                    artifact_dir=artifact_dir,
                    capture_meta_paths=capture_meta_paths,
                    import_result=import_result,
                    config=config,
                )
                return build_result_record(
                    job,
                    classification,
                    completed=completed,
                    import_result=import_result,
                    child_logs=child_logs,
                )
            artifact_dir, capture_meta_paths, summary_path = find_artifact_paths(child.raw_stdout, row)
            meta = load_first_meta(capture_meta_paths)
            classification = classify_attempt(
                exit_code=completed.returncode,
                stdout=child.raw_stdout,
                stderr=child.raw_stderr,
                meta=meta,
            )
            child_summary: dict[str, Any] = {}
            checkpoint_progress = False
            discovery_plan = job.discovery_plan
            if discovery_plan and summary_path and Path(summary_path).is_file() and not args.no_import:
                try:
                    child_summary = load_json(Path(summary_path))
                except (OSError, json.JSONDecodeError, TypeError):
                    child_summary = {}
                checkpoint_after = load_checkpoint(
                    conn,
                    job_id=int(row["id"]),
                    query_fingerprint_value=str(discovery_plan["query_fingerprint"]),
                )
                checkpoint_before = discovery_plan.get("checkpoint_before") or {}
                if checkpoint_after and checkpoint_after.get("last_run_id") == run_id:
                    before_position = (
                        checkpoint_before.get("resume_page", discovery_plan.get("resume_page")),
                        checkpoint_before.get("resume_offset", discovery_plan.get("resume_offset")),
                        checkpoint_before.get("resume_cursor", discovery_plan.get("resume_cursor")),
                    )
                    after_position = (
                        checkpoint_after.get("resume_page"),
                        checkpoint_after.get("resume_offset"),
                        checkpoint_after.get("resume_cursor"),
                    )
                    checkpoint_progress = (
                        after_position != before_position
                        or checkpoint_after.get("status") == "exhausted"
                    )
                    formal_validation = child_summary.get("formal_validation") or {}
                    import_result_value = child_summary.get("import_result") or {}
                    checkpoint_image_artifacts = verify_image_artifacts(
                        child_summary,
                        project_root=ROOT,
                        expect_promotion=True,
                    )
                    checkpoint_image_persistence = verify_image_persistence(
                        child_summary,
                        db_path,
                        project_root=ROOT,
                        media_root=LOCAL_MEDIA_ROOT,
                    )
                    imported_completion = bool(
                        child_summary.get("import_completion_met")
                    ) and not bool(import_result_value.get("reason"))
                    imported_completion = bool(
                        imported_completion
                        and checkpoint_image_artifacts["ok"]
                        and checkpoint_image_persistence["ok"]
                    )
                    if imported_completion:
                        clear_campaign(
                            conn,
                            job_id=int(row["id"]),
                            query_fingerprint_value=str(discovery_plan["query_fingerprint"]),
                        )
                    else:
                        update_campaign(
                            conn,
                            job_id=int(row["id"]),
                            query_fingerprint_value=str(discovery_plan["query_fingerprint"]),
                            summary_path=str(Path(summary_path).resolve()),
                            campaign_candidate_count=int(formal_validation.get("candidate_count") or 0),
                        )
                    conn.commit()
            if checkpoint_progress:
                classification["checkpoint_progress"] = True
            command_evidence = {
                "exit_code": completed.returncode,
                "classification": classification,
            }
            if classification.get("status") == "completed":
                state.complete("command_executed", evidence=command_evidence)
            else:
                state.fail(
                    "command_executed",
                    error=str(classification.get("reason") or "command_failed"),
                    evidence=command_evidence,
                )

            ready_to_finalize_state = False
            if classification.get("status") == "completed":
                state.begin("artifacts_verified")
                if row["job_kind"] == "mediacrawler_search":
                    summary_exists = bool(summary_path and Path(summary_path).is_file())
                    if summary_exists and not child_summary:
                        try:
                            child_summary = load_json(Path(str(summary_path)))
                        except (OSError, json.JSONDecodeError, TypeError):
                            child_summary = {}
                    image_artifacts = verify_image_artifacts(
                        child_summary,
                        project_root=ROOT,
                        expect_promotion=not args.no_import,
                    )
                    artifact_ok = bool(summary_exists and image_artifacts["ok"])
                    artifact_evidence = {
                        "summary_path": summary_path or "",
                        "exists": summary_exists,
                        "local_images": image_artifacts,
                    }
                else:
                    artifact_ok = bool(capture_meta_paths)
                    artifact_evidence = {
                        "capture_meta_paths": capture_meta_paths,
                        "count": len(capture_meta_paths),
                    }
                if artifact_ok:
                    state.complete("artifacts_verified", evidence=artifact_evidence)
                else:
                    state.fail(
                        "artifacts_verified",
                        error="required_artifacts_missing",
                        evidence=artifact_evidence,
                    )
                    classification = {
                        "status": "retry_wait",
                        "failure_type": "artifact_missing",
                        "retryable": True,
                        "wait_seconds": 600,
                        "reason": "required_artifacts_missing",
                    }

            if classification.get("status") == "completed":
                state.begin("persistence_verified")
                if args.no_import:
                    import_result = {"skipped": True, "reason": "no_import"}
                    state.complete("persistence_verified", evidence=import_result, skipped=True)
                elif row["job_kind"] == "ctf_resource_crawl" and not meta.get("skipped"):
                    import_result = import_capture_results(capture_meta_paths, db_path)
                    if import_result.get("ok"):
                        state.complete("persistence_verified", evidence=import_result)
                    else:
                        state.fail(
                            "persistence_verified",
                            error="capture_import_failed",
                            evidence=import_result,
                        )
                elif row["job_kind"] == "mediacrawler_search":
                    if not child_summary:
                        child_summary = load_json(Path(str(summary_path)))
                    import_result = dict(child_summary.get("import_result") or {})
                    import_completion_ok = bool(child_summary.get("import_completion_met"))
                    image_persistence = verify_image_persistence(
                        child_summary,
                        db_path,
                        project_root=ROOT,
                        media_root=LOCAL_MEDIA_ROOT,
                    )
                    import_result["local_images"] = image_persistence
                    persistence_ok = bool(import_completion_ok and image_persistence["ok"])
                    if persistence_ok:
                        state.complete("persistence_verified", evidence=import_result)
                    else:
                        state.fail(
                            "persistence_verified",
                            error="formal_import_completion_not_reached",
                            evidence=import_result,
                        )
                else:
                    import_result = {"skipped": True, "reason": "skipped_capture"}
                    state.complete("persistence_verified", evidence=import_result, skipped=True)

                persistence_step = state.load()["steps"]["persistence_verified"]
                if persistence_step["status"] == "failed":
                    classification = {
                        "status": "retry_wait",
                        "failure_type": "persistence_failed",
                        "retryable": True,
                        "wait_seconds": 600,
                        "reason": str(persistence_step.get("error") or "persistence_failed"),
                    }
                else:
                    ready_to_finalize_state = True
            finalize_attempt(
                conn,
                row=row,
                attempt_id=attempt_id,
                completed=completed,
                classification=classification,
                artifact_dir=artifact_dir,
                capture_meta_paths=capture_meta_paths,
                import_result=import_result,
                config=config,
            )
            if ready_to_finalize_state:
                state.finalize(
                    outcome="completed",
                    evidence={"classification": classification, "import_result": import_result},
                )
            return build_result_record(
                job,
                classification,
                completed=completed,
                artifact_dir=artifact_dir,
                capture_meta_paths=capture_meta_paths,
                import_result=import_result,
                child_logs=child_logs,
            )
    except JobLeaseConflict as exc:
        classification = {
            "status": "blocked",
            "failure_type": "scheduler_lease_conflict",
            "retryable": False,
            "reason": str(exc),
        }
        state_error = fail_open_execution_step(
            state,
            error="scheduler_lease_conflict",
            evidence={"error": str(exc)},
        )
        if state_error:
            classification["reason"] = f"{classification['reason']}; state_update_failed={state_error}"
        return build_result_record(job, classification, import_result=import_result)
    except Exception as exc:
        if interrupt_signum is None:
            # 判定中断后的收尾（如日志写盘）抛错时，首信号仍是本 job 的终止原因。
            interrupt_signum = interrupt_signal()
        if interrupt_signum is not None:
            # 收尾异常不得覆盖首个中断原因。
            classification = operator_interrupt_classification(interrupt_signum)
            classification["reason"] = f"operator_interrupt; scheduler_internal_error={exc!r}"
            terminalize_interrupted_state(
                state,
                error=operator_interrupt_error(interrupt_signum),
                evidence={"interrupt": classification["interrupt"], "error": repr(exc)},
            )
        else:
            classification = {
                "status": "retry_wait",
                "failure_type": "scheduler_internal_error",
                "retryable": True,
                "wait_seconds": 600,
                "reason": repr(exc),
            }
            state_error = fail_open_execution_step(
                state,
                error="scheduler_internal_error",
                evidence={"error": repr(exc)},
            )
            if state_error:
                classification["reason"] = f"{classification['reason']}; state_update_failed={state_error}"
        if attempt_id is not None:
            try:
                with connect_db(db_path, busy_timeout_ms=RUNNER_SQLITE_BUSY_TIMEOUT_MS) as recovery_conn:
                    finalize_attempt(
                        recovery_conn,
                        row=row,
                        attempt_id=attempt_id,
                        completed=completed,
                        classification=classification,
                        artifact_dir=artifact_dir,
                        capture_meta_paths=capture_meta_paths,
                        import_result=import_result,
                        config=config,
                    )
            except Exception as finalize_exc:
                classification["reason"] = (
                    f"{classification['reason']}; scheduler_finalize_failed={finalize_exc!r}"
                )
        return build_result_record(
            job,
            classification,
            completed=completed,
            artifact_dir=artifact_dir,
            capture_meta_paths=capture_meta_paths,
            import_result=import_result,
            child_logs=child_logs,
        )


def _run_platform_lane(
    jobs: Sequence[PreparedJob],
    execute_job: Callable[[PreparedJob], dict[str, Any]],
    skip_job: Callable[[PreparedJob], dict[str, Any] | None] | None,
) -> list[tuple[int, dict[str, Any]]]:
    records: list[tuple[int, dict[str, Any]]] = []
    for job in jobs:
        skipped = skip_job(job) if skip_job is not None else None
        records.append((job.selection_index, skipped if skipped is not None else execute_job(job)))
    return records


def execute_platform_lanes(
    jobs: Sequence[PreparedJob],
    *,
    max_parallel_platforms: int,
    execute_job: Callable[[PreparedJob], dict[str, Any]],
    skip_job: Callable[[PreparedJob], dict[str, Any] | None] | None = None,
) -> list[dict[str, Any]]:
    """skip_job 在每个 job 派发前调用；返回记录时该 job 不再执行（如首信号后的排队 job）。"""

    if max_parallel_platforms <= 0:
        raise ValueError("max_parallel_platforms must be positive")
    lanes: dict[str, list[PreparedJob]] = {}
    for job in jobs:
        lanes.setdefault(job.lane_key, []).append(job)
    if not lanes:
        return []

    indexed_records: list[tuple[int, dict[str, Any]]] = []
    worker_count = min(max_parallel_platforms, len(lanes))
    if worker_count == 1:
        for lane_jobs in lanes.values():
            indexed_records.extend(_run_platform_lane(lane_jobs, execute_job, skip_job))
    else:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="crawl-platform",
        ) as executor:
            futures = [
                executor.submit(_run_platform_lane, lane_jobs, execute_job, skip_job)
                for lane_jobs in lanes.values()
            ]
            pending = set(futures)
            while pending:
                # 带超时轮询，主线程能及时处理锁存信号；运行中的通道靠 child 收束返回。
                done, pending = wait(
                    pending,
                    timeout=RUNNER_LANE_WAIT_SECONDS,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    indexed_records.extend(future.result())
    indexed_records.sort(key=lambda item: item[0])
    return [record for _, record in indexed_records]


def scheduling_summary(
    jobs: Sequence[PreparedJob],
    *,
    max_parallel_platforms: int,
    execution_enabled: bool,
) -> dict[str, Any]:
    lane_keys = list(dict.fromkeys(job.lane_key for job in jobs))
    planned_workers = min(max_parallel_platforms, len(lane_keys)) if lane_keys else 0
    effective_workers = planned_workers if execution_enabled else 0
    return {
        "mode": "parallel_platform_lanes",
        "max_parallel_platforms": max_parallel_platforms,
        "platform_lane_count": len(lane_keys),
        "planned_workers": planned_workers,
        "effective_workers": effective_workers,
        "parallel_execution": effective_workers > 1,
        "execution_started": bool(execution_enabled and jobs),
        "lane_keys": lane_keys,
    }


def interrupt_exit_code(interrupts: DeferredTerminationSignals, exit_code: int) -> int:
    signum = interrupts.signal_received
    return exit_code if signum is None else 128 + signum


def main(interrupts: DeferredTerminationSignals | None = None) -> int:
    """首个 SIGINT/SIGTERM/SIGHUP 只锁存：运行中的 child 由轮询线程收束，排队 job 不再派发，
    摘要与状态照常写出后以 128+首个信号退出。

    CLI 经 run_cli 安装锁存并保持到进程退出；进程内调用（如测试）未传入时由本函数安装并恢复。
    """

    owned = interrupts is None
    latch = interrupts or DeferredTerminationSignals(RUNNER_INTERRUPT_SIGNALS).install()
    try:
        return _main(latch)
    except Exception:
        signum = latch.signal_received
        if signum is None:
            raise
        # 锁存之后的未捕获异常仍以首信号退出；异常写入 stderr，写失败也不改变退出码。
        try:
            traceback.print_exc(file=sys.stderr)
        except OSError:
            pass
        return 128 + signum
    finally:
        if owned:
            latch.restore()


def run_cli() -> int:
    return main(DeferredTerminationSignals(RUNNER_INTERRUPT_SIGNALS).install())


def _main(interrupts: DeferredTerminationSignals) -> int:
    args = parse_args()
    if (args.start_page is not None or args.resume_summary or args.recovery_keyword) and not args.job_key:
        raise SystemExit("recovery options require --job-key")
    if args.recovery_keyword and not args.resume_summary:
        raise SystemExit("--recovery-keyword requires --resume-summary")
    if args.start_page is not None and args.start_page <= 0:
        raise SystemExit("--start-page must be positive")
    if args.max_parallel_platforms <= 0:
        raise SystemExit("--max-parallel-platforms must be positive")
    db_path = Path(args.db).expanduser()
    config_path = Path(args.config).expanduser()
    config = validate_crawl_config(load_json(config_path), config_path)
    run_id = utc_stamp()
    run_dir = ensure_dir(Path(args.run_root).expanduser() / run_id)
    state_dir = ensure_dir(Path(args.execution_state_root).expanduser() / run_id)
    contract_path = FORMAL_CRAWL_CONTRACT

    ensure_parent(db_path)
    with connect_db(db_path, busy_timeout_ms=RUNNER_SQLITE_BUSY_TIMEOUT_MS) as conn:
        bootstrap = bootstrap_connection(conn, config=config, sync_jobs=not args.no_sync_config)
        synced = int(bootstrap["synced_jobs"])
        if args.sync_only:
            print(json.dumps({"db": str(db_path), **bootstrap}, ensure_ascii=False, indent=2))
            return interrupt_exit_code(interrupts, 0)

        create_run_report(conn, run_id)
        jobs = select_due_jobs(conn, args)
        prepared_jobs = [
            prepare_job(
                conn,
                row,
                args,
                selection_index=index,
                run_id=run_id,
                config_path=config_path,
                db_path=db_path,
                state_dir=state_dir,
                contract_path=contract_path,
            )
            for index, row in enumerate(jobs)
        ]
        schedule = scheduling_summary(
            prepared_jobs,
            max_parallel_platforms=args.max_parallel_platforms,
            execution_enabled=not args.dry_run,
        )

        def interrupt_signal() -> int | None:
            return interrupts.signal_received

        def skip_after_interrupt(job: PreparedJob) -> dict[str, Any] | None:
            signum = interrupt_signal()
            return None if signum is None else interrupted_before_dispatch(job, signum)

        if args.dry_run:
            records = [planned_record(job) for job in prepared_jobs]
        else:
            records = execute_platform_lanes(
                prepared_jobs,
                max_parallel_platforms=args.max_parallel_platforms,
                execute_job=lambda job: execute_prepared_job(
                    job,
                    args=args,
                    db_path=db_path,
                    config=config,
                    run_id=run_id,
                    run_dir=run_dir,
                    interrupt_signal=interrupt_signal,
                ),
                skip_job=skip_after_interrupt,
            )

        interrupt_signum = interrupt_signal()
        blocked_statuses = {"blocked", "login_required", "captcha_detected"}
        non_failed_statuses = {"completed", "planned"} | blocked_statuses
        summary = {
            "run_id": run_id,
            "started_at": run_id,
            "finished_at": iso(),
            "db": str(db_path),
            "config": str(config_path),
            "completion_mode": "source-exhausted",
            "execution_state_dir": str(state_dir),
            "scheduling": schedule,
            "synced_jobs": synced,
            "jobs_selected": len(jobs),
            "interrupt": (
                operator_interrupt_details(interrupt_signum)
                if interrupt_signum is not None
                else None
            ),
            "interrupted_count": sum(1 for item in records if item.get("interrupt")),
            "completed_count": sum(1 for item in records if item["status"] == "completed"),
            "failed_count": sum(1 for item in records if item["status"] not in non_failed_statuses),
            "blocked_count": sum(1 for item in records if item["status"] in blocked_statuses),
            "records": records,
        }
        summary_path = run_dir / "run_summary.json"
        report_path = run_dir / "run_summary.md"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        report_path.write_text(markdown_report(summary), encoding="utf-8")
        finish_run_report(conn, run_id, summary, report_path)

    try:
        print(json.dumps({"summary": str(summary_path), "report": str(report_path), **summary}, ensure_ascii=False, indent=2))
    except OSError:
        # SIGHUP 后控制终端已挂断：摘要已落盘，最终输出失败不改变退出码。
        pass
    return interrupt_exit_code(
        interrupts,
        0 if summary["failed_count"] == 0 and summary["blocked_count"] == 0 else 1,
    )


if __name__ == "__main__":
    raise SystemExit(run_cli())
