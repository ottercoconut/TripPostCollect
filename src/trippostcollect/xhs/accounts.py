"""SQLite-backed Xiaohongshu account registry and explicit leases."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trippostcollect.core.paths import XHS_ACCOUNT_ROOT, ensure_dir
from trippostcollect.db.bootstrap import ensure_xhs_control_schema


ACCOUNT_ID_RE = re.compile(r"xhs-[a-z0-9][a-z0-9_-]{1,31}\Z")
ACCOUNT_STATUSES = {
    "login_pending",
    "active",
    "login_required",
    "quarantined",
    "retired",
}


class XhsAccountUnavailable(RuntimeError):
    def __init__(self, reason: str, wait_seconds: int = 0):
        self.reason = reason
        self.wait_seconds = max(0, int(wait_seconds))
        super().__init__(f"{reason}: wait_seconds={self.wait_seconds}")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_account_id(account_id: str) -> str:
    value = account_id.strip()
    if not ACCOUNT_ID_RE.fullmatch(value):
        raise ValueError("account_id must match xhs-[a-z0-9][a-z0-9_-]{1,31}")
    return value


def account_paths(account_id: str) -> dict[str, Path]:
    value = validate_account_id(account_id)
    root = XHS_ACCOUNT_ROOT / value
    return {
        "root": root,
        "profile": root / "profile",
        "lease_lock": root / "lease.lock",
        "encrypted_state": root / "storage_state.enc",
        "metadata": root / "metadata.json",
    }


def ensure_xhs_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_xhs_control_schema(conn)
    conn.commit()


def _row(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM xhs_accounts WHERE account_id=?", (account_id,)).fetchone()


def get_account(conn: sqlite3.Connection, account_id: str) -> dict[str, Any] | None:
    value = validate_account_id(account_id)
    row = _row(conn, value)
    return dict(row) if row else None


def list_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT * FROM xhs_accounts ORDER BY account_id").fetchall()]


def record_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    account_id: str | None = None,
    run_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO xhs_account_events(account_id, run_id, event_type, details_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (account_id, run_id, event_type, json.dumps(details or {}, ensure_ascii=False), iso()),
    )


def enroll_account(conn: sqlite3.Connection, account_id: str) -> dict[str, Any]:
    value = validate_account_id(account_id)
    paths = account_paths(value)
    ensure_dir(paths["profile"])
    paths["root"].chmod(0o700)
    paths["profile"].chmod(0o700)
    conn.execute(
        """
        INSERT INTO xhs_accounts(account_id, status, profile_dir, encrypted_state_path)
        VALUES (?, 'login_pending', ?, ?)
        ON CONFLICT(account_id) DO NOTHING
        """,
        (value, str(paths["profile"]), str(paths["encrypted_state"])),
    )
    record_event(conn, account_id=value, event_type="account_enrolled")
    conn.commit()
    result = get_account(conn, value)
    if result is None:
        raise RuntimeError(f"failed to enroll XHS account {value}")
    return result


def set_account_status(
    conn: sqlite3.Connection,
    account_id: str,
    status: str,
    *,
    reason: str,
) -> None:
    value = validate_account_id(account_id)
    if status not in ACCOUNT_STATUSES:
        raise ValueError(f"unsupported XHS account status: {status}")
    conn.execute(
        """
        UPDATE xhs_accounts
        SET status=?, updated_at=datetime('now')
        WHERE account_id=?
        """,
        (status, value),
    )
    record_event(
        conn,
        account_id=value,
        event_type="account_status_changed",
        details={"status": status, "reason": reason},
    )
    conn.commit()


def mark_account_verified(conn: sqlite3.Connection, account_id: str, identity_hash: str) -> None:
    value = validate_account_id(account_id)
    existing = conn.execute(
        "SELECT account_id FROM xhs_accounts WHERE identity_hash=? AND account_id<>?",
        (identity_hash, value),
    ).fetchone()
    if existing:
        raise ValueError(f"platform identity already belongs to {existing[0]}")
    conn.execute(
        """
        UPDATE xhs_accounts
        SET status='active', identity_hash=?, last_verified_at=?, updated_at=datetime('now')
        WHERE account_id=?
        """,
        (identity_hash, iso(), value),
    )
    record_event(conn, account_id=value, event_type="login_persisted", details={"identity_hash": identity_hash})
    conn.commit()
