#!/usr/bin/env python3
"""Apply the audited schema-v20 topic-relevance migration."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from trippostcollect.core.paths import (
    DATABASE_MIGRATIONS_OUTPUT,
    DEFAULT_DB,
    TEMP_ROOT,
    TOPIC_RELEVANCE_BACKUP_ROOT,
    ensure_dir,
)
from trippostcollect.db.topic_relevance_migration import (
    TopicRelevanceMigrationError,
    migrate_topic_relevance,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill web_posts.topic_relevant without deleting content."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Apply and verify only on a temporary SQLite backup.",
    )
    return parser.parse_args()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_backup(source: sqlite3.Connection, destination: Path) -> dict[str, Any]:
    ensure_dir(destination.parent)
    with sqlite3.connect(destination) as backup_conn:
        source.backup(backup_conn)
    return {
        "path": str(destination),
        "bytes": destination.stat().st_size,
        "sha256": file_sha256(destination),
    }


def apply_and_verify(conn: sqlite3.Connection) -> dict[str, Any]:
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    migration = migrate_topic_relevance(conn)
    foreign_key_violations = [list(row) for row in conn.execute("PRAGMA foreign_key_check")]
    quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    if foreign_key_violations:
        raise TopicRelevanceMigrationError("foreign-key violations remain before commit")
    if quick_check != "ok":
        raise TopicRelevanceMigrationError(f"quick_check failed: {quick_check}")
    return {
        "migration": migration,
        "foreign_key_violations": foreign_key_violations,
        "quick_check": quick_check,
    }


def migrate_database(db_path: Path, run_id: str, *, dry_run: bool) -> dict[str, Any]:
    report: dict[str, Any] = {
        "run_id": run_id,
        "dry_run": dry_run,
        "source_db": str(db_path),
        "status": "running",
    }
    if dry_run:
        ensure_dir(TEMP_ROOT)
        with tempfile.TemporaryDirectory(prefix="topic-relevance-", dir=TEMP_ROOT) as temporary_dir:
            candidate = Path(temporary_dir) / db_path.name
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as source:
                report["temporary_backup"] = sqlite_backup(source, candidate)
            with sqlite3.connect(candidate) as conn:
                try:
                    report.update(apply_and_verify(conn))
                finally:
                    conn.rollback()
        report["status"] = "dry_run_completed"
        return report

    backup_path = TOPIC_RELEVANCE_BACKUP_ROOT / run_id / db_path.name
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as source:
        report["backup"] = sqlite_backup(source, backup_path)
    with sqlite3.connect(db_path) as conn:
        try:
            report.update(apply_and_verify(conn))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    report["status"] = "completed"
    return report


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).expanduser().resolve(strict=True)
    run_id = utc_stamp()
    report_path = ensure_dir(DATABASE_MIGRATIONS_OUTPUT / run_id) / "topic_relevance.json"
    try:
        report = migrate_database(db_path, run_id, dry_run=bool(args.dry_run))
        exit_code = 0
    except BaseException as exc:
        report = {
            "run_id": run_id,
            "dry_run": bool(args.dry_run),
            "source_db": str(db_path),
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        exit_code = 1
    report["report_path"] = str(report_path)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "dry_run": report["dry_run"],
                "source_db": report["source_db"],
                "report_path": str(report_path),
                "distribution": (report.get("migration") or {}).get("distribution"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
