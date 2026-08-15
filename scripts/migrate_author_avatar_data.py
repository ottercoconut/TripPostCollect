#!/usr/bin/env python3
"""Apply the audited schema-v17 author-avatar removal migration."""

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
    AUTHOR_AVATAR_REMOVAL_BACKUP_ROOT,
    DATABASE_MIGRATIONS_OUTPUT,
    DATA_ROOT,
    DEFAULT_DB,
    OUTPUTS_ROOT,
    TEMP_ROOT,
    ensure_dir,
)
from trippostcollect.db.avatar_migration import (
    AvatarMigrationError,
    avatar_aliases,
    migrate_remove_author_avatars,
)


SCAN_SUFFIXES = frozenset({".db", ".json", ".jsonl", ".sqlite"})
HISTORICAL_SCAN_BYTES_PER_FILE = 8 * 1024 * 1024
HISTORICAL_MATCH_PATH_LIMIT = 50


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove author-avatar fields, relationships, and JSON values from SQLite."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Apply the migration only to a temporary SQLite backup.",
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


def historical_residual_summary() -> dict[str, Any]:
    needles = [f'"{alias}"'.encode("utf-8") for alias in avatar_aliases()]
    schema_needles = [alias.encode("utf-8") for alias in avatar_aliases()]
    roots = {
        "backups": DATA_ROOT / "backups",
        "outputs": OUTPUTS_ROOT,
        "temp": TEMP_ROOT,
    }
    report: dict[str, Any] = {}
    for label, root in roots.items():
        scanned = 0
        matched: list[str] = []
        capped_files = 0
        if root.exists():
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in SCAN_SUFFIXES:
                    continue
                scanned += 1
                found = False
                if path.suffix.lower() in {".db", ".sqlite"}:
                    try:
                        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as candidate:
                            schema_text = "\n".join(
                                str(row[0] or "")
                                for row in candidate.execute(
                                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
                                )
                            ).encode("utf-8")
                        found = any(needle in schema_text for needle in schema_needles)
                    except sqlite3.DatabaseError:
                        found = False
                else:
                    bytes_read = 0
                    with path.open("rb") as handle:
                        overlap = b""
                        while bytes_read < HISTORICAL_SCAN_BYTES_PER_FILE:
                            chunk = handle.read(
                                min(
                                    1024 * 1024,
                                    HISTORICAL_SCAN_BYTES_PER_FILE - bytes_read,
                                )
                            )
                            if not chunk:
                                break
                            bytes_read += len(chunk)
                            sample = overlap + chunk
                            if any(needle in sample for needle in needles):
                                found = True
                                break
                            overlap = sample[-64:]
                    capped_files += int(
                        not found
                        and path.stat().st_size > HISTORICAL_SCAN_BYTES_PER_FILE
                    )
                if found:
                    matched.append(str(path))
        report[label] = {
            "root": str(root),
            "scanned_files": scanned,
            "files_with_avatar_alias_evidence": len(matched),
            "matched_path_samples": matched[:HISTORICAL_MATCH_PATH_LIMIT],
            "matched_paths_truncated": len(matched) > HISTORICAL_MATCH_PATH_LIMIT,
            "content_scan_limit_bytes_per_file": HISTORICAL_SCAN_BYTES_PER_FILE,
            "files_exceeding_content_scan_limit_without_early_match": capped_files,
            "action": "retained_read_only_by_user_scope",
        }
    return report


def deferred_historical_residual_summary() -> dict[str, Any]:
    return {
        label: {
            "root": str(root),
            "scanned_files": 0,
            "files_with_avatar_alias_evidence": 0,
            "matched_path_samples": [],
            "matched_paths_truncated": False,
            "action": "deferred_to_formal_migration",
        }
        for label, root in {
            "backups": DATA_ROOT / "backups",
            "outputs": OUTPUTS_ROOT,
            "temp": TEMP_ROOT,
        }.items()
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
        with tempfile.TemporaryDirectory(
            prefix="author-avatar-migration-",
            dir=TEMP_ROOT,
        ) as temporary_dir:
            candidate = Path(temporary_dir) / db_path.name
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as source:
                report["temporary_backup"] = sqlite_backup(source, candidate)
            with sqlite3.connect(candidate) as conn:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")
                report["migration"] = migrate_remove_author_avatars(conn)
                report["foreign_key_violations"] = [
                    list(row) for row in conn.execute("PRAGMA foreign_key_check")
                ]
                report["integrity_check"] = str(
                    conn.execute("PRAGMA integrity_check").fetchone()[0]
                )
                conn.rollback()
        report["status"] = "dry_run_completed"
        return report

    backup_path = (
        AUTHOR_AVATAR_REMOVAL_BACKUP_ROOT / run_id / "trippostcollect.sqlite"
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as backup_source:
                report["backup"] = sqlite_backup(backup_source, backup_path)
            report["migration"] = migrate_remove_author_avatars(conn)
            report["foreign_key_violations"] = [
                list(row) for row in conn.execute("PRAGMA foreign_key_check")
            ]
            report["integrity_check"] = str(
                conn.execute("PRAGMA integrity_check").fetchone()[0]
            )
            if report["foreign_key_violations"]:
                raise AvatarMigrationError("foreign-key violations remain before commit")
            if report["integrity_check"] != "ok":
                raise AvatarMigrationError(
                    f"integrity check failed: {report['integrity_check']}"
                )
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
    report_dir = ensure_dir(DATABASE_MIGRATIONS_OUTPUT / run_id)
    report_path = report_dir / "author_avatar_removal.json"
    try:
        report = migrate_database(db_path, run_id, dry_run=bool(args.dry_run))
        report["historical_residuals"] = (
            deferred_historical_residual_summary()
            if args.dry_run
            else historical_residual_summary()
        )
        exit_code = 0
    except BaseException as exc:
        report = {
            "run_id": run_id,
            "dry_run": bool(args.dry_run),
            "source_db": str(db_path),
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "historical_residuals": (
                deferred_historical_residual_summary()
                if args.dry_run
                else historical_residual_summary()
            ),
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
                "run_id": run_id,
                "status": report["status"],
                "dry_run": report["dry_run"],
                "source_db": report["source_db"],
                "report_path": report_path,
                "migration": report.get("migration"),
                "error": report.get("error"),
                "historical_residual_counts": {
                    key: {
                        "scanned_files": value["scanned_files"],
                        "files_with_avatar_alias_evidence": value[
                            "files_with_avatar_alias_evidence"
                        ],
                    }
                    for key, value in report["historical_residuals"].items()
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
