#!/usr/bin/env python3
"""Atomically promote validated Bilibili repair results into the target database."""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repair_bilibili_articles import (
    assert_external_invariants,
    build_source_manifest,
    control_plane_fingerprint,
    current_image_rows,
    create_online_backup,
    existing_target_row,
    git_commit,
    json_text,
    meta_values,
    non_bilibili_fingerprint,
    repair_lock,
    repair_state_validation,
    repaired_content_image_urls,
    sha256_file,
    sha256_text,
    sqlite_checks,
    sqlite_connect,
    target_summary,
    utc_now,
)
from trippostcollect.core.paths import DEFAULT_DB, ensure_dir


@dataclass(frozen=True)
class PromotionConfig:
    target_db_path: Path
    staged_db_path: Path
    state_db_path: Path
    backup_path: Path
    report_dir: Path
    expected_target_sha256: str
    apply: bool
    confirm_default_db_promotion: bool


def control_plane_is_idle(path: Path) -> dict[str, int | bool]:
    with sqlite_connect(path, readonly=True) as connection:
        running_jobs = int(
            connection.execute(
                "SELECT COUNT(*) FROM crawl_jobs WHERE status='running'"
            ).fetchone()[0]
        )
        running_attempts = int(
            connection.execute(
                "SELECT COUNT(*) FROM crawl_attempts WHERE finished_at IS NULL"
            ).fetchone()[0]
        )
    return {
        "running_jobs": running_jobs,
        "running_attempts": running_attempts,
        "ok": running_jobs == 0 and running_attempts == 0,
    }


