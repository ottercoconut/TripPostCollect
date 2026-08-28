"""SQLite-backed Xiaohongshu account registry and explicit leases."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
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


class XhsOrphanLeaseRecoveryRefused(RuntimeError):
    """Raised when an account lease cannot be proven to be orphaned safely."""


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
        "encrypted_state": root / "storage_state.enc",
        "metadata": root / "metadata.json",
    }


def ensure_xhs_schema(conn: sqlite3.Connection) -> None:
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


def _seconds_until(value: datetime | None, now: datetime) -> int:
    return max(0, int((value - now).total_seconds())) if value else 0


def acquire_account_lease(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    pool_config: dict[str, Any],
    requested_account_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or utc_now()
    current_iso = iso(current)
    lease_expires = iso(current + timedelta(seconds=int(pool_config["lease_seconds"])))
    requested = validate_account_id(requested_account_id)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM xhs_account_leases WHERE expires_at<=?", (current_iso,))
        account = _row(conn, requested)
        if not account or account["status"] != "active":
            raise XhsAccountUnavailable("requested_xhs_account_not_active")
        active_lease = conn.execute(
            "SELECT expires_at FROM xhs_account_leases WHERE account_id=?",
            (requested,),
        ).fetchone()
        if active_lease:
            raise XhsAccountUnavailable(
                "requested_xhs_account_busy",
                _seconds_until(parse_iso(active_lease["expires_at"]), current),
            )
        account_data = dict(account)
        conn.execute(
            "INSERT INTO xhs_account_leases(account_id, run_id, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
            (account_data["account_id"], run_id, current_iso, lease_expires),
        )
        conn.execute(
            "UPDATE xhs_accounts SET last_used_at=?, updated_at=datetime('now') WHERE account_id=?",
            (current_iso, account_data["account_id"]),
        )
        record_event(conn, account_id=account_data["account_id"], run_id=run_id, event_type="lease_acquired")
        conn.commit()
        return {**account_data, "lease_expires_at": lease_expires}
    except Exception:
        conn.rollback()
        raise


def acquire_account_login_lease(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    requested_account_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Exclusively reserve one non-retired account for the full login workflow."""
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")

    current = now or utc_now()
    current_iso = iso(current)
    lease_expires = iso(current + timedelta(seconds=lease_seconds))
    requested = validate_account_id(requested_account_id)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM xhs_account_leases WHERE expires_at<=?", (current_iso,))
        account = _row(conn, requested)
        if not account:
            raise XhsAccountUnavailable("requested_xhs_account_not_enrolled")
        if account["status"] == "retired":
            raise XhsAccountUnavailable("requested_xhs_account_retired")
        active_lease = conn.execute(
            "SELECT expires_at FROM xhs_account_leases WHERE account_id=?",
            (requested,),
        ).fetchone()
        if active_lease:
            raise XhsAccountUnavailable(
                "requested_xhs_account_busy",
                _seconds_until(parse_iso(active_lease["expires_at"]), current),
            )
        account_data = dict(account)
        conn.execute(
            "INSERT INTO xhs_account_leases(account_id, run_id, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
            (account_data["account_id"], run_id, current_iso, lease_expires),
        )
        record_event(
            conn,
            account_id=account_data["account_id"],
            run_id=run_id,
            event_type="login_lease_acquired",
        )
        conn.commit()
        return {**account_data, "lease_expires_at": lease_expires}
    except Exception:
        conn.rollback()
        raise


def release_account_lease(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    run_id: str,
    outcome: str,
) -> None:
    value = validate_account_id(account_id)
    conn.execute("DELETE FROM xhs_account_leases WHERE account_id=? AND run_id=?", (value, run_id))
    record_event(conn, account_id=value, run_id=run_id, event_type="lease_released", details={"outcome": outcome})
    conn.commit()


