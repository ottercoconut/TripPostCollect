#!/usr/bin/env python3
"""Apply operator-approved image exclusions and release deferred posts for retry."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Sequence

import historical_image_worker as worker
from trippostcollect.artifacts.historical_image_materialization import (
    approved_historical_image_exclusions,
    database_integrity,
    protected_database_digests,
    sha256_file,
    sqlite_backup,
)
from trippostcollect.artifacts.image_candidates import content_image_candidates
from trippostcollect.core.paths import DATA_ROOT, DEFAULT_DB, IMAGE_MATERIALIZATION_RUNTIME
from trippostcollect.db.bootstrap import bootstrap_connection


CAMPAIGN_ID = worker.CAMPAIGN_ID
SUPPORTED_PLATFORMS = tuple(worker.PLATFORM_ORDER)
DEFAULT_STATE = (
    IMAGE_MATERIALIZATION_RUNTIME
    / CAMPAIGN_ID
    / "background-worker"
    / "state.json"
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--platform", choices=SUPPORTED_PLATFORMS, required=True)
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PLATFORM_POST_ID:SOURCE_INDEX",
    )
    parser.add_argument("--release-post-id", action="append", default=[])
    parser.add_argument("--reason", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-operator-exclusion", action="store_true")
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


def _parse_targets(values: Sequence[str]) -> list[tuple[str, int]]:
    targets: list[tuple[str, int]] = []
    for value in values:
        post_id, separator, source_index = value.rpartition(":")
        if not separator or not post_id:
            raise ValueError(f"invalid exclusion target: {value}")
        try:
            index = int(source_index)
        except ValueError as exc:
            raise ValueError(f"invalid exclusion source index: {value}") from exc
        if index < 0:
            raise ValueError(f"negative exclusion source index: {value}")
        targets.append((post_id, index))
    if len(set(targets)) != len(targets):
        raise ValueError("duplicate exclusion target")
    return targets


def _failure_evidence(
    state: dict[str, Any], platform_key: str, post_id: str, source_index: int
) -> dict[str, Any]:
    deferred = (
        (state.get("deferred_posts") or {}).get(platform_key, {}).get(post_id)
    )
    if not isinstance(deferred, dict):
        raise ValueError(f"post is not deferred: {post_id}")
    for event in reversed(deferred.get("history") or []):
        for failure in event.get("failures") or []:
            if int(failure.get("source_index") or 0) == source_index:
                return {
                    "deferred_reason": deferred.get("reason"),
                    "failure_codes": deferred.get("failure_codes") or [],
                    "failure": failure,
                    "source_report": event.get("report"),
                }
    raise ValueError(f"deferred failure identity is absent: {post_id}:{source_index}")


def _resolve_targets(
    conn: sqlite3.Connection,
    state: dict[str, Any],
    platform_key: str,
    targets: Sequence[tuple[str, int]],
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for post_id, source_index in targets:
        row = conn.execute(
            """
            SELECT id, raw_sample_json
            FROM web_posts
            WHERE platform_key=? AND platform_post_id=?
            """,
            (platform_key, post_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"{platform_key} post does not exist: {post_id}")
        raw = json.loads(row[1] or "{}")
        candidates = content_image_candidates(platform_key, raw)
        candidate = next(
            (item for item in candidates if item.source_index == source_index), None
        )
        if candidate is None:
            raise ValueError(f"authoritative image does not exist: {post_id}:{source_index}")
        evidence = _failure_evidence(state, platform_key, post_id, source_index)
        failure_key = str((evidence.get("failure") or {}).get("source_asset_key") or "")
        if failure_key != candidate.source_asset_key:
            raise ValueError(f"deferred source identity changed: {post_id}:{source_index}")
        image_row = conn.execute(
            """
            SELECT id, image_url
            FROM web_post_images
            WHERE web_post_id=? AND image_role='content' AND image_index=?
            """,
            (int(row[0]), source_index),
        ).fetchone()
        if image_row is not None and str(image_row[1]) != candidate.source_url:
            raise ValueError(f"persisted image URL changed: {post_id}:{source_index}")
        resolved.append(
            {
                "web_post_id": int(row[0]),
                "platform_post_id": post_id,
                "source_index": source_index,
                "source_asset_key": candidate.source_asset_key,
                "source_url": candidate.source_url,
                "image_row_id": int(image_row[0]) if image_row else None,
                "candidate_count_before": len(candidates),
                "evidence": evidence,
            }
        )
    return resolved


def _apply_database(
    db_path: Path,
    resolved: Sequence[dict[str, Any]],
    *,
    platform_key: str,
    reason: str,
    backup_dir: Path,
) -> dict[str, Any]:
    backup = sqlite_backup(db_path, backup_dir / db_path.name)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        bootstrap_connection(conn, sync_jobs=False)
        protected_before = protected_database_digests(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            approved_at = utc_iso()
            deleted_rows = 0
            affected_post_ids: set[int] = set()
            for item in resolved:
                conn.execute(
                    """
                    INSERT INTO historical_image_exclusions (
                      campaign_id, platform_key, platform_post_id, source_index,
                      source_asset_key, source_url, reason, evidence_json,
                      approved_by, approved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'user', ?)
                    ON CONFLICT(campaign_id, platform_key, platform_post_id, source_asset_key)
                    DO UPDATE SET
                      reason=excluded.reason,
                      evidence_json=excluded.evidence_json,
                      approved_by=excluded.approved_by,
                      approved_at=excluded.approved_at
                    """,
                    (
                        CAMPAIGN_ID,
                        platform_key,
                        item["platform_post_id"],
                        item["source_index"],
                        item["source_asset_key"],
                        item["source_url"],
                        reason,
                        json.dumps(item["evidence"], ensure_ascii=False, sort_keys=True),
                        approved_at,
                    ),
                )
                cursor = conn.execute(
                    """
                    DELETE FROM web_post_images
                    WHERE web_post_id=? AND image_role='content' AND image_index=?
                      AND image_url=?
                    """,
                    (
                        item["web_post_id"],
                        item["source_index"],
                        item["source_url"],
                    ),
                )
                deleted_rows += int(cursor.rowcount or 0)
                affected_post_ids.add(int(item["web_post_id"]))

            exclusions = approved_historical_image_exclusions(conn)
            for web_post_id in affected_post_ids:
                platform_post_id, raw_json = conn.execute(
                    "SELECT platform_post_id, raw_sample_json FROM web_posts WHERE id=?",
                    (web_post_id,),
                ).fetchone()
                excluded_keys = exclusions.get(
                    (platform_key, str(platform_post_id)), set()
                )
                remaining = sum(
                    candidate.source_asset_key not in excluded_keys
                    for candidate in content_image_candidates(
                        platform_key, json.loads(raw_json or "{}")
                    )
                )
                conn.execute(
                    "UPDATE web_posts SET post_images_count=? WHERE id=?",
                    (remaining, web_post_id),
                )
            integrity = database_integrity(conn)
            if integrity != {"quick_check": "ok", "foreign_key_violations": 0}:
                raise RuntimeError(f"database integrity failed: {integrity}")
            if protected_database_digests(conn) != protected_before:
                raise RuntimeError("operator exclusion changed protected post/discovery state")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return {
        "backup": backup,
        "deleted_image_rows": deleted_rows,
        "approved_exclusions": len(resolved),
        "database_sha256_after": sha256_file(db_path),
        "integrity": integrity,
        "protected_invariants": protected_before,
    }


def _release_state(
    state_path: Path,
    state: dict[str, Any],
    *,
    platform_key: str,
    post_ids: set[str],
    report_path: Path,
) -> None:
    deferred = (state.get("deferred_posts") or {}).get(platform_key, {})
    for post_id in post_ids:
        deferred.pop(post_id, None)
    state["retry_events"] = list(state.get("retry_events") or [])
    state["retry_events"].append(
        {
            "event": "operator_resolution_applied",
            "recorded_at": utc_iso(),
            "platform": platform_key,
            "platform_post_ids": sorted(post_ids),
            "report": str(report_path),
        }
    )
    state.update(
        {
            "status": "ready_to_resume",
            "historical_data_complete": False,
            "error": None,
            "active_retry": None,
            "operator_resolution_report": str(report_path),
        }
    )
    state.pop("review_required_at", None)
    paths = worker._paths(state_path.parent)
    worker._sync_deferred_registry(paths["deferred"], state)
    worker._write_state(state_path, state)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    db_path = Path(args.db).expanduser().resolve(strict=True)
    state_path = Path(args.state).expanduser().resolve(strict=True)
    state = _read_object(state_path)
    if state.get("campaign_id") != CAMPAIGN_ID:
        raise SystemExit("worker state belongs to another campaign")
    if state.get("process_alive") is True or worker._pid_alive(
        worker._read_pid(worker._paths(state_path.parent)["pid"])
    ):
        raise SystemExit("historical image worker must be stopped before resolution")
    targets = _parse_targets(args.exclude)
    platform_key = str(args.platform)
    release_post_ids = {str(value) for value in args.release_post_id if value}
    if not targets and not release_post_ids:
        raise SystemExit("at least one exclusion or release post ID is required")
    deferred = (state.get("deferred_posts") or {}).get(platform_key, {})
    unknown_release_ids = release_post_ids - set(deferred)
    if unknown_release_ids:
        raise SystemExit(
            f"release post IDs are not deferred for {platform_key}: "
            f"{sorted(unknown_release_ids)}"
        )
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        resolved = _resolve_targets(conn, state, platform_key, targets)
    release_post_ids.update(item["platform_post_id"] for item in resolved)
    report: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": CAMPAIGN_ID,
        "created_at": utc_iso(),
        "status": "planned",
        "platform": platform_key,
        "database": str(db_path),
        "database_sha256_before": sha256_file(db_path),
        "reason": str(args.reason),
        "exclusions": resolved,
        "release_post_ids": sorted(release_post_ids),
    }
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if not args.confirm_operator_exclusion:
        raise SystemExit("--apply requires --confirm-operator-exclusion")
    stamp = utc_stamp()
    stage = worker.STAGE_BY_PLATFORM[platform_key]
    report_dir = IMAGE_MATERIALIZATION_RUNTIME / CAMPAIGN_ID / stage / f"operator-resolution-{stamp}"
    report_path = report_dir / "report.json"
    backup_dir = (
        DATA_ROOT
        / "backups"
        / "historical_images"
        / CAMPAIGN_ID
        / stage
        / f"operator-resolution-{stamp}"
    )
    report.update(
        _apply_database(
            db_path,
            resolved,
            platform_key=platform_key,
            reason=str(args.reason),
            backup_dir=backup_dir,
        )
    )
    report.update({"status": "completed", "finished_at": utc_iso()})
    worker._write_text_atomic(
        report_path,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _release_state(
        state_path,
        state,
        platform_key=platform_key,
        post_ids=release_post_ids,
        report_path=report_path,
    )
    print(
        json.dumps(
            {
                "status": "completed",
                "report": str(report_path),
                "backup": report["backup"]["path"],
                "deleted_image_rows": report["deleted_image_rows"],
                "released_posts": sorted(release_post_ids),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
