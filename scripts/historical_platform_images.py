#!/usr/bin/env python3
"""Materialize one bounded historical image batch without keyword discovery."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any, Sequence

from mediacrawler_crawl import (
    download_bilibili_record_images,
    load_cookie_snapshot,
)
from repair_bilibili_articles import check_bilibili_login
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
from trippostcollect.artifacts.image_manifest import (
    ImageManifestEntry,
    write_manifest_atomic,
)
from trippostcollect.core.paths import (
    DATA_ROOT,
    DEFAULT_DB,
    IMAGE_MATERIALIZATION_RUNTIME,
    LOCAL_MEDIA_ROOT,
    PROJECT_ROOT,
)


DEFAULT_CAMPAIGN = (
    PROJECT_ROOT
    / "docs"
    / "plans"
    / "2026-08-07-historical-image-h00-input-freeze.json"
)
SUPPORTED_PLATFORMS = ("bilibili",)
STAGE_BY_PLATFORM = {"bilibili": "h04"}
MAX_BATCH_BY_PLATFORM = {"bilibili": 50}
DEFAULT_BATCH_TIMEOUT = {"bilibili": 3600}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=SUPPORTED_PLATFORMS, required=True)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--campaign", default=str(DEFAULT_CAMPAIGN))
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--batch-timeout", type=int)
    parser.add_argument("--report")
    parser.add_argument("--backup-dir")
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


def _assert_h00_protected(
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


def _load_raw_records(
    conn: sqlite3.Connection,
    post_ids: Sequence[int],
    *,
    platform_key: str,
) -> dict[int, dict[str, Any]]:
    records: dict[int, dict[str, Any]] = {}
    for post_id in post_ids:
        row = conn.execute(
            "SELECT raw_sample_json FROM web_posts WHERE id=? AND platform_key=?",
            (post_id, platform_key),
        ).fetchone()
        if row is None:
            raise ValueError(f"planned historical post disappeared: {post_id}")
        record = json.loads(row[0] or "{}")
        if not isinstance(record, dict):
            raise ValueError(f"historical raw record is not an object: {post_id}")
        records[post_id] = record
    return records


def _bilibili_session() -> tuple[str, dict[str, Any], dict[str, Any]]:
    snapshot = load_cookie_snapshot("bilibili")
    if not snapshot:
        raise RuntimeError(
            "missing Bilibili cookie snapshot; run scripts/login_warmup.py --targets bilibili"
        )
    cookie_header = str(snapshot.get("cookie_header") or "")
    login = check_bilibili_login(cookie_header)
    public_snapshot = {
        key: value for key, value in snapshot.items() if key != "cookie_header"
    }
    return cookie_header, public_snapshot, login


def _download_bilibili_batch(
    plan: Any,
    records: dict[int, dict[str, Any]],
    *,
    staging_root: Path,
    cookie_header: str,
    deadline: float,
) -> tuple[Path, list[ImageManifestEntry], list[dict[str, Any]]]:
    manifest_path = staging_root / "image_manifest.jsonl"
    entries: list[ImageManifestEntry] = []
    post_reports: list[dict[str, Any]] = []
    write_manifest_atomic(manifest_path, entries)
    for post in plan.posts:
        if time.monotonic() >= deadline:
            raise TimeoutError("historical Bilibili batch timeout reached before next post")
        started = time.monotonic()
        post_entries = download_bilibili_record_images(
            records[post.web_post_id],
            cookie_header=cookie_header,
            platform_data_root=staging_root,
        )
        entries.extend(post_entries)
        write_manifest_atomic(manifest_path, entries)
        downloaded = sum(entry.fetch_status == "downloaded" for entry in post_entries)
        failures = [entry for entry in post_entries if entry.fetch_status == "failed"]
        post_reports.append(
            {
                "platform_post_id": post.platform_post_id,
                "expected_images": post.authoritative_images,
                "manifest_rows": len(post_entries),
                "downloaded_images": downloaded,
                "failed_images": len(failures),
                "failure_codes": sorted(
                    {str(entry.error_code or "") for entry in failures}
                ),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
        if len(post_entries) != post.authoritative_images or failures:
            return manifest_path, entries, post_reports
    return manifest_path, entries, post_reports


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    platform_key = args.platform
    max_batch = MAX_BATCH_BY_PLATFORM[platform_key]
    if not 1 <= args.batch_size <= max_batch:
        raise SystemExit(f"--batch-size must be between 1 and {max_batch}")
    batch_timeout = int(
        args.batch_timeout or DEFAULT_BATCH_TIMEOUT[platform_key]
    )
    if batch_timeout != DEFAULT_BATCH_TIMEOUT[platform_key]:
        raise SystemExit(
            f"--batch-timeout must equal the H-02 frozen value {DEFAULT_BATCH_TIMEOUT[platform_key]}"
        )
    db_path = Path(args.db).expanduser().resolve()
    if not db_path.is_file():
        raise SystemExit(f"database does not exist: {db_path}")
    campaign_path = Path(args.campaign).expanduser().resolve(strict=True)
    campaign = _read_object(campaign_path)
    campaign_id = _campaign_id(campaign)
    run_id = utc_stamp()
    stage = STAGE_BY_PLATFORM[platform_key]
    run_dir = (
        IMAGE_MATERIALIZATION_RUNTIME
        / campaign_id
        / stage
        / f"{platform_key}-download-{run_id}"
    )
    staging_root = run_dir / "staging" / platform_key
    report_path = (
        Path(args.report).expanduser().resolve()
        if args.report
        else run_dir / "report.json"
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "stage": stage.upper(),
        "run_id": run_id,
        "created_at": utc_iso(),
        "mode": "apply" if args.apply else "dry-run",
        "status": "planning",
        "platform": platform_key,
        "batch_size": args.batch_size,
        "batch_timeout_seconds": batch_timeout,
        "keyword_discovery": False,
        "avatar_download": False,
        "video_download": False,
        "preview_image_download": False,
    }
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            integrity_before = database_integrity(conn)
            if integrity_before != {"quick_check": "ok", "foreign_key_violations": 0}:
                raise RuntimeError(f"input database integrity failed: {integrity_before}")
            protected_before = protected_database_digests(conn)
            _assert_h00_protected(campaign, protected_before)
            inventory_before = projection_inventory(conn)
            relationships_before = table_digest(conn, "web_post_images")
            plan = build_relationship_plan(
                conn,
                platforms=(platform_key,),
                batch_size=args.batch_size,
                project_root=PROJECT_ROOT,
                media_root=LOCAL_MEDIA_ROOT,
                require_missing_local=True,
            )
            records = _load_raw_records(
                conn,
                [post.web_post_id for post in plan.posts],
                platform_key=platform_key,
            )
        planned_images = sum(post.authoritative_images for post in plan.posts)
        report.update(
            {
                "database_sha256_before": sha256_file(db_path),
                "integrity_before": integrity_before,
                "protected_invariants_before": protected_before,
                "content_relationship_sha256_before": relationships_before,
                "inventory_before": inventory_before,
                "planned_posts": len(plan.posts),
                "planned_images": planned_images,
                "planned_platform_post_ids": [post.platform_post_id for post in plan.posts],
            }
        )
        if not plan.posts:
            report.update({"status": "completed", "reason": "no_missing_platform_images"})
            _write_json_atomic(report_path, report)
            print(json.dumps({"status": "completed", "report": str(report_path)}))
            return 0
        if not args.apply:
            report["status"] = "planned"
            _write_json_atomic(report_path, report)
            print(
                json.dumps(
                    {
                        "status": "planned",
                        "platform": platform_key,
                        "posts": len(plan.posts),
                        "images": planned_images,
                        "report": str(report_path),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
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
            / stage
            / f"{platform_key}-download-{run_id}"
        )
        report["backup"] = sqlite_backup(db_path, backup_root / db_path.name)
        cookie_header, session_snapshot, login_before = _bilibili_session()
        report["session_snapshot"] = session_snapshot
        report["login_before"] = login_before
        deadline = time.monotonic() + batch_timeout
        manifest_path, entries, post_reports = _download_bilibili_batch(
            plan,
            records,
            staging_root=staging_root,
            cookie_header=cookie_header,
            deadline=deadline,
        )
        report["downloaded_posts"] = post_reports
        report["manifest_rows"] = len(entries)
        failed_images = sum(
            int(item["failed_images"]) for item in post_reports
        )
        if len(post_reports) != len(plan.posts) or len(entries) != planned_images or failed_images:
            raise RuntimeError(
                "Bilibili image download incomplete: "
                f"posts={len(post_reports)}/{len(plan.posts)} "
                f"manifest={len(entries)}/{planned_images} failed={failed_images}"
            )
        report["login_after"] = check_bilibili_login(cookie_header)
        promoted_plan, promotion = promote_downloaded_plan(
            plan,
            manifest_paths=[manifest_path],
            project_root=PROJECT_ROOT,
            media_root=LOCAL_MEDIA_ROOT,
        )

        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
            try:
                if table_digest(conn, "web_post_images") != relationships_before:
                    raise RuntimeError("image relationships changed during historical download")
                if protected_database_digests(conn) != protected_before:
                    raise RuntimeError("protected state changed during historical download")
                if relationship_source_digest(
                    conn, [post.web_post_id for post in plan.posts]
                ) != plan.source_digest:
                    raise RuntimeError("planned historical image source changed before apply")
                apply_result = apply_relationship_plan(conn, promoted_plan)
                integrity_after = database_integrity(conn)
                if integrity_after != {"quick_check": "ok", "foreign_key_violations": 0}:
                    raise RuntimeError(f"database integrity failed after apply: {integrity_after}")
                if protected_database_digests(conn) != protected_before:
                    raise RuntimeError("protected state changed during historical image apply")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            inventory_after = projection_inventory(conn)
            relationships_after = table_digest(conn, "web_post_images")
        expected_gap = inventory_before[platform_key]["local_gap"] - planned_images
        if inventory_after[platform_key]["local_gap"] != expected_gap:
            raise RuntimeError("platform local image gap did not decrease by the planned amount")
        report.update(
            {
                "status": "completed",
                "promotion": promotion,
                "apply_result": apply_result,
                "database_sha256_after_apply": sha256_file(db_path),
                "content_relationship_sha256_after": relationships_after,
                "inventory_after": inventory_after,
                "integrity_after": integrity_after,
                "protected_invariants_after": protected_before,
                "elapsed_seconds": round(batch_timeout - max(0.0, deadline - time.monotonic()), 3),
                "finished_at": utc_iso(),
            }
        )
    except Exception as exc:
        report.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "finished_at": utc_iso(),
            }
        )
    finally:
        report["database_sha256_final"] = sha256_file(db_path)
        _write_json_atomic(report_path, report)

    print(
        json.dumps(
            {
                "status": report["status"],
                "platform": platform_key,
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
