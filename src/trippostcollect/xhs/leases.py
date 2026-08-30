"""Exact-owner Xiaohongshu leases and safe orphan reconciliation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import FrameType
from typing import Any, Mapping, Sequence

from trippostcollect.core.paths import ensure_dir
from trippostcollect.xhs.accounts import (
    XhsAccountUnavailable,
    account_paths,
    ensure_xhs_schema,
    get_account,
    iso,
    parse_iso,
    record_event,
    validate_account_id,
)


LEASE_IDENTITY_VERSION = 1
DEFAULT_CHILD_SHUTDOWN_BUDGET_SECONDS = 30
DEFAULT_ROOT_FINALIZE_BUDGET_SECONDS = 270
LEASE_DB_ENV = "TRIPPOSTCOLLECT_XHS_LEASE_DB"
LEASE_ID_ENV = "TRIPPOSTCOLLECT_XHS_LEASE_ID"
LEASE_OWNER_TOKEN_ENV = "TRIPPOSTCOLLECT_XHS_LEASE_OWNER_TOKEN"


class XhsLeaseOwnershipError(RuntimeError):
    """Raised when a release or registry write does not own the exact lease."""


class XhsLeaseProcessesAlive(RuntimeError):
    """Raised when tracked runtime processes prevent safe lease release."""


class XhsOrphanLeaseRecoveryRefused(RuntimeError):
    """Raised when an account lease cannot be proven orphaned safely."""


class XhsLeaseSignal(BaseException):
    """Interrupt raised after recording SIGINT/SIGTERM for guarded cleanup."""

    def __init__(self, signum: int):
        self.signum = int(signum)
        super().__init__(f"XHS lease owner interrupted by signal {self.signum}")


@dataclass(frozen=True)
class LeaseBudget:
    runtime_seconds: int
    child_shutdown_seconds: int = DEFAULT_CHILD_SHUTDOWN_BUDGET_SECONDS
    root_finalize_seconds: int = DEFAULT_ROOT_FINALIZE_BUDGET_SECONDS

    def __post_init__(self) -> None:
        if self.runtime_seconds <= 0:
            raise ValueError("runtime_seconds must be positive")
        if self.child_shutdown_seconds < 0 or self.root_finalize_seconds < 0:
            raise ValueError("lease cleanup budgets cannot be negative")

    @property
    def lease_seconds(self) -> int:
        return self.runtime_seconds + self.child_shutdown_seconds + self.root_finalize_seconds

    def public(self) -> dict[str, int]:
        return {
            "runtime_seconds": self.runtime_seconds,
            "child_shutdown_seconds": self.child_shutdown_seconds,
            "root_finalize_seconds": self.root_finalize_seconds,
            "lease_seconds": self.lease_seconds,
        }


def crawl_lease_budget(*, timeout_seconds: int, configured_lease_seconds: int) -> LeaseBudget:
    budget = LeaseBudget(runtime_seconds=int(timeout_seconds))
    if int(configured_lease_seconds) < budget.lease_seconds:
        raise ValueError(
            "configured XHS lease ceiling does not cover runtime, child shutdown, and root finalization"
        )
    return budget


def login_lease_budget(timeout_seconds: int) -> LeaseBudget:
    """Cover both operator waits plus child/browser shutdown and root finalization."""

    return LeaseBudget(runtime_seconds=int(timeout_seconds) * 2)


@dataclass(frozen=True)
class ProcessIdentity:
    host_id: str
    boot_id: str
    pid: int
    process_started_at: str
    process_start_token: str
    pgid: int

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProcessSnapshot:
    identity: ProcessIdentity
    argv: tuple[str, ...]


@dataclass(frozen=True)
class LeaseSubprocessResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


def _read_nonempty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _sysctl_value(name: str) -> str:
    try:
        result = subprocess.run(
            ["sysctl", "-n", name],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def system_host_id() -> str:
    value = _read_nonempty(Path("/etc/machine-id")) or _read_nonempty(
        Path("/var/lib/dbus/machine-id")
    )
    if not value and sys.platform == "darwin":
        value = _sysctl_value("kern.uuid")
    if value:
        return value.lower()
    fallback = f"{socket.gethostname()}:{os.uname().sysname}:{os.uname().machine}"
    return f"fallback-{hashlib.sha256(fallback.encode('utf-8')).hexdigest()}"


def system_boot_id() -> str:
    value = _read_nonempty(Path("/proc/sys/kernel/random/boot_id"))
    if not value and sys.platform == "darwin":
        value = _sysctl_value("kern.bootsessionuuid")
    if value:
        return value.lower()
    boot_marker = _sysctl_value("kern.boottime")
    if not boot_marker:
        boot_marker = f"unknown:{system_host_id()}"
    return f"fallback-{hashlib.sha256(boot_marker.encode('utf-8')).hexdigest()}"


def _wall_time_iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat(timespec="microseconds")


class SystemProcessInspector:
    """Read exact PID/start/PGID identity without command-substring ownership guesses."""

    def __init__(self) -> None:
        self.host_id = system_host_id()
        self.boot_id = system_boot_id()

    def _linux_identity(self, pid: int) -> ProcessIdentity | None:
        stat_path = Path("/proc") / str(pid) / "stat"
        try:
            raw = stat_path.read_text(encoding="utf-8")
            close_paren = raw.rfind(")")
            fields = raw[close_paren + 2 :].split()
            state = fields[0]
            pgid = int(fields[2])
            start_ticks = int(fields[19])
            ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
            boot_epoch = 0
            for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
                if line.startswith("btime "):
                    boot_epoch = int(line.split()[1])
                    break
        except (OSError, ValueError, IndexError):
            return None
        if state == "Z" or boot_epoch <= 0:
            return None
        return ProcessIdentity(
            host_id=self.host_id,
            boot_id=self.boot_id,
            pid=int(pid),
            process_started_at=_wall_time_iso(boot_epoch + start_ticks / ticks_per_second),
            process_start_token=f"linux:{start_ticks}",
            pgid=pgid,
        )

    def _ps_identity(self, pid: int) -> ProcessIdentity | None:
        try:
            result = subprocess.run(
                ["ps", "-p", str(int(pid)), "-o", "pid=,pgid=,lstart=,state="],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        parts = result.stdout.strip().split()
        if len(parts) < 8 or "Z" in parts[7]:
            return None
        try:
            actual_pid = int(parts[0])
            pgid = int(parts[1])
            start_text = " ".join(parts[2:7])
            started = datetime.strptime(start_text, "%a %b %d %H:%M:%S %Y")
        except (ValueError, IndexError):
            return None
        started = started.astimezone().astimezone(timezone.utc)
        return ProcessIdentity(
            host_id=self.host_id,
            boot_id=self.boot_id,
            pid=actual_pid,
            process_started_at=started.isoformat(timespec="seconds"),
            process_start_token=f"ps-lstart:{start_text}",
            pgid=pgid,
        )

    def identity(self, pid: int) -> ProcessIdentity | None:
        if sys.platform.startswith("linux") and Path("/proc").is_dir():
            return self._linux_identity(int(pid))
        return self._ps_identity(int(pid))

    def current_identity(self) -> ProcessIdentity:
        identity = self.identity(os.getpid())
        if identity is None:
            raise RuntimeError("cannot capture exact lease-owner process identity")
        return identity

    def _linux_snapshots(self) -> list[ProcessSnapshot]:
        snapshots: list[ProcessSnapshot] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            identity = self._linux_identity(int(entry.name))
            if identity is None:
                continue
            try:
                raw_argv = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            argv = tuple(
                value.decode("utf-8", errors="replace")
                for value in raw_argv.split(b"\0")
                if value
            )
            snapshots.append(ProcessSnapshot(identity=identity, argv=argv))
        return snapshots

    def _ps_snapshots(self) -> list[ProcessSnapshot]:
        try:
            result = subprocess.run(
                ["ps", "-axo", "pid=,pgid=,lstart=,state=,command="],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError):
            return []
        snapshots: list[ProcessSnapshot] = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(maxsplit=8)
            if len(parts) < 9 or "Z" in parts[7]:
                continue
            try:
                pid = int(parts[0])
                pgid = int(parts[1])
                start_text = " ".join(parts[2:7])
                started = datetime.strptime(start_text, "%a %b %d %H:%M:%S %Y")
                argv = tuple(shlex.split(parts[8]))
            except (ValueError, IndexError):
                continue
            identity = ProcessIdentity(
                host_id=self.host_id,
                boot_id=self.boot_id,
                pid=pid,
                process_started_at=started.astimezone().astimezone(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                process_start_token=f"ps-lstart:{start_text}",
                pgid=pgid,
            )
            snapshots.append(ProcessSnapshot(identity=identity, argv=argv))
        return snapshots

    def snapshots(self) -> list[ProcessSnapshot]:
        if sys.platform.startswith("linux") and Path("/proc").is_dir():
            return self._linux_snapshots()
        return self._ps_snapshots()

    def group_members(self, pgid: int) -> list[ProcessSnapshot]:
        return [item for item in self.snapshots() if item.identity.pgid == int(pgid)]

    def profile_processes(self, profile_dir: Path) -> list[ProcessSnapshot]:
        expected = profile_dir.expanduser().resolve()
        matches: list[ProcessSnapshot] = []
        for snapshot in self.snapshots():
            values: list[str] = []
            for index, argument in enumerate(snapshot.argv):
                if argument.startswith("--user-data-dir="):
                    values.append(argument.split("=", 1)[1])
                elif argument == "--user-data-dir" and index + 1 < len(snapshot.argv):
                    values.append(snapshot.argv[index + 1])
            for value in values:
                try:
                    candidate = Path(value).expanduser().resolve()
                except OSError:
                    continue
                if candidate == expected:
                    matches.append(snapshot)
                    break
        return matches


class AccountLeaseFileLock:
    """Best-effort same-host mutex; SQLite exact ownership remains authoritative."""

    def __init__(self, path: Path):
        self.path = path
        self._handle: Any = None

    def acquire(self) -> None:
        ensure_dir(self.path.parent)
        handle = self.path.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise XhsAccountUnavailable("requested_xhs_account_local_lock_busy") from None
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def _seconds_until(value: datetime | None, now: datetime) -> int:
    return max(0, int((value - now).total_seconds())) if value else 0


def _owner_token_digest(owner_token: str) -> str:
    return hashlib.sha256(owner_token.encode("utf-8")).hexdigest()


def public_lease(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    token = str(result.pop("owner_token", ""))
    if token:
        result["owner_token_sha256"] = _owner_token_digest(token)
    return result


def acquire_exact_account_lease(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    lease_kind: str,
    requested_account_id: str,
    execution_state_path: Path,
    budget: LeaseBudget,
    owner: ProcessIdentity,
    now: datetime | None = None,
    lease_id: str | None = None,
    owner_token: str | None = None,
) -> dict[str, Any]:
    if lease_kind not in {"crawl", "login", "repair"}:
        raise ValueError(f"unsupported XHS lease kind: {lease_kind}")
    if not run_id.strip():
        raise ValueError("run_id must not be empty")
    current = now or datetime.now(timezone.utc)
    current_iso = iso(current)
    requested = validate_account_id(requested_account_id)
    exact_lease_id = lease_id or uuid.uuid4().hex
    exact_owner_token = owner_token or secrets.token_urlsafe(32)
    expires_at = iso(current + timedelta(seconds=budget.lease_seconds))

    conn.execute("BEGIN IMMEDIATE")
    try:
        account = conn.execute(
            "SELECT * FROM xhs_accounts WHERE account_id=?",
            (requested,),
        ).fetchone()
        if not account:
            reason = (
                "requested_xhs_account_not_enrolled"
                if lease_kind == "login"
                else "requested_xhs_account_not_active"
            )
            raise XhsAccountUnavailable(reason)
        account_data = dict(account)
        if lease_kind == "login":
            if account_data["status"] == "retired":
                raise XhsAccountUnavailable("requested_xhs_account_retired")
        elif account_data["status"] != "active":
            raise XhsAccountUnavailable("requested_xhs_account_not_active")
        existing = conn.execute(
            "SELECT expires_at FROM xhs_account_leases WHERE account_id=?",
            (requested,),
        ).fetchone()
        if existing:
            raise XhsAccountUnavailable(
                "requested_xhs_account_busy",
                _seconds_until(parse_iso(existing["expires_at"]), current),
            )
        conn.execute(
            """
            INSERT INTO xhs_account_leases(
                account_id, lease_id, owner_token, run_id, lease_kind,
                owner_host_id, owner_boot_id, owner_pid, owner_process_started_at,
                owner_process_start_token, owner_pgid, execution_state_path,
                acquired_at, heartbeat_at, expires_at, lease_duration_seconds,
                child_shutdown_budget_seconds, root_finalize_budget_seconds,
                identity_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                requested,
                exact_lease_id,
                exact_owner_token,
                run_id,
                lease_kind,
                owner.host_id,
                owner.boot_id,
                owner.pid,
                owner.process_started_at,
                owner.process_start_token,
                owner.pgid,
                str(execution_state_path.expanduser().resolve()),
                current_iso,
                current_iso,
                expires_at,
                budget.lease_seconds,
                budget.child_shutdown_seconds,
                budget.root_finalize_seconds,
                LEASE_IDENTITY_VERSION,
            ),
        )
        if lease_kind != "login":
            conn.execute(
                "UPDATE xhs_accounts SET last_used_at=?, updated_at=datetime('now') WHERE account_id=?",
                (current_iso, requested),
            )
        event_type = "login_lease_acquired" if lease_kind == "login" else "lease_acquired"
        record_event(
            conn,
            account_id=requested,
            run_id=run_id,
            event_type=event_type,
            details={
                "lease_id": exact_lease_id,
                "lease_kind": lease_kind,
                "owner_token_sha256": _owner_token_digest(exact_owner_token),
                "owner": owner.public(),
                "execution_state_path": str(execution_state_path.expanduser().resolve()),
                "budget": budget.public(),
                "expires_at": expires_at,
            },
        )
        conn.commit()
        return {
            **account_data,
            "lease_id": exact_lease_id,
            "owner_token": exact_owner_token,
            "lease_expires_at": expires_at,
            "lease_budget": budget.public(),
            "owner": owner.public(),
        }
    except Exception:
        conn.rollback()
        raise


