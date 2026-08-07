#!/usr/bin/env python3
"""Download and atomically attach one bounded batch of missing historical XHS images."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from typing import Any, Sequence
from urllib.parse import urlparse

from failure_classifier import extract_stdout_json
from trippostcollect.artifacts.historical_image_materialization import (
    apply_relationship_plan,
    build_relationship_plan,
    database_integrity,
    projection_inventory,
    promote_downloaded_plan,
    protected_database_digests,
    relationship_source_digest,
    sha256_file,
    sqlite_backup,
    table_digest,
)
from trippostcollect.core.paths import (
    DATA_ROOT,
    DEFAULT_DB,
    IMAGE_MATERIALIZATION_RUNTIME,
    LOCAL_MEDIA_ROOT,
    PROJECT_ROOT,
    XHS_POOL_CONFIG,
    XHS_RUNS_OUTPUT,
    XHS_RUNTIME_ROOT,
    XHS_TARGET_CONFIG,
    ensure_dir,
)
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
    _login_reason,
    build_child_command,
    load_child_summary,
)


DEFAULT_CAMPAIGN = (
    PROJECT_ROOT
    / "docs"
    / "plans"
    / "2026-08-07-historical-image-h00-input-freeze.json"
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-managed", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--campaign", default=str(DEFAULT_CAMPAIGN))
    parser.add_argument("--target-key", default="qingdao_travel")
    parser.add_argument("--target-config", default=str(XHS_TARGET_CONFIG))
    parser.add_argument("--pool-config", default=str(XHS_POOL_CONFIG))
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--report")
    parser.add_argument("--backup-dir")
    parser.add_argument("--resume-child-summary")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
            json.dump(payload, file_handle, ensure_ascii=False, indent=2, sort_keys=True)
            file_handle.write("\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _campaign_id(campaign: dict[str, Any]) -> str:
    campaign_id = str(campaign.get("campaign_id") or "")
    if (
        int(campaign.get("schema_version") or 0) != 1
        or campaign.get("status") != "input_frozen"
        or not campaign_id
    ):
        raise ValueError("campaign is not an H-00 frozen input")
    return campaign_id


def _assert_protected_campaign(
    campaign: dict[str, Any], current: dict[str, Any]
) -> None:
    expected = campaign.get("invariants") or {}
    if (
        current.get("web_posts_non_image_sha256")
        != expected.get("web_posts_non_image_sha256")
        or current.get("discovery_table_sha256")
        != expected.get("discovery_table_sha256")
    ):
        raise ValueError("protected post or discovery state differs from H-00")


def _detail_urls(conn: sqlite3.Connection, post_ids: Sequence[int]) -> list[str]:
    urls: list[str] = []
    for post_id in post_ids:
        row = conn.execute(
            "SELECT platform_post_id, raw_sample_json FROM web_posts WHERE id=? AND platform_key='xhs'",
            (post_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"planned XHS post disappeared: {post_id}")
        platform_post_id, raw_text = row
        raw = json.loads(raw_text or "{}")
        url = str(raw.get("note_url") or "").strip()
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.xiaohongshu.com"
            or parsed.path.rstrip("/").split("/")[-1] != str(platform_post_id)
            or "xsec_token=" not in parsed.query
            or "xsec_source=" not in parsed.query
        ):
            raise ValueError(f"historical XHS post lacks an exact signed detail URL: {post_id}")
        urls.append(url)
    if len(set(urls)) != len(post_ids):
        raise ValueError("historical XHS detail URLs are not one-to-one with planned posts")
    return urls


def _write_private_urls(path: Path, urls: Sequence[str]) -> str:
    path.write_text(json.dumps(list(urls), ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)
    return sha256(path.read_bytes()).hexdigest()


def _manifest_paths(child_summary: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for record in child_summary.get("records") or []:
        if not isinstance(record, dict) or record.get("platform") != "xhs":
            continue
        output = record.get("output") or {}
        for value in output.get("image_manifest_paths") or []:
            if str(value) not in paths:
                paths.append(str(value))
    return paths


def _validate_child_evidence(
    child_summary: dict[str, Any],
    *,
    planned_posts: int,
    planned_images: int,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    from mediacrawler_crawl import collect_behavior_validation

    records = [
        record
        for record in child_summary.get("records") or []
        if isinstance(record, dict) and record.get("platform") == "xhs"
    ]
    if len(records) != 1 or not records[0].get("ok"):
        raise RuntimeError("XHS detail summary lacks one successful platform record")
    output = records[0].get("output") or {}
    if int(output.get("video_file_count") or 0) != 0:
        raise RuntimeError("XHS historical image output unexpectedly contains video")
    behavior = collect_behavior_validation(
        records,
        ["xhs"],
        str(child_summary.get("keyword") or ""),
        historical_image_only=True,
    )
    materialization = child_summary.get("image_materialization") or {}
    validation = child_summary.get("formal_validation") or {}
    if not behavior.get("ok"):
        raise RuntimeError("XHS detail behavior evidence did not pass")
    if not materialization.get("complete"):
        raise RuntimeError("XHS detail image staging is incomplete")
    if int(validation.get("valid_existing_count") or 0) != planned_posts:
        raise RuntimeError("XHS detail output does not exactly match existing posts")
    if int(materialization.get("expected_images") or 0) != planned_images:
        raise RuntimeError("XHS detail output image count differs from the frozen plan")
    manifests = _manifest_paths(child_summary)
    if not manifests:
        raise RuntimeError("XHS detail child produced no image manifest")
    return behavior, materialization, manifests


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.runner_managed:
        raise SystemExit("XHS historical images must run through scripts/xhs_runner.py")
    if not 1 <= args.batch_size <= 10:
        raise SystemExit("--batch-size must be between 1 and 10")
    db_path = Path(args.db).expanduser().resolve()
    campaign_path = Path(args.campaign).expanduser().resolve(strict=True)
    campaign = _read_object(campaign_path)
    campaign_id = _campaign_id(campaign)
    target = load_target(args.target_key, args.target_config)
    pool = load_pool_config(args.pool_config)
    if int(pool["lease_seconds"]) < int(target["timeout_seconds"]) + 300:
        raise SystemExit("XHS lease does not cover the detail timeout and cleanup budget")
    if not db_path.is_file():
        raise SystemExit(f"database does not exist: {db_path}")
    run_id = utc_stamp()
    run_dir = ensure_dir(
        IMAGE_MATERIALIZATION_RUNTIME / campaign_id / "h03" / f"xhs-download-{run_id}"
    )
    report_path = (
        Path(args.report).expanduser().resolve()
        if args.report
        else run_dir / "report.json"
    )
    output_root = ensure_dir(XHS_RUNS_OUTPUT / f"historical-images-{run_id}")
    session_dir = ensure_dir(XHS_RUNTIME_ROOT / "sessions" / f"historical-images-{run_id}")
    detail_urls_path = session_dir / "detail_urls.json"
    lease_acquired = False
    account: dict[str, Any] | None = None
    child_summary: dict[str, Any] = {}
    stdout = stderr = ""
    report: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "run_id": run_id,
        "created_at": utc_iso(),
        "mode": "apply" if args.apply else "dry-run",
        "platform": "xhs",
        "account_id": args.account_id,
        "batch_size": args.batch_size,
        "status": "planning",
        "video_download": False,
        "avatar_download": False,
        "discovery_checkpoint_write": False,
    }
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            integrity_before = database_integrity(conn)
            if integrity_before != {"quick_check": "ok", "foreign_key_violations": 0}:
                raise RuntimeError(f"input database integrity failed: {integrity_before}")
            protected_before = protected_database_digests(conn)
            _assert_protected_campaign(campaign, protected_before)
            inventory_before = projection_inventory(conn)
            relationships_before = table_digest(conn, "web_post_images")
            plan = build_relationship_plan(
                conn,
                platforms=("xhs",),
                batch_size=args.batch_size,
                project_root=PROJECT_ROOT,
                media_root=LOCAL_MEDIA_ROOT,
                require_missing_local=True,
            )
            urls = _detail_urls(conn, [post.web_post_id for post in plan.posts])
        report.update(
            {
                "database_sha256_before": sha256_file(db_path),
                "integrity_before": integrity_before,
                "protected_invariants_before": protected_before,
                "content_relationship_sha256_before": relationships_before,
                "inventory_before": inventory_before,
                "planned_posts": len(plan.posts),
                "planned_images": sum(post.authoritative_images for post in plan.posts),
                "planned_platform_post_ids": [post.platform_post_id for post in plan.posts],
            }
        )
        if not plan.posts:
            report.update({"status": "completed", "reason": "no_missing_xhs_images"})
            _write_json_atomic(report_path, report)
            print(json.dumps({"status": "completed", "report": str(report_path)}))
            return 0
        if not args.apply:
            report.update({"status": "planned", "signed_detail_url_count": len(urls)})
            _write_json_atomic(report_path, report)
            print(
                json.dumps(
                    {
                        "status": "planned",
                        "posts": len(plan.posts),
                        "images": report["planned_images"],
                        "report": str(report_path),
                    }
                )
            )
            return 0

        backup_root = (
            Path(args.backup_dir).expanduser().resolve()
            if args.backup_dir
            else DATA_ROOT
            / "backups"
            / "historical_images"
            / campaign_id
            / "h03"
            / f"xhs-download-{run_id}"
        )
        report["backup"] = sqlite_backup(db_path, backup_root / db_path.name)
        report["signed_detail_url_count"] = len(urls)
        encrypted_state: Path | None = None
        encrypted_sha_before: str | None = None
        child_summary_path = ""
        if args.resume_child_summary:
            resume_summary_path = Path(args.resume_child_summary).expanduser().resolve(strict=True)
            if PROJECT_ROOT not in resume_summary_path.parents:
                raise RuntimeError("resume child summary escapes project root")
            child_summary = load_child_summary(str(resume_summary_path))
            child_summary_path = str(resume_summary_path)
            report["resumed_child_summary"] = child_summary_path
        else:
            report["signed_detail_url_file_sha256"] = _write_private_urls(
                detail_urls_path, urls
            )
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
            encrypted_state = Path(str(account["encrypted_state_path"]))
            if not encrypted_state.is_file():
                raise RuntimeError("missing encrypted XHS storage state")
            encrypted_sha_before = snapshot_sha256(encrypted_state)
            key = load_snapshot_key(create=False)
            with materialized_storage_state(
                encrypted_state,
                session_dir,
                account_id=str(account["account_id"]),
                key=key,
            ) as storage_state:
                child_target = {
                    **target,
                    "historical_candidate_count": len(plan.posts),
                }
                command = build_child_command(
                    target=child_target,
                    pool=pool,
                    account=account,
                    storage_state=storage_state,
                    db_path=db_path,
                    output_root=output_root,
                    no_import=True,
                    post_interaction="none",
                    discovery={},
                    historical_detail_urls_file=detail_urls_path,
                )
                completed = subprocess.run(
                    command,
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=int(target["timeout_seconds"]) + 300,
                    check=False,
                )
                stdout, stderr = completed.stdout, completed.stderr
                child_pointer = extract_stdout_json(stdout)
                child_summary_path = str(child_pointer.get("summary") or "")
                child_summary = load_child_summary(child_summary_path)
                if not child_summary:
                    raise RuntimeError(
                        f"XHS detail child produced no summary at exit {completed.returncode}"
                    )
                updated_state = json.loads(storage_state.read_text(encoding="utf-8"))
                encrypt_storage_state(
                    updated_state,
                    encrypted_state,
                    account_id=str(account["account_id"]),
                    key=key,
                )

        behavior, materialization, manifests = _validate_child_evidence(
            child_summary,
            planned_posts=len(plan.posts),
            planned_images=int(report["planned_images"]),
        )
        promoted_plan, promotion = promote_downloaded_plan(
            plan,
            manifest_paths=manifests,
            project_root=PROJECT_ROOT,
            media_root=LOCAL_MEDIA_ROOT,
            allow_xhs_detail_index_match=True,
        )

        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
            try:
                if table_digest(conn, "web_post_images") != relationships_before:
                    raise RuntimeError("image relationships changed during XHS detail download")
                if protected_database_digests(conn) != protected_before:
                    raise RuntimeError("protected state changed during XHS detail download")
                if relationship_source_digest(
                    conn, [post.web_post_id for post in plan.posts]
                ) != plan.source_digest:
                    raise RuntimeError("planned XHS image source changed before apply")
                apply_result = apply_relationship_plan(conn, promoted_plan)
                integrity_after = database_integrity(conn)
                if integrity_after != {"quick_check": "ok", "foreign_key_violations": 0}:
                    raise RuntimeError(f"database integrity failed after apply: {integrity_after}")
                if protected_database_digests(conn) != protected_before:
                    raise RuntimeError("protected state changed during XHS image apply")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            inventory_after = projection_inventory(conn)
            relationships_after = table_digest(conn, "web_post_images")
        expected_gap = inventory_before["xhs"]["local_gap"] - report["planned_images"]
        if inventory_after["xhs"]["local_gap"] != expected_gap:
            raise RuntimeError("XHS local image gap did not decrease by the planned amount")
        report.update(
            {
                "status": "completed",
                "child_summary": child_summary_path,
                "child_behavior_validation": behavior,
                "child_image_materialization": materialization,
                "encrypted_state_sha256_before": encrypted_sha_before,
                "encrypted_state_sha256_after": (
                    snapshot_sha256(encrypted_state) if encrypted_state else None
                ),
                "promotion": promotion,
                "apply_result": apply_result,
                "database_sha256_after_apply": sha256_file(db_path),
                "content_relationship_sha256_after": relationships_after,
                "inventory_after": inventory_after,
                "integrity_after": integrity_after,
                "protected_invariants_after": protected_before,
                "finished_at": utc_iso(),
            }
        )
        outcome = "completed"
    except (Exception, subprocess.SubprocessError) as exc:
        outcome = "failed"
        report.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "finished_at": utc_iso(),
            }
        )
    finally:
        try:
            detail_urls_path.unlink()
        except FileNotFoundError:
            pass
        if lease_acquired and account is not None:
            challenge = _challenge_reason(stdout, stderr, child_summary)
            login_reason = _login_reason(stdout, stderr, child_summary)
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                ensure_xhs_schema(conn)
                if challenge:
                    record_event(
                        conn,
                        account_id=str(account["account_id"]),
                        run_id=run_id,
                        event_type="challenge_detected",
                        details={"reason": f"historical_images:{challenge}"},
                    )
                elif login_reason:
                    set_account_status(
                        conn,
                        str(account["account_id"]),
                        "login_required",
                        reason=f"historical_images:{login_reason}",
                    )
                release_account_lease(
                    conn,
                    account_id=str(account["account_id"]),
                    run_id=run_id,
                    outcome=outcome,
                )
        report["database_sha256_final"] = sha256_file(db_path)
        _write_json_atomic(report_path, report)

    print(
        json.dumps(
            {
                "status": report["status"],
                "posts": report.get("planned_posts", 0),
                "images": report.get("planned_images", 0),
                "report": str(report_path),
                "backup": (report.get("backup") or {}).get("path"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
