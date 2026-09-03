#!/usr/bin/env python3
"""Recover existing Xiaohongshu rows whose authoritative detail was not observed."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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
    XHS_POOL_CONFIG,
    XHS_REPAIR_OUTPUT,
    XHS_REPAIR_RUNTIME_ROOT,
    XHS_TARGET_CONFIG,
    ensure_dir,
)
from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.xhs.accounts import (
    ensure_xhs_schema,
    record_event,
)
from trippostcollect.xhs.config import load_pool_config, load_target
from trippostcollect.xhs.leases import (
    LeaseGuard,
    XhsLeaseSignal,
    crawl_lease_budget,
)
from trippostcollect.xhs.runtime import (
    runtime_session_paths,
)
from trippostcollect.xhs.supervision import (
    run_supervised_xhs_subprocess,
    runtime_watchdog_evidence,
)
from xhs_runner import (
    _challenge_reason,
    _eligible_account_for_plan,
    _login_reason,
    lease_cleanup_evidence,
    load_child_summary,
    tail,
    upsert_run,
    utc_iso,
    utc_stamp,
    write_summary,
)


ROOT = PROJECT_ROOT
_ACTIVE_LEASE_GUARD: LeaseGuard | None = None


@contextmanager
def guarded_runtime_session(
    run_id: str,
    guard: LeaseGuard,
) -> Iterator[dict[str, Path]]:
    if run_id != guard.run_id:
        raise ValueError("runtime session run_id does not match its lease")
    yield guard.prepare_runtime_session()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recover existing XHS web_posts rows without touching discovery state."
    )
    parser.add_argument("--target-key", default="qingdao_travel")
    parser.add_argument(
        "--account-id",
        required=True,
        help="Logical checkpoint/lease slot; it does not retain a login profile.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--target-config", default=str(XHS_TARGET_CONFIG))
    parser.add_argument("--pool-config", default=str(XHS_POOL_CONFIG))
    parser.add_argument("--keyword", help="Visible behavior-search keyword; defaults to target keyword.")
    parser.add_argument("--max-items", type=int, default=20)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="Candidates per isolated in-browser repair batch.",
    )
    parser.add_argument("--post-id", action="append", dest="post_ids", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--post-interaction",
        choices=("none", "comment-scroll", "like-one", "random"),
        default="none",
    )
    return parser.parse_args()


def _write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _raw_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _detail_url(row: sqlite3.Row) -> tuple[str | None, str]:
    post_id = str(row["platform_post_id"] or "").strip()
    raw = _raw_object(row["raw_sample_json"])
    candidates = [str(row["canonical_url"] or "").strip(), str(raw.get("note_url") or "").strip()]
    for candidate in candidates:
        parsed = urlparse(candidate)
        if parsed.scheme != "https" or parsed.hostname not in {"xiaohongshu.com", "www.xiaohongshu.com"}:
            continue
        path_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if not path_id or (post_id and path_id != post_id):
            continue
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        token = str(query.get("xsec_token") or raw.get("xsec_token") or "").strip()
        source = str(query.get("xsec_source") or raw.get("xsec_source") or "").strip()
        if not token or not source:
            continue
        query["xsec_token"] = token
        query["xsec_source"] = source
        normalized = urlunparse(parsed._replace(query=urlencode(query), fragment=""))
        return normalized, ""
    return None, "missing_xsec_token_or_source"


def previous_repair_failures(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    failures: dict[str, dict[str, Any]] = {}
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='xhs_runs'"
    ).fetchone():
        return failures
    rows = conn.execute(
        """
        SELECT run_id, report_json
        FROM xhs_runs
        WHERE target_key LIKE 'xhs_repair:%'
          AND status IN ('completed', 'failed')
        ORDER BY started_at, run_id
        """
    ).fetchall()
    for row in rows:
        report = _raw_object(row["report_json"])
        repair_report = report.get("repair_report") or {}
        if isinstance(repair_report, dict):
            for failure in repair_report.get("candidate_failures") or []:
                if not isinstance(failure, dict):
                    continue
                post_id = str(failure.get("platform_post_id") or "").strip()
                if not post_id:
                    continue
                failures[post_id] = {
                    "run_id": str(row["run_id"] or ""),
                    "failure_scope": str(failure.get("failure_scope") or "detail"),
                    "error_code": str(failure.get("error_code") or "candidate_failed"),
                    "attempts": int(failure.get("attempts") or 1),
                    "retryable": bool(failure.get("retryable")),
                }
        for post_id_value in report.get("validation_failed_ids") or []:
            post_id = str(post_id_value or "").strip()
            if not post_id:
                continue
            failures[post_id] = {
                "run_id": str(row["run_id"] or ""),
                "failure_scope": "formal_validation",
                "error_code": "formal_validation_failed",
                "attempts": 1,
                "retryable": False,
            }
    return failures


def select_targets(
    conn: sqlite3.Connection,
    *,
    post_ids: list[str],
    max_items: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT id, platform_post_id, canonical_url, keyword, raw_sample_json, artifact_dir
        FROM web_posts
        WHERE platform_key='xhs'
          AND COALESCE(json_extract(raw_sample_json, '$.content_detail_status'), '')
              <> 'detail_observed'
        ORDER BY id
        """
    ).fetchall()
    requested = {str(value).strip() for value in post_ids if str(value).strip()}
    prior_failures = previous_repair_failures(conn) if not requested else {}
    targets: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        post_id = str(row["platform_post_id"] or "").strip()
        if requested and post_id not in requested:
            continue
        detail_url, reason = _detail_url(row)
        item = {
            "web_post_id": int(row["id"]),
            "platform_post_id": post_id,
            "keyword": str(row["keyword"] or ""),
            "canonical_url": str(row["canonical_url"] or ""),
            "artifact_dir": str(row["artifact_dir"] or ""),
            "detail_url": detail_url,
            "reason": reason,
        }
        if detail_url:
            previous_failure = prior_failures.get(post_id)
            if previous_failure:
                item["reason"] = "previous_repair_failure"
                item["previous_failure"] = previous_failure
                rejected.append(item)
            else:
                targets.append(item)
        else:
            rejected.append(item)
    if max_items > 0:
        targets = targets[:max_items]
    return targets, rejected