def staged_success_items(state_connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return state_connection.execute(
        "SELECT * FROM repair_items WHERE status='succeeded' ORDER BY web_post_id"
    ).fetchall()


def staged_content_images(
    connection: sqlite3.Connection,
    web_post_id: int,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT image_index, image_url, image_role, local_path, width, height,
               mime_type, sha256, raw_image_json, created_at
        FROM web_post_images
        WHERE web_post_id=? AND image_role='content'
        ORDER BY image_index, id
        """,
        (web_post_id,),
    ).fetchall()


def verify_inputs(
    config: PromotionConfig,
    state_connection: sqlite3.Connection,
) -> dict[str, Any]:
    if config.apply and config.target_db_path.resolve() == DEFAULT_DB.resolve():
        if not config.confirm_default_db_promotion:
            raise RuntimeError(
                "default database promotion requires --confirm-default-db-promotion"
            )
    if not config.expected_target_sha256:
        raise RuntimeError("--expected-target-sha256 is required")
    current_target_sha256 = sha256_file(config.target_db_path)
    if current_target_sha256 != config.expected_target_sha256:
        raise RuntimeError("target database SHA-256 changed before promotion")
    if not config.backup_path.is_file():
        raise RuntimeError("--backup-path must point to an existing SQLite backup")

    meta = meta_values(state_connection)
    if not meta:
        raise RuntimeError("repair sidecar has no metadata")
    if meta.get("target_db_path") != str(config.staged_db_path.resolve()):
        raise RuntimeError("repair sidecar does not belong to the staged database")
    staged_validation = repair_state_validation(
        config.staged_db_path,
        state_connection,
        meta,
    )
    if not staged_validation["ok"]:
        raise RuntimeError(f"staged repair validation failed: {staged_validation}")

    target_checks = sqlite_checks(config.target_db_path)
    staged_checks = sqlite_checks(config.staged_db_path)
    backup_checks = sqlite_checks(config.backup_path)
    if not target_checks["ok"] or not staged_checks["ok"] or not backup_checks["ok"]:
        raise RuntimeError("target, staged, or backup SQLite validation failed")

    target_manifest, target_manifest_sha256 = build_source_manifest(config.target_db_path)
    backup_manifest, backup_manifest_sha256 = build_source_manifest(config.backup_path)
    source_manifest_sha256 = meta["source_manifest_sha256"]
    if backup_manifest_sha256 != source_manifest_sha256:
        raise RuntimeError("backup database does not match the repair source manifest")
    if len(target_manifest) != int(meta["source_count"]):
        raise RuntimeError("target Bilibili row count differs from repair source")

    target_validation = validate_incremental_target(
        config.target_db_path,
        config.staged_db_path,
        state_connection,
        meta,
    )
    if not target_validation["ok"]:
        raise RuntimeError(
            f"target is neither original nor previously promoted: {target_validation}"
        )
    external_invariants = assert_external_invariants(config.target_db_path, meta)

    idle = control_plane_is_idle(config.target_db_path)
    if not idle["ok"]:
        raise RuntimeError(f"crawl control plane is not idle: {idle}")
    return {
        "target_sha256": current_target_sha256,
        "target_manifest_sha256": target_manifest_sha256,
        "backup_sha256": sha256_file(config.backup_path),
        "backup_manifest_sha256": backup_manifest_sha256,
        "source_count": len(target_manifest),
        "staged_validation": staged_validation,
        "target_sqlite": target_checks,
        "staged_sqlite": staged_checks,
        "backup_sqlite": backup_checks,
        "control_plane_idle": idle,
        "target_incremental_validation": target_validation,
        "external_invariants": external_invariants,
    }


def original_row_matches(
    target_connection: sqlite3.Connection,
    item: sqlite3.Row,
) -> bool:
    row = existing_target_row(target_connection, int(item["web_post_id"]))
    images = current_image_rows(target_connection, int(item["web_post_id"]))
    return (
        str(row["platform_post_id"]) == str(item["platform_post_id"])
        and sha256_text(str(row["content_text"] or ""))
        == str(item["original_content_sha256"])
        and sha256_text(str(row["raw_sample_json"] or ""))
        == str(item["original_raw_sha256"])
        and sha256_text(json_text(images)) == str(item["original_images_sha256"])
        and row["keyword"] == item["original_keyword"]
        and row["canonical_url"] == item["original_canonical_url"]
        and row["artifact_dir"] == item["original_artifact_dir"]
    )


def repaired_row_matches(
    target_connection: sqlite3.Connection,
    staged_connection: sqlite3.Connection,
    item: sqlite3.Row,
) -> bool:
    web_post_id = int(item["web_post_id"])
    target_row = existing_target_row(target_connection, web_post_id)
    staged_row = existing_target_row(staged_connection, web_post_id)
    target_urls = repaired_content_image_urls(target_connection, web_post_id)
    staged_urls = repaired_content_image_urls(staged_connection, web_post_id)
    return (
        str(target_row["platform_post_id"]) == str(item["platform_post_id"])
        and str(staged_row["platform_post_id"]) == str(item["platform_post_id"])
        and sha256_text(str(target_row["content_text"] or ""))
        == str(item["repaired_content_sha256"])
        and sha256_text(json_text(target_urls))
        == str(item["repaired_images_sha256"])
        and str(target_row["raw_sample_json"] or "")
        == str(staged_row["raw_sample_json"] or "")
        and target_urls == staged_urls
        and target_row["keyword"] == item["original_keyword"]
        and target_row["canonical_url"] == item["original_canonical_url"]
        and target_row["artifact_dir"] == item["original_artifact_dir"]
    )


def validate_incremental_target(
    target_path: Path,
    staged_path: Path,
    state_connection: sqlite3.Connection,
    meta: dict[str, str],
) -> dict[str, Any]:
    original_count = 0
    already_promoted_count = 0
    error_count = 0
    errors: list[dict[str, Any]] = []

    def fail(item: sqlite3.Row, reason: str) -> None:
        nonlocal error_count
        error_count += 1
        if len(errors) < 50:
            errors.append(
                {
                    "web_post_id": int(item["web_post_id"]),
                    "platform_post_id": str(item["platform_post_id"]),
                    "status": str(item["status"]),
                    "reason": reason,
                }
            )

    with sqlite_connect(target_path, readonly=True) as target_connection:
        with sqlite_connect(staged_path, readonly=True) as staged_connection:
            for item in state_connection.execute(
                "SELECT * FROM repair_items ORDER BY web_post_id"
            ):
                status = str(item["status"])
                if status == "succeeded" and repaired_row_matches(
                    target_connection,
                    staged_connection,
                    item,
                ):
                    already_promoted_count += 1
                elif original_row_matches(target_connection, item):
                    original_count += 1
                elif status == "conflict":
                    fail(item, "target_conflict_item_changed")
                else:
                    fail(item, "target_row_matches_neither_original_nor_staged")

    return {
        "ok": error_count == 0,
        "source_count": int(meta["source_count"]),
        "original_count": original_count,
        "already_promoted_count": already_promoted_count,
        "error_count": error_count,
        "errors": errors,
    }


def promote_successes(
    config: PromotionConfig,
    state_connection: sqlite3.Connection,
    items: list[sqlite3.Row],
) -> dict[str, int]:
    promoted_count = 0
    already_promoted_count = 0
    with sqlite_connect(config.staged_db_path, readonly=True) as staged_connection:
        with sqlite_connect(config.target_db_path) as target_connection:
            target_connection.execute("BEGIN IMMEDIATE")
            try:
                for item in items:
                    web_post_id = int(item["web_post_id"])
                    if repaired_row_matches(
                        target_connection,
                        staged_connection,
                        item,
                    ):
                        already_promoted_count += 1
                        continue
                    if not original_row_matches(target_connection, item):
                        raise RuntimeError(
                            f"target row {web_post_id} changed after the repair manifest"
                        )
                    staged_row = existing_target_row(staged_connection, web_post_id)
                    if str(staged_row["platform_post_id"]) != str(
                        item["platform_post_id"]
                    ):
                        raise RuntimeError(
                            f"staged platform ID mismatch for row {web_post_id}"
                        )
                    content_images = staged_content_images(
                        staged_connection,
                        web_post_id,
                    )
                    staged_urls = [str(row["image_url"]) for row in content_images]
                    if sha256_text(json_text(staged_urls)) != str(
                        item["repaired_images_sha256"]
                    ):
                        raise RuntimeError(
                            f"staged content images mismatch for row {web_post_id}"
                        )
                    if sha256_text(str(staged_row["content_text"] or "")) != str(
                        item["repaired_content_sha256"]
                    ):
                        raise RuntimeError(
                            f"staged content mismatch for row {web_post_id}"
                        )

                    target_connection.execute(
                        """
                        UPDATE web_posts
                        SET title=?, content_text=?, content_length=?,
                            post_images_count=?, topic_relevant=?, raw_sample_json=?, status=?,
                            updated_at=?
                        WHERE id=? AND platform_key='bilibili'
                        """,
                        (
                            staged_row["title"],
                            staged_row["content_text"],
                            staged_row["content_length"],
                            staged_row["post_images_count"],
                            staged_row["topic_relevant"],
                            staged_row["raw_sample_json"],
                            staged_row["status"],
                            staged_row["updated_at"],
                            web_post_id,
                        ),
                    )
                    target_connection.execute(
                        "DELETE FROM web_post_images WHERE web_post_id=? AND image_role='content'",
                        (web_post_id,),
                    )
                    target_connection.executemany(
                        """
                        INSERT INTO web_post_images (
                            web_post_id, image_index, image_url, image_role,
                            local_path, width, height, mime_type, sha256,
                            raw_image_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                web_post_id,
                                image["image_index"],
                                image["image_url"],
                                image["image_role"],
                                image["local_path"],
                                image["width"],
                                image["height"],
                                image["mime_type"],
                                image["sha256"],
                                image["raw_image_json"],
                                image["created_at"],
                            )
                            for image in content_images
                        ],
                    )
                    promoted_count += 1

                for item in items:
                    web_post_id = int(item["web_post_id"])
                    row = existing_target_row(target_connection, web_post_id)
                    urls = repaired_content_image_urls(target_connection, web_post_id)
                    if sha256_text(str(row["content_text"] or "")) != str(
                        item["repaired_content_sha256"]
                    ):
                        raise RuntimeError(
                            f"promoted content verification failed for row {web_post_id}"
                        )
                    if sha256_text(json_text(urls)) != str(
                        item["repaired_images_sha256"]
                    ):
                        raise RuntimeError(
                            f"promoted image verification failed for row {web_post_id}"
                        )
                target_connection.commit()
            except Exception:
                target_connection.rollback()
                raise
    return {
        "promoted_count": promoted_count,
        "already_promoted_count": already_promoted_count,
    }


