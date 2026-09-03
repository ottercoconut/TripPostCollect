"""TripPostCollect tests for xhs leases."""

from __future__ import annotations

import argparse
from importlib import import_module
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from trippostcollect.db import bootstrap as db_bootstrap
from trippostcollect.db.bootstrap import XhsLeaseCutoverBlocked, bootstrap_database
from trippostcollect.xhs import accounts, leases as xhs_leases, runtime
from trippostcollect.xhs.accounts import XhsAccountUnavailable
from trippostcollect.xhs.leases import (
    LeaseBudget,
    LeaseGuard,
    ProcessIdentity,
    ProcessSnapshot,
    SystemProcessInspector,
    XhsLeaseOwnershipError,
    XhsLeaseSignal,
    XhsOrphanLeaseRecoveryRefused,
    acquire_exact_account_lease,
    crawl_lease_budget,
    public_lease,
    recover_orphaned_account_lease,
    register_lease_process,
    release_exact_account_lease,
    system_boot_id,
    system_host_id,
)
from trippostcollect.xhs.runtime import prepare_runtime_session, runtime_session_paths


UTC = timezone.utc
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
xhs_accounts_cli = import_module("xhs_accounts")
xhs_runner_cli = import_module("xhs_runner")
OWNER = ProcessIdentity(
    host_id="host-a",
    boot_id="boot-a",
    pid=111,
    process_started_at="2026-08-30T00:00:00+00:00",
    process_start_token="owner-start-111",
    pgid=111,
)


class FakeInspector:
    def __init__(
        self,
        *,
        host_id: str = "host-a",
        boot_id: str = "boot-a",
        identities: dict[int, ProcessIdentity] | None = None,
        presences: dict[int, bool | None] | None = None,
        groups: dict[int, list[ProcessSnapshot]] | None = None,
        profile_processes: list[ProcessSnapshot] | None = None,
        current: ProcessIdentity | None = None,
    ):
        self.host_id = host_id
        self.boot_id = boot_id
        self.identities = identities or {}
        self.presences = presences or {}
        self.groups = groups or {}
        self.profiles = profile_processes or []
        self.current = current

    def identity(self, pid: int) -> ProcessIdentity | None:
        return self.identities.get(int(pid))

    def process_presence(self, pid: int) -> bool | None:
        if int(pid) in self.presences:
            return self.presences[int(pid)]
        return int(pid) in self.identities

    def group_members(self, pgid: int) -> list[ProcessSnapshot]:
        return list(self.groups.get(int(pgid), []))

    def profile_processes(self, _profile_dir: Path) -> list[ProcessSnapshot]:
        return list(self.profiles)

    def current_identity(self) -> ProcessIdentity:
        if self.current is None:
            raise RuntimeError("fake current process identity was not configured")
        return self.current


class NoPresenceInspector:
    host_id = "host-a"
    boot_id = "boot-a"

    def identity(self, _pid: int) -> None:
        return None

    def group_members(self, _pgid: int) -> list[ProcessSnapshot]:
        return []

    def profile_processes(self, _profile_dir: Path) -> list[ProcessSnapshot]:
        return []


