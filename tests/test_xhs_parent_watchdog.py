"""Parent-side XHS supervisor watchdog tests."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from trippostcollect.xhs import leases
from trippostcollect.xhs import runtime
from trippostcollect.xhs.leases import (
    LeaseBudget,
    LeaseGuard,
    LeaseSubprocessResult,
    ProcessIdentity,
    RuntimeStatusWatchdogPolicy,
)


AUTH_KEY = b"k" * 32
WRONG_KEY = b"w" * 32
NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
CHILD = ProcessIdentity(
    host_id="host-a",
    boot_id="boot-a",
    pid=4242,
    process_started_at="2026-09-03T11:59:00+00:00",
    process_start_token="child-start-4242",
    pgid=4242,
)
REUSED_CHILD = ProcessIdentity(
    host_id="host-a",
    boot_id="boot-a",
    pid=4242,
    process_started_at="2026-09-03T12:01:00+00:00",
    process_start_token="reused-start-4242",
    pgid=4242,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


class FakeInspector:
    host_id = "host-a"
    boot_id = "boot-a"

    def __init__(self, identity: ProcessIdentity = CHILD) -> None:
        self.current = identity
        self.identity_calls = 0

    def identity(self, _pid: int) -> ProcessIdentity | None:
        self.identity_calls += 1
        return self.current

    def process_presence(self, _pid: int) -> bool:
        return self.current is not None

    def group_members(self, _pgid: int) -> list[object]:
        return []

    def profile_processes(self, _profile_dir: Path) -> list[object]:
        return []


class FakeProcess:
    pid = CHILD.pid

    def __init__(
        self,
        clock: FakeClock,
        *,
        exit_at: float | None = None,
        returncode: int | None = None,
        advance_overrides: list[float] | None = None,
    ) -> None:
        self.clock = clock
        self.exit_at = exit_at
        self.returncode = returncode
        self.advance_overrides = list(advance_overrides or [])
        self.signals: list[int] = []

    def poll(self) -> int | None:
        if (
            self.returncode is None
            and self.exit_at is not None
            and self.clock.value >= self.exit_at
        ):
            self.returncode = 0
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        if self.poll() is not None:
            return "child-output", ""
        if timeout is None:
            raise AssertionError("an active fake child requires a bounded poll")
        advance = (
            self.advance_overrides.pop(0)
            if self.advance_overrides
            else float(timeout)
        )
        self.clock.advance(advance)
        if self.poll() is not None:
            return "child-output", ""
        raise subprocess.TimeoutExpired(["fake-supervisor"], timeout)


def status_payload(
    sequence: int,
    *,
    heartbeat_at: datetime | None = None,
    network_state: str = "online",
) -> dict[str, Any]:
    reason = "transport_timeout" if network_state == "network_paused" else ""
    return {
        "schema_version": runtime.RUNTIME_STATUS_SCHEMA_VERSION,
        "run_id": "run-1",
        "account_id": "xhs-a01",
        "lease_id": "lease-1",
        "writer_role": "mediacrawler_supervisor",
        "writer_host_id": CHILD.host_id,
        "writer_boot_id": CHILD.boot_id,
        "writer_pid": CHILD.pid,
        "writer_process_started_at": CHILD.process_started_at,
        "writer_process_start_token": CHILD.process_start_token,
        "writer_pgid": CHILD.pgid,
        "sequence": sequence,
        "heartbeat_at": (heartbeat_at or NOW).isoformat(),
        "phase": "running",
        "network_state": network_state,
        "network_reason": reason,
        "auth_tag": "not-exposed-by-result",
    }


def signed_status_payload(sequence: int, *, auth_key: bytes) -> dict[str, Any]:
    unsigned = status_payload(sequence)
    unsigned.pop("auth_tag")
    return runtime.sign_runtime_status(unsigned, auth_key=auth_key)


def configured_guard(tmp_path: Path, inspector: Any) -> LeaseGuard:
    guard = LeaseGuard(
        db_path=tmp_path / "control.sqlite",
        account_id="xhs-a01",
        run_id="run-1",
        lease_kind="crawl",
        execution_state_path=tmp_path / "state.json",
        runtime_profile_dir=runtime.runtime_session_paths("run-1")["profile"],
        budget=LeaseBudget(
            runtime_seconds=30,
            child_shutdown_seconds=2,
            root_finalize_seconds=1,
        ),
        inspector=inspector,
    )
    guard.lease_id = "lease-1"
    guard.owner_token = "owner-token"
    guard.owner = ProcessIdentity(
        host_id="host-a",
        boot_id="boot-a",
        pid=111,
        process_started_at="2026-09-03T11:58:00+00:00",
        process_start_token="owner-start-111",
        pgid=111,
    )
    return guard


def install_process_harness(
    monkeypatch: pytest.MonkeyPatch,
    guard: LeaseGuard,
    process: FakeProcess,
) -> dict[str, Any]:
    observed: dict[str, Any] = {
        "marked": [],
        "owned_cleanup_calls": 0,
    }

    def popen(*_args: object, **kwargs: object) -> FakeProcess:
        observed["popen_env"] = dict(kwargs["env"])
        return process

    def register(pid: int, role: str) -> ProcessIdentity:
        assert (pid, role) == (CHILD.pid, "child")
        observed["registered"] = CHILD
        return CHILD

    def mark(identity: ProcessIdentity, role: str) -> None:
        observed["marked"].append((identity, role))

    def signal_group(identity: ProcessIdentity, signum: int) -> None:
        assert identity == CHILD
        process.signals.append(signum)
        process.returncode = -int(signum)

    def cleanup() -> dict[str, object]:
        observed["owned_cleanup_calls"] += 1
        return {"safe_to_release": True, "checks": [], "blocking": []}

    monkeypatch.setattr(leases.subprocess, "Popen", popen)
    monkeypatch.setattr(guard, "register_process", register)
    monkeypatch.setattr(guard, "mark_process_exited", mark)
    monkeypatch.setattr(guard, "_signal_registered_group", signal_group)
    monkeypatch.setattr(guard, "terminate_owned_processes", cleanup)
    return observed


def run_watchdog(
    guard: LeaseGuard,
    *,
    policy: RuntimeStatusWatchdogPolicy,
    timeout_seconds: int = 1,
) -> LeaseSubprocessResult:
    return guard.run_subprocess(
        ["fake-supervisor"],
        cwd=guard.execution_state_path.parent,
        env={"VISIBLE": "1"},
        timeout_seconds=timeout_seconds,
        runtime_watchdog=policy,
    )


def test_direct_child_exit_wins_before_identity_or_fresh_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "XHS_SESSION_ROOT", tmp_path / "sessions")
    runtime.prepare_runtime_session("run-1")
    runtime.write_runtime_status_atomic(
        runtime.runtime_status_path("run-1"),
        signed_status_payload(1, auth_key=AUTH_KEY),
        auth_key=AUTH_KEY,
    )
    clock = FakeClock()
    inspector = FakeInspector()
    guard = configured_guard(tmp_path, inspector)
    process = FakeProcess(clock, returncode=0)
    observed = install_process_harness(monkeypatch, guard, process)
    monkeypatch.setattr(leases.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(leases.secrets, "token_bytes", lambda size: AUTH_KEY)

    def forbidden_status_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("status must not be read after direct child exit")

    monkeypatch.setattr(
        leases,
        "read_runtime_status_if_present",
        forbidden_status_read,
    )

    result = run_watchdog(
        guard,
        policy=RuntimeStatusWatchdogPolicy(0.2, 0.2, 0.05, 0.05),
    )

    assert result.returncode == 0
    assert result.termination_reason is None
    assert result.runtime_status is None
    assert inspector.identity_calls == 0
    assert observed["marked"] == [(CHILD, "child")]
    assert observed["popen_env"][runtime.RUNTIME_STATUS_AUTH_KEY_ENV] == AUTH_KEY.hex()


def test_authenticated_sequences_use_receive_monotonic_and_outlive_old_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    inspector = FakeInspector()
    guard = configured_guard(tmp_path, inspector)
    process = FakeProcess(clock, exit_at=1.2)
    observed = install_process_harness(monkeypatch, guard, process)
    monkeypatch.setattr(leases.time, "monotonic", clock.monotonic)

    requested_key_sizes: list[int] = []

    def token_bytes(size: int) -> bytes:
        requested_key_sizes.append(size)
        return AUTH_KEY

    monkeypatch.setattr(leases.secrets, "token_bytes", token_bytes)
    sequences = iter(range(1, 20))
    read_arguments: list[dict[str, object]] = []

    def read_status(_path: Path, **kwargs: object) -> Mapping[str, Any]:
        read_arguments.append(dict(kwargs))
        sequence = next(sequences)
        wall_clock = (
            NOW + timedelta(days=365)
            if sequence % 2
            else NOW - timedelta(days=365)
        )
        return status_payload(sequence, heartbeat_at=wall_clock)

    monkeypatch.setattr(leases, "read_runtime_status_if_present", read_status)

    result = run_watchdog(
        guard,
        timeout_seconds=1,
        policy=RuntimeStatusWatchdogPolicy(0.3, 0.4, 0.2, 0.1),
    )

    assert clock.value >= 1.2
    assert result.returncode == 0
    assert result.timed_out is False
    assert result.termination_reason is None
    assert requested_key_sizes == [32]
    assert observed["popen_env"][runtime.RUNTIME_STATUS_AUTH_KEY_ENV] == AUTH_KEY.hex()
    assert read_arguments
    assert all(item["auth_key"] == AUTH_KEY for item in read_arguments)
    assert all(item["expected_writer_identity"] == CHILD for item in read_arguments)
    assert result.runtime_status["sequence"] >= 5
    assert not {
        "auth_tag",
        "run_id",
        "account_id",
        "lease_id",
        "writer_pid",
        "writer_process_start_token",
    } & set(result.runtime_status)
    assert observed["marked"] == [(CHILD, "child")]


def test_real_authenticated_heartbeats_keep_a_long_supervisor_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "XHS_SESSION_ROOT", tmp_path / "sessions")
    runtime.prepare_runtime_session("run-1")
    inspector = leases.SystemProcessInspector()
    guard = configured_guard(tmp_path, inspector)
    registered: list[ProcessIdentity] = []
    marked: list[tuple[ProcessIdentity, str]] = []

    def register(pid: int, role: str) -> ProcessIdentity:
        assert role == "child"
        identity = inspector.identity(pid)
        assert identity is not None
        registered.append(identity)
        return identity

    monkeypatch.setattr(guard, "register_process", register)
    monkeypatch.setattr(
        guard,
        "mark_process_exited",
        lambda identity, role: marked.append((identity, role)),
    )
    monkeypatch.setattr(
        guard,
        "terminate_owned_processes",
        lambda: {"safe_to_release": True, "checks": [], "blocking": []},
    )
    monkeypatch.setattr(leases.secrets, "token_bytes", lambda _size: AUTH_KEY)
    child = r"""
