#!/usr/bin/env python3
"""Independent formal Xiaohongshu runner with isolated account leasing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from execution_state import FORMAL_STEPS, FrozenExecutionState
from failure_classifier import extract_stdout_json
from trippostcollect.artifacts.image_completion import (
    verify_image_artifacts,
    verify_image_persistence,
)
from trippostcollect.core.paths import (
    DEFAULT_DB,
    FORMAL_CRAWL_CONTRACT,
    LOCAL_MEDIA_ROOT,
    PROJECT_ROOT,
    XHS_EXECUTION_STATE_ROOT,
    XHS_POOL_CONFIG,
    XHS_RETRY_STATE_ROOT,
    XHS_RUNS_OUTPUT,
    XHS_RUNTIME_ROOT,
    XHS_TARGET_CONFIG,
    ensure_dir,
)
from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.xhs.accounts import (
    XhsAccountUnavailable,
    ensure_xhs_schema,
    get_account,
    record_event,
    register_account_slot,
    validate_account_id,
)
from trippostcollect.xhs.config import load_pool_config, load_target
from trippostcollect.xhs.discovery import (
    commit_child_discovery,
    resolve_discovery_plan,
)
from trippostcollect.xhs.leases import (
    AccountLeaseFileLock,
    LeaseGuard,
    SystemProcessInspector,
    XhsLeaseSignal,
    crawl_lease_budget,
)
from trippostcollect.xhs.runtime import (
    runtime_session_paths,
)


ROOT = PROJECT_ROOT
_ACTIVE_LEASE_GUARD: LeaseGuard | None = None
PLATFORM_SECURITY_LIMIT_300011 = "platform_security_limit_300011"
SECURITY_LIMIT_RETRY_SECONDS = 30 * 60
RETRY_STATE_SCHEMA_VERSION = 1
CHALLENGE_MARKERS = (
    "captcha",
    "安全验证",
    "请完成验证",
    "请通过验证",
    "操作频繁",
    "环境异常",
    "访问受限",
    "安全限制",
    "账号异常",
    "account exception",
    "300011",
    "platform_security_limit",
)
LOGIN_MARKERS = ("login_required", "扫码登录", "登录后查看")


@contextmanager
def guarded_runtime_session(
    run_id: str,
    guard: LeaseGuard,
) -> Iterator[dict[str, Path]]:
    """Create the one session that only ``LeaseGuard.close`` may remove."""

    if run_id != guard.run_id:
        raise ValueError("runtime session run_id does not match its lease")
    yield guard.prepare_runtime_session()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one independently orchestrated Xiaohongshu target.")
    parser.add_argument("--target-key", required=True)
    parser.add_argument(
        "--account-id",
        required=True,
        help="Logical checkpoint/lease slot; it does not retain a login profile.",
    )
    parser.add_argument(
        "--post-interaction",
        choices=("none", "comment-scroll", "like-one", "random"),
        default="none",
        help="Optional one-post visible interaction. Disabled unless explicitly requested.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--target-config", default=str(XHS_TARGET_CONFIG))
    parser.add_argument("--pool-config", default=str(XHS_POOL_CONFIG))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-import", action="store_true")
    parser.add_argument(
        "--retry-on-300011",
        action="store_true",
        help=(
            "Release each attempt lease, wait 30 minutes after a complete "
            "platform_security_limit_300011 terminal state, and retry until success."
        ),
    )
    args = parser.parse_args()
    if args.dry_run and args.retry_on_300011:
        parser.error("--retry-on-300011 cannot be combined with --dry-run")
    return args


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f%z")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _retry_state_path(args: argparse.Namespace) -> Path:
    account_id = validate_account_id(args.account_id)
    digest = hashlib.sha256(
        f"{account_id}\0{args.target_key}".encode("utf-8")
    ).hexdigest()[:16]
    return XHS_RETRY_STATE_ROOT / account_id / f"{digest}.json"


def _write_retry_state(path: Path, payload: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    document = {
        "schema_version": RETRY_STATE_SCHEMA_VERSION,
        **payload,
        "updated_at": utc_iso(),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _latest_terminal_xhs_run(
    db_path: Path,
    *,
    target_key: str,
    account_id: str,
) -> dict[str, Any] | None:
    if not db_path.is_file():
        return None
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        row = conn.execute(
            """
            SELECT run_id, status, started_at, finished_at, report_json
            FROM xhs_runs
            WHERE target_key=? AND account_id=?
              AND status IN ('completed', 'failed', 'blocked')
            ORDER BY started_at DESC, run_id DESC
            LIMIT 1
            """,
            (target_key, validate_account_id(account_id)),
        ).fetchone()
        release_row = None
        if row is not None:
            release_row = conn.execute(
                """
                SELECT details_json
                FROM xhs_account_events
                WHERE run_id=? AND account_id=? AND event_type='lease_released'
                ORDER BY id DESC
                LIMIT 1
                """,
                (str(row["run_id"]), validate_account_id(account_id)),
            ).fetchone()
    if row is None:
        return None
    try:
        report = json.loads(str(row["report_json"] or "{}"))
    except json.JSONDecodeError:
        report = {}
    if not isinstance(report, dict):
        report = {}
    release_verified = False
    if release_row is not None:
        try:
            release_details = json.loads(str(release_row["details_json"] or "{}"))
        except json.JSONDecodeError:
            release_details = {}
        process_check = release_details.get("process_check") or {}
        release_verified = bool(
            report.get("lease_id")
            and release_details.get("lease_id") == report.get("lease_id")
            and release_details.get("owner_token_sha256")
            and process_check.get("safe_to_release") is True
            and not process_check.get("blocking")
        )
    return {
        **report,
        "run_id": str(row["run_id"]),
        "status": str(row["status"]),
        "started_at": report.get("started_at") or row["started_at"],
        "finished_at": report.get("finished_at") or row["finished_at"],
        "lease_released": release_verified,
    }


def _security_limit_retry_evidence(report: dict[str, Any]) -> tuple[bool, str]:
    if report.get("status") != "failed":
        return False, "latest_terminal_run_not_failed"
    if report.get("challenge") != PLATFORM_SECURITY_LIMIT_300011:
        return False, "latest_terminal_run_not_300011"
    if report.get("lease_released") is not True:
        return False, "triggering_run_lease_not_released"
    if report.get("runtime_session_removed") is not True:
        return False, "triggering_run_runtime_session_not_removed"
    discovery = report.get("discovery") or {}
    if discovery.get("last_stop_reason") != "runtime_failed":
        return False, "triggering_run_discovery_not_runtime_failed"
    child_summary_path = Path(str(report.get("child_summary") or ""))
    if not child_summary_path.is_file():
        return False, "triggering_run_child_summary_missing"
    try:
        child_summary = json.loads(child_summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "triggering_run_child_summary_invalid"
    pagination = child_summary.get("pagination_evidence") or {}
    stop_event = pagination.get("stop_event") or {}
    if not bool(pagination.get("stopped")):
        return False, "triggering_run_stop_event_missing"
    if pagination.get("stop_reason") != "runtime_failed":
        return False, "triggering_run_stop_reason_mismatch"
    if pagination.get("stop_detail") != PLATFORM_SECURITY_LIMIT_300011:
        return False, "triggering_run_stop_detail_mismatch"
    if stop_event.get("batch_complete") is not False:
        return False, "triggering_run_incomplete_boundary_missing"
    return True, "complete_300011_terminal"


def _security_limit_retry_checkpoint_ready(
    db_path: Path,
    *,
    target_key: str,
    account_id: str,
    report: dict[str, Any],
) -> tuple[bool, str]:
    child_summary = Path(str(report.get("child_summary") or "")).resolve()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        active_lease = conn.execute(
            "SELECT 1 FROM xhs_account_leases WHERE account_id=?",
            (validate_account_id(account_id),),
        ).fetchone()
        checkpoint = conn.execute(
            """
            SELECT last_run_id, last_summary_path, last_batch_complete, last_stop_reason
            FROM xhs_discovery_checkpoints
            WHERE target_key=? AND account_id=? AND last_run_id=?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (target_key, validate_account_id(account_id), report.get("run_id")),
        ).fetchone()
    if active_lease is not None:
        return False, "account_lease_still_present"
    if checkpoint is None:
        return False, "triggering_run_checkpoint_missing"
    if int(checkpoint["last_batch_complete"]) != 0:
        return False, "triggering_run_checkpoint_not_incomplete"
    if checkpoint["last_stop_reason"] != "runtime_failed":
        return False, "triggering_run_checkpoint_stop_reason_mismatch"
    if not checkpoint["last_summary_path"]:
        return False, "triggering_run_checkpoint_summary_missing"
    if Path(str(checkpoint["last_summary_path"])).resolve() != child_summary:
        return False, "triggering_run_checkpoint_summary_mismatch"
    return True, "safe_checkpoint_ready"


