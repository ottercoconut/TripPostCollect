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

from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.xhs import accounts, leases as xhs_leases, runtime
from trippostcollect.xhs.accounts import XhsAccountUnavailable
from trippostcollect.xhs.leases import (
    LeaseBudget,
    LeaseGuard,
    ProcessIdentity,
    ProcessSnapshot,
    SystemProcessInspector,
    XhsLeaseOwnershipError,
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
    ):
        self.host_id = host_id
        self.boot_id = boot_id
        self.identities = identities or {}
        self.presences = presences or {}
        self.groups = groups or {}
        self.profiles = profile_processes or []

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


@pytest.fixture
def control_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(accounts, "XHS_LOCK_ROOT", tmp_path / "locks")
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


def test_obsolete_lease_is_discarded_during_exact_schema_cutover(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite"
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
            INSERT INTO xhs_accounts VALUES(
                'xhs-a01', 'active', '/profile', '/state', 'identity',
                NULL, NULL, '2026-08-30', '2026-08-30'
            );
            INSERT INTO xhs_account_leases VALUES(
                'xhs-a01', 'legacy-run', '2026-08-30', '2026-08-31'
            );
            """
        )
        accounts.ensure_xhs_schema(conn)

        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 0
        event = conn.execute(
            """
            SELECT account_id, run_id, event_type, details_json
            FROM xhs_account_events
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert tuple(event[:3]) == (
            "xhs-a01",
            "legacy-run",
            "lease_schema_cutover_discarded",
        )
        assert json.loads(event[3]) == {
            "reason": "unsupported_lease_schema",
            "migration_version": 22,
            "legacy_acquired_at": "2026-08-30",
            "legacy_expires_at": "2026-08-31",
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
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TABLE xhs_lease_processes")
        conn.execute("DROP TABLE xhs_account_leases")
        conn.execute(
            """
            CREATE TABLE xhs_account_leases(
                account_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO xhs_account_leases(account_id, run_id, acquired_at, expires_at)
            VALUES ('xhs-a01', 'obsolete-run', '2026-08-30', '2026-08-31')
            """
        )
        conn.execute("DELETE FROM schema_migrations WHERE version=21")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")

        accounts.ensure_xhs_schema(conn)

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
