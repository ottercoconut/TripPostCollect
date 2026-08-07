#!/usr/bin/env python3
"""Report unreferenced long-term image files; deletion requires explicit confirmation."""

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
    database_integrity,
    sha256_file,
    sqlite_backup,
)
from trippostcollect.core.paths import (
    DATA_ROOT,
    DEFAULT_DB,
    IMAGE_MATERIALIZATION_RUNTIME,
    LOCAL_MEDIA_ROOT,
    PROJECT_ROOT,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database whose references are authoritative.")
    parser.add_argument("--media-root", default=str(LOCAL_MEDIA_ROOT), help="Long-term image root to audit.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT), help=argparse.SUPPRESS)
    parser.add_argument("--report", help="Optional JSON report destination.")
    parser.add_argument("--backup-dir", help="Backup directory required before deletion.")
    parser.add_argument("--campaign-id", default="historical-images-20260807-v1")
    parser.add_argument("--confirm-delete", help="Must exactly equal --campaign-id with --apply.")
    parser.add_argument("--apply", action="store_true", help="Delete the rechecked unreferenced files.")
    return parser.parse_args(argv)


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


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def referenced_media_files(
    conn: sqlite3.Connection,
    *,
    project_root: Path,
    media_root: Path,
) -> tuple[set[Path], list[str]]:
    referenced: set[Path] = set()
    unsafe: list[str] = []
    for (local_path,) in conn.execute(
        """
        SELECT DISTINCT local_path
        FROM web_post_images
        WHERE local_path IS NOT NULL AND local_path<>''
        ORDER BY local_path
        """
    ):
        raw = Path(str(local_path)).expanduser()
        absolute = raw if raw.is_absolute() else project_root / raw
        resolved = absolute.resolve(strict=False)
        if _inside(resolved, media_root):
            referenced.add(resolved)
        elif str(local_path).startswith("data/media/"):
            unsafe.append(str(local_path))
    return referenced, unsafe


def media_inventory(media_root: Path) -> tuple[list[Path], list[str]]:
    files: list[Path] = []
    unsafe: list[str] = []
    if not media_root.is_dir():
        return files, unsafe
    for path in sorted(media_root.rglob("*")):
        if path.is_symlink():
            unsafe.append(path.relative_to(media_root).as_posix())
            continue
        if not path.is_file():
            continue
        resolved = path.resolve(strict=True)
        if not _inside(resolved, media_root):
            unsafe.append(path.relative_to(media_root).as_posix())
            continue
        files.append(resolved)
    return files, unsafe


def _relative_sample(paths: Sequence[Path], media_root: Path, limit: int = 100) -> list[str]:
    return [path.relative_to(media_root).as_posix() for path in paths[:limit]]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and args.confirm_delete != args.campaign_id:
        raise SystemExit("--apply requires --confirm-delete to exactly equal --campaign-id")
    db_path = Path(args.db).expanduser().resolve(strict=True)
    project_root = Path(args.project_root).expanduser().resolve(strict=True)
    media_root = Path(args.media_root).expanduser().resolve()
    if not _inside(media_root, project_root):
        raise SystemExit("--media-root must be inside --project-root")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    report_path = (
        Path(args.report).expanduser().resolve()
        if args.report
        else IMAGE_MATERIALIZATION_RUNTIME / "gc" / timestamp / "report.json"
    )
    db_sha_before = sha256_file(db_path)
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        integrity = database_integrity(conn)
        references, unsafe_references = referenced_media_files(
            conn,
            project_root=project_root,
            media_root=media_root,
        )
    if integrity != {"quick_check": "ok", "foreign_key_violations": 0}:
        raise SystemExit(f"database integrity failed: {integrity}")
    files, unsafe_files = media_inventory(media_root)
    unreferenced = sorted(set(files) - references)
    report: dict[str, Any] = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "campaign_id": args.campaign_id,
        "mode": "apply" if args.apply else "dry-run",
        "database": str(db_path),
        "database_sha256_before": db_sha_before,
        "media_root": str(media_root),
        "referenced_files": len(references),
        "scanned_files": len(files),
        "unreferenced_files": len(unreferenced),
        "unreferenced_bytes": sum(path.stat().st_size for path in unreferenced),
        "unreferenced_sample": _relative_sample(unreferenced, media_root),
        "unsafe_references": unsafe_references,
        "unsafe_files": unsafe_files,
        "backup": None,
        "deleted_files": 0,
        "deleted_bytes": 0,
    }
    if args.apply:
        if unsafe_references or unsafe_files:
            raise RuntimeError("unsafe media paths must be resolved before deletion")
        backup_root = (
            Path(args.backup_dir).expanduser().resolve()
            if args.backup_dir
            else DATA_ROOT / "backups" / "local_image_gc" / args.campaign_id / timestamp
        )
        report["backup"] = sqlite_backup(db_path, backup_root / db_path.name)
        if sha256_file(db_path) != db_sha_before:
            raise RuntimeError("database changed while preparing garbage collection")
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            references_after, unsafe_after = referenced_media_files(
                conn,
                project_root=project_root,
                media_root=media_root,
            )
        if unsafe_after or references_after != references:
            raise RuntimeError("database reference set changed before deletion")
        deleted_bytes = 0
        for path in unreferenced:
            resolved = path.resolve(strict=True)
            if not _inside(resolved, media_root) or resolved in references_after:
                raise RuntimeError(f"refusing unsafe deletion target: {path}")
            deleted_bytes += resolved.stat().st_size
            resolved.unlink()
        report["deleted_files"] = len(unreferenced)
        report["deleted_bytes"] = deleted_bytes
    db_sha_after = sha256_file(db_path)
    if db_sha_after != db_sha_before:
        raise RuntimeError("garbage collection changed the database")
    report["database_sha256_after"] = db_sha_after
    _write_json_atomic(report_path, report)
    print(
        json.dumps(
            {
                "mode": report["mode"],
                "unreferenced_files": report["unreferenced_files"],
                "unreferenced_bytes": report["unreferenced_bytes"],
                "deleted_files": report["deleted_files"],
                "report": str(report_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