@pytest.fixture
def control_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(accounts, "XHS_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(
        accounts,
        "XHS_LEGACY_ACCOUNT_ROOT",
        tmp_path / "legacy-accounts",
    )
    monkeypatch.setattr(runtime, "XHS_SESSION_ROOT", tmp_path / "sessions")
    db_path = tmp_path / "control.sqlite"
    bootstrap_database(db_path, sync_jobs=False)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        accounts.ensure_xhs_schema(conn)
        accounts.register_account_slot(conn, "xhs-a01")
    return db_path


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_v21_xhs_database(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE schema_migrations(
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO schema_migrations(version, name)
            VALUES (21, 'xhs_exact_lease_identity');

            CREATE TABLE xhs_accounts(
                account_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                profile_dir TEXT NOT NULL UNIQUE,
                encrypted_state_path TEXT NOT NULL UNIQUE,
                identity_hash TEXT UNIQUE,
                last_verified_at TEXT,
                last_used_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (status IN (
                    'login_pending', 'active', 'login_required',
                    'quarantined', 'retired'
                ))
            );
            CREATE INDEX idx_xhs_accounts_eligible
            ON xhs_accounts(status, account_id);

            CREATE TABLE xhs_account_events(
                id INTEGER PRIMARY KEY,
                account_id TEXT REFERENCES xhs_accounts(account_id) ON DELETE SET NULL,
                run_id TEXT,
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE xhs_account_leases(
                account_id TEXT PRIMARY KEY REFERENCES xhs_accounts(account_id) ON DELETE CASCADE,
                lease_id TEXT NOT NULL UNIQUE,
                owner_token TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL UNIQUE,
                lease_kind TEXT NOT NULL,
                owner_host_id TEXT NOT NULL,
                owner_boot_id TEXT NOT NULL,
                owner_pid INTEGER NOT NULL,
                owner_process_started_at TEXT NOT NULL,
                owner_process_start_token TEXT NOT NULL,
                owner_pgid INTEGER NOT NULL,
                execution_state_path TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                lease_duration_seconds INTEGER NOT NULL,
                child_shutdown_budget_seconds INTEGER NOT NULL,
                root_finalize_budget_seconds INTEGER NOT NULL,
                identity_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE xhs_lease_processes(
                lease_id TEXT NOT NULL REFERENCES xhs_account_leases(lease_id) ON DELETE CASCADE,
                process_role TEXT NOT NULL,
                host_id TEXT NOT NULL,
                boot_id TEXT NOT NULL,
                pid INTEGER NOT NULL,
                process_started_at TEXT NOT NULL,
                process_start_token TEXT NOT NULL,
                pgid INTEGER NOT NULL,
                registered_at TEXT NOT NULL,
                exited_at TEXT,
                PRIMARY KEY (lease_id, process_role, pid, process_start_token)
            );

            CREATE TABLE xhs_runs(
                run_id TEXT PRIMARY KEY,
                target_key TEXT NOT NULL,
                account_id TEXT REFERENCES xhs_accounts(account_id) ON DELETE SET NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                execution_state_path TEXT NOT NULL,
                child_summary_path TEXT,
                report_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE xhs_discovery_checkpoints(
                id INTEGER PRIMARY KEY,
                target_key TEXT NOT NULL,
                account_id TEXT NOT NULL REFERENCES xhs_accounts(account_id) ON DELETE CASCADE,
                keyword TEXT NOT NULL,
                query_fingerprint TEXT NOT NULL,
                resume_page INTEGER NOT NULL DEFAULT 1,
                resume_search_id TEXT,
                source_has_more INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                last_batch_complete INTEGER NOT NULL DEFAULT 1,
                last_stop_reason TEXT NOT NULL DEFAULT '',
                last_run_id TEXT,
                last_summary_path TEXT,
                campaign_candidate_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(target_key, account_id, query_fingerprint)
            );
            CREATE TABLE xhs_discovery_seen_candidates(
                target_key TEXT NOT NULL,
                account_id TEXT NOT NULL REFERENCES xhs_accounts(account_id) ON DELETE CASCADE,
                query_fingerprint TEXT NOT NULL,
                platform_post_id TEXT NOT NULL,
                first_run_id TEXT NOT NULL,
                last_run_id TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (
                    target_key, account_id, query_fingerprint, platform_post_id
                )
            );
            CREATE TABLE xhs_platform_state(
                site_key TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                daily_runs INTEGER NOT NULL
            );

            INSERT INTO xhs_accounts VALUES
                ('xhs-a01', 'login_required', '/profile-a', '/state-a', 'identity-a',
                 NULL, '2026-08-30T00:00:00+00:00', '2026-08-01', '2026-08-30'),
                ('xhs-a02', 'quarantined', '/profile-b', '/state-b', 'identity-b',
                 NULL, NULL, '2026-08-02', '2026-08-29'),
                ('xhs-a03', 'retired', '/profile-c', '/state-c', 'identity-c',
                 NULL, NULL, '2026-08-03', '2026-08-28');
            INSERT INTO xhs_account_events VALUES(
                41, 'xhs-a01', 'legacy-run', 'login_persisted',
                '{"identity_hash":"identity-a"}', '2026-08-30T00:00:00+00:00'
            );
            INSERT INTO xhs_account_leases VALUES(
                'xhs-a01', 'legacy-lease', 'legacy-owner', 'legacy-run', 'crawl',
                'host-a', 'boot-a', 111, '2026-08-30T00:00:00+00:00',
                'start-111', 111, '/execution.json',
                '2026-08-30T00:00:00+00:00', '2026-08-30T00:01:00+00:00',
                '2026-08-30T01:00:00+00:00', 3600, 30, 270, 1
            );
            INSERT INTO xhs_lease_processes VALUES(
                'legacy-lease', 'child', 'host-a', 'boot-a', 222,
                '2026-08-30T00:02:00+00:00', 'start-222', 222,
                '2026-08-30T00:02:00+00:00', NULL
            );
            INSERT INTO xhs_runs VALUES(
                'legacy-run', 'target', 'xhs-a01', 'running',
                '2026-08-30T00:00:00+00:00', NULL, '/execution.json', NULL, '{}'
            );
            INSERT INTO xhs_discovery_checkpoints VALUES(
                7, 'target', 'xhs-a01', '青岛旅游', 'fingerprint', 9, 'cursor-9',
                1, 'active', 1, 'safe_boundary', 'safe-run', '/summary.json', 23,
                '2026-08-01T00:00:00+00:00', '2026-08-30T00:00:00+00:00'
            );
            INSERT INTO xhs_discovery_seen_candidates VALUES(
                'target', 'xhs-a01', 'fingerprint', 'post-9', 'safe-run',
                'safe-run', '2026-08-01T00:00:00+00:00',
                '2026-08-30T00:00:00+00:00'
            );
            INSERT INTO xhs_platform_state VALUES('xhs', 'active', 3);
            """
        )
        for index, account_id in enumerate(("xhs-a01", "xhs-a02", "xhs-a03"), 1):
            profile_dir = db_path.parent / f"legacy-account-{index}" / "profile"
            conn.execute(
                """
                UPDATE xhs_accounts
                SET profile_dir=?, encrypted_state_path=?
                WHERE account_id=?
                """,
                (
                    str(profile_dir),
                    str(profile_dir.parent / "storage_state.enc"),
                    account_id,
                ),
            )


def dead_v21_inspector() -> FakeInspector:
    return FakeInspector(presences={111: False, 222: False})


def configure_test_lock_roots(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> None:
    monkeypatch.setattr(accounts, "XHS_LOCK_ROOT", root / "locks")
    monkeypatch.setattr(
        accounts,
        "XHS_LEGACY_ACCOUNT_ROOT",
        root / "legacy-accounts",
    )


class FaultingConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        fail_after_sql: str | None = None,
        fail_final_commit: bool = False,
        before_fault: Any | None = None,
    ):
        self.connection = connection
        self.fail_after_sql = fail_after_sql
        self.fail_final_commit = fail_final_commit
        self.before_fault = before_fault
        self.triggered = False
        self.commit_armed = False

    def execute(
        self,
        sql: str,
        parameters: Any = (),
    ) -> sqlite3.Cursor:
        cursor = self.connection.execute(sql, parameters)
        normalized = " ".join(sql.split())
        if (
            self.fail_final_commit
            and normalized.startswith("INSERT OR IGNORE INTO schema_migrations")
            and tuple(parameters) == (22, "xhs_run_scoped_login_state")
        ):
            self.commit_armed = True
        if (
            not self.triggered
            and self.fail_after_sql
            and self.fail_after_sql in normalized
        ):
            self.triggered = True
            if callable(self.before_fault):
                self.before_fault()
            raise sqlite3.OperationalError("database or disk is full")
        return cursor

    def commit(self) -> None:
        if self.commit_armed:
            self.commit_armed = False
            self.triggered = True
            if callable(self.before_fault):
                self.before_fault()
            raise sqlite3.OperationalError("database or disk is full")
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)


LEGACY_ACCOUNT_COLUMNS = {
    "account_id",
    "status",
    "profile_dir",
    "encrypted_state_path",
    "identity_hash",
    "last_verified_at",
    "last_used_at",
    "created_at",
    "updated_at",
}


def xhs_database_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    schema = [
        tuple(row)
        for row in conn.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name='schema_migrations' OR name LIKE 'xhs_%'
            ORDER BY type, name
            """
        ).fetchall()
    ]
    tables = [
        row[1]
        for row in schema
        if row[0] == "table"
    ]
    return {
        "schema": schema,
        "rows": {
            table_name: [
                tuple(row)
                for row in conn.execute(
                    f'SELECT * FROM "{table_name}" ORDER BY rowid'
                ).fetchall()
            ]
            for table_name in tables
        },
    }


def assert_v21_xhs_database_restored(conn: sqlite3.Connection) -> None:
    assert {row[1] for row in conn.execute("PRAGMA table_info(xhs_accounts)")} == (
        LEGACY_ACCOUNT_COLUMNS
    )
    assert conn.execute(
        "SELECT account_id, status FROM xhs_accounts ORDER BY account_id"
    ).fetchall() == [
        ("xhs-a01", "login_required"),
        ("xhs-a02", "quarantined"),
        ("xhs-a03", "retired"),
    ]
    assert conn.execute(
        "SELECT lease_id, owner_token, run_id FROM xhs_account_leases"
    ).fetchone() == ("legacy-lease", "legacy-owner", "legacy-run")
    assert conn.execute(
        "SELECT lease_id, process_role, pid FROM xhs_lease_processes"
    ).fetchone() == ("legacy-lease", "child", 222)
    assert conn.execute(
        "SELECT id, account_id, run_id, event_type FROM xhs_account_events"
    ).fetchone() == (41, "xhs-a01", "legacy-run", "login_persisted")
    assert conn.execute(
        "SELECT run_id, status, account_id FROM xhs_runs"
    ).fetchone() == ("legacy-run", "running", "xhs-a01")
    assert conn.execute(
        "SELECT id, resume_page, resume_search_id FROM xhs_discovery_checkpoints"
    ).fetchone() == (7, 9, "cursor-9")
    assert conn.execute(
        "SELECT platform_post_id FROM xhs_discovery_seen_candidates"
    ).fetchone() == ("post-9",)
    assert conn.execute("SELECT * FROM xhs_platform_state").fetchone() == (
        "xhs",
        "active",
        3,
    )
    assert conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version=22"
    ).fetchone() is None
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def assert_v22_xhs_history_preserved(conn: sqlite3.Connection) -> None:
    assert {row[1] for row in conn.execute("PRAGMA table_info(xhs_accounts)")} == {
        "account_id",
        "status",
        "last_used_at",
        "created_at",
        "updated_at",
    }
    assert conn.execute("SELECT * FROM xhs_accounts ORDER BY account_id").fetchall() == [
        (
            "xhs-a01",
            "active",
            "2026-08-30T00:00:00+00:00",
            "2026-08-01",
            "2026-08-30",
        ),
        ("xhs-a02", "quarantined", None, "2026-08-02", "2026-08-29"),
        ("xhs-a03", "retired", None, "2026-08-03", "2026-08-28"),
    ]
    assert conn.execute(
        """
        SELECT id, account_id, run_id, event_type, details_json, created_at
        FROM xhs_account_events
        WHERE id=41
        """
    ).fetchone() == (
        41,
        "xhs-a01",
        "legacy-run",
        "login_persisted",
        '{"identity_hash":"identity-a"}',
        "2026-08-30T00:00:00+00:00",
    )
    assert conn.execute(
        """
        SELECT run_id, target_key, account_id, status, started_at,
               finished_at, execution_state_path, child_summary_path, report_json
        FROM xhs_runs
        """
    ).fetchone() == (
        "legacy-run",
        "target",
        "xhs-a01",
        "running",
        "2026-08-30T00:00:00+00:00",
        None,
        "/execution.json",
        None,
        "{}",
    )
    assert conn.execute(
        """
        SELECT id, target_key, account_id, keyword, query_fingerprint,
               resume_page, resume_search_id, source_has_more, status,
               last_batch_complete, last_stop_reason, last_run_id,
               last_summary_path, campaign_candidate_count, created_at, updated_at
        FROM xhs_discovery_checkpoints
        """
    ).fetchone() == (
        7,
        "target",
        "xhs-a01",
        "青岛旅游",
        "fingerprint",
        9,
        "cursor-9",
        1,
        "active",
        1,
        "safe_boundary",
        "safe-run",
        "/summary.json",
        23,
        "2026-08-01T00:00:00+00:00",
        "2026-08-30T00:00:00+00:00",
    )
    assert conn.execute(
        """
        SELECT target_key, account_id, query_fingerprint, platform_post_id,
               first_run_id, last_run_id, first_seen_at, last_seen_at
        FROM xhs_discovery_seen_candidates
        """
    ).fetchone() == (
        "target",
        "xhs-a01",
        "fingerprint",
        "post-9",
        "safe-run",
        "safe-run",
        "2026-08-01T00:00:00+00:00",
        "2026-08-30T00:00:00+00:00",
    )
    assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM xhs_lease_processes").fetchone()[0] == 0
    assert conn.execute(
        """
        SELECT account_id, run_id, event_type
        FROM xhs_account_events
        WHERE event_type='lease_schema_cutover_discarded'
        """
    ).fetchall() == [
        ("xhs-a01", "legacy-run", "lease_schema_cutover_discarded")
    ]
    assert conn.execute(
        "SELECT name FROM schema_migrations WHERE version=22"
    ).fetchone() == ("xhs_run_scoped_login_state",)
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='xhs_platform_state'"
    ).fetchone() is None
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def acquire_test_lease(
    db_path: Path,
    *,
    run_id: str,
    state_path: Path,
    owner: ProcessIdentity = OWNER,
    lease_id: str | None = None,
    owner_token: str | None = None,
    lease_kind: str = "crawl",
    runtime_profile_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    exact_lease_id = lease_id or f"lease-{run_id}"
    exact_owner_token = owner_token or f"owner-{run_id}"
    with connect(db_path) as conn:
        return acquire_exact_account_lease(
            conn,
            run_id=run_id,
            lease_kind=lease_kind,
            requested_account_id="xhs-a01",
            execution_state_path=state_path,
            runtime_profile_dir=(
                runtime_profile_dir or runtime_session_paths(run_id)["profile"]
            ),
            budget=LeaseBudget(runtime_seconds=60),
            owner=owner,
            now=now or datetime(2026, 8, 30, 0, 0, tzinfo=UTC),
            lease_id=exact_lease_id,
            owner_token=exact_owner_token,
        )


def install_legacy_exact_lease_schema(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    owner: ProcessIdentity = OWNER,
) -> None:
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        DROP TABLE IF EXISTS xhs_lease_processes;
        DROP TABLE IF EXISTS xhs_account_leases;
        CREATE TABLE xhs_account_leases(
            account_id TEXT PRIMARY KEY,
            lease_id TEXT NOT NULL UNIQUE,
            owner_token TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL UNIQUE,
            lease_kind TEXT NOT NULL,
            owner_host_id TEXT NOT NULL,
            owner_boot_id TEXT NOT NULL,
            owner_pid INTEGER NOT NULL,
            owner_process_started_at TEXT NOT NULL,
            owner_process_start_token TEXT NOT NULL,
            owner_pgid INTEGER NOT NULL,
            execution_state_path TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            lease_duration_seconds INTEGER NOT NULL,
            child_shutdown_budget_seconds INTEGER NOT NULL,
            root_finalize_budget_seconds INTEGER NOT NULL,
            identity_version INTEGER NOT NULL
        );
        CREATE TABLE xhs_lease_processes(
            lease_id TEXT NOT NULL,
            process_role TEXT NOT NULL,
            host_id TEXT NOT NULL,
            boot_id TEXT NOT NULL,
            pid INTEGER NOT NULL,
            process_started_at TEXT NOT NULL,
            process_start_token TEXT NOT NULL,
            pgid INTEGER NOT NULL,
            registered_at TEXT NOT NULL,
            exited_at TEXT
        );
        """
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
        ) VALUES (
            'xhs-a01', ?, 'legacy-owner-token', ?, 'crawl',
            ?, ?, ?, ?, ?, ?, '/legacy/execution.json',
            '2026-08-30T00:00:00+00:00', '2026-08-30T00:00:00+00:00',
            '2026-08-31T00:00:00+00:00', 86400, 30, 270, 1
        )
        """,
        (
            f"lease-{run_id}",
            run_id,
            owner.host_id,
            owner.boot_id,
            owner.pid,
            owner.process_started_at,
            owner.process_start_token,
            owner.pgid,
        ),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")


def start_legacy_lock_holder(path: Path) -> subprocess.Popen[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    script = """
import fcntl
import pathlib
import sys

handle = pathlib.Path(sys.argv[1]).open("a+b")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
print("ready", flush=True)
sys.stdin.readline()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    return process


def probe_file_lock(path: Path) -> str:
    script = """
import fcntl
import pathlib
import sys

handle = pathlib.Path(sys.argv[1]).open("a+b")
try:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("busy")
else:
    print("acquired")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_xhs_exact_lease_schema_and_dynamic_budgets(control_db: Path) -> None:
    with connect(control_db) as conn:
        lease_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(xhs_account_leases)")
        }
        process_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(xhs_lease_processes)")
        }
        assert {
            "lease_id",
            "owner_token",
            "owner_host_id",
            "owner_boot_id",
            "owner_pid",
            "owner_process_started_at",
            "owner_process_start_token",
            "owner_pgid",
            "execution_state_path",
            "runtime_profile_dir",
            "heartbeat_at",
        } <= lease_columns
        assert {"process_role", "process_start_token", "pgid", "exited_at"} <= process_columns
        assert conn.execute(
            "SELECT name FROM schema_migrations WHERE version=21"
        ).fetchone()[0] == "xhs_exact_lease_identity"

    crawl = crawl_lease_budget(timeout_seconds=7_200, configured_lease_seconds=29_100)
    assert crawl.public() == {
        "runtime_seconds": 7_200,
        "child_shutdown_seconds": 30,
        "root_finalize_seconds": 270,
        "lease_seconds": 7_500,
    }
    with pytest.raises(ValueError, match="does not cover"):
        crawl_lease_budget(timeout_seconds=7_200, configured_lease_seconds=7_499)


def test_system_process_identity_uses_platform_exact_start_token() -> None:
    identity = SystemProcessInspector().current_identity()
    assert identity.pid == os.getpid()
    assert identity.pgid == os.getpgid(os.getpid())
    if sys.platform == "darwin":
        assert identity.process_start_token.startswith("darwin:")
    elif sys.platform.startswith("linux"):
        assert identity.process_start_token.startswith("linux:")


def test_xhs_v22_cutover_is_atomic_and_preserves_control_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_test_lock_roots(monkeypatch, tmp_path)
    db_path = tmp_path / "legacy-v21.sqlite"
    create_v21_xhs_database(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        db_bootstrap.ensure_xhs_control_schema(
            conn,
            cutover_inspector=dead_v21_inspector(),
        )
        conn.commit()
        assert_v22_xhs_history_preserved(conn)

        db_bootstrap.ensure_xhs_control_schema(conn)
        conn.commit()
        assert_v22_xhs_history_preserved(conn)

    with sqlite3.connect(db_path) as conn:
        assert_v22_xhs_history_preserved(conn)


def test_xhs_v22_cutover_retries_when_legacy_account_set_expands_before_begin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_test_lock_roots(monkeypatch, tmp_path)
    db_path = tmp_path / "legacy-v21.sqlite"
    create_v21_xhs_database(db_path)

    class ExpandingInspector(FakeInspector):
        def __init__(self) -> None:
            super().__init__(presences={111: False, 222: False})
            self.inserted = False
            self.profile_scans = 0

        def profile_processes(self, profile_dir: Path) -> list[ProcessSnapshot]:
            self.profile_scans += 1
            if not self.inserted:
                self.inserted = True
                inserted_profile = tmp_path / "legacy-account-4" / "profile"
                with sqlite3.connect(db_path) as concurrent:
                    concurrent.execute(
                        """
                        INSERT INTO xhs_accounts(
                            account_id, status, profile_dir, encrypted_state_path,
                            identity_hash, last_verified_at, last_used_at,
                            created_at, updated_at
                        ) VALUES (?, 'active', ?, ?, NULL, NULL, NULL, ?, ?)
                        """,
                        (
                            "xhs-a04",
                            str(inserted_profile),
                            str(inserted_profile.parent / "storage_state.enc"),
                            "2026-09-03",
                            "2026-09-03",
                        ),
                    )
                    concurrent.commit()
            return super().profile_processes(profile_dir)

    inspector = ExpandingInspector()
    with sqlite3.connect(db_path) as conn:
        db_bootstrap.ensure_xhs_control_schema(
            conn,
            cutover_inspector=inspector,
        )
        conn.commit()

        assert inspector.profile_scans >= 7
        assert conn.execute(
            "SELECT status FROM xhs_accounts WHERE account_id='xhs-a04'"
        ).fetchone() == ("active",)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    ("fail_after_sql", "fail_final_commit"),
    [
        ("PRAGMA foreign_keys = OFF", False),
        ("BEGIN IMMEDIATE", False),
        ("CREATE TEMP TABLE xhs_accounts_runtime_backup", False),
        ("DROP TABLE IF EXISTS xhs_account_leases", False),
        ("DROP TABLE xhs_accounts", False),
        ("CREATE TABLE IF NOT EXISTS xhs_account_leases", False),
        ("INSERT INTO xhs_accounts(", False),
        ("DROP TABLE xhs_accounts_runtime_backup", False),
        ("DROP TABLE IF EXISTS xhs_platform_state", False),
        ("INSERT INTO xhs_account_events(", False),
        ("INSERT OR IGNORE INTO schema_migrations", False),
        ("PRAGMA foreign_key_check", False),
        (None, True),
    ],
    ids=[
        "foreign-keys-disabled",
        "transaction-started",
        "backup-created",
        "leases-dropped",
        "accounts-dropped",
        "new-schema-created",
        "accounts-restored",
        "backup-dropped",
        "obsolete-state-dropped",
        "cutover-event-recorded",
        "version-recorded",
        "foreign-key-check",
        "commit",
    ],
)
def test_xhs_v22_cutover_rolls_back_every_mutation_on_disk_full(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_after_sql: str | None,
    fail_final_commit: bool,
) -> None:
    configure_test_lock_roots(monkeypatch, tmp_path)
    db_path = tmp_path / "legacy-v21.sqlite"
    create_v21_xhs_database(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        before = xhs_database_snapshot(conn)
        faulting_conn = FaultingConnection(
            conn,
            fail_after_sql=fail_after_sql,
            fail_final_commit=fail_final_commit,
        )

        with pytest.raises(sqlite3.OperationalError, match="database or disk is full"):
            db_bootstrap.ensure_xhs_control_schema(
                faulting_conn,
                cutover_inspector=dead_v21_inspector(),
            )

        assert faulting_conn.triggered is True
        assert conn.in_transaction is False
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert xhs_database_snapshot(conn) == before
        assert_v21_xhs_database_restored(conn)
        for lock_path in (
            accounts.legacy_account_lock_path("xhs-a01"),
            accounts.account_lock_path("xhs-a01"),
            tmp_path / "legacy-account-1" / "lease.lock",
        ):
            assert probe_file_lock(lock_path) == "acquired"

    with sqlite3.connect(db_path) as conn:
        assert xhs_database_snapshot(conn) == before
        assert_v21_xhs_database_restored(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        db_bootstrap.ensure_xhs_control_schema(
            conn,
            cutover_inspector=dead_v21_inspector(),
        )
        conn.commit()
        assert_v22_xhs_history_preserved(conn)

    with sqlite3.connect(db_path) as conn:
        assert_v22_xhs_history_preserved(conn)


def test_xhs_v22_cutover_holds_every_legacy_lock_until_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_test_lock_roots(monkeypatch, tmp_path)
    db_path = tmp_path / "legacy-v21.sqlite"
    create_v21_xhs_database(db_path)
    lock_paths = (
        accounts.legacy_account_lock_path("xhs-a01"),
        accounts.account_lock_path("xhs-a01"),
        tmp_path / "legacy-account-1" / "lease.lock",
    )
    observed_while_failing: list[list[str]] = []

    def observe_locks() -> None:
        observed_while_failing.append(
            [probe_file_lock(lock_path) for lock_path in lock_paths]
        )

    with sqlite3.connect(db_path) as conn:
        before = xhs_database_snapshot(conn)
        faulting_conn = FaultingConnection(
            conn,
            fail_after_sql="PRAGMA foreign_key_check",
            before_fault=observe_locks,
        )

        with pytest.raises(sqlite3.OperationalError, match="database or disk is full"):
            db_bootstrap.ensure_xhs_control_schema(
                faulting_conn,
                cutover_inspector=dead_v21_inspector(),
            )

        assert observed_while_failing == [["busy", "busy", "busy"]]
        assert [probe_file_lock(path) for path in lock_paths] == [
            "acquired",
            "acquired",
            "acquired",
        ]
        assert xhs_database_snapshot(conn) == before


def test_xhs_v22_lease_only_cutover_rolls_back_schema_and_row(
    control_db: Path,
) -> None:
    with sqlite3.connect(control_db) as conn:
        install_legacy_exact_lease_schema(conn, run_id="obsolete-run")
        conn.execute("DELETE FROM schema_migrations WHERE version=22")
        conn.commit()
        before = xhs_database_snapshot(conn)

        faulting_conn = FaultingConnection(
            conn,
            fail_after_sql="CREATE TABLE IF NOT EXISTS xhs_account_leases",
        )
        with pytest.raises(sqlite3.OperationalError, match="database or disk is full"):
            db_bootstrap.ensure_xhs_control_schema(
                faulting_conn,
                cutover_inspector=dead_v21_inspector(),
            )

        assert faulting_conn.triggered is True
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert xhs_database_snapshot(conn) == before
        legacy_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(xhs_account_leases)")
        }
        assert "runtime_profile_dir" not in legacy_columns
        assert {
            "lease_id",
            "owner_process_start_token",
            "identity_version",
        } <= legacy_columns
        assert conn.execute(
            "SELECT account_id, run_id, lease_id FROM xhs_account_leases"
        ).fetchone() == ("xhs-a01", "obsolete-run", "lease-obsolete-run")
        assert conn.execute(
            "SELECT status FROM xhs_accounts WHERE account_id='xhs-a01'"
        ).fetchone() == ("active",)
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=22"
        ).fetchone() is None
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        db_bootstrap.ensure_xhs_control_schema(
            conn,
            cutover_inspector=dead_v21_inspector(),
        )
        conn.commit()
        assert "runtime_profile_dir" in {
            row[1] for row in conn.execute("PRAGMA table_info(xhs_account_leases)")
        }
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone() == (0,)
        assert conn.execute(
            "SELECT name FROM schema_migrations WHERE version=22"
        ).fetchone() == ("xhs_run_scoped_login_state",)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_xhs_v22_cutover_rejects_foreign_key_corruption_without_committing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_test_lock_roots(monkeypatch, tmp_path)
    db_path = tmp_path / "legacy-v21.sqlite"
    create_v21_xhs_database(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """
            INSERT INTO xhs_account_events(
                id, account_id, run_id, event_type, details_json, created_at
            ) VALUES (42, 'missing-account', 'bad-run', 'legacy-corruption', '{}', '2026-08-30')
            """
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        before = xhs_database_snapshot(conn)

        with pytest.raises(RuntimeError, match="foreign-key check failed"):
            db_bootstrap.ensure_xhs_control_schema(
                conn,
                cutover_inspector=dead_v21_inspector(),
            )

        assert conn.in_transaction is False
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert xhs_database_snapshot(conn) == before
        assert {row[1] for row in conn.execute("PRAGMA table_info(xhs_accounts)")} == (
            LEGACY_ACCOUNT_COLUMNS
        )
        assert conn.execute(
            "SELECT lease_id FROM xhs_account_leases"
        ).fetchone() == ("legacy-lease",)
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=22"
        ).fetchone() is None
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == [
            ("xhs_account_events", 42, "xhs_accounts", 0)
        ]


def test_xhs_schema_statements_do_not_implicitly_commit() -> None:
    with sqlite3.connect(":memory:") as conn:
        statements = db_bootstrap._sqlite_script_statements(
            db_bootstrap.XHS_CONTROL_SCHEMA.read_text(encoding="utf-8")
        )
        conn.execute("BEGIN IMMEDIATE")
        db_bootstrap._execute_sqlite_statements(conn, statements)

        assert conn.in_transaction is True
        conn.rollback()
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='xhs_accounts'"
        ).fetchone() is None


