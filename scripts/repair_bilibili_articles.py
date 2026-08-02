#!/usr/bin/env python3
"""Resumable, evidence-preserving repair for historical Bilibili article rows."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import random
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from mediacrawler_crawl import (
    BILIBILI_DETAIL_PACING_SECONDS,
    BilibiliArticleDetailError,
    fetch_bilibili_article_detail,
    hydrate_bilibili_article_record,
    load_cookie_snapshot,
    normalize_bilibili_article_record,
    validate_formal_record,
)
from trippostcollect.core.paths import (
    BILIBILI_REPAIR_BACKUP_ROOT,
    BILIBILI_REPAIR_OUTPUT,
    BILIBILI_REPAIR_RUNTIME_ROOT,
    DEFAULT_DB,
    ensure_dir,
    ensure_parent,
)


UTC = timezone.utc
TERMINAL_STATUSES = frozenset(
    {"succeeded", "permanent_unavailable", "invalid_detail", "conflict"}
)
REPAIRABLE_STATUSES = frozenset({"pending", "retryable"})
STATE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RepairConfig:
    db_path: Path
    state_db_path: Path
    report_dir: Path
    backup_path: Path
    expected_baseline_sha256: str
    apply: bool
    confirm_default_db_repair: bool
    max_items: int
    session_size: int
    pacing_min: float
    pacing_max: float
    session_pause_min: float
    session_pause_max: float
    retry_delay_seconds: int
    source_limit: int
    only_ids: frozenset[str]


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def json_text(value: Any, *, pretty: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def sqlite_connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(ensure_parent(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def sqlite_checks(path: Path) -> dict[str, Any]:
    with sqlite_connect(path, readonly=True) as connection:
        quick_check = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
    return {
        "quick_check": quick_check,
        "foreign_key_errors": foreign_keys,
        "ok": quick_check == ["ok"] and not foreign_keys,
    }


def create_online_backup(source_path: Path, destination_path: Path) -> dict[str, Any]:
    source = source_path.expanduser().resolve()
    destination = destination_path.expanduser().resolve()
    if source == destination:
        raise RuntimeError("backup destination must differ from source database")
    if destination.exists():
        raise RuntimeError(f"backup destination already exists: {destination}")
    ensure_parent(destination)
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists():
        raise RuntimeError(f"partial backup already exists: {partial}")
    try:
        with sqlite_connect(source, readonly=True) as source_connection:
            with sqlite_connect(partial) as destination_connection:
                source_connection.backup(destination_connection)
                destination_connection.commit()
        checks = sqlite_checks(partial)
        if not checks["ok"]:
            raise RuntimeError(f"backup SQLite verification failed: {checks}")
        partial.rename(destination)
    except Exception:
        if partial.exists():
            partial.unlink()
        raise
    return {
        "source": str(source),
        "destination": str(destination),
        "created_at": utc_now(),
        "bytes": destination.stat().st_size,
        "sha256": sha256_file(destination),
        "sqlite": sqlite_checks(destination),
    }


def initialize_state_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS repair_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS repair_items (
            web_post_id INTEGER PRIMARY KEY,
            platform_post_id TEXT NOT NULL UNIQUE,
            original_content_sha256 TEXT NOT NULL,
            original_content_length INTEGER NOT NULL,
            original_raw_sha256 TEXT NOT NULL,
            original_image_count INTEGER NOT NULL,
            original_images_sha256 TEXT NOT NULL,
            original_keyword TEXT,
            original_canonical_url TEXT,
            original_artifact_dir TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error_type TEXT,
            last_error_code INTEGER,
            last_error TEXT,
            next_retry_at TEXT,
            detail_source TEXT,
            detail_content_length INTEGER,
            detail_image_count INTEGER,
            repaired_content_sha256 TEXT,
            repaired_images_sha256 TEXT,
            repaired_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS repair_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            web_post_id INTEGER,
            platform_post_id TEXT,
            details_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_repair_items_status
        ON repair_items(status, next_retry_at, web_post_id);
        """
    )
    connection.commit()


def meta_values(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row["key"]): str(row["value"])
        for row in connection.execute("SELECT key, value FROM repair_meta")
    }


