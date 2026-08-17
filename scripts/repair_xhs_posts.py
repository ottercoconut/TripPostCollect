#!/usr/bin/env python3
"""Recover existing Xiaohongshu rows whose authoritative detail was not observed."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any
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
    acquire_account_lease,
    ensure_xhs_schema,
    record_event,
    release_account_lease,
    set_account_status,
)
from trippostcollect.xhs.config import load_pool_config, load_target
from trippostcollect.xhs.sessions import (
    encrypt_storage_state,
    load_snapshot_key,
    materialized_storage_state,
    snapshot_sha256,
)
from xhs_runner import (
    _challenge_reason,
    _eligible_account_for_plan,
    _login_reason,
    load_child_summary,
    tail,
    upsert_run,
    utc_iso,
    utc_stamp,
    write_summary,
)


ROOT = PROJECT_ROOT


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recover existing XHS web_posts rows without touching discovery state."
    )
    parser.add_argument("--target-key", default="qingdao_travel")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--target-config", default=str(XHS_TARGET_CONFIG))
    parser.add_argument("--pool-config", default=str(XHS_POOL_CONFIG))
    parser.add_argument("--keyword", help="Visible behavior-search keyword; defaults to target keyword.")
    parser.add_argument("--max-items", type=int, default=20)
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
    storage_state: Path,
    db_path: Path,
    output_root: Path,
    urls_path: Path,
    ids_path: Path,
    keyword: str,
    post_interaction: str,
) -> list[str]:
    urls = json.loads(urls_path.read_text(encoding="utf-8"))
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
        "--candidate-hard-limit",
        str(max(1, len(urls))),
        "--target-new-posts",
        "0",
        "--completion-mode",
        "target-new-posts",
        "--max-stagnant-batches",
        "1",
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
        "--xhs-profile-dir",
        str(account["profile_dir"]),
        "--xhs-storage-state",
        str(storage_state),
        "--xhs-detail-urls-file",
        str(urls_path),
        "--xhs-repair-target-ids-file",
        str(ids_path),
        "--xhs-repair",
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


def _state_fail_open(state: FrozenExecutionState | None, error: str) -> None:
    if state is None:
        return
    try:
        payload = state.load()
        for step in FORMAL_STEPS[1:]:
            status = (payload.get("steps") or {}).get(step, {}).get("status")
            if status in {"pending", "in_progress"}:
                state.fail(step, error=error)
                return
    except Exception:
        return


def main() -> int:
    args = parse_args()
    if args.max_items < 0:
        raise SystemExit("--max-items cannot be negative")
    target = load_target(args.target_key, args.target_config)
    pool = load_pool_config(args.pool_config)
    if int(pool["lease_seconds"]) < int(target["timeout_seconds"]) + 300:
        raise SystemExit("XHS lease_seconds must cover timeout_seconds plus a 300-second cleanup buffer")
    keyword = str(args.keyword or target["keyword"])
    db_path = Path(args.db).expanduser().resolve()
    bootstrap_database(db_path, sync_jobs=False)
    run_id = utc_stamp()
    run_dir = ensure_dir(XHS_REPAIR_OUTPUT / run_id)
    runtime_dir = ensure_dir(XHS_REPAIR_RUNTIME_ROOT / run_id)
    state_path = runtime_dir / "execution_state.json"

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
        "target_ids": [item["platform_post_id"] for item in targets],
        "rejected_count": len(rejected),
        "timeout_seconds": target["timeout_seconds"],
        "lease_seconds": pool["lease_seconds"],
        "behavior_profile": pool["behavior_profile"],
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

    lease_acquired = False
    child_summary: dict[str, Any] = {}
    child_summary_path = ""
    stdout = ""
    stderr = ""
    exit_code = 1
    outcome = "failed"
    state_error = ""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_xhs_schema(conn)
            account = acquire_account_lease(
                conn,
                run_id=run_id,
                pool_config=pool,
                requested_account_id=args.account_id,
            )
            lease_acquired = True
            upsert_run(
                conn,
                run_id=run_id,
                target_key=f"xhs_repair:{args.target_key}",
                account_id=account["account_id"],
                status="running",
                state_path=state_path,
            )
        encrypted_state = Path(str(account["encrypted_state_path"])).expanduser().resolve()
        if not encrypted_state.is_file():
            raise RuntimeError("missing_encrypted_xhs_storage_state")
        encrypted_sha = snapshot_sha256(encrypted_state)
        key = load_snapshot_key(create=False)
        with materialized_storage_state(
            encrypted_state,
            ensure_dir(runtime_dir / "session"),
            account_id=account["account_id"],
            key=key,
        ) as storage_state:
            command = build_child_command(
                target=target,
                pool=pool,
                account=account,
                storage_state=storage_state,
                db_path=db_path,
                output_root=run_dir / "child",
                urls_path=urls_path,
                ids_path=ids_path,
                keyword=keyword,
                post_interaction=args.post_interaction,
            )
            state.begin("command_executed")
            env = os.environ.copy()
            env["TRIPPOSTCOLLECT_EXECUTION_STATE_PATH"] = str(state_path)
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=int(target["timeout_seconds"]) + 300,
                check=False,
            )
            exit_code = int(completed.returncode)
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            stdout_json = extract_stdout_json(stdout)
            child_summary_path = str(stdout_json.get("summary") or "")
            child_summary = load_child_summary(child_summary_path)
            if exit_code != 0 or not child_summary:
                state_error = f"xhs_repair_child_exit_{exit_code}"
                state.fail(
                    "command_executed",
                    error=state_error,
                    evidence={"stdout_tail": tail(stdout), "stderr_tail": tail(stderr)},
                )
            else:
                state.complete("command_executed", evidence={"summary": child_summary_path})
                state.begin("artifacts_verified")
                artifact_evidence = verify_image_artifacts(
                    child_summary,
                    project_root=ROOT,
                    expect_promotion=True,
                )
                if not artifact_evidence["ok"]:
                    state_error = "xhs_repair_image_artifacts_incomplete"
                    state.fail("artifacts_verified", error=state_error, evidence=artifact_evidence)
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
                        post_id for post_id, value in statuses.items() if value["recovered"]
                    )
                    persistence_evidence = {
                        "image_persistence": image_persistence,
                        "target_statuses": statuses,
                        "recovered_ids": recovered_ids,
                        "recovered_count": len(recovered_ids),
                        "child_import_result": child_summary.get("import_result") or {},
                    }
                    import_ok = bool(child_summary.get("import_completion_met"))
                    persistence_ok = bool(import_ok and image_persistence["ok"] and recovered_ids)
                    if not persistence_ok:
                        state_error = "xhs_repair_persistence_not_verified"
                        state.fail("persistence_verified", error=state_error, evidence=persistence_evidence)
                    else:
                        state.complete("persistence_verified", evidence=persistence_evidence)
                        if snapshot_sha256(encrypted_state) != encrypted_sha:
                            updated_state = json.loads(storage_state.read_text(encoding="utf-8"))
                            encrypt_storage_state(updated_state, encrypted_state, account_id=account["account_id"], key=key)
                        state.finalize(
                            outcome="completed",
                            evidence={"summary": child_summary_path, **persistence_evidence},
                        )
                        outcome = "completed"
    except (OSError, sqlite3.Error, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
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
                set_account_status(
                    conn,
                    account["account_id"],
                    "login_required",
                    reason=f"xhs_repair_login:{login_reason}",
                )
            if lease_acquired:
                release_account_lease(
                    conn,
                    account_id=account["account_id"],
                    run_id=run_id,
                    outcome=outcome,
                )
            record_event(
                conn,
                account_id=account["account_id"],
                run_id=run_id,
                event_type="xhs_post_repair_finished",
                details={"outcome": outcome, "exit_code": exit_code, "error": state_error},
            )
            conn.commit()

    summary = {
        **base_summary,
        "status": outcome,
        "child_summary": child_summary_path,
        "exit_code": exit_code,
        "error": state_error,
        "challenge": _challenge_reason(stdout, stderr, child_summary),
        "login_reason": _login_reason(stdout, stderr, child_summary),
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
            target_key=f"xhs_repair:{args.target_key}",
            account_id=account["account_id"],
            status=outcome,
            state_path=state_path,
            child_summary_path=child_summary_path or None,
            report=summary,
            finished=True,
        )
    print(json.dumps({**summary, "summary": str(summary_path)}, ensure_ascii=False, indent=2))
    return 0 if outcome == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