def _security_limit_retry_due_at(report: dict[str, Any]) -> datetime | None:
    finished_at = _parse_utc(report.get("finished_at"))
    if finished_at is None:
        return None
    return finished_at + timedelta(seconds=SECURITY_LIMIT_RETRY_SECONDS)


def _terminal_release_ready(
    db_path: Path,
    *,
    account_id: str,
    report: dict[str, Any],
) -> tuple[bool, str]:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA query_only = ON")
        active_lease = conn.execute(
            "SELECT 1 FROM xhs_account_leases WHERE account_id=?",
            (validate_account_id(account_id),),
        ).fetchone()
    if active_lease is not None:
        return False, "account_lease_still_present"
    if report.get("lease_released") is not True:
        return False, "terminal_run_exact_release_missing"
    if report.get("runtime_session_removed") is not True:
        return False, "terminal_run_runtime_session_not_removed"
    return True, "terminal_exact_release_verified"


def _sleep_until(
    due_at: datetime,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    while True:
        remaining = (due_at - now()).total_seconds()
        if remaining <= 0:
            return
        sleep(min(60.0, remaining))


def _record_retry_event(
    db_path: Path,
    *,
    args: argparse.Namespace,
    event_type: str,
    details: dict[str, Any],
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_xhs_schema(conn)
        record_event(
            conn,
            account_id=args.account_id,
            run_id=details.get("run_id"),
            event_type=event_type,
            details=details,
        )
        conn.commit()


def tail(value: str, limit: int = 6000) -> str:
    return value[-limit:] if len(value) > limit else value


def _eligible_account_for_plan(
    conn: sqlite3.Connection,
    requested: str,
    *,
    check_lease: bool = True,
) -> dict[str, Any]:
    account_id = validate_account_id(requested)
    account = get_account(conn, account_id) or register_account_slot(conn, account_id)
    if account["status"] != "active":
        raise XhsAccountUnavailable("requested_xhs_account_not_active")
    if check_lease:
        active_lease = conn.execute(
            "SELECT expires_at FROM xhs_account_leases WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if active_lease:
            raise XhsAccountUnavailable("requested_xhs_account_busy")
    return account


def build_child_command(
    *,
    target: dict[str, Any],
    pool: dict[str, Any],
    account: dict[str, Any],
    profile_dir: Path,
    db_path: Path,
    output_root: Path,
    no_import: bool,
    post_interaction: str,
    discovery: dict[str, Any],
) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "mediacrawler_crawl.py"),
        "--platforms",
        "xhs",
        "--keyword",
        str(target["keyword"]),
        "--output-dir",
        str(output_root),
        "--timeout-per-platform",
        str(int(target["timeout_seconds"])),
        "--start-page",
        str(int(discovery["resume_page"])),
        "--top-refresh-max-pages",
        str(int(discovery["top_refresh_max_pages"])),
        "--required-fields-profile",
        str(target["required_fields_profile"]),
        "--behavior-profile",
        str(pool["behavior_profile"]),
        "--login-type",
        "qrcode",
        "--db",
        str(db_path),
        "--xhs-account-id",
        str(account["account_id"]),
        "--xhs-discovery-target-key",
        str(discovery["target_key"]),
        "--xhs-discovery-query-fingerprint",
        str(discovery["query_fingerprint"]),
        "--xhs-profile-dir",
        str(profile_dir),
        "--xhs-post-interaction",
        post_interaction,
    ]
    if discovery.get("resume_search_id"):
        command.extend(["--start-cursor", str(discovery["resume_search_id"])])
    if discovery.get("campaign_summary_path"):
        command.extend(["--resume-summary", str(discovery["campaign_summary_path"])])
    if discovery.get("source_exhausted"):
        command.append("--discovery-source-exhausted")
    command.append("--download-images")
    command.extend(["--media-root", str(LOCAL_MEDIA_ROOT.resolve())])
    if pool.get("headed", True):
        command.append("--headed")
    if no_import:
        command.append("--no-import")
    return command


def _structured_failure_records(stdout: str, child_summary: dict[str, Any]) -> list[dict[str, Any]]:
    records = child_summary.get("records") or []
    if not records:
        records = extract_stdout_json(stdout).get("records") or []
    return [record for record in records if isinstance(record, dict)]


def _structured_failure_text(record: dict[str, Any]) -> str:
    classification = record.get("failure_classification") or {}
    behavior = record.get("behavior_evidence") or {}
    values = [
        classification.get("status"),
        classification.get("failure_type"),
        classification.get("reason"),
        behavior.get("status"),
        behavior.get("challenge"),
        behavior.get("error"),
        behavior.get("reason"),
    ]
    return "\n".join(str(value) for value in values if value).lower()


def _challenge_reason(stdout: str, stderr: str, child_summary: dict[str, Any]) -> str:
    records = _structured_failure_records(stdout, child_summary)
    for record in records[-1:]:
        behavior = record.get("behavior_evidence") or {}
        markers = {
            **(behavior.get("initial_visible_markers") or {}),
            **(behavior.get("visible_markers") or {}),
        }
        if bool(markers.get("platform_security_limit")):
            return "platform_security_limit_300011"
        if bool(markers.get("captcha_or_verify")) or bool(markers.get("captcha")):
            return "captcha"
        if bool(markers.get("rate_limited")):
            return "操作频繁"
        if bool(markers.get("blocked")):
            return "访问受限"
        failure_text = _structured_failure_text(record)
        if "platform_security_limit" in failure_text:
            return "platform_security_limit_300011"
        reason = next((marker for marker in CHALLENGE_MARKERS if marker.lower() in failure_text), "")
        if reason:
            return reason
    if records:
        return ""
    combined = f"{tail(stdout, 3000)}\n{tail(stderr, 3000)}".lower()
    return next((marker for marker in CHALLENGE_MARKERS if marker.lower() in combined), "")


def _login_reason(stdout: str, stderr: str, child_summary: dict[str, Any]) -> str:
    records = _structured_failure_records(stdout, child_summary)
    for record in records[-1:]:
        behavior = record.get("behavior_evidence") or {}
        markers = {
            **(behavior.get("initial_visible_markers") or {}),
            **(behavior.get("visible_markers") or {}),
        }
        if bool(markers.get("login_required")):
            return "login_required"
        failure_text = _structured_failure_text(record)
        reason = next((marker for marker in LOGIN_MARKERS if marker.lower() in failure_text), "")
        if reason:
            return reason
    if records:
        return ""
    combined = f"{tail(stdout, 3000)}\n{tail(stderr, 3000)}".lower()
    return next((marker for marker in LOGIN_MARKERS if marker.lower() in combined), "")


def write_summary(run_dir: Path, summary: dict[str, Any]) -> Path:
    path = run_dir / "run_summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def lease_cleanup_evidence(
    db_path: Path,
    *,
    account_id: str,
    run_id: str,
    lease_id: str,
) -> dict[str, Any]:
    """Return the exact release/defer audit written by ``LeaseGuard.close``."""

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT event_type, details_json
            FROM xhs_account_events
            WHERE account_id=? AND run_id=?
              AND event_type IN (
                'lease_released',
                'lease_release_deferred_live_processes',
                'lease_release_deferred_runtime_session_cleanup'
              )
            ORDER BY id DESC
            LIMIT 1
            """,
            (validate_account_id(account_id), run_id),
        ).fetchone()
    if row is None:
        return {
            "ok": False,
            "event_type": "missing",
            "reason": "lease_cleanup_event_missing",
        }
    try:
        details = json.loads(str(row["details_json"] or "{}"))
    except json.JSONDecodeError:
        details = {}
    if not isinstance(details, dict):
        details = {}
    event_type = str(row["event_type"])
    return {
        "ok": bool(
            event_type == "lease_released"
            and details.get("lease_id") == lease_id
            and (details.get("process_check") or {}).get("safe_to_release") is True
            and not (details.get("process_check") or {}).get("blocking")
            and details.get("runtime_session_removed") is True
        ),
        "event_type": event_type,
        **details,
    }


def load_child_summary(path_value: str) -> dict[str, Any]:
    path = Path(path_value).expanduser() if path_value else None
    if path is None or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def frozen_discovery_inputs(discovery: dict[str, Any]) -> list[Path]:
    summary_value = str(discovery.get("campaign_summary_path") or "")
    if not summary_value:
        return []
    summary_path = Path(summary_value).expanduser().resolve()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    inputs = [summary_path]
    for record in summary.get("records") or []:
        output = record.get("output") if isinstance(record, dict) else {}
        for path_value in (output or {}).get("jsonl_files") or []:
            inputs.append(Path(path_value).expanduser().resolve())
    return inputs


def fail_open_step(state: FrozenExecutionState, *, error: str, evidence: dict[str, Any] | None = None) -> None:
    try:
        payload = state.load()
        if payload.get("status") in {"completed", "failed"}:
            return
        for step in FORMAL_STEPS[1:]:
            if (payload.get("steps") or {}).get(step, {}).get("status") in {"pending", "in_progress"}:
                state.fail(step, error=error, evidence=evidence)
                return
    except Exception:
        return


def upsert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    target_key: str,
    account_id: str | None,
    status: str,
    state_path: Path,
    child_summary_path: str | None = None,
    report: dict[str, Any] | None = None,
    finished: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO xhs_runs(
            run_id, target_key, account_id, status, started_at, finished_at,
            execution_state_path, child_summary_path, report_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            status=excluded.status,
            finished_at=excluded.finished_at,
            child_summary_path=excluded.child_summary_path,
            report_json=excluded.report_json
        """,
        (
            run_id,
            target_key,
            account_id,
            status,
            utc_iso(),
            utc_iso() if finished else None,
            str(state_path),
            child_summary_path,
            json.dumps(report or {}, ensure_ascii=False),
        ),
    )
    conn.commit()