def test_process_enumeration_failure_is_not_treated_as_an_empty_process_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("ps unavailable")

    monkeypatch.setattr(xhs_leases.subprocess, "run", unavailable)
    inspector = SystemProcessInspector.__new__(SystemProcessInspector)

    with pytest.raises(RuntimeError, match="cannot enumerate processes"):
        inspector._ps_snapshots()


def test_identityless_legacy_lease_blocks_schema_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(accounts, "XHS_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(
        accounts,
        "XHS_LEGACY_ACCOUNT_ROOT",
        tmp_path / "legacy-accounts",
    )
    db_path = tmp_path / "legacy.sqlite"
    legacy_profile = tmp_path / "legacy-accounts" / "xhs-a01" / "profile"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE xhs_accounts(
                account_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                profile_dir TEXT NOT NULL UNIQUE,
                encrypted_state_path TEXT NOT NULL UNIQUE,
                identity_hash TEXT UNIQUE,
                last_verified_at TEXT,
                last_used_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE xhs_account_leases(
                account_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            INSERT INTO xhs_account_leases VALUES(
                'xhs-a01', 'legacy-run', '2026-08-30', '2026-08-31'
            );
            """
        )
        conn.execute(
            """
            INSERT INTO xhs_accounts VALUES(
                'xhs-a01', 'active', ?, ?, 'identity',
                NULL, NULL, '2026-08-30', '2026-08-30'
            )
            """,
            (
                str(legacy_profile),
                str(legacy_profile.parent / "storage_state.enc"),
            ),
        )
        with pytest.raises(
            XhsLeaseCutoverBlocked,
            match="legacy_owner_identity_unavailable",
        ):
            accounts.ensure_xhs_schema(conn, cutover_inspector=FakeInspector())

        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 1
        assert "runtime_profile_dir" not in {
            row[1] for row in conn.execute("PRAGMA table_info(xhs_account_leases)")
        }
        assert conn.execute(
            "SELECT status FROM xhs_accounts WHERE account_id='xhs-a01'"
        ).fetchone()[0] == "active"


def test_obsolete_lease_cutover_preserves_discovery_memory(
    control_db: Path,
) -> None:
    with connect(control_db) as conn:
        conn.execute(
            """
            INSERT INTO xhs_discovery_checkpoints(
                target_key, account_id, keyword, query_fingerprint,
                resume_page, resume_search_id, last_run_id
            ) VALUES ('target', 'xhs-a01', '青岛旅游', 'fingerprint', 9, 'cursor-9', 'safe-run')
            """
        )
        conn.execute(
            """
            INSERT INTO xhs_discovery_seen_candidates(
                target_key, account_id, query_fingerprint, platform_post_id,
                first_run_id, last_run_id
            ) VALUES ('target', 'xhs-a01', 'fingerprint', 'post-9', 'safe-run', 'safe-run')
            """
        )
        conn.commit()
        install_legacy_exact_lease_schema(conn, run_id="obsolete-run")
        conn.execute("DELETE FROM schema_migrations WHERE version=21")
        conn.commit()

        accounts.ensure_xhs_schema(
            conn,
            cutover_inspector=FakeInspector(presences={OWNER.pid: False}),
        )

        assert "owner_process_start_token" in {
            row[1] for row in conn.execute("PRAGMA table_info(xhs_account_leases)")
        }
        assert tuple(
            conn.execute(
                """
                SELECT resume_page, resume_search_id, last_run_id
                FROM xhs_discovery_checkpoints
                """
            ).fetchone()
        ) == (9, "cursor-9", "safe-run")
        assert conn.execute(
            "SELECT platform_post_id FROM xhs_discovery_seen_candidates"
        ).fetchone()[0] == "post-9"
        assert accounts.get_account(conn, "xhs-a01")["status"] == "active"
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 0
        assert conn.execute(
            """
            SELECT event_type FROM xhs_account_events
            WHERE run_id='obsolete-run'
            """
        ).fetchone()[0] == "lease_schema_cutover_discarded"


@pytest.mark.parametrize(
    ("inspector", "reason"),
    [
        (
            FakeInspector(identities={OWNER.pid: OWNER}),
            "exact_process_identity_alive",
        ),
        (
            FakeInspector(presences={OWNER.pid: True}),
            "exact_process_identity_unavailable",
        ),
        (
            NoPresenceInspector(),
            "exact_process_identity_unavailable",
        ),
        (
            FakeInspector(host_id="other-host", presences={OWNER.pid: False}),
            "different_host_unverifiable",
        ),
    ],
)
def test_legacy_cutover_refuses_live_or_unverifiable_owner(
    control_db: Path,
    inspector: Any,
    reason: str,
) -> None:
    with connect(control_db) as conn:
        install_legacy_exact_lease_schema(conn, run_id="blocked-cutover")

        with pytest.raises(XhsLeaseCutoverBlocked, match=reason):
            accounts.ensure_xhs_schema(conn, cutover_inspector=inspector)

        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 1
        assert "runtime_profile_dir" not in {
            row[1] for row in conn.execute("PRAGMA table_info(xhs_account_leases)")
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM xhs_account_events WHERE run_id='blocked-cutover'"
        ).fetchone()[0] == 0


def test_legacy_cutover_accepts_exact_pid_reuse_as_old_owner_dead(
    control_db: Path,
) -> None:
    reused = ProcessIdentity(
        host_id=OWNER.host_id,
        boot_id=OWNER.boot_id,
        pid=OWNER.pid,
        process_started_at="2026-09-03T00:00:00+00:00",
        process_start_token="reused-process-token",
        pgid=999,
    )
    with connect(control_db) as conn:
        install_legacy_exact_lease_schema(conn, run_id="pid-reused-cutover")

        accounts.ensure_xhs_schema(
            conn,
            cutover_inspector=FakeInspector(identities={OWNER.pid: reused}),
        )

        assert "runtime_profile_dir" in {
            row[1] for row in conn.execute("PRAGMA table_info(xhs_account_leases)")
        }
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 0
        assert conn.execute(
            """
            SELECT event_type FROM xhs_account_events
            WHERE run_id='pid-reused-cutover'
            """
        ).fetchone()[0] == "lease_schema_cutover_discarded"


def test_legacy_cutover_refuses_live_registered_child_after_owner_dies(
    control_db: Path,
) -> None:
    child = ProcessIdentity(
        host_id=OWNER.host_id,
        boot_id=OWNER.boot_id,
        pid=222,
        process_started_at="2026-08-30T00:01:00+00:00",
        process_start_token="legacy-child-222",
        pgid=222,
    )
    with connect(control_db) as conn:
        install_legacy_exact_lease_schema(conn, run_id="live-child-cutover")
        conn.execute(
            """
            INSERT INTO xhs_lease_processes(
                lease_id, process_role, host_id, boot_id, pid,
                process_started_at, process_start_token, pgid,
                registered_at, exited_at
            ) VALUES (
                'lease-live-child-cutover', 'child', ?, ?, ?, ?, ?, ?,
                '2026-08-30T00:01:00+00:00', NULL
            )
            """,
            (
                child.host_id,
                child.boot_id,
                child.pid,
                child.process_started_at,
                child.process_start_token,
                child.pgid,
            ),
        )
        conn.commit()

        with pytest.raises(
            XhsLeaseCutoverBlocked,
            match="exact_process_identity_alive",
        ):
            accounts.ensure_xhs_schema(
                conn,
                cutover_inspector=FakeInspector(
                    identities={child.pid: child},
                    presences={OWNER.pid: False},
                ),
            )

        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 1


def test_legacy_flock_blocks_schema_cutover_even_when_owner_is_dead(
    control_db: Path,
) -> None:
    legacy_lock = accounts.legacy_account_lock_path("xhs-a01")
    holder = start_legacy_lock_holder(legacy_lock)
    try:
        with connect(control_db) as conn:
            install_legacy_exact_lease_schema(conn, run_id="locked-cutover")
            with pytest.raises(
                XhsLeaseCutoverBlocked,
                match="xhs_legacy_cutover_lock_busy",
            ):
                accounts.ensure_xhs_schema(
                    conn,
                    cutover_inspector=FakeInspector(
                        presences={OWNER.pid: False}
                    ),
                )
            assert conn.execute(
                "SELECT COUNT(*) FROM xhs_account_leases"
            ).fetchone()[0] == 1
            assert "runtime_profile_dir" not in {
                row[1]
                for row in conn.execute("PRAGMA table_info(xhs_account_leases)")
            }
    finally:
        holder.communicate("\n", timeout=5)


def test_stored_legacy_account_lock_blocks_account_schema_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(accounts, "XHS_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(
        accounts,
        "XHS_LEGACY_ACCOUNT_ROOT",
        tmp_path / "legacy-accounts-root",
    )
    db_path = tmp_path / "legacy-account.sqlite"
    legacy_profile = tmp_path / "custom-legacy-account" / "profile"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE xhs_accounts(
                account_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                profile_dir TEXT NOT NULL,
                storage_state_path TEXT NOT NULL,
                platform_identity TEXT,
                cooldown_until TEXT,
                last_used_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO xhs_accounts VALUES(
                'xhs-a01', 'active', ?, ?, NULL,
                NULL, NULL, '2026-08-30', '2026-08-30'
            )
            """,
            (
                str(legacy_profile),
                str(legacy_profile.parent / "storage_state.enc"),
            ),
        )
        conn.commit()

    holder = start_legacy_lock_holder(legacy_profile.parent / "lease.lock")
    try:
        with sqlite3.connect(db_path) as conn:
            with pytest.raises(
                XhsLeaseCutoverBlocked,
                match="xhs_legacy_cutover_lock_busy",
            ):
                accounts.ensure_xhs_schema(
                    conn,
                    cutover_inspector=FakeInspector(),
                )
            assert "profile_dir" in {
                row[1] for row in conn.execute("PRAGMA table_info(xhs_accounts)")
            }
    finally:
        holder.communicate("\n", timeout=5)


def test_current_guard_and_legacy_runner_share_the_historical_flock(
    control_db: Path,
    tmp_path: Path,
) -> None:
    run_id = "dual-lock-guard"
    guard = LeaseGuard(
        db_path=control_db,
        account_id="xhs-a01",
        run_id=run_id,
        lease_kind="crawl",
        execution_state_path=tmp_path / "dual-lock.json",
        runtime_profile_dir=runtime_session_paths(run_id)["profile"],
        budget=LeaseBudget(runtime_seconds=10),
    )
    legacy_lock = accounts.legacy_account_lock_path("xhs-a01")
    holder = start_legacy_lock_holder(legacy_lock)
    try:
        with pytest.raises(XhsAccountUnavailable, match="local_lock_busy"):
            guard.acquire()
    finally:
        holder.communicate("\n", timeout=5)

    guard.acquire()
    assert probe_file_lock(legacy_lock) == "busy"
    assert guard.close() is True


def test_guard_migrates_legacy_schema_before_taking_its_own_dual_flock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_test_lock_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "XHS_SESSION_ROOT", tmp_path / "sessions")
    db_path = tmp_path / "legacy-v21.sqlite"
    create_v21_xhs_database(db_path)
    runner = ProcessIdentity(
        host_id="host-a",
        boot_id="boot-a",
        pid=333,
        process_started_at="2026-09-03T00:00:00+00:00",
        process_start_token="runner-start-333",
        pgid=333,
    )
    inspector = FakeInspector(
        identities={runner.pid: runner},
        presences={111: False, 222: False},
        current=runner,
    )
    run_id = "guard-cutover-no-self-deadlock"
    guard = LeaseGuard(
        db_path=db_path,
        account_id="xhs-a01",
        run_id=run_id,
        lease_kind="crawl",
        execution_state_path=tmp_path / "guard-cutover.json",
        runtime_profile_dir=runtime_session_paths(run_id)["profile"],
        budget=LeaseBudget(runtime_seconds=10),
        inspector=inspector,
    )

    acquired = guard.acquire()
    try:
        assert acquired["account_id"] == "xhs-a01"
        assert guard.lease_id
        assert probe_file_lock(accounts.legacy_account_lock_path("xhs-a01")) == "busy"
    finally:
        assert guard.close() is True
    with connect(db_path) as conn:
        assert "runtime_profile_dir" in {
            row[1] for row in conn.execute("PRAGMA table_info(xhs_account_leases)")
        }
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 0
        assert conn.execute(
            """
            SELECT COUNT(*) FROM xhs_account_events
            WHERE event_type='lease_schema_cutover_discarded' AND run_id='legacy-run'
            """
        ).fetchone()[0] == 1


