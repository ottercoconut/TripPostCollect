"""SQLite connection helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_readonly_db(db_path: str | Path, *, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    resolved = Path(db_path).expanduser().resolve()
    uri = f"file:{resolved}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    return conn


def connect_db(db_path: str | Path, *, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(db_path).expanduser(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    return conn
