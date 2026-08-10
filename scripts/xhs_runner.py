#!/usr/bin/env python3
"""Independent formal Xiaohongshu runner with isolated account leasing."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    XHS_RUNS_OUTPUT,
    XHS_RUNTIME_ROOT,
    XHS_TARGET_CONFIG,
    ensure_dir,
)
from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.xhs.accounts import (
    XhsAccountUnavailable,
    acquire_account_lease,
    ensure_xhs_schema,
    get_account,
    record_event,
    release_account_lease,
    set_account_status,
    validate_account_id,
)
from trippostcollect.xhs.config import load_pool_config, load_target
from trippostcollect.xhs.discovery import (
    commit_child_discovery,
    resolve_discovery_plan,
)
from trippostcollect.xhs.sessions import (
    encrypt_storage_state,
    load_snapshot_key,
    materialized_storage_state,
    snapshot_sha256,
)


ROOT = PROJECT_ROOT
CHALLENGE_MARKERS = ("captcha", "安全验证", "请完成验证", "请通过验证", "操作频繁", "环境异常", "访问受限")
LOGIN_MARKERS = ("login_required", "扫码登录", "登录后查看", "missing_xhs_storage_state")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one independently orchestrated Xiaohongshu target.")
    parser.add_argument("--target-key", required=True)
    parser.add_argument("--account-id", required=True, help="Explicit active account selected by the operator.")
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
        "--completion-mode",
        choices=("target-new-posts", "source-exhausted"),
        default="target-new-posts",
        help=(
            "Runtime-only completion gate. source-exhausted ignores quantity and "
            "stagnation stops and imports only after explicit source exhaustion."
        ),
    )
    return parser.parse_args()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f%z")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tail(value: str, limit: int = 6000) -> str:
    return value[-limit:] if len(value) > limit else value


def _eligible_account_for_plan(conn: sqlite3.Connection, requested: str) -> dict[str, Any]:
    account_id = validate_account_id(requested)
    account = get_account(conn, account_id)
    if not account or account["status"] != "active":
        raise XhsAccountUnavailable("requested_xhs_account_not_active")
    active_lease = conn.execute(
        "SELECT expires_at FROM xhs_account_leases WHERE account_id=? AND expires_at>?",
        (account_id, utc_iso()),
    ).fetchone()
    if active_lease:
        raise XhsAccountUnavailable("requested_xhs_account_busy")
    return account


def build_child_command(
    *,
    target: dict[str, Any],
    pool: dict[str, Any],
    account: dict[str, Any],
    storage_state: Path,
    db_path: Path,
    output_root: Path,
    no_import: bool,
    post_interaction: str,
    discovery: dict[str, Any],
    completion_mode: str = "target-new-posts",
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
        "--candidate-hard-limit",
        str(int(target["candidate_hard_limit"])),
        "--target-new-posts",
        str(int(target["target_new_posts"])),
        "--completion-mode",
        completion_mode,
        "--max-stagnant-batches",
        str(int(target["max_stagnant_batches"])),
        "--start-page",
        str(int(discovery["resume_page"])),
        "--top-refresh-max-pages",
        str(int(discovery["top_refresh_max_pages"])),
        "--required-fields-profile",
        str(target["required_fields_profile"]),
        "--behavior-profile",
        str(pool["behavior_profile"]),
        "--login-type",
        "cookie",
        "--db",
        str(db_path),
        "--xhs-account-id",
        str(account["account_id"]),
        "--xhs-discovery-target-key",
        str(discovery["target_key"]),
        "--xhs-discovery-query-fingerprint",
        str(discovery["query_fingerprint"]),
        "--xhs-profile-dir",
        str(account["profile_dir"]),
        "--xhs-storage-state",
        str(storage_state),
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
    for record in records:
        behavior = record.get("behavior_evidence") or {}
        markers = {
            **(behavior.get("initial_visible_markers") or {}),
            **(behavior.get("visible_markers") or {}),
        }
        if bool(markers.get("captcha_or_verify")) or bool(markers.get("captcha")):
            return "captcha"
        if bool(markers.get("rate_limited")):
            return "操作频繁"
        if bool(markers.get("blocked")):
            return "访问受限"
        failure_text = _structured_failure_text(record)
        reason = next((marker for marker in CHALLENGE_MARKERS if marker.lower() in failure_text), "")
        if reason:
            return reason
    if records:
        return ""
    combined = f"{tail(stdout, 3000)}\n{tail(stderr, 3000)}".lower()
    return next((marker for marker in CHALLENGE_MARKERS if marker.lower() in combined), "")


def _login_reason(stdout: str, stderr: str, child_summary: dict[str, Any]) -> str:
    records = _structured_failure_records(stdout, child_summary)
    for record in records:
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
    """Fail the currently actionable step without masking the original exception."""
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


def record_preexecution_failure(
    *,
    args: argparse.Namespace,
    target: dict[str, Any],
    pool: dict[str, Any],
    account: dict[str, Any],
    db_path: Path,
    run_id: str,
    run_dir: Path,
    state_path: Path,
    reason: str,
    lease_acquired: bool,
    login_required: bool,
) -> int:
    plan = {
        "run_id": run_id,
        "target_key": args.target_key,
        "account_id": account["account_id"],
        "profile_dir": account["profile_dir"],
        "encrypted_state_path": account["encrypted_state_path"],
        "keyword": target["keyword"],
        "target_new_posts": target["target_new_posts"],
        "candidate_hard_limit": target["candidate_hard_limit"],
        "completion_mode": args.completion_mode,
        "quantity_limits_enforced": args.completion_mode == "target-new-posts",
        "behavior_profile": pool["behavior_profile"],
        "local_image_storage_required": True,
        "media_root": str(LOCAL_MEDIA_ROOT.resolve()),
        "preexecution_failure": reason,
    }
    state = FrozenExecutionState.create(
        state_path,
        run_id=run_id,
        job_key=args.target_key,
        site_key="xhs",
        job_kind="xhs_account_search",
        plan=plan,
        frozen_inputs=[Path(target["path"]), Path(pool["path"]), FORMAL_CRAWL_CONTRACT],
        dry_run=False,
    )
    state.fail("command_executed", error=reason)
    summary = {
        "status": "failed",
        "run_id": run_id,
        "target_key": args.target_key,
        "account_id": account["account_id"],
        "execution_state": str(state_path),
        "reason": reason,
        "finished_at": utc_iso(),
    }
    summary_path = write_summary(run_dir, summary)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_xhs_schema(conn)
        if login_required:
            set_account_status(conn, account["account_id"], "login_required", reason=reason)
        if lease_acquired:
            release_account_lease(conn, account_id=account["account_id"], run_id=run_id, outcome="failed")
        record_event(
            conn,
            account_id=account["account_id"],
            run_id=run_id,
            event_type="formal_run_preexecution_failed",
            details={"reason": reason},
        )
        upsert_run(
            conn,
            run_id=run_id,
            target_key=args.target_key,
            account_id=account["account_id"],
            status="failed",
            state_path=state_path,
            report=summary,
            finished=True,
        )
    print(json.dumps({**summary, "summary": str(summary_path)}, ensure_ascii=False, indent=2))
    return 2


def main() -> int:
    args = parse_args()
    target = load_target(args.target_key, args.target_config)
    pool = load_pool_config(args.pool_config)
    if int(pool["lease_seconds"]) < int(target["timeout_seconds"]) + 300:
        raise SystemExit("XHS lease_seconds must cover timeout_seconds plus a 300-second cleanup buffer")
    db_path = Path(args.db).expanduser().resolve()
    bootstrap_database(db_path, sync_jobs=False)
    run_id = utc_stamp()
    run_dir = ensure_dir(XHS_RUNTIME_ROOT / "runs" / run_id)
    state_path = ensure_dir(XHS_EXECUTION_STATE_ROOT / run_id) / f"{args.target_key}.json"

    lease_acquired = False
    discovery_plan: dict[str, Any]
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_xhs_schema(conn)
        if args.dry_run:
            account = _eligible_account_for_plan(conn, args.account_id)
        else:
            try:
                account = acquire_account_lease(
                    conn,
                    run_id=run_id,
                    pool_config=pool,
                    requested_account_id=args.account_id,
                )
            except XhsAccountUnavailable as exc:
                blocked_plan = {
                    "run_id": run_id,
                    "target_key": args.target_key,
                    "account_id": args.account_id,
                    "keyword": target["keyword"],
                    "target_new_posts": target["target_new_posts"],
                    "candidate_hard_limit": target["candidate_hard_limit"],
                    "completion_mode": args.completion_mode,
                    "quantity_limits_enforced": args.completion_mode == "target-new-posts",
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
            lease_acquired = True
        try:
            discovery_plan = resolve_discovery_plan(
                conn,
                target=target,
                account_id=str(account["account_id"]),
            )
        except Exception:
            if lease_acquired:
                release_account_lease(
                    conn,
                    account_id=str(account["account_id"]),
                    run_id=run_id,
                    outcome="failed",
                )
                lease_acquired = False
            raise

    encrypted_state = Path(str(account["encrypted_state_path"]))
    if not encrypted_state.is_file():
        return record_preexecution_failure(
            args=args,
            target=target,
            pool=pool,
            account=account,
            db_path=db_path,
            run_id=run_id,
            run_dir=run_dir,
            state_path=state_path,
            reason="missing_encrypted_xhs_storage_state",
            lease_acquired=lease_acquired,
            login_required=not args.dry_run,
        )

    try:
        encrypted_state_sha256 = snapshot_sha256(encrypted_state)
    except OSError as exc:
        return record_preexecution_failure(
            args=args,
            target=target,
            pool=pool,
            account=account,
            db_path=db_path,
            run_id=run_id,
            run_dir=run_dir,
            state_path=state_path,
            reason=f"unreadable_encrypted_xhs_storage_state:{type(exc).__name__}",
            lease_acquired=lease_acquired,
            login_required=not args.dry_run,
        )

    plan = {
        "run_id": run_id,
        "target_key": args.target_key,
        "account_id": account["account_id"],
        "profile_dir": account["profile_dir"],
        "encrypted_state_path": str(encrypted_state),
        "encrypted_state_sha256": encrypted_state_sha256,
        "keyword": target["keyword"],
        "target_new_posts": target["target_new_posts"],
        "candidate_hard_limit": target["candidate_hard_limit"],
        "max_stagnant_batches": target["max_stagnant_batches"],
        "completion_mode": args.completion_mode,
        "quantity_limits_enforced": args.completion_mode == "target-new-posts",
        "timeout_seconds": target["timeout_seconds"],
        "lease_seconds": pool["lease_seconds"],
        "behavior_profile": pool["behavior_profile"],
        "local_image_storage_required": True,
        "media_root": str(LOCAL_MEDIA_ROOT.resolve()),
        "headed": pool["headed"],
        "post_interaction": args.post_interaction,
        "no_import": args.no_import,
        "preflight": {
            "account_status": account["status"],
            "active_lease": False,
            "encrypted_state_exists": True,
            "encrypted_state_readable": True,
            "lease_covers_timeout_cleanup": (
                int(pool["lease_seconds"]) >= int(target["timeout_seconds"]) + 300
            ),
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
        if lease_acquired:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                ensure_xhs_schema(conn)
                release_account_lease(conn, account_id=account["account_id"], run_id=run_id, outcome="failed")
            lease_acquired = False
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
    discovery_commit: dict[str, Any] = {"skipped": True, "reason": "child_not_started"}
    try:
        key = load_snapshot_key(create=False)
        if snapshot_sha256(encrypted_state) != plan["encrypted_state_sha256"]:
            raise RuntimeError("encrypted XHS storage state changed after plan freeze")
        with materialized_storage_state(
            encrypted_state,
            ensure_dir(XHS_RUNTIME_ROOT / "sessions" / run_id),
            account_id=account["account_id"],
            key=key,
        ) as storage_state:
            command = build_child_command(
                target=target,
                pool=pool,
                account=account,
                storage_state=storage_state,
                db_path=db_path,
                output_root=ensure_dir(XHS_RUNS_OUTPUT / run_id),
                no_import=args.no_import,
                post_interaction=args.post_interaction,
                discovery=discovery_plan,
                completion_mode=args.completion_mode,
            )
            state.begin("command_executed")
            env = os.environ.copy()
            env["TRIPPOSTCOLLECT_EXECUTION_STATE_PATH"] = str(state_path)
            try:
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=int(target["timeout_seconds"]) + 300,
                    check=False,
                )
                exit_code = completed.returncode
                stdout = completed.stdout
                stderr = completed.stderr
            except subprocess.TimeoutExpired as exc:
                stdout = str(exc.stdout or "")
                stderr = str(exc.stderr or "")
                exit_code = 124
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
                    import_completion_ok = bool(
                        child_summary.get("import_completion_met")
                        if "import_completion_met" in child_summary
                        else child_summary.get("import_new_target_met")
                    )
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
                            import_completion_ok
                            and image_persistence["ok"]
                            and (
                                args.completion_mode == "source-exhausted"
                                or int(import_result.get("inserted_rows") or 0)
                                >= int(target["target_new_posts"])
                            )
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
                        updated_state = json.loads(storage_state.read_text(encoding="utf-8"))
                        encrypt_storage_state(
                            updated_state,
                            encrypted_state,
                            account_id=account["account_id"],
                            key=key,
                        )
                        state.finalize(
                            outcome="completed",
                            evidence={
                                "account_id": account["account_id"],
                                "summary_path": child_summary_path,
                                "import_result": import_result,
                            },
                        )
                        outcome = "completed"
    except Exception as exc:
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
            set_account_status(conn, account["account_id"], "login_required", reason=f"xhs_login:{login_reason}")
        release_account_lease(conn, account_id=account["account_id"], run_id=run_id, outcome=outcome)
        lease_acquired = False
        record_event(
            conn,
            account_id=account["account_id"],
            run_id=run_id,
            event_type="formal_run_finished",
            details={"outcome": outcome, "exit_code": exit_code, "challenge": challenge, "login_reason": login_reason},
        )
        conn.commit()

    summary = {
        "status": outcome,
        "run_id": run_id,
        "target_key": args.target_key,
        "account_id": account["account_id"],
        "execution_state": str(state_path),
        "child_summary": child_summary_path,
        "discovery": discovery_commit,
        "exit_code": exit_code,
        "challenge": challenge,
        "login_reason": login_reason,
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
        "completion_mode": args.completion_mode,
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
    print(json.dumps({**summary, "summary": str(summary_path)}, ensure_ascii=False, indent=2))
    return 0 if outcome == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