def recover_orphaned_account_lease(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    run_id: str,
    execution_state: dict[str, Any],
    live_processes: list[dict[str, Any]],
    state_path: Path,
    now: datetime | None = None,
    grace_seconds: int = 300,
) -> dict[str, Any]:
    """Release an exact active lease only after its failed runtime is proven dead.

    This intentionally does not mutate the execution state, discovery checkpoint,
    cursor, staged output, or account health. The caller must supply process evidence
    collected immediately before this transaction.
    """
    value = validate_account_id(account_id)
    current = now or utc_now()
    if grace_seconds < 60:
        raise ValueError("grace_seconds must be at least 60")
    if not run_id.strip():
        raise ValueError("run_id must not be empty")
    if live_processes:
        raise XhsOrphanLeaseRecoveryRefused("live XHS process evidence still exists")
    if execution_state.get("run_id") != run_id:
        raise XhsOrphanLeaseRecoveryRefused("execution state run_id does not match the lease")
    if execution_state.get("status") != "running":
        raise XhsOrphanLeaseRecoveryRefused("execution state is not an abandoned running state")
    plan = execution_state.get("plan")
    if not isinstance(plan, dict) or plan.get("account_id") != value:
        raise XhsOrphanLeaseRecoveryRefused("execution state account_id does not match the lease")
    events = execution_state.get("events")
    last_event = events[-1] if isinstance(events, list) and events else None
    if not isinstance(last_event, dict) or last_event.get("type") != "adaptive_search_stopped":
        raise XhsOrphanLeaseRecoveryRefused("last execution event is not adaptive_search_stopped")
    details = last_event.get("details")
    if not isinstance(details, dict):
        raise XhsOrphanLeaseRecoveryRefused("terminal execution event has no details")
    if details.get("stop_reason") != "runtime_failed" or details.get("batch_complete") is not False:
        raise XhsOrphanLeaseRecoveryRefused("terminal event is not an incomplete runtime failure")
    stopped_at = parse_iso(last_event.get("at"))
    if stopped_at is None:
        raise XhsOrphanLeaseRecoveryRefused("terminal event has no valid timestamp")
    terminal_age_seconds = int((current - stopped_at).total_seconds())
    if terminal_age_seconds < grace_seconds:
        raise XhsOrphanLeaseRecoveryRefused(
            f"terminal failure is too recent: age={terminal_age_seconds}s grace={grace_seconds}s"
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        lease = conn.execute(
            "SELECT run_id, acquired_at, expires_at FROM xhs_account_leases WHERE account_id=?",
            (value,),
        ).fetchone()
        if lease is None:
            raise XhsOrphanLeaseRecoveryRefused("no lease exists for the requested account")
        if lease["run_id"] != run_id:
            raise XhsOrphanLeaseRecoveryRefused("active lease run_id does not match the requested run")
        expires_at = parse_iso(lease["expires_at"])
        if expires_at is None:
            raise XhsOrphanLeaseRecoveryRefused("active lease has no valid expiry")
        if expires_at <= current:
            raise XhsOrphanLeaseRecoveryRefused(
                "lease is already expired; normal lease acquisition will remove it"
            )
        deleted = conn.execute(
            "DELETE FROM xhs_account_leases WHERE account_id=? AND run_id=?",
            (value, run_id),
        )
        if deleted.rowcount != 1:
            raise XhsOrphanLeaseRecoveryRefused("exact lease disappeared before recovery")
        audit = {
            "state_path": str(state_path.resolve()),
            "terminal_event_at": iso(stopped_at),
            "terminal_age_seconds": terminal_age_seconds,
            "stop_reason": details["stop_reason"],
            "stop_detail": details.get("stop_detail"),
            "batch_complete": details["batch_complete"],
            "checkpoint_mutated": False,
            "staged_outputs_mutated": False,
        }
        record_event(
            conn,
            account_id=value,
            run_id=run_id,
            event_type="orphan_lease_recovered",
            details=audit,
        )
        conn.commit()
        return {
            "account_id": value,
            "run_id": run_id,
            "lease_acquired_at": lease["acquired_at"],
            "lease_expires_at": lease["expires_at"],
            **audit,
        }
    except Exception:
        conn.rollback()
        raise
