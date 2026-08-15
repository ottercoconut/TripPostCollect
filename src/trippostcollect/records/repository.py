"""Read-only record repository."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any


@dataclass(frozen=True)
class RecordFilters:
    platform_key: str | None = None
    source_type: str | None = None
    status: str | None = None
    keyword: str | None = None
    published_from: str | None = None
    published_to: str | None = None
    captured_from: str | None = None
    captured_to: str | None = None
    has_images: bool | None = None
    missing_published_at: bool | None = None
    missing_author_followers: bool | None = None
    missing_field: str | None = None
    q: str | None = None


@dataclass(frozen=True)
class RecordListResult:
    items: list[dict[str, Any]]
    meta: dict[str, Any]


SORT_COLUMNS = {
    "id": "p.id",
    "captured_at": "p.captured_at",
    "published_at": "p.published_at",
    "created_at": "p.created_at",
    "updated_at": "p.updated_at",
    "post_images_count": "image_count",
    "post_likes_count": "p.post_likes_count",
    "post_comments_count": "p.post_comments_count",
}


RECORD_SELECT = """
    SELECT
        p.*,
        sp.display_name AS platform_name,
        COALESCE(img.image_count, 0) AS image_count
    FROM web_posts p
    LEFT JOIN source_platforms sp ON sp.platform_key = p.platform_key
    LEFT JOIN (
        SELECT web_post_id, COUNT(*) AS image_count
        FROM web_post_images
        WHERE image_role = 'content'
        GROUP BY web_post_id
    ) img ON img.web_post_id = p.id
"""


class RecordRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def list_records(
        self,
        filters: RecordFilters,
        *,
        page: int,
        page_size: int,
        sort: str,
    ) -> RecordListResult:
        where, params = self._where(filters)
        order_sql, normalized_sort = self._order_by(sort)
        offset = (page - 1) * page_size
        count_row = self.conn.execute(
            f"SELECT COUNT(*) FROM web_posts p LEFT JOIN source_platforms sp ON sp.platform_key = p.platform_key {where}",
            params,
        ).fetchone()
        total = int(count_row[0] or 0)
        rows = self.conn.execute(
            f"""
            {RECORD_SELECT}
            {where}
            {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, offset],
        ).fetchall()
        return RecordListResult(
            items=[dict(row) for row in rows],
            meta={
                "page": page,
                "page_size": page_size,
                "total": total,
                "sort": normalized_sort,
            },
        )

    def get_record(self, record_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"{RECORD_SELECT} WHERE p.id = ?",
            (record_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_record_images(self, record_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM web_post_images
            WHERE web_post_id = ? AND image_role = 'content'
            ORDER BY image_index ASC, id ASC
            """,
            (record_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_record_image(self, image_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT i.*, p.platform_key, p.id AS record_id
            FROM web_post_images i
            JOIN web_posts p ON p.id = i.web_post_id
            WHERE i.id = ? AND i.image_role = 'content'
            """,
            (image_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_capture_for_record(self, record: dict[str, Any]) -> dict[str, Any] | None:
        capture_id = record.get("source_capture_id")
        if capture_id is None:
            return None
        row = self.conn.execute(
            "SELECT * FROM ctf_captures WHERE id = ?",
            (capture_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_capture_images(self, capture_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM ctf_capture_images
            WHERE ctf_capture_id = ?
            ORDER BY image_index ASC, id ASC
            """,
            (capture_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _where(self, filters: RecordFilters) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        self._add_equal(clauses, params, "p.platform_key", filters.platform_key)
        self._add_equal(clauses, params, "p.source_type", filters.source_type)
        self._add_equal(clauses, params, "p.status", filters.status)
        self._add_equal(clauses, params, "p.keyword", filters.keyword)
        self._add_range(clauses, params, "p.published_at", filters.published_from, filters.published_to)
        self._add_range(clauses, params, "p.captured_at", filters.captured_from, filters.captured_to)
        if filters.has_images is not None:
            op = ">" if filters.has_images else "="
            clauses.append(f"COALESCE(p.post_images_count, 0) {op} 0")
        if filters.missing_published_at or filters.missing_field == "published_at":
            clauses.append("(p.published_at IS NULL OR p.published_at = '')")
        if filters.missing_author_followers or filters.missing_field == "author_followers":
            clauses.append("p.author_followers_count IS NULL")
        if filters.missing_field == "images":
            clauses.append("COALESCE(p.post_images_count, 0) = 0")
        if filters.q:
            like = f"%{filters.q.strip()}%"
            clauses.append(
                """(
                    p.title LIKE ?
                    OR p.content_text LIKE ?
                    OR p.author_display_name LIKE ?
                    OR p.platform_post_id LIKE ?
                    OR p.source_url LIKE ?
                    OR p.canonical_url LIKE ?
                )"""
            )
            params.extend([like] * 6)
        if not clauses:
            return "", params
        return "WHERE " + " AND ".join(clauses), params

    @staticmethod
    def _add_equal(clauses: list[str], params: list[Any], column: str, value: str | None) -> None:
        if value is not None and value != "":
            clauses.append(f"{column} = ?")
            params.append(value)

    @staticmethod
    def _add_range(
        clauses: list[str],
        params: list[Any],
        column: str,
        start: str | None,
        end: str | None,
    ) -> None:
        if start:
            clauses.append(f"{column} >= ?")
            params.append(start)
        if end:
            clauses.append(f"{column} <= ?")
            params.append(end)

    @staticmethod
    def _order_by(sort: str) -> tuple[str, str]:
        descending = sort.startswith("-")
        key = sort[1:] if descending else sort
        if key not in SORT_COLUMNS:
            key = "captured_at"
            descending = True
        direction = "DESC" if descending else "ASC"
        normalized = f"-{key}" if descending else key
        return f"ORDER BY {SORT_COLUMNS[key]} {direction}, p.id DESC", normalized