def clone_repair_state_for_target(
    *,
    source_state_path: Path,
    destination_state_path: Path,
    staged_db_path: Path,
    target_db_path: Path,
) -> dict[str, Any]:
    if destination_state_path.exists():
        raise RuntimeError(
            f"destination repair state already exists: {destination_state_path}"
        )
    with sqlite_connect(source_state_path, readonly=True) as state_connection:
        meta = meta_values(state_connection)
        if meta.get("target_db_path") != str(staged_db_path.resolve()):
            raise RuntimeError("source repair state does not belong to staged database")
        staged_validation = repair_state_validation(
            staged_db_path,
            state_connection,
            meta,
        )
        target_validation = repair_state_validation(
            target_db_path,
            state_connection,
            meta,
        )
        if not staged_validation["ok"] or not target_validation["ok"]:
            raise RuntimeError(
                "staged or promoted target validation failed before state clone"
            )
        external_invariants = assert_external_invariants(target_db_path, meta)
    backup = create_online_backup(source_state_path, destination_state_path)
    with sqlite_connect(destination_state_path) as clone_connection:
        clone_connection.execute(
            """
            INSERT INTO repair_meta(key, value) VALUES ('target_db_path', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(target_db_path.resolve()),),
        )
        clone_connection.executemany(
            """
            INSERT INTO repair_meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            [
                ("cloned_from_state_db", str(source_state_path.resolve())),
                ("cloned_from_staged_db", str(staged_db_path.resolve())),
                ("cloned_at", utc_now()),
                ("cloned_by_code_commit", git_commit()),
                ("target_db_sha256_at_clone", sha256_file(target_db_path)),
            ],
        )
        clone_connection.commit()
        cloned_meta = meta_values(clone_connection)
        cloned_validation = repair_state_validation(
            target_db_path,
            clone_connection,
            cloned_meta,
        )
    if not cloned_validation["ok"]:
        raise RuntimeError("cloned repair state failed target validation")
    return {
        "source_state": str(source_state_path.resolve()),
        "destination_state": str(destination_state_path.resolve()),
        "target_db": str(target_db_path.resolve()),
        "backup": backup,
        "staged_validation": staged_validation,
        "target_validation": target_validation,
        "cloned_validation": cloned_validation,
        "external_invariants": external_invariants,
    }


