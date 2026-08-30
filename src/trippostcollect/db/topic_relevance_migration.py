"""Idempotent schema-v20 topic-relevance migration and historical backfill."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
import sqlite3
from typing import Any

from trippostcollect.records.topic_relevance import is_topic_relevant


MIGRATION_VERSION = 20
MIGRATION_NAME = "topic_relevance"


class TopicRelevanceMigrationError(RuntimeError):
    """Raised when schema-v20 cannot preserve the persistence contract."""


def _hash_rows(conn: sqlite3.Connection, sql: str) -> str:
    digest = sha256()
    for row in conn.execute(sql):
        digest.update(
            json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), default=str).encode(
                "utf-8"
            )
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _invariants(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "web_posts": int(conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0]),
        "web_post_images": int(
            conn.execute("SELECT COUNT(*) FROM web_post_images").fetchone()[0]
        ),
        "post_content_sha256": _hash_rows(
            conn,
            """
            SELECT id, platform_key, platform_post_id, title, content_text, keyword,
                   published_at, captured_at, author_display_name, author_platform_id
            FROM web_posts ORDER BY id
            """,
        ),
        "image_relationships_sha256": _hash_rows(
            conn,
            """
            SELECT id, web_post_id, image_index, image_url, image_role, local_path,
                   width, height, mime_type, sha256
            FROM web_post_images ORDER BY id
            """,
        ),
    }


def topic_relevance_distribution(conn: sqlite3.Connection) -> dict[str, Any]:
    totals = {False: 0, True: 0}
    platforms: Counter[tuple[str, bool]] = Counter()
    keywords: Counter[tuple[str, bool]] = Counter()
    for platform, keyword, relevant, count in conn.execute(
        """
        SELECT platform_key, COALESCE(keyword, ''), topic_relevant, COUNT(*)
        FROM web_posts
        GROUP BY platform_key, COALESCE(keyword, ''), topic_relevant
        ORDER BY platform_key, keyword, topic_relevant
        """
    ):
        state = bool(relevant)
        amount = int(count)
        totals[state] += amount
        platforms[(str(platform), state)] += amount
        keywords[(str(keyword), state)] += amount
    return {
        "total": totals[False] + totals[True],
        "relevant": totals[True],
        "irrelevant": totals[False],
        "platforms": [
            {"platform_key": platform, "topic_relevant": state, "count": count}
            for (platform, state), count in sorted(platforms.items())
        ],
        "keywords": [
            {"keyword": keyword, "topic_relevant": state, "count": count}
            for (keyword, state), count in sorted(keywords.items())
        ],
    }


def migrate_topic_relevance(conn: sqlite3.Connection) -> dict[str, Any]:
    """Add/backfill topic relevance in the caller's current transaction."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(web_posts)")}
    before = _invariants(conn)
    column_added = "topic_relevant" not in columns
    if column_added:
        conn.execute(
            """
            ALTER TABLE web_posts
            ADD COLUMN topic_relevant INTEGER NOT NULL DEFAULT 0
            CHECK (topic_relevant IN (0, 1))
            """
        )

    applied = bool(
        conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?",
            (MIGRATION_VERSION,),
        ).fetchone()
    )
    rows_updated = 0
    if not applied:
        for row_id, title, content_text, keyword in conn.execute(
            "SELECT id, title, content_text, keyword FROM web_posts ORDER BY id"
        ).fetchall():
            relevant = is_topic_relevant(
                title=title,
                content_text=content_text,
                keyword=keyword,
            )
            conn.execute(
                "UPDATE web_posts SET topic_relevant=? WHERE id=?",
                (int(relevant), int(row_id)),
            )
            rows_updated += 1
        conn.execute(
            "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
            (MIGRATION_VERSION, MIGRATION_NAME),
        )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_web_posts_topic_relevant
        ON web_posts(topic_relevant, captured_at DESC)
        """
    )
    after = _invariants(conn)
    if before != after:
        raise TopicRelevanceMigrationError(
            "topic relevance migration changed protected post/image data"
        )
    return {
        "version": MIGRATION_VERSION,
        "name": MIGRATION_NAME,
        "already_applied": applied,
        "column_added": column_added,
        "rows_updated": rows_updated,
        "distribution": topic_relevance_distribution(conn),
        "invariants": after,
    }
