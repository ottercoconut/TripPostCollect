"""#151：并行启动的入口在 bootstrap 时等待另一进程的写锁，而不是立即报 database is locked。"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from trippostcollect.db import bootstrap


def _hold_write_lock(db_path: Path, seconds: float, started: threading.Event) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        started.set()
        time.sleep(seconds)
        conn.execute("COMMIT")
    finally:
        conn.close()


def _bootstrap_while_locked(db_path: Path, hold_seconds: float) -> None:
    started = threading.Event()
    holder = threading.Thread(target=_hold_write_lock, args=(db_path, hold_seconds, started))
    holder.start()
    try:
        assert started.wait(5)
        bootstrap.bootstrap_database(db_path, sync_jobs=False)
    finally:
        holder.join()


def test_bootstrap_busy_timeout_matches_formal_import() -> None:
    from trippostcollect.db.content import FORMAL_SQLITE_BUSY_TIMEOUT_MS

    assert bootstrap.BOOTSTRAP_SQLITE_BUSY_TIMEOUT_MS == FORMAL_SQLITE_BUSY_TIMEOUT_MS


def test_bootstrap_waits_for_concurrent_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "db.sqlite"
    bootstrap.bootstrap_database(db_path, sync_jobs=False)
    monkeypatch.setattr(bootstrap, "BOOTSTRAP_SQLITE_BUSY_TIMEOUT_MS", 5_000)

    _bootstrap_while_locked(db_path, hold_seconds=1.0)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=3").fetchone() == (1,)


def test_bootstrap_timeout_is_applied_to_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "db.sqlite"
    bootstrap.bootstrap_database(db_path, sync_jobs=False)
    monkeypatch.setattr(bootstrap, "BOOTSTRAP_SQLITE_BUSY_TIMEOUT_MS", 100)

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        _bootstrap_while_locked(db_path, hold_seconds=1.0)