def write_report(config: PromotionConfig, payload: dict[str, Any]) -> Path:
    report_dir = ensure_dir(config.report_dir)
    report_path = report_dir / "promotion_report.json"
    report_path.write_text(json_text(payload, pretty=True) + "\n", encoding="utf-8")
    return report_path


def run_promotion(config: PromotionConfig) -> tuple[int, dict[str, Any]]:
    with sqlite_connect(config.state_db_path, readonly=True) as state_connection:
        meta = meta_values(state_connection)
        inputs = verify_inputs(config, state_connection)
        items = staged_success_items(state_connection)
        if not items:
            raise RuntimeError("repair sidecar has no succeeded rows to promote")
        payload: dict[str, Any] = {
            "schema_version": 1,
            "generated_at": utc_now(),
            "code_commit": git_commit(),
            "apply": config.apply,
            "target_db": str(config.target_db_path),
            "staged_db": str(config.staged_db_path),
            "state_db": str(config.state_db_path),
            "backup_path": str(config.backup_path),
            "repair_run_id": meta["run_id"],
            "repair_source_manifest_sha256": meta["source_manifest_sha256"],
            "planned_count": len(items),
            "platform_post_ids_sha256": sha256_text(
                json_text([str(item["platform_post_id"]) for item in items])
            ),
            "inputs": inputs,
        }
        if not config.apply:
            payload["status"] = "dry_run"
            payload["promoted_count"] = 0
            report_path = write_report(config, payload)
            payload["report_path"] = str(report_path)
            return 0, payload

        before_control = control_plane_fingerprint(config.target_db_path)
        before_non_bilibili = non_bilibili_fingerprint(config.target_db_path)
        promotion_counts = promote_successes(config, state_connection, items)
        if control_plane_fingerprint(config.target_db_path) != before_control:
            raise RuntimeError("crawl control plane changed during promotion")
        if non_bilibili_fingerprint(config.target_db_path) != before_non_bilibili:
            raise RuntimeError("non-Bilibili rows changed during promotion")
        external_invariants = assert_external_invariants(config.target_db_path, meta)
        target_validation = repair_state_validation(
            config.target_db_path,
            state_connection,
            meta,
        )
        if not target_validation["ok"]:
            raise RuntimeError(
                f"promoted target validation failed: {target_validation}"
            )
        payload.update(
            {
                "status": "completed",
                **promotion_counts,
                "finished_at": utc_now(),
                "target": target_summary(config.target_db_path),
                "target_validation": target_validation,
                "external_invariants": external_invariants,
                "target_sha256_after": sha256_file(config.target_db_path),
            }
        )
        report_path = write_report(config, payload)
        payload["report_path"] = str(report_path)
        return 0, payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-db", default=str(DEFAULT_DB))
    parser.add_argument("--staged-db", required=True)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--backup-path", required=True)
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--expected-target-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-default-db-promotion", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = PromotionConfig(
        target_db_path=Path(args.target_db).expanduser().resolve(),
        staged_db_path=Path(args.staged_db).expanduser().resolve(),
        state_db_path=Path(args.state_db).expanduser().resolve(),
        backup_path=Path(args.backup_path).expanduser().resolve(),
        report_dir=Path(args.report_dir).expanduser().resolve(),
        expected_target_sha256=str(args.expected_target_sha256).strip(),
        apply=bool(args.apply),
        confirm_default_db_promotion=bool(args.confirm_default_db_promotion),
    )
    lock_path = config.report_dir / "promotion.sqlite"
    with repair_lock(lock_path):
        return_code, result = run_promotion(config)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
