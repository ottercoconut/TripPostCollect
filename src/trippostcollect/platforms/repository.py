"""Read-only platform repository."""

from __future__ import annotations

import sqlite3
from typing import Any


def list_platforms(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM source_platforms
        ORDER BY status DESC, platform_key ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]