def set_meta(connection: sqlite3.Connection, values: dict[str, Any]) -> None:
    connection.executemany(
        """
        INSERT INTO repair_meta(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        [(key, str(value)) for key, value in values.items()],
    )


def image_rows_by_post(connection: sqlite3.Connection) -> dict[int, list[dict[str, Any]]]:
    rows: dict[int, list[dict[str, Any]]] = {}
    for row in connection.execute(
        """
        SELECT i.web_post_id, i.image_index, i.image_url, i.image_role,
               i.local_path, i.width, i.height, i.mime_type, i.sha256
        FROM web_post_images i
        JOIN web_posts p ON p.id=i.web_post_id
        WHERE p.platform_key='bilibili'
        ORDER BY i.web_post_id, i.image_index, i.id
        """
    ):
        item = dict(row)
        rows.setdefault(int(row["web_post_id"]), []).append(item)
    return rows


def build_source_manifest(path: Path) -> tuple[list[dict[str, Any]], str]:
    with sqlite_connect(path, readonly=True) as connection:
        images = image_rows_by_post(connection)
        manifest: list[dict[str, Any]] = []
        for row in connection.execute(
            """
            SELECT id, platform_post_id, keyword, canonical_url, artifact_dir,
                   content_text, content_length, raw_sample_json, post_images_count
            FROM web_posts
            WHERE platform_key='bilibili'
            ORDER BY id
            """
        ):
            post_id = str(row["platform_post_id"] or "").strip()
            if not post_id:
                raise RuntimeError(f"Bilibili row {row['id']} has no platform_post_id")
            image_rows = images.get(int(row["id"]), [])
            manifest.append(
                {
                    "web_post_id": int(row["id"]),
                    "platform_post_id": post_id,
                    "keyword": row["keyword"],
                    "canonical_url": row["canonical_url"],
                    "artifact_dir": row["artifact_dir"],
                    "content_sha256": sha256_text(str(row["content_text"] or "")),
                    "content_length": int(row["content_length"] or 0),
                    "raw_sha256": sha256_text(str(row["raw_sample_json"] or "")),
                    "post_images_count": int(row["post_images_count"] or 0),
                    "images_sha256": sha256_text(json_text(image_rows)),
                }
            )
    ids = [item["platform_post_id"] for item in manifest]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Bilibili platform_post_id values are not unique")
    return manifest, sha256_text(json_text(manifest))


def table_fingerprint(connection: sqlite3.Connection, table: str) -> str:
    rows = [list(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    return sha256_text(json_text(rows))


def control_plane_fingerprint(path: Path) -> str:
    with sqlite_connect(path, readonly=True) as connection:
        payload = {
            table: table_fingerprint(connection, table)
            for table in (
                "crawl_jobs",
                "crawl_discovery_checkpoints",
                "crawl_discovery_seen_candidates",
            )
        }
    return sha256_text(json_text(payload))


def non_bilibili_fingerprint(path: Path) -> str:
    with sqlite_connect(path, readonly=True) as connection:
        posts = [
            list(row)
            for row in connection.execute(
                "SELECT * FROM web_posts WHERE platform_key!='bilibili' ORDER BY id"
            )
        ]
        images = [
            list(row)
            for row in connection.execute(
                """
                SELECT i.*
                FROM web_post_images i
                JOIN web_posts p ON p.id=i.web_post_id
                WHERE p.platform_key!='bilibili'
                ORDER BY i.id
                """
            )
        ]
    return sha256_text(json_text({"posts": posts, "images": images}))


def initialize_repair_state(
    target_path: Path,
    state_connection: sqlite3.Connection,
    report_dir: Path,
) -> dict[str, str]:
    initialize_state_schema(state_connection)
    current_meta = meta_values(state_connection)
    if current_meta:
        expected_path = str(target_path.resolve())
        if current_meta.get("target_db_path") != expected_path:
            raise RuntimeError("state database belongs to a different target database")
        if current_meta.get("schema_version") != str(STATE_SCHEMA_VERSION):
            raise RuntimeError("unsupported Bilibili repair state schema")
        return current_meta

    checks = sqlite_checks(target_path)
    if not checks["ok"]:
        raise RuntimeError(f"target SQLite verification failed: {checks}")
    manifest, manifest_sha256 = build_source_manifest(target_path)
    run_id = "bilibili-repair-" + uuid.uuid4().hex
    baseline_sha256 = sha256_file(target_path)
    control_sha256 = control_plane_fingerprint(target_path)
    non_bilibili_sha256 = non_bilibili_fingerprint(target_path)
    created_at = utc_now()
    state_connection.execute("BEGIN IMMEDIATE")
    try:
        set_meta(
            state_connection,
            {
                "schema_version": STATE_SCHEMA_VERSION,
                "run_id": run_id,
                "created_at": created_at,
                "target_db_path": str(target_path.resolve()),
                "baseline_db_sha256": baseline_sha256,
                "source_manifest_sha256": manifest_sha256,
                "source_count": len(manifest),
                "source_id_set_sha256": sha256_text(
                    json_text([item["platform_post_id"] for item in manifest])
                ),
                "control_plane_sha256": control_sha256,
                "non_bilibili_sha256": non_bilibili_sha256,
                "code_commit": git_commit(),
            },
        )
        state_connection.executemany(
            """
            INSERT INTO repair_items (
                web_post_id, platform_post_id, original_content_sha256,
                original_content_length, original_raw_sha256,
                original_image_count, original_images_sha256,
                original_keyword, original_canonical_url, original_artifact_dir,
                status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            [
                (
                    item["web_post_id"],
                    item["platform_post_id"],
                    item["content_sha256"],
                    item["content_length"],
                    item["raw_sha256"],
                    item["post_images_count"],
                    item["images_sha256"],
                    item["keyword"],
                    item["canonical_url"],
                    item["artifact_dir"],
                    created_at,
                )
                for item in manifest
            ],
        )
        state_connection.execute(
            """
            INSERT INTO repair_events(event_at, event_type, details_json)
            VALUES (?, 'manifest_created', ?)
            """,
            (created_at, json_text({"count": len(manifest), "sha256": manifest_sha256})),
        )
        state_connection.commit()
    except Exception:
        state_connection.rollback()
        raise

    report_dir = ensure_dir(report_dir)
    manifest_path = report_dir / "source_manifest.json"
    manifest_path.write_text(json_text(manifest, pretty=True) + "\n", encoding="utf-8")
    set_meta(
        state_connection,
        {
            "source_manifest_path": manifest_path,
            "source_manifest_file_sha256": sha256_file(manifest_path),
        },
    )
    state_connection.commit()
    return meta_values(state_connection)


def add_event(
    connection: sqlite3.Connection,
    event_type: str,
    *,
    item: sqlite3.Row | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO repair_events (
            event_at, event_type, web_post_id, platform_post_id, details_json
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            event_type,
            int(item["web_post_id"]) if item is not None else None,
            str(item["platform_post_id"]) if item is not None else None,
            json_text(details or {}),
        ),
    )


def read_json_object(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def current_image_rows(connection: sqlite3.Connection, web_post_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT web_post_id, image_index, image_url, image_role,
                   local_path, width, height, mime_type, sha256
            FROM web_post_images
            WHERE web_post_id=?
            ORDER BY image_index, id
            """,
            (web_post_id,),
        )
    ]


def existing_target_row(connection: sqlite3.Connection, web_post_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM web_posts WHERE id=? AND platform_key='bilibili'",
        (web_post_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Bilibili target row disappeared: {web_post_id}")
    return row


def prepare_search_record(
    target_row: sqlite3.Row,
    existing_images: list[dict[str, Any]],
) -> dict[str, Any]:
    raw = read_json_object(target_row["raw_sample_json"])
    author = read_json_object(target_row["author_json"])
    metrics = read_json_object(target_row["metrics_json"])
    preview_urls = raw.get("search_preview_urls")
    if not isinstance(preview_urls, list):
        preview_urls = raw.get("image_urls")
    if not isinstance(preview_urls, list):
        preview_urls = [
            str(item["image_url"])
            for item in existing_images
            if item.get("image_role") == "content" and item.get("image_url")
        ]
    item = dict(raw)
    item.update(
        {
            "id": str(target_row["platform_post_id"]),
            "title": raw.get("title") or target_row["title"],
            "desc": raw.get("search_desc") or raw.get("desc") or "",
            "arcurl": target_row["canonical_url"] or target_row["source_url"],
            "image_urls": preview_urls,
            "pubdate": raw.get("pubdate") or raw.get("published_at") or target_row["published_at"],
            "author": raw.get("author") or raw.get("nickname") or target_row["author_display_name"],
            "mid": raw.get("mid") or raw.get("user_id") or target_row["author_platform_id"],
            "like": raw.get("like") if raw.get("like") is not None else target_row["post_likes_count"],
            "reply": raw.get("reply") if raw.get("reply") is not None else target_row["post_comments_count"],
            "view": raw.get("view") if raw.get("view") is not None else target_row["post_views_count"],
        }
    )
    normalized = normalize_bilibili_article_record(
        item,
        str(raw.get("source_keyword") or target_row["keyword"] or ""),
    )
    if normalized is None:
        raise RuntimeError("cannot reconstruct Bilibili search evidence")
    follower_count = target_row["author_followers_count"]
    normalized.update(
        {
            "followers_count": follower_count,
            "author_followers_count": follower_count,
            "followers_observed": bool(
                raw.get("followers_observed") is True
                or author.get("followers_observed") is True
                or follower_count is not None
            ),
            "author_followers_source": (
                raw.get("author_followers_source")
                or author.get("followers_source")
                or "relation_stat"
            ),
            "liked_count": raw.get("liked_count", metrics.get("liked_count")),
            "comment_count": raw.get("comment_count", metrics.get("comments_count")),
            "view_count": raw.get("view_count", metrics.get("views_count")),
        }
    )
    return normalized


def selected_items(
    connection: sqlite3.Connection,
    *,
    max_items: int,
    source_limit: int,
    only_ids: frozenset[str],
) -> list[sqlite3.Row]:
    now = utc_now()
    rows = connection.execute(
        """
        SELECT *
        FROM repair_items
        WHERE status='pending'
           OR (status='retryable' AND COALESCE(next_retry_at, '') <= ?)
        ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                 CASE WHEN status='retryable' THEN COALESCE(next_retry_at, '') ELSE '' END,
                 web_post_id
        """,
        (now,),
    ).fetchall()
    if source_limit:
        scoped_ids = {
            int(row[0])
            for row in connection.execute(
                "SELECT web_post_id FROM repair_items ORDER BY web_post_id LIMIT ?",
                (source_limit,),
            )
        }
        rows = [row for row in rows if int(row["web_post_id"]) in scoped_ids]
    if only_ids:
        rows = [row for row in rows if str(row["platform_post_id"]) in only_ids]
    return rows[:max_items]


def scoped_state_counts(
    connection: sqlite3.Connection,
    *,
    source_limit: int,
    only_ids: frozenset[str],
) -> dict[str, int]:
    rows = connection.execute(
        "SELECT web_post_id, platform_post_id, status FROM repair_items ORDER BY web_post_id"
    ).fetchall()
    if source_limit:
        rows = rows[:source_limit]
    if only_ids:
        rows = [row for row in rows if str(row["platform_post_id"]) in only_ids]
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row["status"])
        counts[status] = counts.get(status, 0) + 1
    for status in (*REPAIRABLE_STATUSES, *TERMINAL_STATUSES):
        counts.setdefault(status, 0)
    counts["total"] = len(rows)
    return counts


def fetch_repair_article_detail(
    post_id: str,
    cookie_header: str,
) -> tuple[dict[str, Any], int, float]:
    """Make one detail request so a rate-limit response freezes the whole run."""

    return fetch_bilibili_article_detail(post_id, cookie_header), 1, 0.0


def migrate_legacy_retryable_backoff(
    connection: sqlite3.Connection,
    *,
    base_delay_seconds: int,
) -> None:
    meta = meta_values(connection)
    if "global_retryable_streak" in meta:
        return
    streak = trailing_retryable_streak(connection)
    set_meta(connection, {"global_retryable_streak": streak})
    next_request_at = meta.get("global_next_request_at")
    latest_failure = connection.execute(
        "SELECT event_at FROM repair_events WHERE event_type='retryable' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if streak > 0 and next_request_at and latest_failure:
        failure_at = datetime.fromisoformat(str(latest_failure["event_at"]))
        if failure_at.tzinfo is None:
            raise RuntimeError("retryable event_at must include a timezone")
        multiplier = 2 ** min(streak - 1, 6)
        effective_delay = min(max(0, base_delay_seconds) * multiplier, 3600)
        migrated_deadline = (
            failure_at + timedelta(seconds=effective_delay)
        ).isoformat(timespec="seconds")
        if migrated_deadline > next_request_at:
            set_meta(connection, {"global_next_request_at": migrated_deadline})
            next_request_at = migrated_deadline
    add_event(
        connection,
        "global_retryable_backoff_migrated",
        details={"streak": streak, "next_request_at": next_request_at},
    )
    connection.commit()


def active_global_cooldown(
    connection: sqlite3.Connection,
    *,
    base_delay_seconds: int,
) -> dict[str, str] | None:
    migrate_legacy_retryable_backoff(
        connection,
        base_delay_seconds=base_delay_seconds,
    )
    meta = meta_values(connection)
    next_request_at = meta.get("global_next_request_at")
    if not next_request_at:
        return None
    try:
        deadline = datetime.fromisoformat(next_request_at)
    except ValueError as exc:
        raise RuntimeError("invalid global_next_request_at in repair state") from exc
    if deadline.tzinfo is None:
        raise RuntimeError("global_next_request_at must include a timezone")
    if deadline > datetime.now(UTC):
        return {
            "next_request_at": next_request_at,
            "reason": meta.get("global_cooldown_reason", "retryable_failure"),
        }
    connection.execute(
        "DELETE FROM repair_meta WHERE key IN ('global_next_request_at', 'global_cooldown_reason')"
    )
    add_event(
        connection,
        "global_cooldown_expired",
        details={"next_request_at": next_request_at},
    )
    connection.commit()
    return None


def trailing_retryable_streak(connection: sqlite3.Connection) -> int:
    streak = 0
    for row in connection.execute(
        """
        SELECT event_type
        FROM repair_events
        WHERE event_type IN ('retryable', 'succeeded', 'reconciled_after_commit')
        ORDER BY id DESC
        """
    ):
        if str(row["event_type"]) != "retryable":
            break
        streak += 1
    return streak


def current_retryable_streak(connection: sqlite3.Connection) -> int:
    value = meta_values(connection).get("global_retryable_streak")
    if value is None:
        return trailing_retryable_streak(connection)
    try:
        return max(0, int(value))
    except ValueError as exc:
        raise RuntimeError("invalid global_retryable_streak in repair state") from exc


def reset_global_retryable_streak(connection: sqlite3.Connection) -> None:
    previous = current_retryable_streak(connection)
    if previous <= 0:
        return
    set_meta(
        connection,
        {
            "global_retryable_streak": 0,
            "global_retryable_last_success_at": utc_now(),
        },
    )
    add_event(
        connection,
        "global_retryable_streak_reset",
        details={"previous_streak": previous},
    )
    connection.commit()


def start_global_cooldown(
    connection: sqlite3.Connection,
    item: sqlite3.Row,
    *,
    next_request_at: str,
    reason: str,
) -> None:
    current = meta_values(connection).get("global_next_request_at")
    effective_next_request_at = max(current or "", next_request_at)
    set_meta(
        connection,
        {
            "global_next_request_at": effective_next_request_at,
            "global_cooldown_reason": reason,
        },
    )
    add_event(
        connection,
        "global_cooldown_started",
        item=item,
        details={
            "next_request_at": effective_next_request_at,
            "reason": reason,
        },
    )


def mark_attempt(connection: sqlite3.Connection, item: sqlite3.Row) -> None:
    connection.execute(
        """
        UPDATE repair_items
        SET attempt_count=attempt_count+1, last_error_type=NULL,
            last_error_code=NULL, last_error=NULL, next_retry_at=NULL,
            updated_at=?
        WHERE web_post_id=?
        """,
        (utc_now(), int(item["web_post_id"])),
    )
    add_event(connection, "attempt_started", item=item)
    connection.commit()


def mark_failure(
    connection: sqlite3.Connection,
    item: sqlite3.Row,
    *,
    status: str,
    error_type: str,
    error: str,
    code: int | None = None,
    retry_delay_seconds: int = 0,
    details: dict[str, Any] | None = None,
) -> None:
    next_retry_at = None
    retryable_streak = 0
    effective_retry_delay_seconds = 0
    if status == "retryable":
        retryable_streak = current_retryable_streak(connection) + 1
        multiplier = 2 ** min(retryable_streak - 1, 6)
        effective_retry_delay_seconds = min(
            max(0, retry_delay_seconds) * multiplier,
            3600,
        )
        next_retry_at = (
            datetime.now(UTC) + timedelta(seconds=effective_retry_delay_seconds)
        ).isoformat(timespec="seconds")
        set_meta(
            connection,
            {
                "global_retryable_streak": retryable_streak,
                "global_retryable_last_failure_at": utc_now(),
            },
        )
    connection.execute(
        """
        UPDATE repair_items
        SET status=?, last_error_type=?, last_error_code=?, last_error=?,
            next_retry_at=?, updated_at=?
        WHERE web_post_id=?
        """,
        (
            status,
            error_type,
            code,
            error[:2000],
            next_retry_at,
            utc_now(),
            int(item["web_post_id"]),
        ),
    )
    add_event(
        connection,
        status,
        item=item,
        details={
            "error_type": error_type,
            "code": code,
            "error": error,
            "retryable_streak": retryable_streak,
            "effective_retry_delay_seconds": effective_retry_delay_seconds,
            **(details or {}),
        },
    )
    if status == "retryable" and next_retry_at:
        start_global_cooldown(
            connection,
            item,
            next_request_at=next_retry_at,
            reason=f"{error_type}:{code}" if code is not None else error_type,
        )
    connection.commit()


def repaired_content_image_urls(
    connection: sqlite3.Connection,
    web_post_id: int,
) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            """
            SELECT image_url
            FROM web_post_images
            WHERE web_post_id=? AND image_role='content'
            ORDER BY image_index, id
            """,
            (web_post_id,),
        )
    ]


def reconcile_committed_item(
    target_connection: sqlite3.Connection,
    state_connection: sqlite3.Connection,
    item: sqlite3.Row,
    meta: dict[str, str],
) -> bool:
    target_row = existing_target_row(target_connection, int(item["web_post_id"]))
    raw = read_json_object(target_row["raw_sample_json"])
    evidence = raw.get("bilibili_history_repair")
    if not isinstance(evidence, dict):
        return False
    if evidence.get("run_id") != meta["run_id"]:
        return False
    content_sha256 = sha256_text(str(target_row["content_text"] or ""))
    image_urls = repaired_content_image_urls(target_connection, int(item["web_post_id"]))
    images_sha256 = sha256_text(json_text(image_urls))
    if (
        content_sha256 != evidence.get("repaired_content_sha256")
        or images_sha256 != evidence.get("repaired_images_sha256")
    ):
        return False
    state_connection.execute(
        """
        UPDATE repair_items
        SET status='succeeded', detail_source='article_view_api',
            detail_content_length=?, detail_image_count=?,
            repaired_content_sha256=?, repaired_images_sha256=?,
            repaired_at=?, updated_at=?
        WHERE web_post_id=?
        """,
        (
            len(str(target_row["content_text"] or "")),
            len(image_urls),
            content_sha256,
            images_sha256,
            evidence.get("repaired_at") or utc_now(),
            utc_now(),
            int(item["web_post_id"]),
        ),
    )
    add_event(state_connection, "reconciled_after_commit", item=item)
    state_connection.commit()
    return True


def apply_repaired_record(
    target_connection: sqlite3.Connection,
    state_connection: sqlite3.Connection,
    item: sqlite3.Row,
    hydrated: dict[str, Any],
    meta: dict[str, str],
) -> str:
    web_post_id = int(item["web_post_id"])
    target_connection.execute("BEGIN IMMEDIATE")
    try:
        target_row = existing_target_row(target_connection, web_post_id)
        current_images = current_image_rows(target_connection, web_post_id)
        current_content_sha = sha256_text(str(target_row["content_text"] or ""))
        current_raw_sha = sha256_text(str(target_row["raw_sample_json"] or ""))
        current_images_sha = sha256_text(json_text(current_images))
        if (
            current_content_sha != str(item["original_content_sha256"])
            or current_raw_sha != str(item["original_raw_sha256"])
            or current_images_sha != str(item["original_images_sha256"])
        ):
            target_connection.rollback()
            if reconcile_committed_item(target_connection, state_connection, item, meta):
                return "reconciled"
            mark_failure(
                state_connection,
                item,
                status="conflict",
                error_type="optimistic_lock_conflict",
                error="target row or image relations changed after manifest creation",
            )
            return "conflict"

        detail_urls = [str(url) for url in hydrated.get("detail_image_urls") or []]
        if not detail_urls:
            target_connection.rollback()
            raise RuntimeError("detail record has no content image URLs")
        repaired_at = utc_now()
        repaired_content_sha = sha256_text(str(hydrated["content_text"]))
        repaired_images_sha = sha256_text(json_text(detail_urls))
        hydrated["bilibili_history_repair"] = {
            "run_id": meta["run_id"],
            "source_manifest_sha256": meta["source_manifest_sha256"],
            "code_commit": meta["code_commit"],
            "repaired_at": repaired_at,
            "original_content_sha256": item["original_content_sha256"],
            "original_image_count": int(item["original_image_count"]),
            "repaired_content_sha256": repaired_content_sha,
            "repaired_images_sha256": repaired_images_sha,
        }
        target_connection.execute(
            """
            UPDATE web_posts
            SET title=?, content_text=?, content_length=?, post_images_count=?,
                raw_sample_json=?, status='captured', updated_at=datetime('now')
            WHERE id=? AND platform_key='bilibili'
            """,
            (
                hydrated.get("title"),
                hydrated["content_text"],
                len(str(hydrated["content_text"])),
                len(detail_urls),
                json_text(hydrated),
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
                local_path, raw_image_json
            ) VALUES (?, ?, ?, 'content', NULL, ?)
            """,
            [
                (
                    web_post_id,
                    index,
                    url,
                    json_text({"url": url, "role": "content", "source_key": "detail_image_urls"}),
                )
                for index, url in enumerate(detail_urls)
            ],
        )
        actual_count = int(
            target_connection.execute(
                """
                SELECT COUNT(*) FROM web_post_images
                WHERE web_post_id=? AND image_role='content'
                """,
                (web_post_id,),
            ).fetchone()[0]
        )
        if actual_count != len(detail_urls):
            raise RuntimeError("content image write count mismatch")
        target_connection.commit()
    except Exception:
        target_connection.rollback()
        raise

    state_connection.execute(
        """
        UPDATE repair_items
        SET status='succeeded', detail_source='article_view_api',
            detail_content_length=?, detail_image_count=?,
            repaired_content_sha256=?, repaired_images_sha256=?,
            repaired_at=?, updated_at=?
        WHERE web_post_id=?
        """,
        (
            len(str(hydrated["content_text"])),
            len(detail_urls),
            repaired_content_sha,
            repaired_images_sha,
            repaired_at,
            utc_now(),
            web_post_id,
        ),
    )
    add_event(
        state_connection,
        "succeeded",
        item=item,
        details={
            "content_length": len(str(hydrated["content_text"])),
            "image_count": len(detail_urls),
        },
    )
    state_connection.commit()
    return "updated"


def assert_external_invariants(path: Path, meta: dict[str, str]) -> dict[str, Any]:
    manifest, manifest_sha256 = build_source_manifest(path)
    current_id_set_sha256 = sha256_text(
        json_text([item["platform_post_id"] for item in manifest])
    )
    result = {
        "source_count": len(manifest),
        "source_count_unchanged": len(manifest) == int(meta["source_count"]),
        "source_id_set_unchanged": current_id_set_sha256 == meta["source_id_set_sha256"],
        "control_plane_unchanged": (
            control_plane_fingerprint(path) == meta["control_plane_sha256"]
        ),
        "non_bilibili_unchanged": (
            non_bilibili_fingerprint(path) == meta["non_bilibili_sha256"]
        ),
        "current_manifest_sha256": manifest_sha256,
    }
    result["ok"] = all(
        result[key]
        for key in (
            "source_count_unchanged",
            "source_id_set_unchanged",
            "control_plane_unchanged",
            "non_bilibili_unchanged",
        )
    )
    if not result["ok"]:
        raise RuntimeError(f"repair external invariant failed: {result}")
    return result


def state_counts(connection: sqlite3.Connection) -> dict[str, int]:
    counts = {
        str(row["status"]): int(row["count"])
        for row in connection.execute(
            "SELECT status, COUNT(*) AS count FROM repair_items GROUP BY status"
        )
    }
    for status in (*REPAIRABLE_STATUSES, *TERMINAL_STATUSES):
        counts.setdefault(status, 0)
    counts["total"] = sum(
        value for key, value in counts.items() if key != "total"
    )
    return counts


def target_summary(path: Path) -> dict[str, Any]:
    with sqlite_connect(path, readonly=True) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS rows,
                   COUNT(DISTINCT platform_post_id) AS distinct_ids,
                   SUM(
                     json_extract(raw_sample_json, '$.content_detail_status')='detail_observed'
                   ) AS detail_observed,
                   SUM(
                     json_type(raw_sample_json, '$.bilibili_history_repair')='object'
                   ) AS repaired_rows
            FROM web_posts WHERE platform_key='bilibili'
            """
        ).fetchone()
        mismatch = int(
            connection.execute(
                """
                WITH content_images AS (
                    SELECT web_post_id, COUNT(*) AS image_rows
                    FROM web_post_images
                    WHERE image_role='content'
                    GROUP BY web_post_id
                )
                SELECT COUNT(*)
                FROM web_posts p
                LEFT JOIN content_images i ON i.web_post_id=p.id
                WHERE p.platform_key='bilibili'
                  AND COALESCE(i.image_rows, 0) != p.post_images_count
                """
            ).fetchone()[0]
        )
    return {
        "rows": int(row["rows"]),
        "distinct_ids": int(row["distinct_ids"]),
        "detail_observed": int(row["detail_observed"] or 0),
        "repaired_rows": int(row["repaired_rows"] or 0),
        "image_count_mismatches": mismatch,
        "sqlite": sqlite_checks(path),
        "sha256": sha256_file(path),
    }


def write_report(
    config: RepairConfig,
    state_connection: sqlite3.Connection,
    meta: dict[str, str],
    *,
    invocation: dict[str, Any],
    invariants: dict[str, Any] | None,
) -> Path:
    report_dir = ensure_dir(config.report_dir)
    payload = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "run_id": meta["run_id"],
        "target_db": str(config.db_path),
        "state_db": str(config.state_db_path),
        "backup_path": str(config.backup_path),
        "baseline_db_sha256": meta["baseline_db_sha256"],
        "source_manifest_sha256": meta["source_manifest_sha256"],
        "source_manifest_path": meta.get("source_manifest_path"),
        "code_commit": meta["code_commit"],
        "apply": config.apply,
        "invocation": invocation,
        "status_counts": state_counts(state_connection),
        "target": target_summary(config.db_path),
        "external_invariants": invariants,
    }
    report_path = report_dir / f"report_{utc_stamp()}.json"
    report_path.write_text(json_text(payload, pretty=True) + "\n", encoding="utf-8")
    latest_path = report_dir / "latest.json"
    latest_path.write_text(json_text(payload, pretty=True) + "\n", encoding="utf-8")
    set_meta(
        state_connection,
        {
            "latest_report_path": latest_path,
            "latest_report_sha256": sha256_file(latest_path),
        },
    )
    state_connection.commit()
    return report_path


def verify_apply_inputs(config: RepairConfig, meta: dict[str, str]) -> dict[str, Any]:
    if config.db_path.resolve() == DEFAULT_DB.resolve() and not config.confirm_default_db_repair:
        raise RuntimeError("default database repair requires --confirm-default-db-repair")
    if not config.expected_baseline_sha256:
        raise RuntimeError("--expected-baseline-sha256 is required with --apply")
    if config.expected_baseline_sha256 != meta["baseline_db_sha256"]:
        raise RuntimeError("expected baseline SHA-256 does not match repair state")
    if not config.backup_path.is_file():
        raise RuntimeError("--backup-path must point to an existing SQLite backup")
    backup_manifest, backup_manifest_sha = build_source_manifest(config.backup_path)
    if backup_manifest_sha != meta["source_manifest_sha256"]:
        raise RuntimeError("backup source manifest does not match repair baseline")
    backup_checks = sqlite_checks(config.backup_path)
    if not backup_checks["ok"]:
        raise RuntimeError("backup SQLite verification failed")
    with sqlite_connect(config.state_db_path) as state_connection:
        set_meta(
            state_connection,
            {
                "backup_path": config.backup_path.resolve(),
                "backup_sha256": sha256_file(config.backup_path),
                "backup_count": len(backup_manifest),
            },
        )
        state_connection.commit()
    return {
        "path": str(config.backup_path.resolve()),
        "sha256": sha256_file(config.backup_path),
        "manifest_sha256": backup_manifest_sha,
        "count": len(backup_manifest),
        "sqlite": backup_checks,
    }


def run_repair(config: RepairConfig) -> tuple[int, dict[str, Any]]:
    ensure_parent(config.state_db_path)
    with sqlite_connect(config.state_db_path) as state_connection:
        meta = initialize_repair_state(config.db_path, state_connection, config.report_dir)
        initial_invariants = assert_external_invariants(config.db_path, meta)
        cooldown = (
            active_global_cooldown(
                state_connection,
                base_delay_seconds=config.retry_delay_seconds,
            )
            if config.apply
            else None
        )
        selected = (
            []
            if cooldown
            else selected_items(
                state_connection,
                max_items=config.max_items,
                source_limit=config.source_limit,
                only_ids=config.only_ids,
            )
        )
        invocation: dict[str, Any] = {
            "started_at": utc_now(),
            "selected_count": len(selected),
            "selected_ids": [str(item["platform_post_id"]) for item in selected],
            "scope_status_counts": scoped_state_counts(
                state_connection,
                source_limit=config.source_limit,
                only_ids=config.only_ids,
            ),
            "updated": 0,
            "reconciled": 0,
            "invalid_detail": 0,
            "conflict": 0,
            "retryable": 0,
            "stopped_reason": (
                "dry_run"
                if not config.apply
                else "global_cooldown" if cooldown else "completed_batch"
            ),
        }
        if cooldown:
            invocation["global_cooldown"] = cooldown
        if not config.apply:
            report_path = write_report(
                config,
                state_connection,
                meta,
                invocation=invocation,
                invariants=initial_invariants,
            )
            invocation["report_path"] = str(report_path)
            return 0, invocation

        backup = verify_apply_inputs(config, meta)
        invocation["backup"] = backup
        if cooldown:
            invocation["finished_at"] = utc_now()
            report_path = write_report(
                config,
                state_connection,
                meta,
                invocation=invocation,
                invariants=initial_invariants,
            )
            invocation["report_path"] = str(report_path)
            return 3, invocation
        cookie = load_cookie_snapshot("bilibili") or {}
        cookie_header = str(cookie.get("cookie_header") or "")
        last_request_at: float | None = None
        requests_in_session = 0
        return_code = 0
        with sqlite_connect(config.db_path) as target_connection:
            for index, item in enumerate(selected):
                if reconcile_committed_item(target_connection, state_connection, item, meta):
                    invocation["reconciled"] += 1
                    continue
                if requests_in_session >= config.session_size:
                    assert_external_invariants(config.db_path, meta)
                    pause = random.uniform(
                        config.session_pause_min,
                        config.session_pause_max,
                    )
                    add_event(
                        state_connection,
                        "session_pause",
                        details={"seconds": round(pause, 3)},
                    )
                    state_connection.commit()
                    time.sleep(pause)
                    requests_in_session = 0
                    last_request_at = None
                applied_pacing = 0.0
                if last_request_at is not None:
                    pacing = random.uniform(config.pacing_min, config.pacing_max)
                    elapsed = time.monotonic() - last_request_at
                    if elapsed < pacing:
                        applied_pacing = pacing - elapsed
                        time.sleep(applied_pacing)
                mark_attempt(state_connection, item)
                try:
                    detail, attempts, retry_wait = fetch_repair_article_detail(
                        str(item["platform_post_id"]),
                        cookie_header,
                    )
                    reset_global_retryable_streak(state_connection)
                    last_request_at = time.monotonic()
                    requests_in_session += 1
                    target_row = existing_target_row(
                        target_connection,
                        int(item["web_post_id"]),
                    )
                    existing_images = current_image_rows(
                        target_connection,
                        int(item["web_post_id"]),
                    )
                    search_record = prepare_search_record(target_row, existing_images)
                    hydrated = hydrate_bilibili_article_record(
                        search_record,
                        detail,
                        attempts=attempts,
                        retry_wait_seconds=retry_wait,
                        pacing_wait_seconds=applied_pacing,
                    )
                    validation = validate_formal_record("bilibili", hydrated, set())
                    detail_urls = hydrated.get("detail_image_urls") or []
                    if not validation["valid"] or not detail_urls:
                        reasons = list(validation["reasons"])
                        if not detail_urls and "missing_content_image" not in reasons:
                            reasons.append("missing_content_image")
                        mark_failure(
                            state_connection,
                            item,
                            status="invalid_detail",
                            error_type="formal_validation_failed",
                            error=",".join(reasons),
                            details={
                                "content_length": len(str(hydrated.get("content_text") or "")),
                                "image_count": len(detail_urls),
                            },
                        )
                        invocation["invalid_detail"] += 1
                        continue
                    result = apply_repaired_record(
                        target_connection,
                        state_connection,
                        item,
                        hydrated,
                        meta,
                    )
                    invocation[result] = int(invocation.get(result) or 0) + 1
                except BilibiliArticleDetailError as exc:
                    last_request_at = time.monotonic()
                    status = "retryable" if exc.retryable else "invalid_detail"
                    mark_failure(
                        state_connection,
                        item,
                        status=status,
                        error_type=type(exc).__name__,
                        error=str(exc),
                        code=exc.code,
                        retry_delay_seconds=config.retry_delay_seconds,
                        details={
                            "attempts": exc.attempts,
                            "retry_wait_seconds": exc.retry_wait_seconds,
                        },
                    )
                    invocation[status] += 1
                    if status == "retryable":
                        invocation["stopped_reason"] = "retryable_detail_error"
                        return_code = 2
                        break
                except Exception as exc:
                    mark_failure(
                        state_connection,
                        item,
                        status="retryable",
                        error_type=type(exc).__name__,
                        error=str(exc),
                        retry_delay_seconds=config.retry_delay_seconds,
                    )
                    invocation["retryable"] += 1
                    invocation["stopped_reason"] = "unexpected_retryable_error"
                    return_code = 2
                    break
                if (index + 1) % 10 == 0:
                    print(
                        json_text(
                            {
                                "processed": index + 1,
                                "selected": len(selected),
                                "status_counts": state_counts(state_connection),
                            }
                        ),
                        flush=True,
                    )

        final_invariants = assert_external_invariants(config.db_path, meta)
        invocation["scope_status_counts"] = scoped_state_counts(
            state_connection,
            source_limit=config.source_limit,
            only_ids=config.only_ids,
        )
        invocation["finished_at"] = utc_now()
        report_path = write_report(
            config,
            state_connection,
            meta,
            invocation=invocation,
            invariants=final_invariants,
        )
        invocation["report_path"] = str(report_path)
        return return_code, invocation


@contextmanager
def repair_lock(state_path: Path) -> Iterator[None]:
    lock_path = ensure_parent(state_path.with_suffix(state_path.suffix + ".lock"))
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another repair process holds {lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_only_ids(path_value: str | None) -> frozenset[str]:
    if not path_value:
        return frozenset()
    path = Path(path_value).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read --only-ids-file: {exc}") from exc
    if not isinstance(payload, list):
        raise SystemExit("--only-ids-file must contain a JSON array")
    values = frozenset(str(value).strip() for value in payload if str(value).strip())
    if not values:
        raise SystemExit("--only-ids-file contains no IDs")
    return values


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--state-db")
    parser.add_argument("--report-dir")
    parser.add_argument("--create-backup")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-default-db-repair", action="store_true")
    parser.add_argument("--expected-baseline-sha256", default="")
    parser.add_argument("--backup-path")
    parser.add_argument("--max-items", type=int, default=10)
    parser.add_argument("--session-size", type=int, default=10)
    parser.add_argument("--pacing-min", type=float, default=BILIBILI_DETAIL_PACING_SECONDS[0])
    parser.add_argument("--pacing-max", type=float, default=BILIBILI_DETAIL_PACING_SECONDS[1])
    parser.add_argument("--session-pause-min", type=float, default=8.0)
    parser.add_argument("--session-pause-max", type=float, default=15.0)
    parser.add_argument("--retry-delay-seconds", type=int, default=300)
    parser.add_argument(
        "--source-limit",
        type=int,
        default=0,
        help="restrict a pilot to the first N source-manifest rows (0 means all)",
    )
    parser.add_argument("--only-ids-file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).expanduser().resolve()
    if args.create_backup:
        result = create_online_backup(
            db_path,
            Path(args.create_backup).expanduser().resolve(),
        )
        print(json_text(result, pretty=True))
        return 0
    if not args.state_db:
        raise SystemExit("--state-db is required unless --create-backup is used")
    if args.max_items <= 0 or args.session_size <= 0:
        raise SystemExit("--max-items and --session-size must be positive")
    if args.source_limit < 0:
        raise SystemExit("--source-limit cannot be negative")
    if min(
        args.pacing_min,
        args.pacing_max,
        args.session_pause_min,
        args.session_pause_max,
        args.retry_delay_seconds,
    ) < 0:
        raise SystemExit("wait and retry values cannot be negative")
    state_path = Path(args.state_db).expanduser().resolve()
    report_dir = (
        Path(args.report_dir).expanduser().resolve()
        if args.report_dir
        else (BILIBILI_REPAIR_OUTPUT / state_path.stem).resolve()
    )
    backup_path = (
        Path(args.backup_path).expanduser().resolve()
        if args.backup_path
        else (BILIBILI_REPAIR_BACKUP_ROOT / "missing.sqlite").resolve()
    )
    config = RepairConfig(
        db_path=db_path,
        state_db_path=state_path,
        report_dir=report_dir,
        backup_path=backup_path,
        expected_baseline_sha256=str(args.expected_baseline_sha256).strip(),
        apply=bool(args.apply),
        confirm_default_db_repair=bool(args.confirm_default_db_repair),
        max_items=int(args.max_items),
        session_size=int(args.session_size),
        pacing_min=min(args.pacing_min, args.pacing_max),
        pacing_max=max(args.pacing_min, args.pacing_max),
        session_pause_min=min(args.session_pause_min, args.session_pause_max),
        session_pause_max=max(args.session_pause_min, args.session_pause_max),
        retry_delay_seconds=int(args.retry_delay_seconds),
        source_limit=int(args.source_limit),
        only_ids=load_only_ids(args.only_ids_file),
    )
    ensure_dir(BILIBILI_REPAIR_RUNTIME_ROOT)
    with repair_lock(state_path):
        return_code, result = run_repair(config)
    print(json_text(result, pretty=True))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