def _run_main(args: argparse.Namespace | None = None) -> int:
    global _ACTIVE_LEASE_GUARD
    args = args or parse_args()
    target = load_target(args.target_key, args.target_config)
    pool = load_pool_config(args.pool_config)
    try:
        lease_budget = crawl_lease_budget(
            timeout_seconds=int(target["timeout_seconds"]),
            configured_lease_seconds=int(pool["lease_seconds"]),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    db_path = Path(args.db).expanduser().resolve()
    bootstrap_database(db_path, sync_jobs=False)
    run_id = utc_stamp()
    run_dir = ensure_dir(XHS_RUNTIME_ROOT / "runs" / run_id)
    state_path = ensure_dir(XHS_EXECUTION_STATE_ROOT / run_id) / f"{args.target_key}.json"
    session_paths = runtime_session_paths(run_id)

    discovery_plan: dict[str, Any]
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_xhs_schema(conn)
        account = _eligible_account_for_plan(
            conn,
            args.account_id,
            check_lease=args.dry_run,
        )
        if not args.dry_run:
            guard = LeaseGuard(
                db_path=db_path,
                account_id=args.account_id,
                run_id=run_id,
                lease_kind="crawl",
                execution_state_path=state_path,
                runtime_profile_dir=session_paths["profile"],
                budget=lease_budget,
            )
            _ACTIVE_LEASE_GUARD = guard
            try:
                account = guard.acquire()
            except XhsAccountUnavailable as exc:
                blocked_plan = {
                    "run_id": run_id,
                    "target_key": args.target_key,
                    "account_id": args.account_id,
                    "keyword": target["keyword"],
                    "completion_policy": "source_exhausted",
                    "behavior_profile": pool["behavior_profile"],
                    "local_image_storage_required": True,
                    "media_root": str(LOCAL_MEDIA_ROOT.resolve()),
                    "blocked_before_lease": True,
                    "reason": exc.reason,
                    "wait_seconds": exc.wait_seconds,
                }
                state = FrozenExecutionState.create(
                    state_path,
                    run_id=run_id,
                    job_key=args.target_key,
                    site_key="xhs",
                    job_kind="xhs_account_search",
                    plan=blocked_plan,
                    frozen_inputs=[Path(target["path"]), Path(pool["path"]), FORMAL_CRAWL_CONTRACT],
                    dry_run=False,
                )
                state.fail(
                    "command_executed",
                    error=f"xhs_pool_blocked:{exc.reason}",
                    evidence={"wait_seconds": exc.wait_seconds},
                )
                known_account = get_account(conn, args.account_id) if args.account_id else None
                summary = {
                    "status": "blocked",
                    "run_id": run_id,
                    "target_key": args.target_key,
                    "account_id": known_account["account_id"] if known_account else None,
                    "execution_state": str(state_path),
                    "reason": exc.reason,
                    "wait_seconds": exc.wait_seconds,
                    "finished_at": utc_iso(),
                }
                summary_path = write_summary(run_dir, summary)
                upsert_run(
                    conn,
                    run_id=run_id,
                    target_key=args.target_key,
                    account_id=summary["account_id"],
                    status="blocked",
                    state_path=state_path,
                    report=summary,
                    finished=True,
                )
                print(json.dumps({**summary, "summary": str(summary_path)}, ensure_ascii=False, indent=2))
                return 2
        discovery_plan = resolve_discovery_plan(
            conn,
            target=target,
            account_id=str(account["account_id"]),
        )

    plan = {
        "run_id": run_id,
        "target_key": args.target_key,
        "account_id": account["account_id"],
        "login_mode": "per_run_qrcode",
        "session_retention": "none",
        "runtime_profile_dir": str(session_paths["profile"]),
        "keyword": target["keyword"],
        "completion_policy": "source_exhausted",
        "timeout_seconds": target["timeout_seconds"],
        "lease_seconds": lease_budget.lease_seconds,
        "configured_lease_ceiling_seconds": pool["lease_seconds"],
        "lease_budget": lease_budget.public(),
        "behavior_profile": pool["behavior_profile"],
        "local_image_storage_required": True,
        "media_root": str(LOCAL_MEDIA_ROOT.resolve()),
        "headed": pool["headed"],
        "post_interaction": args.post_interaction,
        "no_import": args.no_import,
        "preflight": {
            "account_status": account["status"],
            "active_lease": False,
            "persistent_account_profile": False,
            "persistent_login_state": False,
            "lease_covers_timeout_cleanup": True,
        },
        "discovery": discovery_plan,
    }
    state: FrozenExecutionState | None = None
    try:
        state = FrozenExecutionState.create(
            state_path,
            run_id=run_id,
            job_key=args.target_key,
            site_key="xhs",
            job_kind="xhs_account_search",
            plan=plan,
            frozen_inputs=[
                Path(target["path"]),
                Path(pool["path"]),
                FORMAL_CRAWL_CONTRACT,
                *frozen_discovery_inputs(discovery_plan),
            ],
            dry_run=args.dry_run,
        )
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_xhs_schema(conn)
            upsert_run(
                conn,
                run_id=run_id,
                target_key=args.target_key,
                account_id=account["account_id"],
                status="planned" if args.dry_run else "running",
                state_path=state_path,
            )
    except Exception as exc:
        if state is not None and not args.dry_run:
            fail_open_step(state, error=f"xhs_run_initialization_failed:{type(exc).__name__}")
        raise

    if args.dry_run:
        summary = {"status": "planned", "dry_run": True, "run_id": run_id, "plan": plan, "state": str(state_path)}
        summary_path = write_summary(run_dir, summary)
        print(json.dumps({**summary, "summary": str(summary_path)}, ensure_ascii=False, indent=2))
        return 0

    outcome = "failed"
    child_summary: dict[str, Any] = {}
    child_summary_path = ""
    stdout = ""
    stderr = ""
    exit_code = 1
    interrupt: dict[str, Any] | None = None
    discovery_commit: dict[str, Any] = {"skipped": True, "reason": "child_not_started"}
    try:
        with guarded_runtime_session(run_id, guard) as runtime_session:
            command = build_child_command(
                target=target,
                pool=pool,
                account=account,
                profile_dir=runtime_session["profile"],
                db_path=db_path,
                output_root=ensure_dir(XHS_RUNS_OUTPUT / run_id),
                no_import=args.no_import,
                post_interaction=args.post_interaction,
                discovery=discovery_plan,
            )
            state.begin("command_executed")
            env = os.environ.copy()
            env["TRIPPOSTCOLLECT_EXECUTION_STATE_PATH"] = str(state_path)
            env["TRIPPOSTCOLLECT_XHS_RUN_ID"] = run_id
            completed = guard.run_subprocess(
                command,
                cwd=ROOT,
                env=env,
                timeout_seconds=int(target["timeout_seconds"]),
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
            stdout_json = extract_stdout_json(stdout)
            child_summary_path = str(stdout_json.get("summary") or "")
            child_summary = load_child_summary(child_summary_path)
            child_import_result = child_summary.get("import_result") or {}
            if args.no_import:
                discovery_commit = {"skipped": True, "reason": "no_import"}
            elif child_import_result.get("reason") == "sqlite_import_failed":
                discovery_commit = {"skipped": True, "reason": "sqlite_import_failed"}
            elif child_summary_path and child_summary:
                try:
                    discovery_image_artifacts = verify_image_artifacts(
                        child_summary,
                        project_root=ROOT,
                        expect_promotion=True,
                    )
                    discovery_image_persistence = verify_image_persistence(
                        child_summary,
                        db_path,
                        project_root=ROOT,
                        media_root=LOCAL_MEDIA_ROOT,
                    )
                    with sqlite3.connect(db_path) as conn:
                        conn.row_factory = sqlite3.Row
                        ensure_xhs_schema(conn)
                        discovery_commit = commit_child_discovery(
                            conn,
                            target=target,
                            account_id=str(account["account_id"]),
                            run_id=run_id,
                            discovery_plan=discovery_plan,
                            child_summary_path=child_summary_path,
                            child_summary=child_summary,
                            imported_completion_verified=bool(
                                discovery_image_artifacts["ok"]
                                and discovery_image_persistence["ok"]
                            ),
                        )
                except (OSError, sqlite3.Error, TypeError, ValueError, RuntimeError) as exc:
                    discovery_commit = {
                        "skipped": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    stderr = (
                        f"{stderr}\nXHS discovery checkpoint write failed: {type(exc).__name__}: {exc}"
                    ).strip()
                    exit_code = 2
            else:
                discovery_commit = {"skipped": True, "reason": "child_summary_missing"}
            if exit_code != 0:
                state.fail(
                    "command_executed",
                    error=f"xhs_child_exit_{exit_code}",
                    evidence={"command": command, "stdout_tail": tail(stdout), "stderr_tail": tail(stderr)},
                )
            else:
                state.complete("command_executed", evidence={"command": command, "exit_code": exit_code})
                state.begin("artifacts_verified")
                if not child_summary_path or not Path(child_summary_path).is_file():
                    state.fail("artifacts_verified", error="xhs_child_summary_missing")
                else:
                    image_artifacts = verify_image_artifacts(
                        child_summary,
                        project_root=ROOT,
                        expect_promotion=not args.no_import,
                    )
                    artifact_evidence = {
                        "summary_path": child_summary_path,
                        "local_images": image_artifacts,
                    }
                    if image_artifacts["ok"]:
                        state.complete("artifacts_verified", evidence=artifact_evidence)
                    else:
                        state.fail(
                            "artifacts_verified",
                            error="xhs_local_image_artifacts_incomplete",
                            evidence=artifact_evidence,
                        )
                if state.load()["steps"]["artifacts_verified"]["status"] == "completed":
                    state.begin("persistence_verified")
                    import_result = dict(child_summary.get("import_result") or {})
                    import_completion_ok = bool(child_summary.get("import_completion_met"))
                    if args.no_import:
                        state.complete("persistence_verified", evidence=import_result, skipped=True)
                    else:
                        image_persistence = verify_image_persistence(
                            child_summary,
                            db_path,
                            project_root=ROOT,
                            media_root=LOCAL_MEDIA_ROOT,
                        )
                        import_result["local_images"] = image_persistence
                        persistence_ok = bool(
                            import_completion_ok and image_persistence["ok"]
                        )
                        if persistence_ok:
                            state.complete("persistence_verified", evidence=import_result)
                        else:
                            state.fail(
                                "persistence_verified",
                                error="xhs_completion_not_persisted",
                                evidence=import_result,
                            )
                    if state.load()["steps"]["persistence_verified"]["status"] in {"completed", "skipped"}:
                        state.finalize(
                            outcome="completed",
                            evidence={
                                "account_id": account["account_id"],
                                "summary_path": child_summary_path,
                                "import_result": import_result,
                            },
                        )
                        outcome = "completed"
    except BaseException as exc:
        if isinstance(exc, (XhsLeaseSignal, KeyboardInterrupt)):
            signum = (
                int(exc.signum)
                if isinstance(exc, XhsLeaseSignal)
                else int(signal.SIGINT)
            )
            signal_name = signal.Signals(signum).name
            exit_code = 128 + signum
            guard.signal_received = signum
            interrupt = {
                "reason": "operator_interrupt",
                "source": (
                    "lease_signal"
                    if isinstance(exc, XhsLeaseSignal)
                    else "keyboard_interrupt"
                ),
                "signum": signum,
                "signal": signal_name,
                "exit_code": exit_code,
            }
            discovery_commit = {
                **discovery_commit,
                "skipped": True,
                "reason": "operator_interrupt",
                "interrupt": interrupt,
            }
            failure_error = f"xhs_runtime_failed:operator_interrupt:{signal_name}"
            stderr = f"{stderr}\n{failure_error}".strip()
            fail_open_step(
                state,
                error=failure_error,
                evidence={
                    "interrupt": interrupt,
                    "stderr_tail": tail(stderr),
                },
            )
        else:
            stderr = f"{stderr}\n{type(exc).__name__}: {exc}".strip()
            fail_open_step(
                state,
                error=f"xhs_runner_exception:{type(exc).__name__}",
                evidence={"stderr_tail": tail(stderr)},
            )

    challenge = _challenge_reason(stdout, stderr, child_summary)
    login_reason = _login_reason(stdout, stderr, child_summary)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_xhs_schema(conn)
        if challenge:
            record_event(
                conn,
                account_id=account["account_id"],
                run_id=run_id,
                event_type="challenge_detected",
                details={"reason": f"xhs_challenge:{challenge}"},
            )
        elif login_reason:
            record_event(
                conn,
                account_id=account["account_id"],
                run_id=run_id,
                event_type="run_scoped_login_failed",
                details={"reason": f"xhs_login:{login_reason}"},
            )
        record_event(
            conn,
            account_id=account["account_id"],
            run_id=run_id,
            event_type="formal_run_finished",
            details={
                "outcome": outcome,
                "exit_code": exit_code,
                "challenge": challenge,
                "login_reason": login_reason,
                "failure_type": "runtime_failed" if interrupt else "",
                "stop_reason": "runtime_failed" if interrupt else "",
                "reason": "operator_interrupt" if interrupt else "",
                "interrupt": interrupt,
            },
        )
        conn.commit()
    guard.set_outcome(outcome)

    summary = {
        "status": outcome,
        "run_id": run_id,
        "target_key": args.target_key,
        "account_id": account["account_id"],
        "lease_id": guard.lease_id,
        "lease_budget": lease_budget.public(),
        "execution_state": str(state_path),
        "child_summary": child_summary_path,
        "discovery": discovery_commit,
        "exit_code": exit_code,
        "challenge": challenge,
        "login_reason": login_reason,
        "login_mode": "per_run_qrcode",
        "persistent_account_profile": False,
        "runtime_session_removed": not session_paths["root"].exists(),
        "failure_type": "runtime_failed" if interrupt else "",
        "stop_reason": "runtime_failed" if interrupt else "",
        "reason": "operator_interrupt" if interrupt else "",
        "interrupt": interrupt,
        "post_interaction": {
            "requested_mode": args.post_interaction,
            "ok": (
                ((child_summary.get("behavior_validation") or {}).get("platforms") or {})
                .get("xhs", {})
                .get("post_interaction_ok", args.post_interaction == "none")
            ),
            "events": (
                ((child_summary.get("behavior_validation") or {}).get("platforms") or {})
                .get("xhs", {})
                .get("post_interactions", [])
            )[-1:],
        },
        "completion_mode": "source-exhausted",
        "import_result": child_summary.get("import_result") or {},
        "stdout_tail": tail(stdout),
        "stderr_tail": tail(stderr),
        "finished_at": utc_iso(),
    }
    summary_path = write_summary(run_dir, summary)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_xhs_schema(conn)
        upsert_run(
            conn,
            run_id=run_id,
            target_key=args.target_key,
            account_id=account["account_id"],
            status="completed" if outcome == "completed" else "failed",
            state_path=state_path,
            child_summary_path=child_summary_path or None,
            report=summary,
            finished=True,
        )
    lease_released = guard.close()
    runtime_session_removed = guard.runtime_session_removed
    cleanup_evidence = lease_cleanup_evidence(
        db_path,
        account_id=str(account["account_id"]),
        run_id=run_id,
        lease_id=guard.lease_id,
    )
    summary["lease_released"] = lease_released
    summary["runtime_session_removed"] = runtime_session_removed
    if not lease_released:
        summary["status"] = "failed"
        cleanup_error = (
            "lease_release_deferred_live_processes"
            if cleanup_evidence.get("event_type")
            == "lease_release_deferred_live_processes"
            else "runtime_session_cleanup_failed"
        )
        if not interrupt:
            summary["reason"] = cleanup_error
        summary["cleanup_error"] = cleanup_error
    summary["lease_cleanup"] = cleanup_evidence
    write_summary(run_dir, summary)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_xhs_schema(conn)
        upsert_run(
            conn,
            run_id=run_id,
            target_key=args.target_key,
            account_id=account["account_id"],
            status="completed" if summary["status"] == "completed" else "failed",
            state_path=state_path,
            child_summary_path=child_summary_path or None,
            report=summary,
            finished=True,
        )
    print(json.dumps({**summary, "summary": str(summary_path)}, ensure_ascii=False, indent=2))
    if (
        summary.get("lease_released") is not True
        or summary.get("runtime_session_removed") is not True
    ):
        return 2
    if interrupt:
        return int(interrupt["exit_code"])
    return 0 if summary["status"] == "completed" else 2


def _run_security_limit_retry_controller(args: argparse.Namespace) -> int:
    global _ACTIVE_LEASE_GUARD
    db_path = Path(args.db).expanduser().resolve()
    bootstrap_database(db_path, sync_jobs=False)
    state_path = _retry_state_path(args)
    controller_lock = AccountLeaseFileLock(state_path.with_suffix(".lock"))
    try:
        controller_lock.acquire()
    except XhsAccountUnavailable as exc:
        raise SystemExit(
            "XHS 300011 retry controller is already active for "
            f"account={args.account_id} target={args.target_key}: {exc.reason}"
        ) from exc

    controller = SystemProcessInspector().current_identity().public()
    base_state = {
        "target_key": args.target_key,
        "account_id": validate_account_id(args.account_id),
        "completion_policy": "source_exhausted",
        "retry_reason": PLATFORM_SECURITY_LIMIT_300011,
        "retry_interval_seconds": SECURITY_LIMIT_RETRY_SECONDS,
        "controller": controller,
    }
    attempt_count = 0
    scheduled_run_id = ""

    def update_state(status: str, **details: Any) -> None:
        _write_retry_state(
            state_path,
            {
                **base_state,
                "status": status,
                "attempt_count": attempt_count,
                **details,
            },
        )

    def stop_for_other_outcome(
        *,
        report: dict[str, Any] | None,
        reason: str,
        exit_code: int = 2,
    ) -> int:
        run_id = str((report or {}).get("run_id") or "")
        update_state(
            "stopped",
            last_run_id=run_id,
            last_challenge=(report or {}).get("challenge") or "",
            stop_reason=reason,
            next_retry_at=None,
        )
        _record_retry_event(
            db_path,
            args=args,
            event_type="security_limit_retry_stopped",
            details={
                "run_id": run_id or None,
                "reason": reason,
                "attempt_count": attempt_count,
            },
        )
        return exit_code

    try:
        while True:
            latest = _latest_terminal_xhs_run(
                db_path,
                target_key=args.target_key,
                account_id=args.account_id,
            )
            if latest and latest.get("status") == "completed":
                release_ok, release_reason = _terminal_release_ready(
                    db_path,
                    account_id=args.account_id,
                    report=latest,
                )
                if release_reason == "account_lease_still_present":
                    update_state(
                        "waiting_for_account",
                        last_run_id=latest.get("run_id"),
                        last_challenge="",
                        stop_reason=release_reason,
                        next_retry_at=None,
                    )
                    time.sleep(60)
                    continue
                if not release_ok:
                    return stop_for_other_outcome(
                        report=latest,
                        reason=release_reason,
                    )
                run_id = str(latest.get("run_id") or "")
                update_state(
                    "completed",
                    last_run_id=run_id,
                    last_challenge="",
                    stop_reason="formal_run_completed",
                    next_retry_at=None,
                )
                _record_retry_event(
                    db_path,
                    args=args,
                    event_type="security_limit_retry_completed",
                    details={
                        "run_id": run_id,
                        "attempt_count": attempt_count,
                    },
                )
                return 0

            evidence_ok = False
            evidence_reason = "no_prior_terminal_run"
            checkpoint_ok = False
            checkpoint_reason = "no_prior_terminal_run"
            if latest:
                evidence_ok, evidence_reason = _security_limit_retry_evidence(latest)
                if evidence_ok:
                    checkpoint_ok, checkpoint_reason = _security_limit_retry_checkpoint_ready(
                        db_path,
                        target_key=args.target_key,
                        account_id=args.account_id,
                        report=latest,
                    )

            if evidence_ok and checkpoint_reason == "account_lease_still_present":
                update_state(
                    "waiting_for_account",
                    last_run_id=latest.get("run_id"),
                    last_challenge=latest.get("challenge"),
                    stop_reason=checkpoint_reason,
                    next_retry_at=None,
                )
                time.sleep(60)
                continue

            if evidence_ok and not checkpoint_ok:
                return stop_for_other_outcome(
                    report=latest,
                    reason=checkpoint_reason,
                )

            if evidence_ok and checkpoint_ok:
                due_at = _security_limit_retry_due_at(latest)
                if due_at is None:
                    return stop_for_other_outcome(
                        report=latest,
                        reason="triggering_run_finished_at_invalid",
                    )
                current = datetime.now(timezone.utc)
                if due_at > current:
                    run_id = str(latest.get("run_id") or "")
                    update_state(
                        "waiting",
                        last_run_id=run_id,
                        last_challenge=latest.get("challenge"),
                        stop_reason="scheduled_after_300011",
                        next_retry_at=due_at.isoformat(timespec="seconds"),
                    )
                    if scheduled_run_id != run_id:
                        scheduled_run_id = run_id
                        _record_retry_event(
                            db_path,
                            args=args,
                            event_type="security_limit_retry_scheduled",
                            details={
                                "run_id": run_id,
                                "retry_interval_seconds": SECURITY_LIMIT_RETRY_SECONDS,
                                "next_retry_at": due_at.isoformat(timespec="seconds"),
                                "controller": controller,
                            },
                        )
                        print(
                            json.dumps(
                                {
                                    "status": "waiting",
                                    "reason": PLATFORM_SECURITY_LIMIT_300011,
                                    "triggering_run_id": run_id,
                                    "next_retry_at": due_at.isoformat(timespec="seconds"),
                                    "retry_state": str(state_path),
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                    _sleep_until(due_at)
                    continue

            if attempt_count and not evidence_ok:
                return stop_for_other_outcome(
                    report=latest,
                    reason=evidence_reason,
                )

            triggering_run_id = str((latest or {}).get("run_id") or "")
            attempt_count += 1
            update_state(
                "running",
                last_run_id=triggering_run_id,
                last_challenge=(latest or {}).get("challenge") or "",
                stop_reason="retry_attempt_running",
                next_retry_at=None,
            )
            _record_retry_event(
                db_path,
                args=args,
                event_type="security_limit_retry_attempt_started",
                details={
                    "run_id": triggering_run_id or None,
                    "attempt_count": attempt_count,
                    "controller": controller,
                },
            )
            exit_code = _run_main(args)
            current = _latest_terminal_xhs_run(
                db_path,
                target_key=args.target_key,
                account_id=args.account_id,
            )
            if current and current.get("lease_released") is True:
                _ACTIVE_LEASE_GUARD = None
            if current is None or current.get("run_id") == triggering_run_id:
                return stop_for_other_outcome(
                    report=current,
                    reason="retry_attempt_terminal_report_missing",
                    exit_code=exit_code or 2,
                )
            if exit_code == 0 and current.get("status") == "completed":
                continue
            current_evidence_ok, current_reason = _security_limit_retry_evidence(current)
            if exit_code == 2 and current_evidence_ok:
                current_checkpoint_ok, current_checkpoint_reason = (
                    _security_limit_retry_checkpoint_ready(
                        db_path,
                        target_key=args.target_key,
                        account_id=args.account_id,
                        report=current,
                    )
                )
                if current_checkpoint_ok:
                    continue
                current_reason = current_checkpoint_reason
            return stop_for_other_outcome(
                report=current,
                reason=current_reason,
                exit_code=exit_code or 2,
            )
    finally:
        controller_lock.release()


def main() -> int:
    global _ACTIVE_LEASE_GUARD
    _ACTIVE_LEASE_GUARD = None
    code = 2
    try:
        try:
            args = parse_args()
            if args.retry_on_300011:
                code = _run_security_limit_retry_controller(args)
            else:
                code = _run_main(args)
        except XhsLeaseSignal as exc:
            code = 128 + exc.signum
        except KeyboardInterrupt:
            code = 130
    finally:
        guard = _ACTIVE_LEASE_GUARD
        _ACTIVE_LEASE_GUARD = None
        released = guard is None or guard.close()
        if not released:
            code = 2
    return code


if __name__ == "__main__":
    raise SystemExit(main())
