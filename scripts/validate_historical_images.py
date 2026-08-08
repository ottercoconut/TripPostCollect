#!/usr/bin/env python3
"""Validate completed historical body-image storage for one platform or the full campaign."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Sequence

from trippostcollect.artifacts.historical_image_materialization import (
    HISTORICAL_PLATFORM_ORDER,
    build_relationship_plan,
    database_integrity,
    projection_inventory,
    protected_database_digests,
    sha256_file,
)
from trippostcollect.artifacts.image_materialization import (
    ImageMaterializationError,
    validate_image_file,
)
from trippostcollect.core.paths import (
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
    parser.add_argument("--platform", choices=("all", *HISTORICAL_PLATFORM_ORDER), required=True)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--campaign", default=str(DEFAULT_CAMPAIGN))
    parser.add_argument("--project-root", default=str(PROJECT_ROOT), help=argparse.SUPPRESS)
    parser.add_argument("--media-root", default=str(LOCAL_MEDIA_ROOT), help=argparse.SUPPRESS)
    parser.add_argument("--report")
    parser.add_argument("--markdown-report")
    return parser.parse_args(argv)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
            file_handle.write(text)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_text_atomic(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _operator_exclusions(
    conn: sqlite3.Connection, platforms: Sequence[str]
) -> list[dict[str, Any]]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='historical_image_exclusions'"
    ).fetchone()
    if not exists:
        return []
    placeholders = ",".join("?" for _ in platforms)
    return [
        {
            "campaign_id": row[0],
            "platform_key": row[1],
            "platform_post_id": row[2],
            "source_index": row[3],
            "source_asset_key": row[4],
            "reason": row[5],
            "approved_by": row[6],
            "approved_at": row[7],
        }
        for row in conn.execute(
            f"""
            SELECT campaign_id, platform_key, platform_post_id, source_index,
                   source_asset_key, reason, approved_by, approved_at
            FROM historical_image_exclusions
            WHERE platform_key IN ({placeholders})
            ORDER BY platform_key, platform_post_id, source_index, id
            """,
            tuple(platforms),
        )
    ]


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _platform_file_validation(
    conn: sqlite3.Connection,
    platform_key: str,
    *,
    project_root: Path,
    media_root: Path,
) -> dict[str, Any]:
    platform_root = (media_root / platform_key).resolve()
    failures: list[dict[str, Any]] = []
    referenced: set[Path] = set()
    inventory_digest = sha256()
    verified_bytes = 0
    rows = conn.execute(
        """
        SELECT i.local_path, i.width, i.height, i.mime_type, i.sha256
        FROM web_post_images AS i
        JOIN web_posts AS p ON p.id=i.web_post_id
        WHERE p.platform_key=? AND i.image_role='content'
        ORDER BY p.id, i.image_index, i.id
        """,
        (platform_key,),
    ).fetchall()
    for local_path, width, height, mime_type, expected_sha in rows:
        raw_path = str(local_path or "")
        if not raw_path:
            failures.append({"code": "local_path_missing", "local_path": raw_path})
            continue
        candidate = Path(raw_path).expanduser()
        absolute = candidate if candidate.is_absolute() else project_root / candidate
        resolved = absolute.resolve(strict=False)
        if not _inside(resolved, platform_root):
            failures.append({"code": "local_path_outside_platform", "local_path": raw_path})
            continue
        try:
            verified = validate_image_file(
                resolved,
                allowed_root=platform_root,
                expected_sha256=str(expected_sha or ""),
            )
        except (ImageMaterializationError, OSError) as exc:
            failures.append(
                {
                    "code": getattr(exc, "code", type(exc).__name__),
                    "local_path": raw_path,
                    "error": str(exc),
                }
            )
            continue
        mismatches = []
        if int(width or 0) != verified.width:
            mismatches.append("width")
        if int(height or 0) != verified.height:
            mismatches.append("height")
        if str(mime_type or "") != verified.mime_type:
            mismatches.append("mime_type")
        if mismatches:
            failures.append(
                {
                    "code": "image_metadata_mismatch",
                    "local_path": raw_path,
                    "fields": mismatches,
                }
            )
            continue
        referenced.add(resolved)
        verified_bytes += verified.size_bytes
        inventory_digest.update(
            json.dumps(
                [raw_path, verified.sha256, verified.size_bytes, verified.width, verified.height],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        inventory_digest.update(b"\n")

    media_files = (
        {path.resolve(strict=True) for path in platform_root.rglob("*") if path.is_file() and not path.is_symlink()}
        if platform_root.is_dir()
        else set()
    )
    orphan_files = sorted(media_files - referenced)
    missing_references = sorted(referenced - media_files)
    return {
        "relationship_rows": len(rows),
        "verified_files": len(referenced),
        "verified_bytes": verified_bytes,
        "inventory_sha256": inventory_digest.hexdigest(),
        "media_files": len(media_files),
        "orphan_files": len(orphan_files),
        "orphan_sample": [path.relative_to(platform_root).as_posix() for path in orphan_files[:20]],
        "missing_references": len(missing_references),
        "missing_reference_sample": [str(path) for path in missing_references[:20]],
        "file_failures": len(failures),
        "failure_sample": failures[:20],
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 历史正文图片自动验收报告",
        "",
        f"- 状态：`{report['status']}`",
        f"- campaign：`{report['campaign_id']}`",
        f"- 平台：`{', '.join(report['platforms'])}`",
        f"- 数据库 SHA-256：`{report['database_sha256']}`",
        f"- quick_check：`{report['integrity']['quick_check']}`",
        f"- 外键违规：`{report['integrity']['foreign_key_violations']}`",
        f"- HISTORICAL_DATA_COMPLETE：`{str(report['historical_data_complete']).lower()}`",
        "",
        "| 平台 | 权威图 | 关系 | 本地关系 | 文件 | 字节 | gap | orphan | 文件失败 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for platform_key in report["platforms"]:
        inventory = report["inventory"][platform_key]
        files = report["file_validation"][platform_key]
        lines.append(
            f"| {platform_key} | {inventory['authoritative_images']} | "
            f"{inventory['current_content_rows']} | {inventory['existing_local_rows']} | "
            f"{files['verified_files']} | {files['verified_bytes']} | "
            f"{inventory['local_gap']} | {files['orphan_files']} | {files['file_failures']} |"
        )
    lines.append("")
    if report.get("errors"):
        lines.extend(["## 失败项", ""])
        lines.extend(f"- {value}" for value in report["errors"])
        lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    db_path = Path(args.db).expanduser().resolve(strict=True)
    campaign_path = Path(args.campaign).expanduser().resolve(strict=True)
    project_root = Path(args.project_root).expanduser().resolve(strict=True)
    media_root = Path(args.media_root).expanduser().resolve()
    campaign = _read_object(campaign_path)
    campaign_id = str(campaign.get("campaign_id") or "")
    if campaign.get("status") != "input_frozen" or not campaign_id:
        raise SystemExit("campaign is not an H-00 frozen input")
    platforms = (
        HISTORICAL_PLATFORM_ORDER
        if args.platform == "all"
        else (str(args.platform),)
    )
    stamp = utc_stamp()
    report_path = (
        Path(args.report).expanduser().resolve()
        if args.report
        else IMAGE_MATERIALIZATION_RUNTIME / campaign_id / "validation" / stamp / "report.json"
    )
    markdown_path = (
        Path(args.markdown_report).expanduser().resolve()
        if args.markdown_report
        else report_path.with_suffix(".md")
    )
    errors: list[str] = []
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        integrity = database_integrity(conn)
        protected = protected_database_digests(conn)
        inventory = projection_inventory(conn)
        plan = build_relationship_plan(
            conn,
            platforms=platforms,
            batch_size=0,
            project_root=project_root,
            media_root=media_root,
        )
        file_validation = {
            platform_key: _platform_file_validation(
                conn,
                platform_key,
                project_root=project_root,
                media_root=media_root,
            )
            for platform_key in platforms
        }
        avatar_local_rows = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM web_post_images AS i
                JOIN web_posts AS p ON p.id=i.web_post_id
                WHERE p.platform_key IN ({})
                  AND i.image_role='author_avatar'
                  AND i.local_path IS NOT NULL AND i.local_path<>''
                """.format(",".join("?" for _ in platforms)),
                platforms,
            ).fetchone()[0]
        )
        operator_exclusions = _operator_exclusions(conn, platforms)

    expected = campaign.get("invariants") or {}
    if protected != {
        "web_posts_non_image_sha256": expected.get("web_posts_non_image_sha256"),
        "discovery_table_sha256": expected.get("discovery_table_sha256"),
    }:
        errors.append("protected H-00 post/discovery invariants changed")
    if integrity != {"quick_check": "ok", "foreign_key_violations": 0}:
        errors.append(f"database integrity failed: {integrity}")
    if avatar_local_rows:
        errors.append(f"author avatar local rows are non-zero: {avatar_local_rows}")
    for post in plan.posts:
        if (
            post.current_content_rows != post.authoritative_images
            or post.preserved_local_rows != post.authoritative_images
            or post.misclassified_rows
            or post.duplicate_variant_rows
            or post.missing_authoritative_relations
            or post.url_normalizations
        ):
            errors.append(f"relationship mismatch: {post.platform_key}:{post.platform_post_id}")
            if len(errors) >= 100:
                break
    for platform_key in platforms:
        row = inventory[platform_key]
        files = file_validation[platform_key]
        if not (
            row["authoritative_images"]
            == row["current_content_rows"]
            == row["existing_local_rows"]
            == files["relationship_rows"]
            == files["verified_files"]
            == files["media_files"]
        ):
            errors.append(f"count equality failed: {platform_key}")
        if row["misclassified_rows"] or row["local_gap"]:
            errors.append(f"projection mismatch remains: {platform_key}")
        if files["orphan_files"] or files["missing_references"] or files["file_failures"]:
            errors.append(f"file validation failed: {platform_key}")

    all_platforms = tuple(platforms) == HISTORICAL_PLATFORM_ORDER
    status = "completed" if not errors else "failed"
    report = {
        "schema_version": 1,
        "created_at": utc_iso(),
        "campaign_id": campaign_id,
        "status": status,
        "platforms": list(platforms),
        "database": str(db_path),
        "database_sha256": sha256_file(db_path),
        "integrity": integrity,
        "protected_invariants": protected,
        "inventory": {key: inventory[key] for key in platforms},
        "file_validation": file_validation,
        "avatar_local_rows": avatar_local_rows,
        "operator_exclusion_count": len(operator_exclusions),
        "operator_exclusions": operator_exclusions,
        "relationship_mismatches": sum(value.startswith("relationship mismatch") for value in errors),
        "errors": errors,
        "historical_data_complete": bool(all_platforms and status == "completed"),
    }
    _write_json_atomic(report_path, report)
    _write_text_atomic(markdown_path, _markdown(report))
    print(
        json.dumps(
            {
                "status": status,
                "platforms": list(platforms),
                "historical_data_complete": report["historical_data_complete"],
                "report": str(report_path),
                "markdown_report": str(markdown_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if status == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
