#!/usr/bin/env python3
"""Backfill verified MediaCrawler local image paths into web_post_images."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from trippostcollect.artifacts.local_image_backfill import (
    apply_backfill_plan,
    build_backfill_plan,
    mapping_digest,
)
from trippostcollect.core.paths import (
    DATA_ROOT,
    DEFAULT_DB,
    XHS_RUNS_OUTPUT,
    ensure_dir,
    ensure_parent,
    runtime_dir,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database to inspect or update.")
    parser.add_argument("--runs-root", default=str(XHS_RUNS_OUTPUT), help="XHS run artifact root.")
    parser.add_argument("--report", help="Optional JSON report path.")
    parser.add_argument("--apply", action="store_true", help="Apply verified mappings after creating a backup.")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_backup(source_path: Path, destination_path: Path) -> None:
    ensure_parent(destination_path)
    with sqlite3.connect(source_path) as source, sqlite3.connect(destination_path) as destination:
        source.backup(destination)
        quick_check = destination.execute("PRAGMA quick_check").fetchone()
        if quick_check != ("ok",):
            raise RuntimeError(f"backup quick_check failed: {quick_check}")


def report_sample(plan, limit: int = 20) -> list[dict[str, object]]:
    return [
        {
            "image_row_id": item.image_row_id,
            "web_post_id": item.web_post_id,
            "platform_post_id": item.platform_post_id,
            "image_index": item.image_index,
            "source_image_index": item.source_image_index,
            "image_url": item.image_url,
            "local_path": item.local_path,
            "sha256": item.sha256,
            "mime_type": item.mime_type,
            "width": item.width,
            "height": item.height,
        }
        for item in plan.mappings[:limit]
    ]


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).expanduser().resolve(strict=True)
    runs_root = Path(args.runs_root).expanduser().resolve(strict=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    report_path = (
        Path(args.report).expanduser().resolve()
        if args.report
        else runtime_dir("local_image_path_backfill") / timestamp / "report.json"
    )

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        plan = build_backfill_plan(conn, runs_root)

    report: dict[str, object] = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "apply" if args.apply else "dry-run",
        "db": str(db_path),
        "runs_root": str(runs_root),
        "scanned_image_roots": plan.scanned_image_roots,
        "scanned_note_directories": plan.scanned_note_directories,
        "source_records": plan.source_records,
        "database_posts": plan.database_posts,
        "database_raw_image_urls": plan.database_raw_image_urls,
        "candidate_posts": plan.candidate_posts,
        "mapped_posts": len({item.web_post_id for item in plan.mappings}),
        "unmapped_database_posts": plan.database_posts - len({item.web_post_id for item in plan.mappings}),
        "mapped_image_rows": len(plan.mappings),
        "mapping_sha256": mapping_digest(plan.mappings),
        "reason_counts": plan.reason_counts,
        "sample": report_sample(plan),
    }

    if args.apply:
        backup_dir = ensure_dir(DATA_ROOT / "backups" / "local_image_path_backfill" / timestamp)
        backup_path = backup_dir / db_path.name
        db_sha256_before = sha256_file(db_path)
        sqlite_backup(db_path, backup_path)
        backup_sha256 = sha256_file(backup_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            apply_result = apply_backfill_plan(conn, plan)
            quick_check = conn.execute("PRAGMA quick_check").fetchone()
            foreign_key_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        report.update(
            {
                "backup": str(backup_path),
                "source_db_sha256_before": db_sha256_before,
                "backup_sha256": backup_sha256,
                "apply_result": apply_result,
                "quick_check": quick_check[0] if quick_check else None,
                "foreign_key_violations": len(foreign_key_violations),
                "db_sha256_after": sha256_file(db_path),
            }
        )

    ensure_parent(report_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**report, "report": str(report_path), "sample": report["sample"][:3]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