def test_dual_lock_rolls_back_legacy_lock_when_current_lock_is_busy(
    control_db: Path,
) -> None:
    current_lock = accounts.account_lock_path("xhs-a01")
    current_holder = start_legacy_lock_holder(current_lock)
    dual_lock = xhs_leases.AccountLeaseFileLock(
        accounts.account_lock_paths("xhs-a01")
    )
    try:
        with pytest.raises(XhsAccountUnavailable, match="local_lock_busy"):
            dual_lock.acquire()
        assert probe_file_lock(accounts.legacy_account_lock_path("xhs-a01")) == "acquired"
    finally:
        current_holder.communicate("\n", timeout=5)


def test_current_guard_refuses_orphan_browser_using_legacy_profile(
    control_db: Path,
    tmp_path: Path,
) -> None:
    browser = ProcessSnapshot(
        identity=ProcessIdentity(
            host_id="host-a",
            boot_id="boot-a",
            pid=444,
            process_started_at="2026-09-03T00:00:00+00:00",
            process_start_token="legacy-browser-444",
            pgid=444,
        ),
        argv=(
            "Chromium",
            f"--user-data-dir={accounts.legacy_account_profile_path('xhs-a01')}",
        ),
    )
    run_id = "legacy-browser-blocked"
    guard = LeaseGuard(
        db_path=control_db,
        account_id="xhs-a01",
        run_id=run_id,
        lease_kind="crawl",
        execution_state_path=tmp_path / "legacy-browser.json",
        runtime_profile_dir=runtime_session_paths(run_id)["profile"],
        budget=LeaseBudget(runtime_seconds=10),
        inspector=FakeInspector(profile_processes=[browser]),
    )

    with pytest.raises(XhsAccountUnavailable, match="legacy_browser_alive"):
        guard.acquire()
    with connect(control_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 0


def test_xhs_control_bootstrap_does_not_create_content_or_scheduler_tables(
    tmp_path: Path,
) -> None:
    db_path = accounts.bootstrap_xhs_control_database(tmp_path / "control-only.sqlite")
    with connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "xhs_account_leases" in tables
    assert "xhs_lease_processes" in tables
    assert "web_posts" not in tables
    assert "crawl_jobs" not in tables


def test_normal_end_releases_exact_lease(control_db: Path, tmp_path: Path) -> None:
    guard = LeaseGuard(
        db_path=control_db,
        account_id="xhs-a01",
        run_id="normal-end",
        lease_kind="crawl",
        execution_state_path=tmp_path / "normal-state.json",
        runtime_profile_dir=runtime_session_paths("normal-end")["profile"],
        budget=LeaseBudget(runtime_seconds=10, child_shutdown_seconds=2, root_finalize_seconds=1),
    )
    with guard:
        paths = guard.prepare_runtime_session()
        (paths["profile"] / "Cookies").write_bytes(b"run-secret")
        result = guard.run_subprocess(
            [sys.executable, "-c", "print('ok')"],
            cwd=tmp_path,
            env={},
            timeout_seconds=5,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "ok"
        guard.set_outcome("completed")

    assert guard.runtime_session_removed is True
    assert not paths["root"].exists()
    with connect(control_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 0
        event = conn.execute(
            "SELECT event_type, details_json FROM xhs_account_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert event["event_type"] == "lease_released"
        details = json.loads(event["details_json"])
        assert details["outcome"] == "completed"
        assert details["runtime_session_removed"] is True


def test_guard_removes_runtime_session_before_releasing_database_lease(
    control_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "cleanup-before-release"
    guard = LeaseGuard(
        db_path=control_db,
        account_id="xhs-a01",
        run_id=run_id,
        lease_kind="crawl",
        execution_state_path=tmp_path / "cleanup-before-release.json",
        runtime_profile_dir=runtime_session_paths(run_id)["profile"],
        budget=LeaseBudget(runtime_seconds=10),
    )
    guard.acquire()
    paths = guard.prepare_runtime_session()
    (paths["profile"] / "Cookies").write_bytes(b"delete-before-release")
    original_release = xhs_leases.release_exact_account_lease
    observed: list[bool] = []

    def assert_cleanup_first(conn, **kwargs) -> None:
        observed.append(not paths["root"].exists())
        original_release(conn, **kwargs)

    monkeypatch.setattr(xhs_leases, "release_exact_account_lease", assert_cleanup_first)

    assert guard.close() is True
    assert observed == [True]


def test_ordinary_exception_releases_without_changing_health(
    control_db: Path,
    tmp_path: Path,
) -> None:
    guard = LeaseGuard(
        db_path=control_db,
        account_id="xhs-a01",
        run_id="ordinary-error",
        lease_kind="crawl",
        execution_state_path=tmp_path / "error-state.json",
        runtime_profile_dir=runtime_session_paths("ordinary-error")["profile"],
        budget=LeaseBudget(runtime_seconds=10, child_shutdown_seconds=2, root_finalize_seconds=1),
    )
    with pytest.raises(RuntimeError, match="ordinary failure"):
        with guard:
            raise RuntimeError("ordinary failure")

    with connect(control_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 0
        assert accounts.get_account(conn, "xhs-a01")["status"] == "active"


def test_guard_repeated_close_preserves_deferred_release_result(
    control_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = LeaseGuard(
        db_path=control_db,
        account_id="xhs-a01",
        run_id="deferred-close",
        lease_kind="crawl",
        execution_state_path=tmp_path / "deferred-close.json",
        runtime_profile_dir=runtime_session_paths("deferred-close")["profile"],
        budget=LeaseBudget(runtime_seconds=10, child_shutdown_seconds=2, root_finalize_seconds=1),
    )
    guard.acquire()
    paths = guard.prepare_runtime_session()
    (paths["profile"] / "Cookies").write_bytes(b"still-live")
    deferred = {
        "safe_to_release": False,
        "checks": [],
        "blocking": [{"role": "child", "reason": "test_live_child"}],
    }
    monkeypatch.setattr(guard, "terminate_owned_processes", lambda: deferred)
    assert guard.close() is False
    assert guard.close() is False
    assert paths["root"].exists()
    with connect(control_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 1
        event = conn.execute(
            "SELECT event_type, details_json FROM xhs_account_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert event["event_type"] == "lease_release_deferred_live_processes"
        assert json.loads(event["details_json"])["runtime_session_removed"] is False


def test_runtime_session_cleanup_failure_retains_exact_lease(
    control_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "cleanup-failure"
    guard = LeaseGuard(
        db_path=control_db,
        account_id="xhs-a01",
        run_id=run_id,
        lease_kind="crawl",
        execution_state_path=tmp_path / "cleanup-failure.json",
        runtime_profile_dir=runtime_session_paths(run_id)["profile"],
        budget=LeaseBudget(runtime_seconds=10),
    )
    guard.acquire()
    paths = guard.prepare_runtime_session()
    (paths["profile"] / "Cookies").write_bytes(b"retain-me")

    def fail_cleanup(*_args, **_kwargs) -> bool:
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(
        "trippostcollect.xhs.leases.remove_runtime_session_for_profile",
        fail_cleanup,
    )

    assert guard.close() is False
    assert guard.close() is False
    assert guard.runtime_session_removed is False
    assert paths["root"].exists()
    with connect(control_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 1
        event = conn.execute(
            "SELECT event_type, details_json FROM xhs_account_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert event["event_type"] == (
            "lease_release_deferred_runtime_session_cleanup"
        )
        details = json.loads(event["details_json"])
        assert details["runtime_session_removed"] is False
        assert details["cleanup_error"].startswith("PermissionError:")


def test_incomplete_runtime_session_removal_retains_exact_lease(
    control_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "cleanup-incomplete"
    guard = LeaseGuard(
        db_path=control_db,
        account_id="xhs-a01",
        run_id=run_id,
        lease_kind="repair",
        execution_state_path=tmp_path / "cleanup-incomplete.json",
        runtime_profile_dir=runtime_session_paths(run_id)["profile"],
        budget=LeaseBudget(runtime_seconds=10),
    )
    guard.acquire()
    paths = guard.prepare_runtime_session()
    monkeypatch.setattr(
        xhs_leases,
        "remove_runtime_session_for_profile",
        lambda *_args, **_kwargs: False,
    )

    assert guard.close() is False
    assert paths["root"].exists()
    with connect(control_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 1
        details = json.loads(
            conn.execute(
                "SELECT details_json FROM xhs_account_events ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert details["cleanup_error"] == "runtime_session_still_exists"


def test_guard_does_not_delete_session_it_failed_to_create(
    control_db: Path,
    tmp_path: Path,
) -> None:
    run_id = "preexisting-session"
    paths = prepare_runtime_session(run_id)
    marker = paths["profile"] / "Cookies"
    marker.write_bytes(b"not-owned-by-guard")
    guard = LeaseGuard(
        db_path=control_db,
        account_id="xhs-a01",
        run_id=run_id,
        lease_kind="crawl",
        execution_state_path=tmp_path / "preexisting-session.json",
        runtime_profile_dir=paths["profile"],
        budget=LeaseBudget(runtime_seconds=10),
    )
    guard.acquire()

    with pytest.raises(RuntimeError, match="already exists"):
        guard.prepare_runtime_session()
    assert guard.close() is True
    assert guard.runtime_session_removed is True
    assert marker.read_bytes() == b"not-owned-by-guard"


@pytest.mark.parametrize(
    "profile_value",
    [
        Path("relative/profile"),
        Path("/tmp/not-the-requested-run/profile"),
    ],
)
def test_lease_rejects_noncanonical_runtime_profile(
    control_db: Path,
    tmp_path: Path,
    profile_value: Path,
) -> None:
    with pytest.raises(ValueError, match="runtime profile"):
        LeaseGuard(
            db_path=control_db,
            account_id="xhs-a01",
            run_id="canonical-run",
            lease_kind="crawl",
            execution_state_path=tmp_path / "canonical-run.json",
            runtime_profile_dir=profile_value,
            budget=LeaseBudget(runtime_seconds=10),
        )


def test_lease_rejects_symlinked_runtime_session(
    control_db: Path,
    tmp_path: Path,
) -> None:
    target = tmp_path / "symlink-target"
    (target / "profile").mkdir(parents=True)
    sessions = runtime.XHS_SESSION_ROOT
    sessions.mkdir(parents=True)
    (sessions / "symlink-run").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        LeaseGuard(
            db_path=control_db,
            account_id="xhs-a01",
            run_id="symlink-run",
            lease_kind="crawl",
            execution_state_path=tmp_path / "symlink-run.json",
            runtime_profile_dir=runtime_session_paths("symlink-run")["profile"],
            budget=LeaseBudget(runtime_seconds=10),
        )


def test_child_registration_failure_stops_untracked_process_group(
    control_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = LeaseGuard(
        db_path=control_db,
        account_id="xhs-a01",
        run_id="registration-failure",
        lease_kind="crawl",
        execution_state_path=tmp_path / "registration-failure.json",
        runtime_profile_dir=runtime_session_paths("registration-failure")["profile"],
        budget=LeaseBudget(runtime_seconds=10, child_shutdown_seconds=2, root_finalize_seconds=1),
    )
    child_pid: list[int] = []

    def fail_registration(pid: int, _role: str) -> ProcessIdentity:
        child_pid.append(pid)
        raise RuntimeError("registry unavailable")

    with pytest.raises(RuntimeError, match="registry unavailable"):
        with guard:
            monkeypatch.setattr(guard, "register_process", fail_registration)
            guard.run_subprocess(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=tmp_path,
                env={},
                timeout_seconds=5,
            )
    assert child_pid
    assert SystemProcessInspector().process_presence(child_pid[0]) is False
    with connect(control_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 0


def test_expired_ttl_does_not_authorize_implicit_release(
    control_db: Path,
    tmp_path: Path,
) -> None:
    acquired_at = datetime(2026, 8, 30, 0, 0, tzinfo=UTC)
    acquire_test_lease(
        control_db,
        run_id="expired-owner",
        state_path=tmp_path / "expired-state.json",
        now=acquired_at,
    )
    with connect(control_db) as conn:
        with pytest.raises(XhsAccountUnavailable, match="busy"):
            acquire_exact_account_lease(
                conn,
                run_id="new-owner",
                lease_kind="crawl",
                requested_account_id="xhs-a01",
                execution_state_path=tmp_path / "new-state.json",
                runtime_profile_dir=runtime_session_paths("new-owner")["profile"],
                budget=LeaseBudget(runtime_seconds=60),
                owner=ProcessIdentity(
                    host_id="host-a",
                    boot_id="boot-a",
                    pid=222,
                    process_started_at="2026-08-30T01:00:00+00:00",
                    process_start_token="owner-start-222",
                    pgid=222,
                ),
                now=acquired_at + timedelta(hours=1),
            )
        assert conn.execute("SELECT run_id FROM xhs_account_leases").fetchone()[0] == "expired-owner"


def test_repair_and_crawl_share_one_exact_account_mutex(
    control_db: Path,
    tmp_path: Path,
) -> None:
    repair = acquire_test_lease(
        control_db,
        run_id="repair-owner",
        state_path=tmp_path / "repair-state.json",
        lease_kind="repair",
    )
    with connect(control_db) as conn:
        with pytest.raises(XhsAccountUnavailable, match="busy"):
            acquire_exact_account_lease(
                conn,
                run_id="crawl-contender",
                lease_kind="crawl",
                requested_account_id="xhs-a01",
                execution_state_path=tmp_path / "crawl-contender.json",
                runtime_profile_dir=runtime_session_paths("crawl-contender")["profile"],
                budget=LeaseBudget(runtime_seconds=60),
                owner=ProcessIdentity(
                    "host-a", "boot-a", 222, "2026-08-30T00:02:00+00:00", "owner-222", 222
                ),
            )
        release_exact_account_lease(
            conn,
            account_id="xhs-a01",
            run_id="repair-owner",
            lease_id=repair["lease_id"],
            owner_token=repair["owner_token"],
            outcome="completed",
        )

    crawl = acquire_test_lease(
        control_db,
        run_id="crawl-owner",
        state_path=tmp_path / "crawl-state.json",
        lease_kind="crawl",
    )
    with connect(control_db) as conn:
        with pytest.raises(XhsAccountUnavailable, match="busy"):
            acquire_exact_account_lease(
                conn,
                run_id="repair-contender",
                lease_kind="repair",
                requested_account_id="xhs-a01",
                execution_state_path=tmp_path / "repair-contender.json",
                runtime_profile_dir=runtime_session_paths("repair-contender")["profile"],
                budget=LeaseBudget(runtime_seconds=60),
                owner=ProcessIdentity(
                    "host-a", "boot-a", 333, "2026-08-30T00:03:00+00:00", "owner-333", 333
                ),
            )
        release_exact_account_lease(
            conn,
            account_id="xhs-a01",
            run_id="crawl-owner",
            lease_id=crawl["lease_id"],
            owner_token=crawl["owner_token"],
            outcome="completed",
        )


def test_exact_leases_only_block_the_same_account(
    control_db: Path,
    tmp_path: Path,
) -> None:
    acquire_test_lease(
        control_db,
        run_id="account-one",
        state_path=tmp_path / "account-one.json",
    )
    with connect(control_db) as conn:
        accounts.register_account_slot(conn, "xhs-a02")
        second = acquire_exact_account_lease(
            conn,
            run_id="account-two",
            lease_kind="crawl",
            requested_account_id="xhs-a02",
            execution_state_path=tmp_path / "account-two.json",
            runtime_profile_dir=runtime_session_paths("account-two")["profile"],
            budget=LeaseBudget(runtime_seconds=60),
            owner=ProcessIdentity(
                "host-a", "boot-a", 222, "2026-08-30T00:02:00+00:00", "owner-222", 222
            ),
        )
        assert second["account_id"] == "xhs-a02"
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 2


def test_dry_run_preflight_treats_expired_row_as_busy(
    control_db: Path,
    tmp_path: Path,
) -> None:
    acquire_test_lease(
        control_db,
        run_id="dry-run-owner",
        state_path=tmp_path / "dry-run-owner.json",
        now=datetime(2020, 1, 1, tzinfo=UTC),
    )
    with connect(control_db) as conn:
        with pytest.raises(XhsAccountUnavailable, match="busy"):
            xhs_runner_cli._eligible_account_for_plan(conn, "xhs-a01")


def test_preflight_rejects_missing_slot_without_implicit_database_writes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "missing-slot.sqlite"
    bootstrap_database(db_path, sync_jobs=False)

    with connect(db_path) as conn:
        before_changes = conn.total_changes
        with pytest.raises(
            XhsAccountUnavailable,
            match="requested_xhs_account_missing",
        ):
            xhs_runner_cli._eligible_account_for_plan(conn, "xhs-a01")

        assert conn.total_changes == before_changes
        assert conn.execute("SELECT COUNT(*) FROM xhs_accounts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_events").fetchone()[0] == 0


def test_preflight_typo_does_not_fork_checkpoint_namespace(
    control_db: Path,
) -> None:
    with connect(control_db) as conn:
        accounts.register_account_slot(conn, "xhs-a02")
        before_accounts = conn.execute(
            "SELECT account_id FROM xhs_accounts ORDER BY account_id"
        ).fetchall()

        with pytest.raises(
            XhsAccountUnavailable,
            match="requested_xhs_account_missing",
        ):
            xhs_runner_cli._eligible_account_for_plan(conn, "xhs-a03")

        assert conn.execute(
            "SELECT account_id FROM xhs_accounts ORDER BY account_id"
        ).fetchall() == before_accounts
        assert conn.execute(
            "SELECT COUNT(*) FROM xhs_account_events WHERE account_id='xhs-a03'"
        ).fetchone()[0] == 0


def test_wrong_owner_token_cannot_release(control_db: Path, tmp_path: Path) -> None:
    lease = acquire_test_lease(
        control_db,
        run_id="wrong-owner",
        state_path=tmp_path / "wrong-owner-state.json",
    )
    with connect(control_db) as conn:
        with pytest.raises(XhsLeaseOwnershipError, match="owner token"):
            release_exact_account_lease(
                conn,
                account_id="xhs-a01",
                run_id="wrong-owner",
                lease_id=lease["lease_id"],
                owner_token="not-the-owner",
                outcome="failed",
            )
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM xhs_account_events WHERE event_type='lease_released'"
        ).fetchone()[0] == 0


def test_reboot_allows_mutex_only_reconciliation_without_terminal_state(
    control_db: Path,
    tmp_path: Path,
) -> None:
    lease = acquire_test_lease(
        control_db,
        run_id="rebooted",
        state_path=tmp_path / "missing-after-reboot.json",
        owner=ProcessIdentity(
            host_id="host-a",
            boot_id="old-boot",
            pid=111,
            process_started_at=OWNER.process_started_at,
            process_start_token=OWNER.process_start_token,
            pgid=111,
        ),
    )
    with connect(control_db) as conn:
        result = recover_orphaned_account_lease(
            conn,
            account_id="xhs-a01",
            run_id="rebooted",
            lease_id=lease["lease_id"],
            inspector=FakeInspector(boot_id="new-boot"),
        )
        assert result["release_scope"] == "account_mutex_only"
        assert result["terminal_assessment"]["reason"] == "execution_state_missing"
        assert result["mutations"] == {
            "execution_state": False,
            "checkpoint": False,
            "cursor": False,
            "seen": False,
            "staging": False,
            "sqlite_content": False,
            "account_health": False,
        }
        assert accounts.get_account(conn, "xhs-a01")["status"] == "active"


def test_orphan_reconciliation_removes_run_scoped_profile(
    control_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trippostcollect.xhs.runtime.XHS_SESSION_ROOT",
        tmp_path / "sessions",
    )
    paths = prepare_runtime_session("orphan-session")
    lease = acquire_test_lease(
        control_db,
        run_id="orphan-session",
        state_path=tmp_path / "missing-state.json",
        runtime_profile_dir=paths["profile"],
    )

    with connect(control_db) as conn:
        result = recover_orphaned_account_lease(
            conn,
            account_id="xhs-a01",
            run_id="orphan-session",
            lease_id=lease["lease_id"],
            inspector=FakeInspector(),
        )

    assert result["runtime_session_removed"] is True
    assert not paths["root"].exists()


def test_orphan_reconciliation_rejects_profile_from_another_run(
    control_db: Path,
    tmp_path: Path,
) -> None:
    foreign = prepare_runtime_session("foreign-session")
    marker = foreign["profile"] / "Cookies"
    marker.write_bytes(b"foreign-session")
    lease = acquire_test_lease(
        control_db,
        run_id="claimed-session",
        state_path=tmp_path / "missing-state.json",
    )
    with connect(control_db) as conn:
        conn.execute(
            "UPDATE xhs_account_leases SET runtime_profile_dir=? WHERE lease_id=?",
            (str(foreign["profile"]), lease["lease_id"]),
        )
        conn.commit()
        with pytest.raises(
            XhsOrphanLeaseRecoveryRefused,
            match="exact run-scoped path",
        ):
            recover_orphaned_account_lease(
                conn,
                account_id="xhs-a01",
                run_id="claimed-session",
                lease_id=lease["lease_id"],
                inspector=FakeInspector(),
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM xhs_account_leases WHERE lease_id=?",
            (lease["lease_id"],),
        ).fetchone()[0] == 1
    assert marker.read_bytes() == b"foreign-session"


def test_orphan_reconciliation_removes_partial_session_without_profile(
    control_db: Path,
    tmp_path: Path,
) -> None:
    run_id = "partial-session"
    paths = runtime_session_paths(run_id)
    paths["root"].mkdir(parents=True)
    status_file = paths["root"] / "runtime_status.json"
    status_file.write_bytes(b"partial-runtime-material")
    lease = acquire_test_lease(
        control_db,
        run_id=run_id,
        state_path=tmp_path / "missing-state.json",
    )

    with connect(control_db) as conn:
        result = recover_orphaned_account_lease(
            conn,
            account_id="xhs-a01",
            run_id=run_id,
            lease_id=lease["lease_id"],
            inspector=FakeInspector(),
        )

    assert result["runtime_session_removed"] is True
    assert not paths["root"].exists()


def test_missing_adaptive_event_does_not_mutate_state_or_discovery(
    control_db: Path,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "incomplete-state.json"
    state = {
        "run_id": "no-stop-event",
        "status": "running",
        "plan": {"account_id": "xhs-a01"},
        "steps": {"task_finalized": {"status": "frozen"}},
        "events": [{"at": "2026-08-30T00:00:00+00:00", "type": "command_started"}],
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    state_before = state_path.read_bytes()
    staging_path = tmp_path / "staging" / "old.jsonl"
    staging_path.parent.mkdir(parents=True)
    staging_path.write_bytes(b"old-staging\n")
    lease = acquire_test_lease(
        control_db,
        run_id="no-stop-event",
        state_path=state_path,
    )
    with connect(control_db) as conn:
        conn.execute(
            """
            INSERT INTO xhs_discovery_checkpoints(
                target_key, account_id, keyword, query_fingerprint,
                resume_page, resume_search_id, last_run_id
            ) VALUES ('target', 'xhs-a01', '青岛旅游', 'fingerprint', 7, 'search-id', 'safe-run')
            """
        )
        conn.execute(
            """
            INSERT INTO xhs_discovery_seen_candidates(
                target_key, account_id, query_fingerprint, platform_post_id,
                first_run_id, last_run_id
            ) VALUES ('target', 'xhs-a01', 'fingerprint', 'post-1', 'safe-run', 'safe-run')
            """
        )
        conn.commit()
        before_checkpoint = tuple(
            conn.execute(
                "SELECT resume_page, resume_search_id, last_run_id FROM xhs_discovery_checkpoints"
            ).fetchone()
        )
        before_seen = conn.execute(
            "SELECT COUNT(*) FROM xhs_discovery_seen_candidates"
        ).fetchone()[0]
        result = recover_orphaned_account_lease(
            conn,
            account_id="xhs-a01",
            run_id="no-stop-event",
            lease_id=lease["lease_id"],
            inspector=FakeInspector(),
        )
        assert result["terminal_assessment"]["adaptive_search_stopped_present"] is False
        assert result["terminal_assessment"]["terminal_complete"] is False
        assert tuple(
            conn.execute(
                "SELECT resume_page, resume_search_id, last_run_id FROM xhs_discovery_checkpoints"
            ).fetchone()
        ) == before_checkpoint
        assert conn.execute(
            "SELECT COUNT(*) FROM xhs_discovery_seen_candidates"
        ).fetchone()[0] == before_seen
    assert state_path.read_bytes() == state_before
    assert staging_path.read_bytes() == b"old-staging\n"


def test_pid_reuse_is_not_mistaken_for_old_owner(control_db: Path, tmp_path: Path) -> None:
    lease = acquire_test_lease(
        control_db,
        run_id="pid-reuse",
        state_path=tmp_path / "pid-reuse-state.json",
    )
    reused = ProcessIdentity(
        host_id="host-a",
        boot_id="boot-a",
        pid=OWNER.pid,
        process_started_at="2026-08-30T02:00:00+00:00",
        process_start_token="new-process-same-pid",
        pgid=999,
    )
    with connect(control_db) as conn:
        result = recover_orphaned_account_lease(
            conn,
            account_id="xhs-a01",
            run_id="pid-reuse",
            lease_id=lease["lease_id"],
            inspector=FakeInspector(identities={OWNER.pid: reused}),
        )
    owner_check = result["process_checks"][0]["checks"][0]
    assert owner_check["reason"] == "pid_reused_or_identity_changed"
    assert owner_check["live"] is False


def test_live_pid_with_unavailable_exact_identity_blocks_reconciliation(
    control_db: Path,
    tmp_path: Path,
) -> None:
    lease = acquire_test_lease(
        control_db,
        run_id="identity-unavailable",
        state_path=tmp_path / "identity-unavailable.json",
    )
    with connect(control_db) as conn:
        with pytest.raises(XhsOrphanLeaseRecoveryRefused, match="still live"):
            recover_orphaned_account_lease(
                conn,
                account_id="xhs-a01",
                run_id="identity-unavailable",
                lease_id=lease["lease_id"],
                inspector=FakeInspector(presences={OWNER.pid: True}),
            )
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 1


def test_residual_registered_child_group_blocks_reconciliation(
    control_db: Path,
    tmp_path: Path,
) -> None:
    lease = acquire_test_lease(
        control_db,
        run_id="residual-child",
        state_path=tmp_path / "residual-child-state.json",
    )
    child = ProcessIdentity(
        host_id="host-a",
        boot_id="boot-a",
        pid=222,
        process_started_at="2026-08-30T00:01:00+00:00",
        process_start_token="child-222",
        pgid=222,
    )
    descendant = ProcessSnapshot(
        identity=ProcessIdentity(
            host_id="host-a",
            boot_id="boot-a",
            pid=223,
            process_started_at="2026-08-30T00:01:01+00:00",
            process_start_token="child-descendant-223",
            pgid=222,
        ),
        argv=("python", "worker"),
    )
    with connect(control_db) as conn:
        register_lease_process(
            conn,
            lease_id=lease["lease_id"],
            owner_token=lease["owner_token"],
            process_role="child",
            identity=child,
        )
        inspector = FakeInspector(groups={222: [descendant]})
        with pytest.raises(XhsOrphanLeaseRecoveryRefused, match="still live"):
            recover_orphaned_account_lease(
                conn,
                account_id="xhs-a01",
                run_id="residual-child",
                lease_id=lease["lease_id"],
                inspector=inspector,
            )
        inspector.groups.clear()
        recover_orphaned_account_lease(
            conn,
            account_id="xhs-a01",
            run_id="residual-child",
            lease_id=lease["lease_id"],
            inspector=inspector,
        )


def test_exact_runtime_profile_chrome_blocks_reconciliation(
    control_db: Path,
    tmp_path: Path,
) -> None:
    lease = acquire_test_lease(
        control_db,
        run_id="profile-chrome",
        state_path=tmp_path / "profile-state.json",
    )
    chrome = ProcessSnapshot(
        identity=ProcessIdentity(
            host_id="host-a",
            boot_id="boot-a",
            pid=333,
            process_started_at="2026-08-30T00:03:00+00:00",
            process_start_token="chrome-333",
            pgid=333,
        ),
        argv=("Chromium", f"--user-data-dir={lease['runtime_profile_dir']}"),
    )
    inspector = FakeInspector(profile_processes=[chrome])
    with connect(control_db) as conn:
        with pytest.raises(XhsOrphanLeaseRecoveryRefused, match="profile Chrome"):
            recover_orphaned_account_lease(
                conn,
                account_id="xhs-a01",
                run_id="profile-chrome",
                lease_id=lease["lease_id"],
                inspector=inspector,
            )
        inspector.profiles.clear()
        recover_orphaned_account_lease(
            conn,
            account_id="xhs-a01",
            run_id="profile-chrome",
            lease_id=lease["lease_id"],
            inspector=inspector,
        )


def test_dead_registered_browser_does_not_claim_the_owner_process_group(
    control_db: Path,
    tmp_path: Path,
) -> None:
    lease = acquire_test_lease(
        control_db,
        run_id="dead-browser",
        state_path=tmp_path / "dead-browser-state.json",
    )
    browser = ProcessIdentity(
        host_id="host-a",
        boot_id="boot-a",
        pid=334,
        process_started_at="2026-08-30T00:03:00+00:00",
        process_start_token="browser-334",
        pgid=OWNER.pgid,
    )
    unrelated_owner_group_member = ProcessSnapshot(
        identity=ProcessIdentity(
            host_id="host-a",
            boot_id="boot-a",
            pid=335,
            process_started_at="2026-08-30T00:03:01+00:00",
            process_start_token="unrelated-335",
            pgid=OWNER.pgid,
        ),
        argv=("shell-helper",),
    )
    with connect(control_db) as conn:
        register_lease_process(
            conn,
            lease_id=lease["lease_id"],
            owner_token=lease["owner_token"],
            process_role="browser",
            identity=browser,
        )
        result = recover_orphaned_account_lease(
            conn,
            account_id="xhs-a01",
            run_id="dead-browser",
            lease_id=lease["lease_id"],
            inspector=FakeInspector(groups={OWNER.pgid: [unrelated_owner_group_member]}),
        )
    assert result["release_scope"] == "account_mutex_only"


def test_profile_detection_uses_exact_user_data_dir_argument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tmp_path / "xhs-a01" / "profile"
    exact = ProcessSnapshot(
        identity=ProcessIdentity("host", "boot", 1, "start-1", "token-1", 1),
        argv=("Chrome", f"--user-data-dir={expected}"),
    )
    other_account = ProcessSnapshot(
        identity=ProcessIdentity("host", "boot", 2, "start-2", "token-2", 2),
        argv=("Chrome", f"--user-data-dir={tmp_path / 'xhs-a010' / 'profile'}"),
    )
    misleading_text = ProcessSnapshot(
        identity=ProcessIdentity("host", "boot", 3, "start-3", "token-3", 3),
        argv=("python", "worker.py", f"note={expected}"),
    )
    inspector = SystemProcessInspector.__new__(SystemProcessInspector)
    inspector.host_id = "host"
    inspector.boot_id = "boot"
    monkeypatch.setattr(inspector, "snapshots", lambda: [exact, other_account, misleading_text])
    assert inspector.profile_processes(expected) == [exact]


def test_signal_handler_latches_first_signal_and_restores_handlers(
    control_db: Path,
    tmp_path: Path,
) -> None:
    run_id = "signal-latch"
    guard = LeaseGuard(
        db_path=control_db,
        account_id="xhs-a01",
        run_id=run_id,
        lease_kind="crawl",
        execution_state_path=tmp_path / "signal-latch.json",
        runtime_profile_dir=runtime_session_paths(run_id)["profile"],
        budget=LeaseBudget(runtime_seconds=10),
        inspector=FakeInspector(),
    )
    previous = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    guard._install_signal_handlers()
    try:
        with pytest.raises(XhsLeaseSignal) as first:
            guard._signal_handler(signal.SIGINT, None)
        assert first.value.signum == signal.SIGINT
        assert guard.signal_received == signal.SIGINT

        guard._signal_handler(signal.SIGTERM, None)
        assert guard.signal_received == signal.SIGINT

        guard._closing = True
        guard._signal_handler(signal.SIGTERM, None)
        assert guard.signal_received == signal.SIGINT
    finally:
        guard._closing = False
        guard.close()

    assert guard._previous_handlers == {}
    assert {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    } == previous


def test_signal_latch_survives_deferred_close_and_restores_handlers(
    control_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "signal-latch-deferred-close"
    previous = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    guard = LeaseGuard(
        db_path=control_db,
        account_id="xhs-a01",
        run_id=run_id,
        lease_kind="crawl",
        execution_state_path=tmp_path / "signal-latch-deferred-close.json",
        runtime_profile_dir=runtime_session_paths(run_id)["profile"],
        budget=LeaseBudget(runtime_seconds=10),
        inspector=FakeInspector(current=OWNER),
    )
    guard.acquire()
    with pytest.raises(XhsLeaseSignal):
        guard._signal_handler(signal.SIGINT, None)

    def block_release() -> dict[str, Any]:
        assert guard._closing is True
        guard._signal_handler(signal.SIGTERM, None)
        return {
            "safe_to_release": False,
            "checks": [],
            "blocking": [{"reason": "test_process_still_live"}],
        }

    monkeypatch.setattr(guard, "terminate_owned_processes", block_release)

    assert guard.close() is False
    assert guard.signal_received == signal.SIGINT
    assert guard._previous_handlers == {}
    assert {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    } == previous
    with connect(control_db) as conn:
        row = conn.execute(
            """
            SELECT details_json FROM xhs_account_events
            WHERE run_id=? AND event_type='lease_release_deferred_live_processes'
            """,
            (run_id,),
        ).fetchone()
        assert row is not None
        assert json.loads(row[0])["signal"] == signal.SIGINT


def test_concurrent_orphan_reconciliation_has_one_winner(
    control_db: Path,
    tmp_path: Path,
) -> None:
    lease = acquire_test_lease(
        control_db,
        run_id="concurrent",
        state_path=tmp_path / "concurrent-state.json",
    )
    barrier = Barrier(2)

    def attempt() -> str:
        barrier.wait(timeout=5)
        with connect(control_db) as conn:
            try:
                recover_orphaned_account_lease(
                    conn,
                    account_id="xhs-a01",
                    run_id="concurrent",
                    lease_id=lease["lease_id"],
                    inspector=FakeInspector(),
                )
            except XhsOrphanLeaseRecoveryRefused:
                return "refused"
            return "released"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attempt) for _ in range(2)]
        results = sorted(future.result() for future in futures)
    assert results == ["refused", "released"]
    with connect(control_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM xhs_account_events WHERE event_type='orphan_lease_reconciled'"
        ).fetchone()[0] == 1


GUARD_DRIVER = r"""
import sys
from pathlib import Path
from trippostcollect.xhs import accounts, runtime
from trippostcollect.xhs.leases import LeaseBudget, LeaseGuard, XhsLeaseSignal

root = Path(sys.argv[1])
run_id = sys.argv[2]
accounts.XHS_LOCK_ROOT = root / "locks"
accounts.XHS_LEGACY_ACCOUNT_ROOT = root / "legacy-accounts"
runtime.XHS_SESSION_ROOT = root / "sessions"
guard = LeaseGuard(
    db_path=root / "control.sqlite",
    account_id="xhs-a01",
    run_id=run_id,
    lease_kind="crawl",
    execution_state_path=root / f"{run_id}.missing.json",
    runtime_profile_dir=runtime.runtime_session_paths(run_id)["profile"],
    budget=LeaseBudget(runtime_seconds=60, child_shutdown_seconds=4, root_finalize_seconds=1),
)
try:
    with guard:
        guard.run_subprocess(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=root,
            env={},
            timeout_seconds=60,
        )
except XhsLeaseSignal as exc:
    raise SystemExit(128 + exc.signum)
"""


def wait_for_guard_process(db_path: Path, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with connect(db_path) as conn:
            lease = conn.execute(
                "SELECT * FROM xhs_account_leases WHERE run_id=?",
                (run_id,),
            ).fetchone()
            child = conn.execute(
                """
                SELECT process.*
                FROM xhs_lease_processes AS process
                JOIN xhs_account_leases AS lease ON lease.lease_id=process.lease_id
                WHERE lease.run_id=? AND process.process_role='child'
                """,
                (run_id,),
            ).fetchone()
            if lease and child:
                return dict(lease), dict(child)
        time.sleep(0.05)
    raise AssertionError(f"guarded child did not start for {run_id}")


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups and signals required")
def test_sigterm_stops_process_group_before_release(control_db: Path, tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", GUARD_DRIVER, str(tmp_path), "sigterm-matrix"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child: dict[str, Any] | None = None
    try:
        _, child = wait_for_guard_process(control_db, "sigterm-matrix")
        os.kill(proc.pid, signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=12)
        assert proc.returncode == 128 + signal.SIGTERM, (stdout, stderr)
        assert SystemProcessInspector().identity(int(child["pid"])) is None
        with connect(control_db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 0
            details = json.loads(
                conn.execute(
                    """
                    SELECT details_json FROM xhs_account_events
                    WHERE run_id='sigterm-matrix' AND event_type='lease_released'
                    """
                ).fetchone()[0]
            )
            assert details["signal"] == signal.SIGTERM
            assert details["process_check"]["safe_to_release"] is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if child and SystemProcessInspector().identity(int(child["pid"])) is not None:
            os.killpg(int(child["pgid"]), signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups and signals required")
def test_sigkill_keeps_lease_and_live_child_blocks_orphan_recovery(
    control_db: Path,
    tmp_path: Path,
) -> None:
    proc = subprocess.Popen([sys.executable, "-c", GUARD_DRIVER, str(tmp_path), "sigkill-matrix"])
    child: dict[str, Any] | None = None
    try:
        lease, child = wait_for_guard_process(control_db, "sigkill-matrix")
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        with connect(control_db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 1
            with pytest.raises(XhsOrphanLeaseRecoveryRefused, match="still live"):
                recover_orphaned_account_lease(
                    conn,
                    account_id="xhs-a01",
                    run_id="sigkill-matrix",
                    lease_id=lease["lease_id"],
                )
        os.killpg(int(child["pgid"]), signal.SIGKILL)
        deadline = time.monotonic() + 5
        while (
            SystemProcessInspector().identity(int(child["pid"])) is not None
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        with connect(control_db) as conn:
            result = recover_orphaned_account_lease(
                conn,
                account_id="xhs-a01",
                run_id="sigkill-matrix",
                lease_id=lease["lease_id"],
            )
            assert result["terminal_assessment"]["reason"] == "execution_state_missing"
            assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if child and SystemProcessInspector().identity(int(child["pid"])) is not None:
            os.killpg(int(child["pgid"]), signal.SIGKILL)


def test_public_lease_hides_owner_token(control_db: Path, tmp_path: Path) -> None:
    lease = acquire_test_lease(
        control_db,
        run_id="public-lease",
        state_path=tmp_path / "public-state.json",
    )
    with connect(control_db) as conn:
        row = dict(conn.execute("SELECT * FROM xhs_account_leases").fetchone())
    public = public_lease(row)
    assert "owner_token" not in public
    assert public["owner_token_sha256"]
    assert public["lease_id"] == lease["lease_id"]


def test_recovery_cli_uses_lease_id_and_accepts_missing_terminal_state(
    control_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lease = acquire_test_lease(
        control_db,
        run_id="cli-orphan",
        state_path=tmp_path / "missing-cli-state.json",
        owner=ProcessIdentity(
            host_id=system_host_id(),
            boot_id=system_boot_id(),
            pid=999_999,
            process_started_at="2026-08-30T00:00:00+00:00",
            process_start_token="missing-cli-owner",
            pgid=999_999,
        ),
    )
    monkeypatch.setattr(
        xhs_accounts_cli,
        "parse_args",
        lambda: argparse.Namespace(
            db=str(control_db),
            command="recover-orphan-lease",
            account_id="xhs-a01",
            run_id="cli-orphan",
            lease_id=lease["lease_id"],
        ),
    )
    assert xhs_accounts_cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["release_scope"] == "account_mutex_only"
    assert result["terminal_assessment"]["reason"] == "execution_state_missing"

    monkeypatch.setattr(
        xhs_accounts_cli,
        "parse_args",
        lambda: argparse.Namespace(db=str(control_db), command="list"),
    )
    assert xhs_accounts_cli.main() == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["leases"] == []
    assert listing["slots"][0]["status"] == "active"
