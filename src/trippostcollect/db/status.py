"""Read-only database status inspection."""

from __future__ import annotations

import sqlite3
from typing import Any


EXPECTED_TABLES = [
    "schema_migrations",
    "source_platforms",
    "web_posts",
    "web_post_images",
    "ctf_captures",
    "ctf_capture_images",
    "crawl_jobs",
    "crawl_attempts",
    "crawl_run_reports",
    "profile_health_checks",
]


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return bool(row)


def count_table_rows(conn: sqlite3.Connection, table_name: str) -> int | None:
    if not table_exists(conn, table_name):
        return None
    row = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    return int(row[0] or 0)


def index_names(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]


def schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {
        table: {
            "exists": table_exists(conn, table),
            "count": count_table_rows(conn, table),
        }
        for table in EXPECTED_TABLES
    }
    return {
        "ok": all(item["exists"] for item in tables.values()),
        "tables": tables,
        "indexes": index_names(conn),
        "source_platforms": count_table_rows(conn, "source_platforms"),
        "crawl_jobs": count_table_rows(conn, "crawl_jobs"),
        "enabled_crawl_jobs": _enabled_jobs(conn),
    }


def _enabled_jobs(conn: sqlite3.Connection) -> int | None:
    if not table_exists(conn, "crawl_jobs"):
        return None
    row = conn.execute("SELECT COUNT(*) FROM crawl_jobs WHERE enabled=1").fetchone()
    return int(row[0] or 0)