def build_child_command(
    *,
    target: dict[str, Any],
    pool: dict[str, Any],
    account: dict[str, Any],
    profile_dir: Path,
    db_path: Path,
    output_root: Path,
    urls_path: Path,
    ids_path: Path,
    keyword: str,
    post_interaction: str,
    batch_size: int,
) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "mediacrawler_crawl.py"),
        "--platforms",
        "xhs",
        "--keyword",
        keyword,
        "--output-dir",
        str(output_root),
        "--timeout-per-platform",
        str(int(target["timeout_seconds"])),
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
        "--xhs-profile-dir",
        str(profile_dir),
        "--xhs-detail-urls-file",
        str(urls_path),
        "--xhs-repair-target-ids-file",
        str(ids_path),
        "--xhs-repair",
        "--xhs-repair-batch-size",
        str(batch_size),
        "--xhs-post-interaction",
        post_interaction,
        "--download-images",
        "--media-root",
        str(LOCAL_MEDIA_ROOT.resolve()),
        "--no-checkpoint-write",
    ]
    if pool.get("headed", True):
        command.append("--headed")
    return command


def repaired_rows(conn: sqlite3.Connection, target_ids: list[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    placeholders = ",".join("?" for _ in target_ids)
    if not placeholders:
        return result
    rows = conn.execute(
        f"""
        SELECT platform_post_id,
               json_extract(raw_sample_json, '$.content_detail_status') AS detail_status,
               json_extract(raw_sample_json, '$.content_detail_source') AS detail_source
        FROM web_posts
        WHERE platform_key='xhs' AND platform_post_id IN ({placeholders})
        """,
        target_ids,
    ).fetchall()
    for row in rows:
        result[str(row["platform_post_id"])] = {
            "detail_status": str(row["detail_status"] or ""),
            "detail_source": str(row["detail_source"] or ""),
            "recovered": row["detail_status"] == "detail_observed"
            and row["detail_source"] == "note_detail",
        }
    return result


def _state_fail_open(
    state: FrozenExecutionState | None,
    error: str,
    *,
    evidence: dict[str, Any] | None = None,
) -> None:
    if state is None:
        return
    try:
        payload = state.load()
        for step in FORMAL_STEPS[1:]:
            status = (payload.get("steps") or {}).get(step, {}).get("status")
            if status in {"pending", "in_progress"}:
                state.fail(step, error=error, evidence=evidence)
                return
    except Exception:
        return


def _repair_report(child_summary: dict[str, Any]) -> dict[str, Any]:
    return next(
        (
            record.get("repair_report")
            for record in child_summary.get("records") or []
            if isinstance(record, dict)
            and record.get("platform") == "xhs"
            and isinstance(record.get("repair_report"), dict)
        ),
        {},
    )


def candidate_only_child_failure(child_summary: dict[str, Any]) -> bool:
    validation = child_summary.get("formal_validation") or {}
    report = _repair_report(child_summary)
    return bool(
        child_summary
        and validation.get("stop_reason") == "repair_no_valid_detail"
        and int(validation.get("valid_total_count") or 0) == 0
        and int(validation.get("local_image_failure_count") or 0) == 0
        and validation.get("pagination_runtime_blocked") is False
        and validation.get("pagination_incomplete") is False
        and validation.get("behavior_evidence_ok") is True
        and validation.get("policy_evidence_ok") is True
        and not report.get("runtime_blocker")
    )


def _run_main() -> int:
    global _ACTIVE_LEASE_GUARD
    args = parse_args()
    if args.max_items < 0 or args.batch_size <= 0:
        raise SystemExit("--max-items cannot be negative; --batch-size must be positive")
    target = load_target(args.target_key, args.target_config)
    pool = load_pool_config(args.pool_config)
    try:
        lease_budget = crawl_lease_budget(
            timeout_seconds=int(target["timeout_seconds"]),
            configured_lease_seconds=int(pool["lease_seconds"]),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    keyword = str(args.keyword or target["keyword"])
    db_path = Path(args.db).expanduser().resolve()
    bootstrap_database(db_path, sync_jobs=False)
    run_id = utc_stamp()
    run_dir = ensure_dir(XHS_REPAIR_OUTPUT / run_id)
    runtime_dir = ensure_dir(XHS_REPAIR_RUNTIME_ROOT / run_id)
    state_path = runtime_dir / "execution_state.json"
    session_paths = runtime_session_paths(run_id)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_xhs_schema(conn)
        account = _eligible_account_for_plan(conn, args.account_id)
        targets, rejected = select_targets(
            conn,
            post_ids=args.post_ids,
            max_items=args.max_items,
        )

    urls_path = _write_json(run_dir / "detail_urls.json", [item["detail_url"] for item in targets])
    ids_path = _write_json(run_dir / "target_ids.json", [item["platform_post_id"] for item in targets])
    manifest_path = _write_json(
        run_dir / "targets.json",
        {"targets": targets, "rejected": rejected, "keyword": keyword},
    )
    plan = {
        "run_id": run_id,
        "job_kind": "xhs_post_repair",
        "target_key": args.target_key,
        "account_id": account["account_id"],
        "keyword": keyword,
        "target_count": len(targets),
        "batch_size": args.batch_size,
        "batch_count": (len(targets) + args.batch_size - 1) // args.batch_size,
        "target_ids": [item["platform_post_id"] for item in targets],
        "rejected_count": len(rejected),
        "timeout_seconds": target["timeout_seconds"],
        "lease_seconds": lease_budget.lease_seconds,
        "configured_lease_ceiling_seconds": pool["lease_seconds"],
        "lease_budget": lease_budget.public(),
        "behavior_profile": pool["behavior_profile"],
        "login_mode": "per_run_qrcode",
        "session_retention": "none",
        "runtime_profile_dir": str(session_paths["profile"]),
        "headed": pool["headed"],
        "discovery_writes": False,
        "local_image_storage_required": True,
        "media_root": str(LOCAL_MEDIA_ROOT.resolve()),
    }
    state = FrozenExecutionState.create(
        state_path,
        run_id=run_id,
        job_key=f"xhs_repair:{args.target_key}",
        site_key="xhs",
        job_kind="xhs_post_repair",
        plan=plan,
        frozen_inputs=[
            Path(target["path"]),
            Path(pool["path"]),
            FORMAL_CRAWL_CONTRACT,
            manifest_path,
            urls_path,
            ids_path,
        ],
        dry_run=args.dry_run,
    )
    base_summary = {
        "status": "planned" if args.dry_run else "running",
        "run_id": run_id,
        "job_kind": "xhs_post_repair",
        "target_key": args.target_key,
        "account_id": account["account_id"],
        "execution_state": str(state_path),
        "plan": plan,
        "targets": targets,
        "rejected": rejected,
    }
    if args.dry_run:
        summary_path = write_summary(run_dir, base_summary)
        print(json.dumps({**base_summary, "summary": str(summary_path)}, ensure_ascii=False, indent=2))
        return 0
    if not targets:
        state.begin("command_executed")
        state.complete("command_executed", evidence={"skipped": True, "reason": "no_repair_targets"})
        state.begin("artifacts_verified")
        state.complete("artifacts_verified", evidence={"skipped": True, "reason": "no_repair_targets"})
        state.begin("persistence_verified")
        state.complete("persistence_verified", evidence={"skipped": True, "reason": "no_repair_targets"}, skipped=True)
        state.finalize(outcome="completed", evidence={"skipped": True, "reason": "no_repair_targets"})
        base_summary["status"] = "completed"
        base_summary["no_op"] = True
        summary_path = write_summary(run_dir, base_summary)
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_xhs_schema(conn)
            upsert_run(
                conn,
                run_id=run_id,
                target_key=f"xhs_repair:{args.target_key}",
                account_id=account["account_id"],
                status="completed",
                state_path=state_path,
                report=base_summary,
                finished=True,
            )
        print(json.dumps({**base_summary, "summary": str(summary_path)}, ensure_ascii=False, indent=2))
        return 0

    child_summary: dict[str, Any] = {}
    child_summary_path = ""
    stdout = ""
    stderr = ""
    exit_code = 1
    completed = None
    outcome = "failed"
    state_error = ""
    interrupt: dict[str, Any] | None = None
    candidate_only_failure = False
    guard = LeaseGuard(
        db_path=db_path,
        account_id=args.account_id,
        run_id=run_id,
        lease_kind="repair",
        execution_state_path=state_path,
        runtime_profile_dir=session_paths["profile"],
        budget=lease_budget,
    )
    _ACTIVE_LEASE_GUARD = guard
    try:
        account = guard.acquire()
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_xhs_schema(conn)
            upsert_run(
                conn,
                run_id=run_id,
                target_key=f"xhs_repair:{args.target_key}",
                account_id=account["account_id"],
                status="running",
                state_path=state_path,
            )
        with guarded_runtime_session(run_id, guard) as runtime_session:
            command = build_child_command(
                target=target,
                pool=pool,
                account=account,
                profile_dir=runtime_session["profile"],
                db_path=db_path,
                output_root=run_dir / "child",
                urls_path=urls_path,
                ids_path=ids_path,
                keyword=keyword,
                post_interaction=args.post_interaction,
                batch_size=args.batch_size,
            )
            state.begin("command_executed")
            env = os.environ.copy()
            env["TRIPPOSTCOLLECT_EXECUTION_STATE_PATH"] = str(state_path)
            env["TRIPPOSTCOLLECT_XHS_RUN_ID"] = run_id
            completed = run_supervised_xhs_subprocess(
                guard,
                command,
                cwd=ROOT,
                env=env,
                timeout_seconds=int(target["timeout_seconds"]),
            )
            exit_code = int(completed.returncode)
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            stdout_json = extract_stdout_json(stdout)
            child_summary_path = str(stdout_json.get("summary") or "")
            child_summary = load_child_summary(child_summary_path)
            candidate_only_failure = bool(
                candidate_only_child_failure(child_summary)
                and not _challenge_reason(stdout, stderr, child_summary)
                and not _login_reason(stdout, stderr, child_summary)
            )
            if not child_summary or (exit_code != 0 and not candidate_only_failure):
                watchdog = runtime_watchdog_evidence(completed)
                termination_reason = str(watchdog.get("termination_reason") or "")
                state_error = (
                    f"xhs_repair_runtime_watchdog:{termination_reason}"
                    if termination_reason
                    else f"xhs_repair_child_exit_{exit_code}"
                )
                state.fail(
                    "command_executed",
                    error=state_error,
                    evidence={
                        "stdout_tail": tail(stdout),
                        "stderr_tail": tail(stderr),
                        "runtime_watchdog": watchdog,
                    },
                )
            else:
                state.complete(
                    "command_executed",
                    evidence={
                        "summary": child_summary_path,
                        "child_exit_code": exit_code,
                        "candidate_only_failure": candidate_only_failure,
                        "runtime_watchdog": runtime_watchdog_evidence(completed),
                    },
                )
                state.begin("artifacts_verified")
                if candidate_only_failure:
                    skipped_evidence = {
                        "skipped": True,
                        "reason": "repair_no_valid_detail",
                        "candidate_failures": _repair_report(child_summary).get(
                            "candidate_failures"
                        )
                        or [],
                    }
                    state.complete(
                        "artifacts_verified",
                        evidence=skipped_evidence,
                        skipped=True,
                    )
                    state.begin("persistence_verified")
                    state.complete(
                        "persistence_verified",
                        evidence=skipped_evidence,
                        skipped=True,
                    )
                    state.finalize(
                        outcome="completed",
                        evidence={
                            "summary": child_summary_path,
                            "candidate_only_failure": True,
                            "recovered_count": 0,
                        },
                    )
                    outcome = "completed"
                else:
                    artifact_evidence = verify_image_artifacts(
                        child_summary,
                        project_root=ROOT,
                        expect_promotion=True,
                    )
                    if not artifact_evidence["ok"]:
                        state_error = "xhs_repair_image_artifacts_incomplete"
                        state.fail(
                            "artifacts_verified",
                            error=state_error,
                            evidence=artifact_evidence,
                        )
                    else:
                        state.complete("artifacts_verified", evidence=artifact_evidence)
                        state.begin("persistence_verified")
                        image_persistence = verify_image_persistence(
                            child_summary,
                            db_path,
                            project_root=ROOT,
                            media_root=LOCAL_MEDIA_ROOT,
                        )
                        with sqlite3.connect(db_path) as conn:
                            conn.row_factory = sqlite3.Row
                            statuses = repaired_rows(
                                conn,
                                [item["platform_post_id"] for item in targets],
                            )
                        recovered_ids = sorted(
                            post_id
                            for post_id, value in statuses.items()
                            if value["recovered"]
                        )
                        persistence_evidence = {
                            "image_persistence": image_persistence,
                            "target_statuses": statuses,
                            "recovered_ids": recovered_ids,
                            "recovered_count": len(recovered_ids),
                            "child_import_result": child_summary.get("import_result") or {},
                        }
                        import_ok = bool(child_summary.get("import_completion_met"))
                        persistence_ok = bool(
                            import_ok and image_persistence["ok"] and recovered_ids
                        )
                        if not persistence_ok:
                            state_error = "xhs_repair_persistence_not_verified"
                            state.fail(
                                "persistence_verified",
                                error=state_error,
                                evidence=persistence_evidence,
                            )
                        else:
                            state.complete(
                                "persistence_verified",
                                evidence=persistence_evidence,
                            )
                            state.finalize(
                                outcome="completed",
                                evidence={
                                    "summary": child_summary_path,
                                    **persistence_evidence,
                                },
                            )
                            outcome = "completed"
    except (XhsLeaseSignal, KeyboardInterrupt) as exc:
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
        state_error = f"xhs_repair_runtime_failed:operator_interrupt:{signal_name}"
        stderr = f"{stderr}\n{state_error}".strip()
        _state_fail_open(
            state,
            state_error,
            evidence={
                "interrupt": interrupt,
                "stderr_tail": tail(stderr),
            },
        )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        state_error = f"xhs_repair_exception:{type(exc).__name__}:{exc}"
        stderr = f"{stderr}\n{state_error}".strip()
        _state_fail_open(state, state_error)
    finally:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_xhs_schema(conn)
            challenge = _challenge_reason(stdout, stderr, child_summary)
            login_reason = _login_reason(stdout, stderr, child_summary)
            if challenge:
                record_event(
                    conn,
                    account_id=account["account_id"],
                    run_id=run_id,
                    event_type="xhs_post_repair_challenge_detected",
                    details={"reason": challenge},
                )
            elif login_reason:
                record_event(
                    conn,
                    account_id=account["account_id"],
                    run_id=run_id,
                    event_type="xhs_post_repair_run_scoped_login_failed",
                    details={"reason": login_reason},
                )
            record_event(
                conn,
                account_id=account["account_id"],
                run_id=run_id,
                event_type="xhs_post_repair_finished",
                details={
                    "outcome": outcome,
                    "exit_code": exit_code,
                    "error": state_error,
                    "failure_type": "runtime_failed" if interrupt else "",
                    "stop_reason": "runtime_failed" if interrupt else "",
                    "reason": "operator_interrupt" if interrupt else "",
                    "interrupt": interrupt,
                },
            )
            conn.commit()
    guard.set_outcome(outcome)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        final_statuses = repaired_rows(
            conn,
            [item["platform_post_id"] for item in targets],
        )
    recovered_ids = sorted(
        post_id for post_id, value in final_statuses.items() if value["recovered"]
    )
    remaining_ids = sorted(
        {item["platform_post_id"] for item in targets} - set(recovered_ids)
    )
    repair_report = _repair_report(child_summary)
    validation_failed_ids = sorted(
        set(repair_report.get("successful_ids") or []) - set(recovered_ids)
    )
    candidate_failed_ids = sorted(
        {
            str(item.get("platform_post_id") or "")
            for item in repair_report.get("candidate_failures") or []
            if isinstance(item, dict) and str(item.get("platform_post_id") or "")
        }
    )
    summary = {
        **base_summary,
        "status": outcome,
        "lease_id": guard.lease_id,
        "lease_budget": lease_budget.public(),
        "child_summary": child_summary_path,
        "exit_code": exit_code,
        "error": state_error,
        "challenge": _challenge_reason(stdout, stderr, child_summary),
        "login_reason": _login_reason(stdout, stderr, child_summary),
        "login_mode": "per_run_qrcode",
        "persistent_account_profile": False,
        **guard.runtime_session_cleanup_evidence(),
        "runtime_watchdog": runtime_watchdog_evidence(completed),
        "failure_type": "runtime_failed" if interrupt else "",
        "stop_reason": "runtime_failed" if interrupt else "",
        "reason": "operator_interrupt" if interrupt else "",
        "interrupt": interrupt,
        "import_result": child_summary.get("import_result") or {},
        "repair_report": repair_report,
        "candidate_only_failure": candidate_only_failure,
        "candidate_failed_ids": candidate_failed_ids,
        "validation_failed_ids": validation_failed_ids,
        "recovered_count": len(recovered_ids),
        "recovered_ids": recovered_ids,
        "remaining_target_count": len(remaining_ids),
        "remaining_target_ids": remaining_ids,
        "all_selected_targets_recovered": not remaining_ids,
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
            target_key=f"xhs_repair:{args.target_key}",
            account_id=account["account_id"],
            status=outcome,
            state_path=state_path,
            child_summary_path=child_summary_path or None,
            report=summary,
            finished=True,
        )
    lease_released = guard.close()
    cleanup_evidence = lease_cleanup_evidence(
        db_path,
        account_id=str(account["account_id"]),
        run_id=run_id,
        lease_id=guard.lease_id,
    )
    summary["lease_released"] = lease_released
    summary.update(guard.runtime_session_cleanup_evidence())
    if not lease_released:
        summary["status"] = "failed"
        summary["error"] = (
            "lease_release_deferred_live_processes"
            if cleanup_evidence.get("event_type")
            == "lease_release_deferred_live_processes"
            else "runtime_session_cleanup_failed"
        )
    summary["lease_cleanup"] = cleanup_evidence
    write_summary(run_dir, summary)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_xhs_schema(conn)
        upsert_run(
            conn,
            run_id=run_id,
            target_key=f"xhs_repair:{args.target_key}",
            account_id=account["account_id"],
            status=summary["status"],
            state_path=state_path,
            child_summary_path=child_summary_path or None,
            report=summary,
            finished=True,
        )
    print(json.dumps({**summary, "summary": str(summary_path)}, ensure_ascii=False, indent=2))
    if (
        summary.get("lease_released") is not True
        or summary.get("runtime_session_cleanup_complete") is not True
        or summary.get("runtime_session_actually_absent") is not True
    ):
        return 2
    if interrupt:
        return int(interrupt["exit_code"])
    return 0 if summary["status"] == "completed" else 2


def main() -> int:
    global _ACTIVE_LEASE_GUARD
    _ACTIVE_LEASE_GUARD = None
    code = 2
    try:
        try:
            code = _run_main()
        except XhsLeaseSignal as exc:
            code = 128 + exc.signum
    finally:
        guard = _ACTIVE_LEASE_GUARD
        _ACTIVE_LEASE_GUARD = None
        released = guard is None or guard.close()
        if not released:
            code = 2
    return code


if __name__ == "__main__":
    raise SystemExit(main())