import os
import sys
import time
from pathlib import Path

from trippostcollect.xhs import runtime
from trippostcollect.xhs.leases import SystemProcessInspector

runtime.XHS_SESSION_ROOT = Path(sys.argv[1])
auth_key = bytes.fromhex(os.environ.pop(runtime.RUNTIME_STATUS_AUTH_KEY_ENV))
identity = SystemProcessInspector().current_identity()
for sequence in range(1, 41):
    heartbeat_at = (
        "2126-09-03T12:00:00+00:00"
        if sequence % 2
        else "1926-09-03T12:00:00+00:00"
    )
    unsigned = {
        "schema_version": runtime.RUNTIME_STATUS_SCHEMA_VERSION,
        "run_id": "run-1",
        "account_id": "xhs-a01",
        "lease_id": "lease-1",
        "writer_role": "mediacrawler_supervisor",
        "writer_host_id": identity.host_id,
        "writer_boot_id": identity.boot_id,
        "writer_pid": identity.pid,
        "writer_process_started_at": identity.process_started_at,
        "writer_process_start_token": identity.process_start_token,
        "writer_pgid": identity.pgid,
        "sequence": sequence,
        "heartbeat_at": heartbeat_at,
        "phase": "running",
        "network_state": "online",
        "network_reason": "",
    }
    runtime.write_runtime_status_atomic(
        runtime.runtime_status_path("run-1"),
        runtime.sign_runtime_status(unsigned, auth_key=auth_key),
        auth_key=auth_key,
    )
    time.sleep(0.05)
