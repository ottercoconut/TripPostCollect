"""Overview queries for admin dashboards."""

from __future__ import annotations

import sqlite3
from typing import Any


def _scope_predicate(topic_scope: str, *, alias: str = "") -> tuple[str, tuple[int, ...]]:
    if topic_scope == "all":
        return "1=1", ()
    if topic_scope not in {"relevant", "irrelevant"}:
        raise ValueError(f"unsupported topic scope: {topic_scope}")
    return f"{alias}topic_relevant=?", (1 if topic_scope == "relevant" else 0,)


def counts(conn: sqlite3.Connection, *, topic_scope: str = "relevant") -> dict[str, Any]:
    predicate, params = _scope_predicate(topic_scope, alias="p.")
    total_records = int(
        conn.execute(f"SELECT COUNT(*) FROM web_posts p WHERE {predicate}", params).fetchone()[0]
        or 0
    )
    all_records = _count(conn, "web_posts")
    relevant_records = int(
        conn.execute("SELECT COUNT(*) FROM web_posts WHERE topic_relevant=1").fetchone()[0]
        or 0
    )
    total_images = _count(conn, "web_post_images")
    total_captures = _count(conn, "ctf_captures")
    total_jobs = _count(conn, "crawl_jobs")
    platform_rows = conn.execute(
        """
        SELECT p.platform_key, sp.display_name AS platform_name, COUNT(*) AS records
        FROM web_posts p
        LEFT JOIN source_platforms sp ON sp.platform_key = p.platform_key
        WHERE """ + predicate + """
        GROUP BY p.platform_key, sp.display_name
        ORDER BY records DESC, p.platform_key
        """,
        params,
    ).fetchall()
    return {
        "records": total_records,
        "topic_scope": topic_scope,
        "all_records": all_records,
        "topic_relevant_records": relevant_records,
        "topic_irrelevant_records": all_records - relevant_records,
        "record_images": total_images,
        "captures": total_captures,
        "crawl_jobs": total_jobs,
        "platforms": [dict(row) for row in platform_rows],
    }


def field_gaps(conn: sqlite3.Connection, *, topic_scope: str = "relevant") -> dict[str, Any]:
    predicate, params = _scope_predicate(topic_scope)
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS records,
            SUM(CASE WHEN COALESCE(post_images_count, 0) = 0 THEN 1 ELSE 0 END) AS missing_images,
            SUM(CASE WHEN published_at IS NULL OR published_at = '' THEN 1 ELSE 0 END) AS missing_published_at,
            SUM(CASE WHEN author_followers_count IS NULL THEN 1 ELSE 0 END) AS missing_author_followers,
            SUM(CASE WHEN author_display_name IS NULL OR author_display_name = '' THEN 1 ELSE 0 END) AS missing_author_name
        FROM web_posts
        WHERE """ + predicate + """
        """,
        params,
    ).fetchone()
    return {**dict(row), "topic_scope": topic_scope}


def recent_runs(conn: sqlite3.Connection, *, limit: int = 10) -> list[dict[str, Any]]:
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


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
