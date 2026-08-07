#!/usr/bin/env python3
"""Rebuild and later resume historical local-image relationships; defaults to dry-run."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Sequence

from trippostcollect.artifacts.historical_image_materialization import (
    DISCOVERY_TABLES,
    HISTORICAL_PLATFORM_ORDER,
    apply_relationship_plan,
    assert_initial_inventory_matches_campaign,
    build_relationship_plan,
    database_integrity,
    projection_inventory,
    promote_existing_plan,
    protected_database_digests,
    sha256_file,
    sqlite_backup,
    summarize_plan,
    table_digest,
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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database to inspect or update.")
    parser.add_argument(
        "--campaign",
        default=str(DEFAULT_CAMPAIGN),
        help="H-00 machine-readable input freeze JSON.",
    )
    parser.add_argument(
        "--platform",
        action="append",
        choices=("all", *HISTORICAL_PLATFORM_ORDER),
        help="Platform to process; repeat as needed. Defaults to all in the frozen order.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Maximum posts in this batch; zero processes every remaining post.",
    )
    parser.add_argument("--resume-state", help="Optional JSON state used to continue after a committed batch.")
    parser.add_argument(
        "--resume-report",
        help="Previous committed report used to prove permitted control-plane-only database drift.",
    )
    parser.add_argument(
        "--allow-control-plane-drift",
        action="store_true",
        help="Allow a database SHA change only when content and discovery hashes match --resume-report.",
    )
    parser.add_argument("--report", help="Optional JSON report destination.")
    parser.add_argument("--backup-dir", help="Backup directory used before --apply.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT), help=argparse.SUPPRESS)
    parser.add_argument("--media-root", default=str(LOCAL_MEDIA_ROOT), help=argparse.SUPPRESS)
    parser.add_argument(
        "--promote-existing",
        action="store_true",
        help="Select only posts whose existing local relationships are complete and promote them to media root.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the planned relationship rebuild after creating a verified SQLite backup.",
    )
    return parser.parse_args(argv)


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
            json.dump(payload, file_handle, ensure_ascii=False, indent=2, sort_keys=True)
            file_handle.write("\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _platforms(values: Sequence[str] | None) -> tuple[str, ...]:
    if not values or "all" in values:
        return HISTORICAL_PLATFORM_ORDER
    selected = set(values)
    return tuple(platform for platform in HISTORICAL_PLATFORM_ORDER if platform in selected)


def _default_report_path(campaign_id: str, timestamp: str) -> Path:
    return IMAGE_MATERIALIZATION_RUNTIME / campaign_id / timestamp / "report.json"


def _default_resume_path(campaign_id: str, db_path: Path, operation: str) -> Path:
    return IMAGE_MATERIALIZATION_RUNTIME / campaign_id / f"{db_path.stem}-{operation}-state.json"


def _validate_campaign(campaign: dict[str, Any]) -> str:
    campaign_id = str(campaign.get("campaign_id") or "")
    if not campaign_id or campaign.get("status") != "input_frozen":
        raise ValueError("campaign is not an H-00 frozen input")
    if int(campaign.get("schema_version") or 0) != 1:
        raise ValueError("unsupported campaign schema_version")
    return campaign_id


def _resume_context(
    *,
    resume_path: Path | None,
    campaign_id: str,
    database_sha256: str,
    operation: str,
    db_path: Path,
    allow_control_plane_drift: bool,
    resume_report_path: Path | None,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    if resume_path is None or not resume_path.exists():
        return None, {}
    state = _read_json_object(resume_path)
    if state.get("campaign_id") != campaign_id:
        raise ValueError("resume state belongs to a different campaign")
    if state.get("operation") != operation:
        raise ValueError("resume state belongs to a different operation")
    if state.get("database_sha256_after") != database_sha256:
        if not allow_control_plane_drift or resume_report_path is None:
            raise ValueError("database SHA does not match resume state")
        previous_report = _read_json_object(resume_report_path)
        if (
            previous_report.get("campaign_id") != campaign_id
            or previous_report.get("operation") != operation
            or previous_report.get("database_sha256_after") != state.get("database_sha256_after")
            or previous_report.get("resume") != state
        ):
            raise ValueError("resume report does not exactly attest the current resume state")
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            current_relationships = table_digest(conn, "web_post_images")
            current_protected = protected_database_digests(conn)
            current_inventory = projection_inventory(conn)
        if (
            current_relationships != previous_report.get("content_relationship_sha256_after")
            or current_protected != previous_report.get("protected_invariants_after")
            or current_inventory != previous_report.get("inventory_after")
        ):
            raise ValueError("database drift touched content or discovery state")
        state = {
            **state,
            "control_plane_drift": {
                "database_sha256_before": state["database_sha256_after"],
                "database_sha256_after": database_sha256,
                "attested_by_report": str(resume_report_path),
            },
        }
    cursors = {
        str(platform): int(value)
        for platform, value in (state.get("after_post_ids") or {}).items()
    }
    return state, cursors


def _initial_invariants_match(campaign: dict[str, Any], digests: dict[str, Any]) -> None:
    expected = campaign.get("invariants", {})
    if digests["web_posts_non_image_sha256"] != expected.get("web_posts_non_image_sha256"):
        raise ValueError("web_posts non-image invariant differs from H-00")
    expected_discovery = expected.get("discovery_table_sha256", {})
    for table in DISCOVERY_TABLES:
        if digests["discovery_table_sha256"][table] != expected_discovery.get(table):
            raise ValueError(f"discovery invariant differs from H-00: {table}")


def _report_paths(
    args: argparse.Namespace,
    campaign_id: str,
    timestamp: str,
    db_path: Path,
    operation: str,
) -> tuple[Path, Path | None]:
    report_path = (
        Path(args.report).expanduser().resolve()
        if args.report
        else _default_report_path(campaign_id, timestamp)
    )
    if args.resume_state:
        resume_path = Path(args.resume_state).expanduser().resolve()
    elif args.apply:
        resume_path = _default_resume_path(campaign_id, db_path, operation)
    else:
        resume_path = None
    return report_path, resume_path


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.batch_size < 0:
        raise SystemExit("--batch-size must be zero or positive")
    db_path = Path(args.db).expanduser().resolve(strict=True)
    campaign_path = Path(args.campaign).expanduser().resolve(strict=True)
    project_root = Path(args.project_root).expanduser().resolve(strict=True)
    media_root = Path(args.media_root).expanduser().resolve()
    campaign = _read_json_object(campaign_path)
    campaign_id = _validate_campaign(campaign)
    operation = "promote_existing" if args.promote_existing else "relationship_rebuild"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    report_path, resume_path = _report_paths(args, campaign_id, timestamp, db_path, operation)
    selected_platforms = _platforms(args.platform)
    if args.promote_existing and selected_platforms != ("xhs",):
        raise SystemExit("--promote-existing currently requires exactly --platform xhs")
    db_sha_before = sha256_file(db_path)
    resume_state, cursors = _resume_context(
        resume_path=resume_path,
        campaign_id=campaign_id,
        database_sha256=db_sha_before,
        operation=operation,
        db_path=db_path,
        allow_control_plane_drift=args.allow_control_plane_drift,
        resume_report_path=(
            Path(args.resume_report).expanduser().resolve(strict=True)
            if args.resume_report
            else None
        ),
    )
    if resume_state is None and db_sha_before != campaign.get("input", {}).get("database_sha256"):
        raise SystemExit("database SHA does not match H-00 input and no matching resume state exists")

    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        integrity_before = database_integrity(conn)
        if integrity_before != {"quick_check": "ok", "foreign_key_violations": 0}:
            raise SystemExit(f"input database integrity failed: {integrity_before}")
        invariants_before = protected_database_digests(conn)
        inventory_before = projection_inventory(conn)
        if resume_state is None:
            _initial_invariants_match(campaign, invariants_before)
            assert_initial_inventory_matches_campaign(inventory_before, campaign)
        plan = build_relationship_plan(
            conn,
            platforms=selected_platforms,
            after_post_ids=cursors,
            batch_size=args.batch_size,
            project_root=project_root,
            media_root=media_root,
            require_complete_existing_local=args.promote_existing,
        )
    plan_summary = summarize_plan(plan)
    report: dict[str, Any] = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "campaign_id": campaign_id,
        "mode": "apply" if args.apply else "dry-run",
        "operation": operation,
        "database": str(db_path),
        "database_sha256_before": db_sha_before,
        "campaign_input_database_sha256": campaign.get("input", {}).get("database_sha256"),
        "platforms": list(selected_platforms),
        "batch_size": args.batch_size,
        "resume_state": str(resume_path) if resume_path else None,
        "resume_loaded": resume_state is not None,
        "control_plane_drift": (resume_state or {}).get("control_plane_drift"),
        "inventory_before": inventory_before,
        "integrity_before": integrity_before,
        "protected_invariants_before": invariants_before,
        "plan": plan_summary,
        "backup": None,
        "apply_result": None,
        "promotion": None,
    }

    if args.apply:
        backup_root = (
            Path(args.backup_dir).expanduser().resolve()
            if args.backup_dir
            else DATA_ROOT / "backups" / "historical_images" / campaign_id / timestamp
        )
        backup_path = backup_root / db_path.name
        report["backup"] = sqlite_backup(db_path, backup_path)
        apply_plan = plan
        if args.promote_existing:
            apply_plan, promotion = promote_existing_plan(
                plan,
                project_root=project_root,
                media_root=media_root,
                staging_root=IMAGE_MATERIALIZATION_RUNTIME / campaign_id / "staging" / operation,
            )
            report["promotion"] = promotion
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
            try:
                apply_result = apply_relationship_plan(conn, apply_plan)
                invariants_after = protected_database_digests(conn)
                if invariants_after != invariants_before:
                    raise RuntimeError("protected database invariants changed during relationship rebuild")
                integrity_after = database_integrity(conn)
                if integrity_after != {"quick_check": "ok", "foreign_key_violations": 0}:
                    raise RuntimeError(f"database integrity failed after rebuild: {integrity_after}")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        db_sha_after = sha256_file(db_path)
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            inventory_after = projection_inventory(conn)
            content_relationship_sha256 = table_digest(conn, "web_post_images")
            web_posts_sha256 = table_digest(conn, "web_posts")
        after_post_ids = dict(cursors)
        for post in apply_plan.posts:
            after_post_ids[post.platform_key] = max(
                after_post_ids.get(post.platform_key, 0),
                post.web_post_id,
            )
        completed_platforms = sorted(
            set((resume_state or {}).get("completed_platforms") or [])
            | {
                platform
                for platform in selected_platforms
                if plan.remaining_posts_by_platform.get(platform, 0) == 0
            }
        )
        state = {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "operation": operation,
            "database": str(db_path),
            "database_sha256_after": db_sha_after,
            "after_post_ids": after_post_ids,
            "completed_platforms": completed_platforms,
            "last_plan_digest": apply_plan.plan_digest,
            "web_posts_sha256_after": web_posts_sha256,
            "web_post_images_sha256_after": content_relationship_sha256,
            "protected_invariants_after": invariants_after,
            "inventory_after": inventory_after,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if resume_path is None:
            raise RuntimeError("apply mode requires a resume state path")
        _write_json_atomic(resume_path, state)
        report.update(
            {
                "apply_result": apply_result,
                "database_sha256_after": db_sha_after,
                "inventory_after": inventory_after,
                "integrity_after": integrity_after,
                "protected_invariants_after": invariants_after,
                "content_relationship_sha256_after": content_relationship_sha256,
                "resume": state,
            }
        )
    else:
        db_sha_after = sha256_file(db_path)
        if db_sha_after != db_sha_before:
            raise RuntimeError("dry-run changed the database file")
        report["database_sha256_after"] = db_sha_after

    _write_json_atomic(report_path, report)
    print(
        json.dumps(
            {
                "campaign_id": campaign_id,
                "mode": report["mode"],
                "planned_posts": plan_summary["planned_posts"],
                "planned_images": plan_summary["planned_images"],
                "report": str(report_path),
                "backup": (report.get("backup") or {}).get("path"),
                "database_sha256_after": report["database_sha256_after"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