"""
    started = time.monotonic()

    result = guard.run_subprocess(
        [
            sys.executable,
            "-c",
            child,
            str(tmp_path / "sessions"),
        ],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout_seconds=1,
        runtime_watchdog=RuntimeStatusWatchdogPolicy(1, 0.5, 0.02, 0.1),
    )

    assert time.monotonic() - started > 1
    assert result.returncode == 0
    assert result.timed_out is False
    assert result.termination_reason is None
    assert 1 <= result.runtime_status["sequence"] <= 40
    assert registered
    assert marked == [(registered[0], "child")]


def test_reused_child_identity_stops_before_status_without_signalling_reused_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    inspector = FakeInspector(REUSED_CHILD)
    guard = configured_guard(tmp_path, inspector)
    process = FakeProcess(clock)
    observed = install_process_harness(monkeypatch, guard, process)
    monkeypatch.setattr(leases.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(leases.secrets, "token_bytes", lambda _size: AUTH_KEY)

    def forbidden_status_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("reused PID must be rejected before status")

    monkeypatch.setattr(
        leases,
        "read_runtime_status_if_present",
        forbidden_status_read,
    )

    result = run_watchdog(
        guard,
        policy=RuntimeStatusWatchdogPolicy(0.2, 0.2, 0.05, 0.05),
    )

    assert result.returncode == 1
    assert result.termination_reason == "child_process_identity_changed"
    assert process.signals == []
    assert observed["marked"] == []
    assert observed["owned_cleanup_calls"] == 1


@pytest.mark.parametrize(
    ("observations", "reason"),
    [
        ([status_payload(2), status_payload(1)], "runtime_status_sequence_regressed"),
        ([status_payload(1), None], "runtime_status_missing_after_observation"),
        (
            [status_payload(1), status_payload(1, heartbeat_at=NOW + timedelta(days=1))],
            "runtime_status_sequence_reused",
        ),
    ],
)
def test_status_rollback_rebuild_and_same_sequence_mutation_are_terminal(
    observations: list[Mapping[str, Any] | None],
    reason: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    inspector = FakeInspector()
    guard = configured_guard(tmp_path, inspector)
    process = FakeProcess(clock)
    install_process_harness(monkeypatch, guard, process)
    monkeypatch.setattr(leases.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(leases.secrets, "token_bytes", lambda _size: AUTH_KEY)
    queued = iter(observations)
    monkeypatch.setattr(
        leases,
        "read_runtime_status_if_present",
        lambda *_args, **_kwargs: next(queued),
    )

    result = run_watchdog(
        guard,
        policy=RuntimeStatusWatchdogPolicy(0.2, 0.3, 0.1, 0.1),
    )

    assert result.returncode == 1
    assert result.timed_out is False
    assert result.termination_reason == reason
    assert process.signals == [signal.SIGTERM]


def test_missing_status_obeys_startup_grace_then_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    guard = configured_guard(tmp_path, FakeInspector())
    process = FakeProcess(clock)
    install_process_harness(monkeypatch, guard, process)
    monkeypatch.setattr(leases.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(leases.secrets, "token_bytes", lambda _size: AUTH_KEY)
    monkeypatch.setattr(
        leases,
        "read_runtime_status_if_present",
        lambda *_args, **_kwargs: None,
    )

    result = run_watchdog(
        guard,
        policy=RuntimeStatusWatchdogPolicy(0.25, 0.2, 0.1, 0.0),
    )

    assert clock.value >= 0.3
    assert result.returncode == 124
    assert result.timed_out is True
    assert result.termination_reason == "runtime_status_startup_timeout"
    assert result.runtime_status is None


@pytest.mark.parametrize(
    "observed_status",
    [None, status_payload(1)],
    ids=["startup", "stale"],
)
def test_child_exit_at_timeout_boundary_wins_without_parent_signal(
    observed_status: Mapping[str, Any] | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    guard = configured_guard(tmp_path, FakeInspector())
    process = FakeProcess(clock)
    observed = install_process_harness(monkeypatch, guard, process)
    monkeypatch.setattr(leases.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(leases.secrets, "token_bytes", lambda _size: AUTH_KEY)
    reads = 0

    def exit_while_parent_reads_status(
        *_args: object,
        **_kwargs: object,
    ) -> Mapping[str, Any] | None:
        nonlocal reads
        reads += 1
        if reads == 1 and observed_status is not None:
            return observed_status
        clock.advance(1.0)
        process.returncode = 7
        return observed_status

    monkeypatch.setattr(
        leases,
        "read_runtime_status_if_present",
        exit_while_parent_reads_status,
    )

    result = run_watchdog(
        guard,
        policy=RuntimeStatusWatchdogPolicy(0.2, 0.2, 0.05, 0.0),
    )

    assert result.returncode == 7
    assert result.timed_out is False
    assert result.termination_reason is None
    assert process.signals == []
    assert observed["owned_cleanup_calls"] == 0
    assert observed["marked"] == [(CHILD, "child")]


def test_tampered_authenticated_file_is_terminal_and_not_echoed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "XHS_SESSION_ROOT", tmp_path / "sessions")
    runtime.prepare_runtime_session("run-1")
    status_path = runtime.runtime_status_path("run-1")
    tampered = signed_status_payload(1, auth_key=WRONG_KEY)
    runtime.write_runtime_status_atomic(status_path, tampered, auth_key=WRONG_KEY)

    clock = FakeClock()
    guard = configured_guard(tmp_path, FakeInspector())
    process = FakeProcess(clock)
    install_process_harness(monkeypatch, guard, process)
    monkeypatch.setattr(leases.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(leases.secrets, "token_bytes", lambda _size: AUTH_KEY)

    result = run_watchdog(
        guard,
        policy=RuntimeStatusWatchdogPolicy(0.2, 0.2, 0.05, 0.05),
    )

    assert result.returncode == 1
    assert result.termination_reason == "runtime_status_invalid"
    assert result.runtime_status is None
    assert WRONG_KEY.hex() not in repr(result)
    assert AUTH_KEY.hex() not in repr(result)


def test_status_same_sequence_is_not_progress_and_network_pause_has_no_privilege(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    guard = configured_guard(tmp_path, FakeInspector())
    process = FakeProcess(clock)
    install_process_harness(monkeypatch, guard, process)
    monkeypatch.setattr(leases.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(leases.secrets, "token_bytes", lambda _size: AUTH_KEY)
    unchanged = status_payload(1, network_state="network_paused")
    monkeypatch.setattr(
        leases,
        "read_runtime_status_if_present",
        lambda *_args, **_kwargs: unchanged,
    )

    result = run_watchdog(
        guard,
        policy=RuntimeStatusWatchdogPolicy(0.2, 0.5, 0.2, 0.0),
    )

    assert clock.value >= 0.6
    assert result.returncode == 124
    assert result.termination_reason == "runtime_status_stale"
    assert result.runtime_status["sequence"] == 1
    assert result.runtime_status["network_state"] == "network_paused"


def test_large_poll_gap_grants_one_bounded_resume_window_per_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    guard = configured_guard(tmp_path, FakeInspector())
    process = FakeProcess(clock, advance_overrides=[5.0])
    install_process_harness(monkeypatch, guard, process)
    monkeypatch.setattr(leases.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(leases.secrets, "token_bytes", lambda _size: AUTH_KEY)
    unchanged = status_payload(1)
    reads = 0

    def read_status(*_args: object, **_kwargs: object) -> Mapping[str, Any]:
        nonlocal reads
        reads += 1
        return unchanged

    monkeypatch.setattr(leases, "read_runtime_status_if_present", read_status)

    result = run_watchdog(
        guard,
        policy=RuntimeStatusWatchdogPolicy(0.2, 0.5, 0.1, 0.2),
    )

    assert 5.2 < clock.value < 5.5
    assert reads <= 5
    assert result.returncode == 124
    assert result.termination_reason == "runtime_status_stale"
    assert result.watchdog_resume_grace_used is True


def test_legacy_mode_keeps_absolute_timeout_and_generates_no_runtime_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    guard = configured_guard(tmp_path, FakeInspector())
    process = FakeProcess(clock)
    observed = install_process_harness(monkeypatch, guard, process)
    monkeypatch.setattr(leases.time, "monotonic", clock.monotonic)

    def forbidden_key(_size: int) -> bytes:
        raise AssertionError("legacy timeout must not generate a runtime key")

    monkeypatch.setattr(leases.secrets, "token_bytes", forbidden_key)

    result = guard.run_subprocess(
        ["fake-supervisor"],
        cwd=tmp_path,
        env={},
        timeout_seconds=1,
    )

    assert clock.value == 1
    assert result.returncode == 124
    assert result.timed_out is True
    assert result.termination_reason == "absolute_timeout"
    assert result.runtime_status is None
    assert runtime.RUNTIME_STATUS_AUTH_KEY_ENV not in observed["popen_env"]


@pytest.mark.parametrize(
    "policy",
    [
        lambda: RuntimeStatusWatchdogPolicy(-1, 1, 0.1, 0.1),
        lambda: RuntimeStatusWatchdogPolicy(0, 0, 0.1, 0.0),
        lambda: RuntimeStatusWatchdogPolicy(0, 1, 2, 0.0),
        lambda: RuntimeStatusWatchdogPolicy(0, 1, 0.1, 2),
        lambda: RuntimeStatusWatchdogPolicy(0, float("inf"), 0.1, 0.0),
    ],
)
def test_watchdog_policy_rejects_unbounded_or_incoherent_values(
    policy: Callable[[], RuntimeStatusWatchdogPolicy],
) -> None:
    with pytest.raises(ValueError):
        policy()