def release_exact_account_lease(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    run_id: str,
    lease_id: str,
    owner_token: str,
    outcome: str,
    details: dict[str, Any] | None = None,
) -> None:
    value = validate_account_id(account_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        deleted = conn.execute(
            """
            DELETE FROM xhs_account_leases
            WHERE account_id=? AND run_id=? AND lease_id=? AND owner_token=?
            """,
            (value, run_id, lease_id, owner_token),
        )
        if deleted.rowcount != 1:
            raise XhsLeaseOwnershipError(
                "exact XHS lease release rejected: owner token or lease identity mismatch"
            )
        record_event(
            conn,
            account_id=value,
            run_id=run_id,
            event_type="lease_released",
            details={
                "lease_id": lease_id,
                "owner_token_sha256": _owner_token_digest(owner_token),
                "outcome": outcome,
                **(details or {}),
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def register_lease_process(
    conn: sqlite3.Connection,
    *,
    lease_id: str,
    owner_token: str,
    process_role: str,
    identity: ProcessIdentity,
) -> None:
    if process_role not in {"child", "exporter", "browser"}:
        raise ValueError(f"unsupported lease process role: {process_role}")
    owned = conn.execute(
        "SELECT 1 FROM xhs_account_leases WHERE lease_id=? AND owner_token=?",
        (lease_id, owner_token),
    ).fetchone()
    if not owned:
        raise XhsLeaseOwnershipError("cannot register a process for a lease owned by another token")
    conn.execute(
        """
        INSERT INTO xhs_lease_processes(
            lease_id, process_role, host_id, boot_id, pid, process_started_at,
            process_start_token, pgid, registered_at, exited_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(lease_id, process_role, pid, process_start_token) DO UPDATE SET
            exited_at=NULL
        """,
        (
            lease_id,
            process_role,
            identity.host_id,
            identity.boot_id,
            identity.pid,
            identity.process_started_at,
            identity.process_start_token,
            identity.pgid,
            iso(),
        ),
    )
    conn.commit()


def mark_lease_process_exited(
    conn: sqlite3.Connection,
    *,
    lease_id: str,
    owner_token: str,
    process_role: str,
    identity: ProcessIdentity,
) -> None:
    updated = conn.execute(
        """
        UPDATE xhs_lease_processes
        SET exited_at=?
        WHERE lease_id=? AND process_role=? AND pid=? AND process_start_token=?
          AND EXISTS (
              SELECT 1 FROM xhs_account_leases
              WHERE lease_id=? AND owner_token=?
          )
        """,
        (
            iso(),
            lease_id,
            process_role,
            identity.pid,
            identity.process_start_token,
            lease_id,
            owner_token,
        ),
    )
    if updated.rowcount != 1:
        raise XhsLeaseOwnershipError("cannot mark a lease process exited with the wrong owner")
    conn.commit()


def register_lease_process_from_environment(
    *,
    pid: int,
    process_role: str,
    environ: Mapping[str, str] | None = None,
    inspector: SystemProcessInspector | None = None,
) -> ProcessIdentity | None:
    source = environ or os.environ
    db_value = str(source.get(LEASE_DB_ENV) or "")
    lease_id = str(source.get(LEASE_ID_ENV) or "")
    owner_token = str(source.get(LEASE_OWNER_TOKEN_ENV) or "")
    if not db_value or not lease_id or not owner_token:
        return None
    process_inspector = inspector or SystemProcessInspector()
    identity = process_inspector.identity(int(pid))
    if identity is None:
        raise RuntimeError(f"cannot capture {process_role} process identity for pid {pid}")
    with sqlite3.connect(Path(db_value).expanduser().resolve()) as conn:
        conn.row_factory = sqlite3.Row
        ensure_xhs_schema(conn)
        register_lease_process(
            conn,
            lease_id=lease_id,
            owner_token=owner_token,
            process_role=process_role,
            identity=identity,
        )
    return identity


def mark_lease_process_exited_from_environment(
    *,
    identity: ProcessIdentity | None,
    process_role: str,
    environ: Mapping[str, str] | None = None,
) -> None:
    if identity is None:
        return
    source = environ or os.environ
    db_value = str(source.get(LEASE_DB_ENV) or "")
    lease_id = str(source.get(LEASE_ID_ENV) or "")
    owner_token = str(source.get(LEASE_OWNER_TOKEN_ENV) or "")
    if not db_value or not lease_id or not owner_token:
        return
    with sqlite3.connect(Path(db_value).expanduser().resolve()) as conn:
        conn.row_factory = sqlite3.Row
        ensure_xhs_schema(conn)
        mark_lease_process_exited(
            conn,
            lease_id=lease_id,
            owner_token=owner_token,
            process_role=process_role,
            identity=identity,
        )


def _stored_identity(row: Mapping[str, Any], *, prefix: str = "") -> ProcessIdentity:
    return ProcessIdentity(
        host_id=str(row[f"{prefix}host_id"]),
        boot_id=str(row[f"{prefix}boot_id"]),
        pid=int(row[f"{prefix}pid"]),
        process_started_at=str(row[f"{prefix}process_started_at"]),
        process_start_token=str(row[f"{prefix}process_start_token"]),
        pgid=int(row[f"{prefix}pgid"]),
    )


def _identity_assessment(
    stored: ProcessIdentity,
    inspector: SystemProcessInspector,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {"stored": stored.public(), "live": False}
    if stored.host_id != inspector.host_id:
        return {**evidence, "live": None, "reason": "different_host_unverifiable"}
    if stored.boot_id != inspector.boot_id:
        return {**evidence, "reason": "different_boot_proves_old_process_dead"}
    observed = inspector.identity(stored.pid)
    if observed is None:
        return {**evidence, "reason": "pid_absent"}
    evidence["observed"] = observed.public()
    if (
        observed.process_start_token != stored.process_start_token
        or observed.process_started_at != stored.process_started_at
        or observed.pgid != stored.pgid
    ):
        return {**evidence, "reason": "pid_reused_or_identity_changed"}
    return {**evidence, "live": True, "reason": "exact_process_identity_alive"}


def assess_lease_runtime(
    conn: sqlite3.Connection,
    *,
    lease: Mapping[str, Any],
    profile_dir: Path,
    inspector: SystemProcessInspector,
    include_owner: bool,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = []
    if include_owner:
        owner = _stored_identity(lease, prefix="owner_")
        assessment = {"role": "owner", **_identity_assessment(owner, inspector)}
        checks.append(assessment)
        if assessment["live"] is not False:
            blocking.append(assessment)

    process_rows = conn.execute(
        "SELECT * FROM xhs_lease_processes WHERE lease_id=? ORDER BY process_role, registered_at",
        (lease["lease_id"],),
    ).fetchall()
    for raw_row in process_rows:
        row = dict(raw_row)
        stored = _stored_identity(row)
        assessment = {
            "role": row["process_role"],
            "registered_at": row["registered_at"],
            "exited_at": row["exited_at"],
            **_identity_assessment(stored, inspector),
        }
        checks.append(assessment)
        if assessment["live"] is True or assessment["live"] is None:
            blocking.append(assessment)
            continue
        if (
            stored.host_id == inspector.host_id
            and stored.boot_id == inspector.boot_id
            and assessment["reason"] == "pid_absent"
        ):
            members = inspector.group_members(stored.pgid)
            if members:
                residual = {
                    "role": f"{row['process_role']}_process_group",
                    "live": True,
                    "reason": "registered_group_has_residual_members",
                    "pgid": stored.pgid,
                    "members": [item.identity.public() for item in members],
                }
                checks.append(residual)
                blocking.append(residual)

    profile_processes = inspector.profile_processes(profile_dir)
    for snapshot in profile_processes:
        evidence = {
            "role": "account_profile_chrome",
            "live": True,
            "reason": "exact_profile_argument_alive",
            "observed": snapshot.identity.public(),
        }
        checks.append(evidence)
        blocking.append(evidence)
    return {
        "safe_to_release": not blocking,
        "current_host_id": inspector.host_id,
        "current_boot_id": inspector.boot_id,
        "checks": checks,
        "blocking": blocking,
    }


def execution_terminal_assessment(
    state_path: Path,
    *,
    expected_run_id: str,
    expected_account_id: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "state_path": str(state_path.expanduser().resolve()),
        "exists": state_path.is_file(),
        "readable": False,
        "terminal_complete": False,
        "adaptive_search_stopped_present": False,
    }
    if not result["exists"]:
        result["reason"] = "execution_state_missing"
        return result
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result["reason"] = f"execution_state_unreadable:{type(exc).__name__}"
        return result
    if not isinstance(value, dict):
        result["reason"] = "execution_state_not_object"
        return result
    result["readable"] = True
    result["state_run_id"] = value.get("run_id")
    result["state_status"] = value.get("status")
    plan = value.get("plan") if isinstance(value.get("plan"), dict) else {}
    result["state_account_id"] = plan.get("account_id")
    events = value.get("events") if isinstance(value.get("events"), list) else []
    event_types = [item.get("type") for item in events if isinstance(item, dict)]
    result["last_event_type"] = event_types[-1] if event_types else None
    result["adaptive_search_stopped_present"] = "adaptive_search_stopped" in event_types
    steps = value.get("steps") if isinstance(value.get("steps"), dict) else {}
    finalized = steps.get("task_finalized") if isinstance(steps.get("task_finalized"), dict) else {}
    identity_matches = bool(
        value.get("run_id") == expected_run_id and plan.get("account_id") == expected_account_id
    )
    result["identity_matches"] = identity_matches
    result["terminal_complete"] = bool(
        identity_matches
        and value.get("status") in {"completed", "failed"}
        and finalized.get("status") in {"completed", "failed", "skipped"}
    )
    result["reason"] = (
        "terminal_state_complete" if result["terminal_complete"] else "terminal_state_incomplete"
    )
    return result


def _load_exact_lease(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    run_id: str,
    lease_id: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT * FROM xhs_account_leases
        WHERE account_id=? AND run_id=? AND lease_id=?
        """,
        (account_id, run_id, lease_id),
    ).fetchone()
    if row is None:
        raise XhsOrphanLeaseRecoveryRefused("exact requested XHS lease does not exist")
    value = dict(row)
    if int(value.get("identity_version") or 0) != LEASE_IDENTITY_VERSION:
        raise XhsOrphanLeaseRecoveryRefused("lease has no supported exact owner identity")
    return value


def recover_orphaned_account_lease(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    run_id: str,
    lease_id: str,
    inspector: SystemProcessInspector | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Release only mutual exclusion after two exact dead-process reconciliations."""

    value = validate_account_id(account_id)
    if not run_id.strip() or not lease_id.strip():
        raise ValueError("run_id and lease_id must not be empty")
    account = get_account(conn, value)
    if not account:
        raise XhsOrphanLeaseRecoveryRefused("requested XHS account is not enrolled")
    lock = AccountLeaseFileLock(account_paths(value)["lease_lock"])
    try:
        lock.acquire()
    except XhsAccountUnavailable as exc:
        raise XhsOrphanLeaseRecoveryRefused("account lease flock is still held") from exc
    process_inspector = inspector or SystemProcessInspector()
    current = now or datetime.now(timezone.utc)
    try:
        lease = _load_exact_lease(
            conn,
            account_id=value,
            run_id=run_id,
            lease_id=lease_id,
        )
        first_process_check = assess_lease_runtime(
            conn,
            lease=lease,
            profile_dir=Path(str(account["profile_dir"])),
            inspector=process_inspector,
            include_owner=True,
        )
        if not first_process_check["safe_to_release"]:
            raise XhsOrphanLeaseRecoveryRefused(
                "exact owner, child, exporter, or account-profile Chrome is still live"
            )
        terminal = execution_terminal_assessment(
            Path(str(lease["execution_state_path"])),
            expected_run_id=run_id,
            expected_account_id=value,
        )

        conn.execute("BEGIN IMMEDIATE")
        try:
            locked_lease = _load_exact_lease(
                conn,
                account_id=value,
                run_id=run_id,
                lease_id=lease_id,
            )
            if locked_lease["owner_token"] != lease["owner_token"]:
                raise XhsOrphanLeaseRecoveryRefused(
                    "lease owner changed during orphan reconciliation"
                )
            second_process_check = assess_lease_runtime(
                conn,
                lease=locked_lease,
                profile_dir=Path(str(account["profile_dir"])),
                inspector=process_inspector,
                include_owner=True,
            )
            if not second_process_check["safe_to_release"]:
                raise XhsOrphanLeaseRecoveryRefused(
                    "runtime process appeared during orphan reconciliation"
                )
            deleted = conn.execute(
                """
                DELETE FROM xhs_account_leases
                WHERE account_id=? AND run_id=? AND lease_id=? AND owner_token=?
                """,
                (value, run_id, lease_id, locked_lease["owner_token"]),
            )
            if deleted.rowcount != 1:
                raise XhsOrphanLeaseRecoveryRefused(
                    "exact lease disappeared before orphan reconciliation committed"
                )
            acquired_at = parse_iso(locked_lease["acquired_at"])
            audit = {
                "lease_id": lease_id,
                "owner_token_sha256": _owner_token_digest(locked_lease["owner_token"]),
                "lease_kind": locked_lease["lease_kind"],
                "owner": _stored_identity(locked_lease, prefix="owner_").public(),
                "reconciled_at": iso(current),
                "lease_age_seconds": (
                    max(0, int((current - acquired_at).total_seconds())) if acquired_at else None
                ),
                "lease_expires_at": locked_lease["expires_at"],
                "lease_was_expired": bool(
                    parse_iso(locked_lease["expires_at"])
                    and parse_iso(locked_lease["expires_at"]) <= current
                ),
                "process_checks": [first_process_check, second_process_check],
                "terminal_assessment": terminal,
                "release_scope": "account_mutex_only",
                "mutations": {
                    "execution_state": False,
                    "checkpoint": False,
                    "cursor": False,
                    "seen": False,
                    "staging": False,
                    "sqlite_content": False,
                    "account_health": False,
                },
            }
            record_event(
                conn,
                account_id=value,
                run_id=run_id,
                event_type="orphan_lease_reconciled",
                details=audit,
            )
            conn.commit()
            return {
                "account_id": value,
                "run_id": run_id,
                "lease_acquired_at": locked_lease["acquired_at"],
                **audit,
            }
        except Exception:
            conn.rollback()
            raise
    finally:
        lock.release()


class LeaseGuard:
    """Own one exact SQLite lease, local flock, signals, and runtime processes."""

    def __init__(
        self,
        *,
        db_path: Path,
        account_id: str,
        run_id: str,
        lease_kind: str,
        execution_state_path: Path,
        budget: LeaseBudget,
        inspector: SystemProcessInspector | None = None,
    ):
        self.db_path = db_path.expanduser().resolve()
        self.account_id = validate_account_id(account_id)
        self.run_id = run_id
        self.lease_kind = lease_kind
        self.execution_state_path = execution_state_path.expanduser().resolve()
        self.budget = budget
        self.inspector = inspector or SystemProcessInspector()
        self.file_lock = AccountLeaseFileLock(account_paths(self.account_id)["lease_lock"])
        self.account: dict[str, Any] | None = None
        self.lease_id = ""
        self.owner_token = ""
        self.owner: ProcessIdentity | None = None
        self.outcome = "failed"
        self.signal_received: int | None = None
        self._previous_handlers: dict[int, Any] = {}
        self._closed = False

    def acquire(self) -> dict[str, Any]:
        if self.account is not None:
            raise RuntimeError("XHS LeaseGuard is already acquired")
        self.file_lock.acquire()
        try:
            self.owner = self.inspector.current_identity()
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                ensure_xhs_schema(conn)
                result = acquire_exact_account_lease(
                    conn,
                    run_id=self.run_id,
                    lease_kind=self.lease_kind,
                    requested_account_id=self.account_id,
                    execution_state_path=self.execution_state_path,
                    budget=self.budget,
                    owner=self.owner,
                )
            self.account = result
            self.lease_id = str(result["lease_id"])
            self.owner_token = str(result["owner_token"])
            self._install_signal_handlers()
            return result
        except BaseException:
            self.file_lock.release()
            raise

    def __enter__(self) -> LeaseGuard:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: FrameType | None,
    ) -> bool:
        released = self.close()
        if not released and exc is None:
            raise XhsLeaseProcessesAlive(
                "XHS lease retained because guarded runtime processes are still live"
            )
        return False

    def set_outcome(self, outcome: str) -> None:
        self.outcome = outcome

    def child_environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        if not self.lease_id or not self.owner_token:
            raise RuntimeError("XHS LeaseGuard is not acquired")
        result = dict(base or os.environ)
        result[LEASE_DB_ENV] = str(self.db_path)
        result[LEASE_ID_ENV] = self.lease_id
        result[LEASE_OWNER_TOKEN_ENV] = self.owner_token
        return result

    def _install_signal_handlers(self) -> None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._signal_handler)
            except (ValueError, OSError):
                continue

    def _restore_signal_handlers(self) -> None:
        for signum, handler in self._previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        self._previous_handlers.clear()

    def _signal_handler(self, signum: int, _frame: FrameType | None) -> None:
        self.signal_received = int(signum)
        raise XhsLeaseSignal(signum)

    def register_process(self, pid: int, role: str) -> ProcessIdentity:
        identity = self.inspector.identity(int(pid))
        if identity is None:
            raise RuntimeError(f"cannot capture {role} process identity for pid {pid}")
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_xhs_schema(conn)
            register_lease_process(
                conn,
                lease_id=self.lease_id,
                owner_token=self.owner_token,
                process_role=role,
                identity=identity,
            )
        return identity

    def mark_process_exited(self, identity: ProcessIdentity, role: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_xhs_schema(conn)
            mark_lease_process_exited(
                conn,
                lease_id=self.lease_id,
                owner_token=self.owner_token,
                process_role=role,
                identity=identity,
            )

    def observe_profile_processes(self) -> list[ProcessIdentity]:
        if not self.account:
            raise RuntimeError("XHS LeaseGuard is not acquired")
        observed: list[ProcessIdentity] = []
        for snapshot in self.inspector.profile_processes(Path(str(self.account["profile_dir"]))):
            self.register_process(snapshot.identity.pid, "browser")
            observed.append(snapshot.identity)
        return observed

    def run_subprocess(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: int,
    ) -> LeaseSubprocessResult:
        child_env = self.child_environment(env)
        proc = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        identity = self.register_process(proc.pid, "child")
        timed_out = False
        stdout = ""
        stderr = ""
        returncode = 1
        try:
            try:
                stdout, stderr = proc.communicate(timeout=int(timeout_seconds))
                returncode = int(proc.returncode or 0)
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                stdout = str(exc.stdout or "")
                stderr = str(exc.stderr or "")
                self._signal_registered_group(identity, signal.SIGTERM)
                try:
                    extra_stdout, extra_stderr = proc.communicate(
                        timeout=max(1, self.budget.child_shutdown_seconds // 2)
                    )
                except subprocess.TimeoutExpired:
                    self._signal_registered_group(identity, signal.SIGKILL)
                    extra_stdout, extra_stderr = proc.communicate(
                        timeout=max(1, self.budget.child_shutdown_seconds // 2)
                    )
                stdout += str(extra_stdout or "")
                stderr += str(extra_stderr or "")
                returncode = 124
        finally:
            if proc.poll() is not None:
                try:
                    self.mark_process_exited(identity, "child")
                except XhsLeaseOwnershipError:
                    pass
        return LeaseSubprocessResult(
            args=list(command),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
        )

    def _signal_registered_group(self, identity: ProcessIdentity, signum: int) -> None:
        if identity.pgid == (self.owner.pgid if self.owner else None):
            os.kill(identity.pid, signum)
            return
        try:
            os.killpg(identity.pgid, signum)
        except ProcessLookupError:
            pass

    def _current_lease(self, conn: sqlite3.Connection) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM xhs_account_leases WHERE lease_id=? AND owner_token=?",
            (self.lease_id, self.owner_token),
        ).fetchone()
        if row is None:
            raise XhsLeaseOwnershipError("XHS LeaseGuard no longer owns its exact lease")
        return dict(row)

    def _runtime_assessment(self) -> dict[str, Any]:
        if not self.account:
            return {"safe_to_release": True, "checks": [], "blocking": []}
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_xhs_schema(conn)
            lease = self._current_lease(conn)
            return assess_lease_runtime(
                conn,
                lease=lease,
                profile_dir=Path(str(self.account["profile_dir"])),
                inspector=self.inspector,
                include_owner=False,
            )

    def terminate_owned_processes(self) -> dict[str, Any]:
        if not self.account:
            return {"safe_to_release": True, "checks": [], "blocking": []}
        initial = self._runtime_assessment()
        if initial["safe_to_release"]:
            return initial
        term_targets: set[tuple[str, int]] = set()
        for item in initial["blocking"]:
            observed = item.get("observed") if isinstance(item, dict) else None
            if item.get("role") in {"child", "exporter", "child_process_group", "exporter_process_group"}:
                pgid = int(item.get("pgid") or (observed or {}).get("pgid") or 0)
                if pgid and (self.owner is None or pgid != self.owner.pgid):
                    term_targets.add(("pgid", pgid))
            elif observed and int(observed.get("pid") or 0) != os.getpid():
                term_targets.add(("pid", int(observed["pid"])))
            for member in item.get("members") or []:
                pgid = int(member.get("pgid") or 0)
                if pgid and (self.owner is None or pgid != self.owner.pgid):
                    term_targets.add(("pgid", pgid))

        def send(signum: int) -> None:
            for target_type, value in sorted(term_targets):
                try:
                    if target_type == "pgid":
                        os.killpg(value, signum)
                    else:
                        os.kill(value, signum)
                except ProcessLookupError:
                    continue

        half_budget = max(1, self.budget.child_shutdown_seconds // 2)
        send(signal.SIGTERM)
        deadline = time.monotonic() + half_budget
        assessment = self._runtime_assessment()
        while not assessment["safe_to_release"] and time.monotonic() < deadline:
            time.sleep(0.1)
            assessment = self._runtime_assessment()
        if assessment["safe_to_release"]:
            return assessment
        send(signal.SIGKILL)
        deadline = time.monotonic() + max(1, self.budget.child_shutdown_seconds - half_budget)
        while not assessment["safe_to_release"] and time.monotonic() < deadline:
            time.sleep(0.1)
            assessment = self._runtime_assessment()
        return assessment

    def close(self) -> bool:
        if self._closed:
            return True
        self._restore_signal_handlers()
        if self.account is None:
            self.file_lock.release()
            self._closed = True
            return True
        try:
            process_check = self.terminate_owned_processes()
            if not process_check["safe_to_release"]:
                with sqlite3.connect(self.db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    ensure_xhs_schema(conn)
                    record_event(
                        conn,
                        account_id=self.account_id,
                        run_id=self.run_id,
                        event_type="lease_release_deferred_live_processes",
                        details={
                            "lease_id": self.lease_id,
                            "signal": self.signal_received,
                            "process_check": process_check,
                        },
                    )
                    conn.commit()
                return False
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                ensure_xhs_schema(conn)
                release_exact_account_lease(
                    conn,
                    account_id=self.account_id,
                    run_id=self.run_id,
                    lease_id=self.lease_id,
                    owner_token=self.owner_token,
                    outcome=self.outcome,
                    details={
                        "signal": self.signal_received,
                        "process_check": process_check,
                    },
                )
            self._closed = True
            return True
        finally:
            self.file_lock.release()
