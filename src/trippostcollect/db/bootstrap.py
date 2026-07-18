"""Shared SQLite bootstrap for crawl schemas, platform registry, and config jobs."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trippostcollect.core.paths import (
    CRAWL_SCHEDULER_SCHEMA,
    CTF_CAPTURES_SCHEMA,
    DEFAULT_CONFIG,
    DEFAULT_DB,
    SOURCE_PLATFORMS_SCHEMA,
    WEB_POSTS_SCHEMA,
    XHS_CONTROL_SCHEMA,
    ensure_parent,
)
from trippostcollect.platforms.registry import SITES


CURRENT_JOB_KINDS = {"mediacrawler_search", "ctf_resource_crawl"}
JOB_KIND_CHECK_RE = re.compile(r"CHECK\s*\(\s*job_kind\s+IN\s*\(([^)]*)\)", re.IGNORECASE | re.DOTALL)
CRAWL_ATTEMPT_COLUMNS = (
    "id",
    "job_id",
    "run_id",
    "attempt_no",
    "status",
    "failure_type",
    "retryable",
    "wait_seconds",
    "command_json",
    "started_at",
    "finished_at",
    "exit_code",
    "artifact_dir",
    "capture_meta_paths_json",
    "import_result_json",
    "stdout_tail",
    "stderr_tail",
    "classification_json",
    "created_at",
)


def qmarks(values: set[str] | list[str]) -> str:
    return ",".join("?" for _ in values)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return bool(row)


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> bool:
    if column_name in table_columns(conn, table_name):
        return False
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
    return True


def ensure_source_platforms(conn: sqlite3.Connection) -> int:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SOURCE_PLATFORMS_SCHEMA.read_text(encoding="utf-8"))
    conn.execute("INSERT OR IGNORE INTO schema_migrations(version, name) VALUES (?, ?)", (3, "source_platforms"))
    platform_keys = set(SITES)
    for site in SITES.values():
        conn.execute(
            """
            INSERT INTO source_platforms (
                platform_key, display_name, status, default_url, login_url, requires_login,
                cookie_domains_json, recommended_scrapling_mode, browser_engine, mobile_context,
                default_wait_ms, min_delay_seconds, max_requests_per_session, daily_request_budget,
                cooldown_minutes, account_risk_level, content_focus, browser_profile_dir,
                storage_state_path, login_state_exists, crawl_policy_json, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(platform_key) DO UPDATE SET
                display_name=excluded.display_name,
                status=excluded.status,
                default_url=excluded.default_url,
                login_url=excluded.login_url,
                requires_login=excluded.requires_login,
                cookie_domains_json=excluded.cookie_domains_json,
                recommended_scrapling_mode=excluded.recommended_scrapling_mode,
                browser_engine=excluded.browser_engine,
                mobile_context=excluded.mobile_context,
                default_wait_ms=excluded.default_wait_ms,
                min_delay_seconds=excluded.min_delay_seconds,
                max_requests_per_session=excluded.max_requests_per_session,
                daily_request_budget=excluded.daily_request_budget,
                cooldown_minutes=excluded.cooldown_minutes,
                account_risk_level=excluded.account_risk_level,
                content_focus=excluded.content_focus,
                browser_profile_dir=excluded.browser_profile_dir,
                crawl_policy_json=excluded.crawl_policy_json,
                notes=excluded.notes,
                updated_at=datetime('now')
            """,
            (
                site.key,
                site.name,
                "active" if site.active else "inactive",
                site.default_url,
                site.login_url,
                1 if site.login_required else 0,
                json_dump(list(site.cookie_domains)),
                site.recommended_mode,
                site.preferred_engine,
                1 if site.mobile_context else 0,
                site.default_wait_ms,
                site.min_delay_seconds,
                site.max_requests_per_session,
                site.daily_request_budget,
                site.cooldown_minutes,
                site.account_risk_level,
                site.content_focus,
                str(site.profile_dir_override) if site.profile_dir_override else None,
                None,
                json_dump(
                    {
                        "jitter_ratio": site.jitter_ratio,
                        "min_jitter_seconds": site.min_jitter_seconds,
                        "max_jitter_seconds": site.max_jitter_seconds,
                    }
                ),
                site.notes,
            ),
        )
    if platform_keys and table_exists(conn, "web_posts") and table_exists(conn, "ctf_captures"):
        placeholders = qmarks(platform_keys)
        conn.execute(
            f"""
            DELETE FROM source_platforms
            WHERE platform_key NOT IN ({placeholders})
              AND platform_key NOT IN (SELECT platform_key FROM web_posts)
              AND platform_key NOT IN (SELECT site_key FROM ctf_captures)
            """,
            sorted(platform_keys),
        )
    return len(SITES)


def ensure_content_schema(conn: sqlite3.Connection) -> int:
    platform_count = ensure_source_platforms(conn)
    conn.executescript(WEB_POSTS_SCHEMA.read_text(encoding="utf-8"))
    conn.execute("INSERT OR IGNORE INTO schema_migrations(version, name) VALUES (?, ?)", (4, "web_posts"))
    ensure_column(conn, "web_posts", "published_at", "TEXT")
    ensure_column(conn, "web_posts", "source_capture_id", "INTEGER REFERENCES ctf_captures(id) ON DELETE SET NULL")
    conn.executescript(CTF_CAPTURES_SCHEMA.read_text(encoding="utf-8"))
    conn.execute("INSERT OR IGNORE INTO schema_migrations(version, name) VALUES (?, ?)", (5, "ctf_captures"))
    ensure_column(conn, "ctf_captures", "published_at", "TEXT")
    conn.execute("INSERT OR IGNORE INTO schema_migrations(version, name) VALUES (?, ?)", (7, "published_at_fields"))
    conn.execute("INSERT OR IGNORE INTO schema_migrations(version, name) VALUES (?, ?)", (8, "capture_to_posts"))
    return platform_count


def ensure_scheduler_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='crawl_jobs'").fetchone()
    table_sql = str(row[0]) if row else ""
    match = JOB_KIND_CHECK_RE.search(table_sql)
    configured_job_kinds = {
        value.strip().strip("'\"")
        for value in (match.group(1).split(",") if match else [])
        if value.strip()
    }
    attempt_fk_targets = {
        str(item[2])
        for item in conn.execute("PRAGMA foreign_key_list(crawl_attempts)").fetchall()
    } if table_exists(conn, "crawl_attempts") else set()
    needs_rebuild = row and (
        configured_job_kinds != CURRENT_JOB_KINDS or attempt_fk_targets != {"crawl_jobs"}
    )
    if needs_rebuild:
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            has_attempts = table_exists(conn, "crawl_attempts")
            has_checkpoints = table_exists(conn, "crawl_discovery_checkpoints")
            if has_attempts:
                conn.execute("ALTER TABLE crawl_attempts RENAME TO crawl_attempts_old")
            if has_checkpoints:
                conn.execute(
                    "ALTER TABLE crawl_discovery_checkpoints RENAME TO crawl_discovery_checkpoints_old"
                )
            conn.execute("ALTER TABLE crawl_jobs RENAME TO crawl_jobs_old")
            conn.executescript(CRAWL_SCHEDULER_SCHEMA.read_text(encoding="utf-8"))
            conn.execute(
                """
                INSERT INTO crawl_jobs (
                    id, job_key, site_key, target_url, job_kind, enabled, status, priority,
                    schedule_seconds, next_run_at, max_attempts, consecutive_failures,
                    last_attempt_id, last_status, last_failure_type, params_json,
                    behavior_profile_json, created_at, updated_at
                )
                SELECT
                    id, job_key, site_key, target_url, job_kind, enabled, status, priority,
                    schedule_seconds, next_run_at, max_attempts, consecutive_failures,
                    last_attempt_id, last_status, last_failure_type, params_json,
                    behavior_profile_json, created_at, updated_at
                FROM crawl_jobs_old
                WHERE job_kind IN ('mediacrawler_search', 'ctf_resource_crawl')
                """
            )
            if has_attempts:
                columns = ", ".join(CRAWL_ATTEMPT_COLUMNS)
                conn.execute(
                    f"INSERT INTO crawl_attempts ({columns}) SELECT {columns} FROM crawl_attempts_old"
                )
                conn.execute("DROP TABLE crawl_attempts_old")
            if has_checkpoints:
                conn.execute(
                    """
                    INSERT INTO crawl_discovery_checkpoints (
                        id, job_id, platform_key, keyword, query_fingerprint, resume_page,
                        resume_offset, resume_cursor, source_has_more, status,
                        last_batch_complete, last_stop_reason, last_run_id, last_summary_path,
                        campaign_candidate_count, created_at, updated_at
                    )
                    SELECT
                        id, job_id, platform_key, keyword, query_fingerprint, resume_page,
                        resume_offset, resume_cursor, source_has_more, status,
                        last_batch_complete, last_stop_reason, last_run_id, last_summary_path,
                        campaign_candidate_count, created_at, updated_at
                    FROM crawl_discovery_checkpoints_old
                    """
                )
                conn.execute("DROP TABLE crawl_discovery_checkpoints_old")
            conn.execute("DROP TABLE crawl_jobs_old")
            conn.executescript(CRAWL_SCHEDULER_SCHEMA.read_text(encoding="utf-8"))
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"crawl scheduler foreign-key repair failed: {violations[:3]!r}")
    conn.executescript(CRAWL_SCHEDULER_SCHEMA.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, name) VALUES (?, ?)",
        (6, "crawl_scheduler"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, name) VALUES (?, ?)",
        (11, "crawl_discovery_checkpoints"),
    )


def ensure_xhs_control_schema(conn: sqlite3.Connection) -> None:
    legacy_account_columns = {
        "health_score",
        "consecutive_failures",
        "daily_date",
        "daily_runs",
        "cooldown_until",
    }
    requires_v10_migration = (
        table_exists(conn, "xhs_accounts")
        and bool(table_columns(conn, "xhs_accounts") & legacy_account_columns)
    ) or table_exists(conn, "xhs_platform_state")
    if requires_v10_migration:
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.executescript(
                """
                CREATE TEMP TABLE xhs_accounts_v9_backup AS
                SELECT
                    account_id,
                    CASE
                        WHEN status IN ('active', 'cooling', 'challenge') THEN 'active'
                        WHEN status IN ('login_pending', 'login_required', 'quarantined', 'retired') THEN status
                        ELSE 'login_pending'
                    END AS status,
                    profile_dir,
                    encrypted_state_path,
                    identity_hash,
                    last_verified_at,
                    last_used_at,
                    created_at,
                    updated_at
                FROM xhs_accounts;

                CREATE TEMP TABLE xhs_account_events_v9_backup AS
                SELECT id, account_id, run_id, event_type, details_json, created_at
                FROM xhs_account_events;

                CREATE TEMP TABLE xhs_account_leases_v9_backup AS
                SELECT account_id, run_id, acquired_at, expires_at
                FROM xhs_account_leases;

                CREATE TEMP TABLE xhs_runs_v9_backup AS
                SELECT
                    run_id, target_key, account_id, status, started_at, finished_at,
                    execution_state_path, child_summary_path, report_json
                FROM xhs_runs;

                DROP TABLE xhs_account_leases;
                DROP TABLE xhs_account_events;
                DROP TABLE xhs_runs;
                DROP TABLE xhs_platform_state;
                DROP TABLE xhs_accounts;
                """
            )
            conn.executescript(XHS_CONTROL_SCHEMA.read_text(encoding="utf-8"))
            conn.executescript(
                """
                INSERT INTO xhs_accounts(
                    account_id, status, profile_dir, encrypted_state_path, identity_hash,
                    last_verified_at, last_used_at, created_at, updated_at
                )
                SELECT
                    account_id, status, profile_dir, encrypted_state_path, identity_hash,
                    last_verified_at, last_used_at, created_at, updated_at
                FROM xhs_accounts_v9_backup;

                INSERT INTO xhs_account_events(id, account_id, run_id, event_type, details_json, created_at)
                SELECT id, account_id, run_id, event_type, details_json, created_at
                FROM xhs_account_events_v9_backup;

                INSERT INTO xhs_account_leases(account_id, run_id, acquired_at, expires_at)
                SELECT account_id, run_id, acquired_at, expires_at
                FROM xhs_account_leases_v9_backup;

                INSERT INTO xhs_runs(
                    run_id, target_key, account_id, status, started_at, finished_at,
                    execution_state_path, child_summary_path, report_json
                )
                SELECT
                    run_id, target_key, account_id, status, started_at, finished_at,
                    execution_state_path, child_summary_path, report_json
                FROM xhs_runs_v9_backup;

                DROP TABLE xhs_accounts_v9_backup;
                DROP TABLE xhs_account_events_v9_backup;
                DROP TABLE xhs_account_leases_v9_backup;
                DROP TABLE xhs_runs_v9_backup;
                """
            )
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(XHS_CONTROL_SCHEMA.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, name) VALUES (?, ?)",
        (10, "xhs_manual_account_selection"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, name) VALUES (?, ?)",
        (12, "xhs_discovery_checkpoints"),
    )


def sync_config_jobs(conn: sqlite3.Connection, config: dict[str, Any]) -> int:
    jobs = config.get("jobs") or []
    active_keys = {item["job_key"] for item in jobs}
    count = 0
    for item in jobs:
        next_run_at = item.get("next_run_at") or iso()
        row = {
            "job_key": item["job_key"],
            "site_key": item["site_key"],
            "target_url": item["target_url"],
            "job_kind": item["job_kind"],
            "enabled": 1 if item.get("enabled", True) else 0,
            "priority": int(item.get("priority", 100)),
            "schedule_seconds": int(item.get("schedule_seconds", 86400)),
            "next_run_at": next_run_at,
            "max_attempts": int(item.get("max_attempts", 2)),
            "params_json": json_dump(item.get("params") or {}),
            "behavior_profile_json": json_dump(item.get("behavior_profile") or {}),
        }
        conn.execute(
            """
            INSERT INTO crawl_jobs (
                job_key, site_key, target_url, job_kind, enabled, priority, schedule_seconds,
                next_run_at, max_attempts, params_json, behavior_profile_json
            )
            VALUES (
                :job_key, :site_key, :target_url, :job_kind, :enabled, :priority, :schedule_seconds,
                :next_run_at, :max_attempts, :params_json, :behavior_profile_json
            )
            ON CONFLICT(job_key) DO UPDATE SET
                site_key=excluded.site_key,
                target_url=excluded.target_url,
                job_kind=excluded.job_kind,
                enabled=excluded.enabled,
                priority=excluded.priority,
                schedule_seconds=excluded.schedule_seconds,
                max_attempts=excluded.max_attempts,
                params_json=excluded.params_json,
                behavior_profile_json=excluded.behavior_profile_json,
                status=CASE WHEN excluded.enabled=0 THEN 'disabled' WHEN crawl_jobs.status='disabled' THEN 'pending' ELSE crawl_jobs.status END,
                updated_at=datetime('now')
            """,
            row,
        )
        count += 1
    if active_keys:
        placeholders = qmarks(active_keys)
        conn.execute(
            f"""
            DELETE FROM crawl_jobs
            WHERE job_key NOT IN ({placeholders})
              AND id NOT IN (SELECT DISTINCT job_id FROM crawl_attempts)
            """,
            sorted(active_keys),
        )
        conn.execute(
            f"UPDATE crawl_jobs SET enabled=0, status='disabled', updated_at=datetime('now') WHERE job_key NOT IN ({placeholders})",
            sorted(active_keys),
        )
    return count


def bootstrap_connection(
    conn: sqlite3.Connection,
    *,
    config: dict[str, Any] | None = None,
    config_path: str | Path = DEFAULT_CONFIG,
    sync_content: bool = True,
    sync_scheduler: bool = True,
    sync_jobs: bool = True,
) -> dict[str, Any]:
    conn.execute("PRAGMA foreign_keys = ON")
    platform_count = ensure_content_schema(conn) if sync_content else 0
    if sync_scheduler:
        ensure_scheduler_schema(conn)
        ensure_xhs_control_schema(conn)
    synced_jobs = 0
    if sync_jobs:
        loaded_config = config if config is not None else load_json(config_path)
        synced_jobs = sync_config_jobs(conn, loaded_config)
    conn.commit()
    return {
        "source_platforms": platform_count,
        "scheduler_schema": bool(sync_scheduler),
        "synced_jobs": synced_jobs,
    }


def bootstrap_database(
    db_path: str | Path = DEFAULT_DB,
    *,
    config: dict[str, Any] | None = None,
    config_path: str | Path = DEFAULT_CONFIG,
    sync_content: bool = True,
    sync_scheduler: bool = True,
    sync_jobs: bool = True,
) -> dict[str, Any]:
    resolved = ensure_parent(Path(db_path).expanduser())
    with sqlite3.connect(resolved) as conn:
        result = bootstrap_connection(
            conn,
            config=config,
            config_path=config_path,
            sync_content=sync_content,
            sync_scheduler=sync_scheduler,
            sync_jobs=sync_jobs,
        )
    return {"db": str(resolved), **result}
