"""Read-only scheduler repository."""

from __future__ import annotations

import sqlite3
from typing import Any


def list_jobs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM crawl_jobs
        ORDER BY enabled DESC, priority ASC, next_run_at ASC, id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def list_reports(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM crawl_run_reports
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
