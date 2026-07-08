"""Read-only capture and artifact repositories."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CaptureFilters:
    site_key: str | None = None
    capture_kind: str | None = None
    ok: bool | None = None
    content_ready: bool | None = None
    q: str | None = None


CAPTURE_SORT_COLUMNS = {
    "id": "id",
    "captured_at": "captured_at",
    "site_key": "site_key",
    "ok": "ok",
}


class CaptureRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def list_captures(
        self,
        filters: CaptureFilters,
        *,
        page: int,
        page_size: int,
        sort: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        where, params = self._where(filters)
        order_sql, normalized_sort = self._order_by(sort)
        offset = (page - 1) * page_size
        total = int(self.conn.execute(f"SELECT COUNT(*) FROM ctf_captures {where}", params).fetchone()[0] or 0)
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM ctf_captures
            {where}
            {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, offset],
        ).fetchall()
        return [dict(row) for row in rows], {
            "page": page,
            "page_size": page_size,
            "total": total,
            "sort": normalized_sort,
        }

    def get_capture(self, capture_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ctf_captures WHERE id = ?", (capture_id,)).fetchone()
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

    def get_capture_image(self, image_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT i.*, c.site_key, c.id AS capture_id
            FROM ctf_capture_images i
            JOIN ctf_captures c ON c.id = i.ctf_capture_id
            WHERE i.id = ?
            """,
            (image_id,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _where(filters: CaptureFilters) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if filters.site_key:
            clauses.append("site_key = ?")
            params.append(filters.site_key)
        if filters.capture_kind:
            clauses.append("capture_kind = ?")
            params.append(filters.capture_kind)
        if filters.ok is not None:
            clauses.append("ok = ?")
            params.append(1 if filters.ok else 0)
        if filters.content_ready is not None:
            clauses.append("content_ready = ?")
            params.append(1 if filters.content_ready else 0)
        if filters.q:
            like = f"%{filters.q.strip()}%"
            clauses.append("(target_url LIKE ? OR final_url LIKE ? OR title LIKE ? OR primary_flag LIKE ?)")
            params.extend([like] * 4)
        if not clauses:
            return "", params
        return "WHERE " + " AND ".join(clauses), params

    @staticmethod
    def _order_by(sort: str) -> tuple[str, str]:
        descending = sort.startswith("-")
        key = sort[1:] if descending else sort
        if key not in CAPTURE_SORT_COLUMNS:
            key = "captured_at"
            descending = True
        direction = "DESC" if descending else "ASC"
        normalized = f"-{key}" if descending else key
        return f"ORDER BY {CAPTURE_SORT_COLUMNS[key]} {direction}, id DESC", normalized
