"""TripPostCollect MediaCrawler process watchdog tests."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from support.signal_driver import isolated_signal_test


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

mediacrawler_crawl = import_module("mediacrawler_crawl")
FrozenExecutionState = import_module("execution_state").FrozenExecutionState
runtime = import_module("trippostcollect.xhs.runtime")
ProcessIdentity = import_module("trippostcollect.xhs.leases").ProcessIdentity

AUTH_KEY_HEX = "ab" * 32
AUTH_KEY = bytes.fromhex(AUTH_KEY_HEX)
NOW = datetime(2026, 9, 3, 8, 0, 0, tzinfo=timezone.utc)
PRIVATE_LEASE_ENV_KEYS = (
    mediacrawler_crawl.LEASE_DB_ENV,
    mediacrawler_crawl.LEASE_ID_ENV,
    mediacrawler_crawl.LEASE_OWNER_TOKEN_ENV,
)
PRIVATE_EXPORTER_ENV_KEYS = (
    runtime.RUNTIME_STATUS_AUTH_KEY_ENV,
    *PRIVATE_LEASE_ENV_KEYS,
)


class StaticInspector:
    def __init__(self, identity: object) -> None:
        self.current = identity

    def identity(self, _pid: int) -> object:
        return self.current

    def current_identity(self) -> object:
        return self.current


def process_identity(
    *,
    pid: int = 4321,
    process_start_token: str = "token-1",
) -> object:
    return ProcessIdentity(
        host_id="host-1",
        boot_id="boot-1",
        pid=pid,
        process_started_at="2026-09-03T08:00:00+00:00",
        process_start_token=process_start_token,
        pgid=pid,
    )


@pytest.fixture
def runtime_reporter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, dict[str, Path], object]:
    monkeypatch.setattr(runtime, "XHS_SESSION_ROOT", tmp_path / "sessions")
    paths = runtime.prepare_runtime_session("run-1")
    identity = process_identity()
    reporter = mediacrawler_crawl.XhsSupervisorRuntimeReporter(
        run_id="run-1",
        account_id="xhs-a01",
        lease_id="lease-1",
        status_path=runtime.runtime_status_path("run-1"),
        auth_key=AUTH_KEY,
        writer_identity=identity,
        inspector=StaticInspector(identity),
    )
    reporter.checkpoint()
    return reporter, {**paths, "status": runtime.runtime_status_path("run-1")}, identity


def read_reporter_status(path: Path, identity: object) -> dict[str, object]:
    return runtime.read_runtime_status(
        path,
        auth_key=AUTH_KEY,
        expected_run_id="run-1",
        expected_account_id="xhs-a01",
        expected_lease_id="lease-1",
        expected_writer_identity=identity,
    )


def reporter_environment(*, auth_key: str | None = AUTH_KEY_HEX) -> dict[str, str]:
    environ = {
        mediacrawler_crawl.LEASE_DB_ENV: "/tmp/xhs-control.sqlite",
        mediacrawler_crawl.LEASE_ID_ENV: "lease-1",
        mediacrawler_crawl.LEASE_OWNER_TOKEN_ENV: "owner-token",
        mediacrawler_crawl.XHS_RUNTIME_STATUS_RUN_ID_ENV: "run-1",
    }
    if auth_key is not None:
        environ[runtime.RUNTIME_STATUS_AUTH_KEY_ENV] = auth_key
    return environ


def configure_real_supervised_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered: dict[str, object] = {}

    def register_exporter(*, pid: int, **_kwargs: object) -> object:
        identity = process_identity(pid=pid, process_start_token="exporter-live")
        registered["identity"] = identity
        return identity

    class StableExporterInspector:
        def identity(self, _pid: int) -> object:
            return registered.get("identity")

    monkeypatch.setattr(
        mediacrawler_crawl,
        "browser_launch_environment",
        reporter_environment,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "register_lease_process_from_environment",
        register_exporter,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "mark_lease_process_exited_from_environment",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "SystemProcessInspector",
        StableExporterInspector,
    )


@pytest.mark.parametrize(
    ("auth_key", "error"),
    [
        (None, "incomplete XHS runtime reporter environment"),
        ("AB" * 32, "64 lowercase hexadecimal"),
        ("ab" * 31, "64 lowercase hexadecimal"),
    ],
)
def test_xhs_runtime_key_is_required_and_validated_before_main_or_popen(
    auth_key: str | None,
    error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "XHS_SESSION_ROOT", tmp_path / "sessions")
    paths = runtime.prepare_runtime_session("run-1")
    args = SimpleNamespace(
        xhs_account_id="xhs-a01",
        xhs_profile_dir=str(paths["profile"]),
    )
    environ = reporter_environment(auth_key=auth_key)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    if auth_key is None:
        monkeypatch.delenv(runtime.RUNTIME_STATUS_AUTH_KEY_ENV, raising=False)
    monkeypatch.setattr(mediacrawler_crawl, "parse_args", lambda: args)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("main body and Popen must not run")

    monkeypatch.setattr(mediacrawler_crawl, "_run_main", forbidden)
    monkeypatch.setattr(mediacrawler_crawl.subprocess, "Popen", forbidden)

    with pytest.raises(runtime.RuntimeStatusValidationError, match=error):
        mediacrawler_crawl.main()
    assert runtime.RUNTIME_STATUS_AUTH_KEY_ENV not in os.environ


def test_runtime_reporter_consumes_key_and_uses_registered_supervisor_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "XHS_SESSION_ROOT", tmp_path / "sessions")
    paths = runtime.prepare_runtime_session("run-1")
    identity = process_identity()
    environ = reporter_environment()
    reporter = mediacrawler_crawl.xhs_supervisor_runtime_reporter_from_context(
        SimpleNamespace(
            xhs_account_id="xhs-a01",
            xhs_profile_dir=str(paths["profile"]),
        ),
        environ=environ,
        inspector=StaticInspector(identity),
    )

    assert reporter is not None
    assert runtime.RUNTIME_STATUS_AUTH_KEY_ENV not in environ
    assert environ[mediacrawler_crawl.LEASE_OWNER_TOKEN_ENV] == "owner-token"
    status = read_reporter_status(runtime.runtime_status_path("run-1"), identity)
    assert status["writer_pid"] == identity.pid
    assert status["writer_process_start_token"] == identity.process_start_token
    assert status["phase"] == "starting"


@pytest.mark.parametrize("run_id", [None, "run-2"])
def test_runtime_reporter_requires_exact_run_id_environment(
    run_id: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "XHS_SESSION_ROOT", tmp_path / "sessions")
    paths = runtime.prepare_runtime_session("run-1")
    environ = reporter_environment()
    if run_id is None:
        environ.pop(mediacrawler_crawl.XHS_RUNTIME_STATUS_RUN_ID_ENV)
    else:
        environ[mediacrawler_crawl.XHS_RUNTIME_STATUS_RUN_ID_ENV] = run_id

    with pytest.raises(runtime.RuntimeStatusValidationError, match="run id|missing"):
        mediacrawler_crawl.xhs_supervisor_runtime_reporter_from_context(
            SimpleNamespace(
                xhs_account_id="xhs-a01",
                xhs_profile_dir=str(paths["profile"]),
            ),
            environ=environ,
            inspector=StaticInspector(process_identity()),
        )
    assert runtime.RUNTIME_STATUS_AUTH_KEY_ENV not in environ


def test_no_xhs_runtime_environment_is_a_noop() -> None:
    reporter = mediacrawler_crawl.xhs_supervisor_runtime_reporter_from_context(
        SimpleNamespace(xhs_account_id=None, xhs_profile_dir=None),
        environ={},
    )

    assert reporter is None


@pytest.mark.macos_process
def test_private_runtime_environment_is_not_forwarded_to_exporter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    original_popen = mediacrawler_crawl.subprocess.Popen

    def capture_popen(*args: object, **kwargs: object) -> object:
        captured["child_env"] = dict(kwargs["env"])
        return original_popen(*args, **kwargs)

    def register_exporter(
        *,
        pid: int,
        environ: dict[str, str],
        **_kwargs: object,
    ) -> object:
        captured["registration_env"] = dict(environ)
        identity = process_identity(pid=pid, process_start_token="exporter-normal")
        captured["identity"] = identity
        return identity

    def mark_exporter_exited(
        *,
        identity: object,
        environ: dict[str, str],
        **_kwargs: object,
    ) -> None:
        captured["marked_identity"] = identity
        captured["exit_env"] = dict(environ)

    monkeypatch.setattr(
        mediacrawler_crawl,
        "browser_launch_environment",
        reporter_environment,
    )
    monkeypatch.setattr(
        mediacrawler_crawl.subprocess,
        "Popen",
        capture_popen,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "register_lease_process_from_environment",
        register_exporter,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "mark_lease_process_exited_from_environment",
        mark_exporter_exited,
    )
    child = (
        "import os, sys; "
        f"sys.exit(any(key in os.environ for key in {PRIVATE_EXPORTER_ENV_KEYS!r}))"
    )

    result = mediacrawler_crawl.run_command(
        [sys.executable, "-c", child],
        tmp_path,
        2,
        tmp_path / "logs",
    )

    assert result["returncode"] == 0
    child_env = captured["child_env"]
    registration_env = captured["registration_env"]
    exit_env = captured["exit_env"]
    assert isinstance(child_env, dict)
    assert isinstance(registration_env, dict)
    assert isinstance(exit_env, dict)
    assert not set(PRIVATE_EXPORTER_ENV_KEYS) & set(child_env)
    for key, expected in reporter_environment(auth_key=None).items():
        assert registration_env[key] == expected
        assert exit_env[key] == expected
    assert runtime.RUNTIME_STATUS_AUTH_KEY_ENV not in registration_env
    assert captured["marked_identity"] == captured["identity"]
    logs = "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "logs").iterdir()
    )
    assert AUTH_KEY_HEX not in logs
    assert "owner-token" not in logs


@pytest.mark.macos_process
def test_registration_failure_terminates_stripped_child_without_false_exit_mark(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {"mark_calls": 0}
    target_side_effect = tmp_path / "exporter-started"
    original_spawn = mediacrawler_crawl.spawn_gated_subprocess

    def capture_spawn(*args: object, **kwargs: object) -> object:
        captured["child_env"] = dict(kwargs["env"])
        gate = original_spawn(*args, **kwargs)
        captured["pid"] = gate.process.pid
        return gate

    def fail_registration(
        *,
        environ: dict[str, str],
        **_kwargs: object,
    ) -> object:
        captured["registration_env"] = dict(environ)
        raise RuntimeError("synthetic registration failure")

    def mark_exit(**_kwargs: object) -> None:
        captured["mark_calls"] = int(captured["mark_calls"]) + 1

    monkeypatch.setattr(
        mediacrawler_crawl,
        "browser_launch_environment",
        reporter_environment,
    )
    monkeypatch.setattr(mediacrawler_crawl, "spawn_gated_subprocess", capture_spawn)
    monkeypatch.setattr(
        mediacrawler_crawl,
        "register_lease_process_from_environment",
        fail_registration,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "mark_lease_process_exited_from_environment",
        mark_exit,
    )

    with pytest.raises(RuntimeError, match="synthetic registration failure"):
        mediacrawler_crawl.run_command(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import time; "
                    f"Path({str(target_side_effect)!r}).write_text('started'); "
                    "time.sleep(60)"
                ),
            ],
            tmp_path,
            1,
            tmp_path / "registration-failure-logs",
            cleanup_grace_seconds=1,
        )

    child_env = captured["child_env"]
    registration_env = captured["registration_env"]
    assert isinstance(child_env, dict)
    assert isinstance(registration_env, dict)
    assert not set(PRIVATE_EXPORTER_ENV_KEYS) & set(child_env)
    assert registration_env[mediacrawler_crawl.LEASE_OWNER_TOKEN_ENV] == "owner-token"
    assert captured["mark_calls"] == 0
    assert not target_side_effect.exists()
    assert (
        mediacrawler_crawl.SystemProcessInspector().process_presence(
            int(captured["pid"])
        )
        is False
    )


@pytest.mark.parametrize(
    ("first_signal", "later_signal"),
    [
        (signal.SIGINT, signal.SIGTERM),
        (signal.SIGTERM, signal.SIGINT),
    ],
)
@pytest.mark.macos_process
@isolated_signal_test
def test_signal_after_exporter_popen_cancels_gate_before_registration_or_exec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    first_signal: int,
    later_signal: int,
) -> None:
    class ReplayedSignal(BaseException):
        def __init__(self, signum: int) -> None:
            self.signum = signum

    target_side_effect = tmp_path / f"unregistered-exporter-{first_signal}"
    captured: dict[str, object] = {"registration_calls": 0}
    original_spawn = mediacrawler_crawl.spawn_gated_subprocess
    original_handlers = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }

    def replayed(signum: int, _frame: object) -> None:
        raise ReplayedSignal(signum)

    def spawn_then_interrupt(*args: object, **kwargs: object) -> object:
        gate = original_spawn(*args, **kwargs)
        captured["gate"] = gate
        captured["pid"] = gate.process.pid
        os.kill(os.getpid(), first_signal)
        os.kill(os.getpid(), later_signal)
        return gate

    def forbidden_registration(**_kwargs: object) -> object:
        captured["registration_calls"] = int(captured["registration_calls"]) + 1
        raise AssertionError("registration must not run after a latched pre-register signal")

    monkeypatch.setattr(
        mediacrawler_crawl,
        "browser_launch_environment",
        reporter_environment,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "spawn_gated_subprocess",
        spawn_then_interrupt,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "register_lease_process_from_environment",
        forbidden_registration,
    )
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, replayed)
    try:
        with pytest.raises(ReplayedSignal) as interrupted:
            mediacrawler_crawl.run_command(
                [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; "
                        f"Path({str(target_side_effect)!r}).write_text('started')"
                    ),
                ],
                tmp_path,
                1,
                tmp_path / f"pre-register-signal-{first_signal}",
                cleanup_grace_seconds=1,
            )
        assert interrupted.value.signum == first_signal
        assert captured["registration_calls"] == 0
        assert not target_side_effect.exists()
        assert (
            mediacrawler_crawl.SystemProcessInspector().process_presence(
                int(captured["pid"])
            )
            is False
        )
        gate = captured["gate"]
        assert getattr(gate, "_release_fd") is None
        assert all(
            signal.getsignal(signum) is replayed
            for signum in (signal.SIGINT, signal.SIGTERM)
        )
    finally:
        for signum, handler in original_handlers.items():
            signal.signal(signum, handler)


@pytest.mark.parametrize("injected", [KeyboardInterrupt(), RuntimeError("register failed")])
@pytest.mark.macos_process
def test_exporter_registration_exception_cancels_gate_without_target_exec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    injected: BaseException,
) -> None:
    target_side_effect = tmp_path / f"registration-exception-{type(injected).__name__}"
    captured: dict[str, object] = {}
    original_spawn = mediacrawler_crawl.spawn_gated_subprocess

    def capture_spawn(*args: object, **kwargs: object) -> object:
        gate = original_spawn(*args, **kwargs)
        captured["gate"] = gate
        captured["pid"] = gate.process.pid
        return gate

    def fail_registration(**_kwargs: object) -> object:
        raise injected

    monkeypatch.setattr(
        mediacrawler_crawl,
        "browser_launch_environment",
        reporter_environment,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "spawn_gated_subprocess",
        capture_spawn,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "register_lease_process_from_environment",
        fail_registration,
    )

    expected_message = str(injected)
    raises_kwargs = {"match": expected_message} if expected_message else {}
    with pytest.raises(type(injected), **raises_kwargs):
        mediacrawler_crawl.run_command(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path({str(target_side_effect)!r}).write_text('started')"
                ),
            ],
            tmp_path,
            1,
            tmp_path / f"registration-exception-{type(injected).__name__}-logs",
            cleanup_grace_seconds=1,
        )

    assert not target_side_effect.exists()
    assert (
        mediacrawler_crawl.SystemProcessInspector().process_presence(
            int(captured["pid"])
        )
        is False
    )
    assert getattr(captured["gate"], "_release_fd") is None


@pytest.mark.macos_process
def test_exporter_gate_release_failure_marks_registered_wrapper_exited(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target_side_effect = tmp_path / "release-failed-exporter"
    captured: dict[str, object] = {"mark_calls": 0}
    original_spawn = mediacrawler_crawl.spawn_gated_subprocess

    def capture_spawn(*args: object, **kwargs: object) -> object:
        gate = original_spawn(*args, **kwargs)
        captured["gate"] = gate
        captured["pid"] = gate.process.pid
        return gate

    def register_exporter(*, pid: int, **_kwargs: object) -> object:
        identity = mediacrawler_crawl.SystemProcessInspector().identity(pid)
        assert identity is not None
        captured["identity"] = identity
        return identity

    def mark_exporter(**kwargs: object) -> None:
        captured["mark_calls"] = int(captured["mark_calls"]) + 1
        captured["marked_identity"] = kwargs["identity"]

    def fail_before_write(_gate: object) -> None:
        raise OSError("synthetic exporter release failure")

    monkeypatch.setattr(
        mediacrawler_crawl,
        "browser_launch_environment",
        reporter_environment,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "spawn_gated_subprocess",
        capture_spawn,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "register_lease_process_from_environment",
        register_exporter,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "mark_lease_process_exited_from_environment",
        mark_exporter,
    )
    monkeypatch.setattr(
        mediacrawler_crawl.GatedSubprocess,
        "release",
        fail_before_write,
    )

    with pytest.raises(OSError, match="synthetic exporter release failure"):
        mediacrawler_crawl.run_command(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path({str(target_side_effect)!r}).write_text('started')"
                ),
            ],
            tmp_path,
            1,
            tmp_path / "release-failure-logs",
            cleanup_grace_seconds=1,
        )

    assert not target_side_effect.exists()
    assert captured["mark_calls"] == 1
    assert captured["marked_identity"] == captured["identity"]
    assert (
        mediacrawler_crawl.SystemProcessInspector().process_presence(
            int(captured["pid"])
        )
        is False
    )
    assert getattr(captured["gate"], "_release_fd") is None


@pytest.mark.macos_process
@isolated_signal_test
def test_signal_after_exporter_registration_reaps_exact_released_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ReplayedSignal(BaseException):
        pass

    target_side_effect = tmp_path / "registered-signal-exporter"
    captured: dict[str, object] = {"mark_calls": 0}
    original_spawn = mediacrawler_crawl.spawn_gated_subprocess
    original_release = mediacrawler_crawl.GatedSubprocess.release
    original_handlers = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }

    def replayed(signum: int, _frame: object) -> None:
        raise ReplayedSignal(signum)

    def capture_spawn(*args: object, **kwargs: object) -> object:
        gate = original_spawn(*args, **kwargs)
        captured["gate"] = gate
        captured["pid"] = gate.process.pid
        return gate

    def register_exporter(*, pid: int, **_kwargs: object) -> object:
        identity = mediacrawler_crawl.SystemProcessInspector().identity(pid)
        assert identity is not None
        captured["identity"] = identity
        return identity

    def release_then_interrupt(gate: object) -> None:
        original_release(gate)
        os.kill(os.getpid(), signal.SIGTERM)

    def mark_exporter(**kwargs: object) -> None:
        captured["mark_calls"] = int(captured["mark_calls"]) + 1
        captured["marked_identity"] = kwargs["identity"]

    monkeypatch.setattr(
        mediacrawler_crawl,
        "browser_launch_environment",
        reporter_environment,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "spawn_gated_subprocess",
        capture_spawn,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "register_lease_process_from_environment",
        register_exporter,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "mark_lease_process_exited_from_environment",
        mark_exporter,
    )
    monkeypatch.setattr(
        mediacrawler_crawl.GatedSubprocess,
        "release",
        release_then_interrupt,
    )
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, replayed)
    try:
        with pytest.raises(ReplayedSignal):
            mediacrawler_crawl.run_command(
                [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; import time; "
                        f"Path({str(target_side_effect)!r}).write_text('started'); "
                        "time.sleep(60)"
                    ),
                ],
                tmp_path,
                1,
                tmp_path / "registered-signal-logs",
                cleanup_grace_seconds=1,
            )
        assert captured["mark_calls"] == 1
        assert captured["marked_identity"] == captured["identity"]
        assert (
            mediacrawler_crawl.SystemProcessInspector().process_presence(
                int(captured["pid"])
            )
            is False
        )
        assert all(
            signal.getsignal(signum) is replayed
            for signum in (signal.SIGINT, signal.SIGTERM)
        )
    finally:
        for signum, handler in original_handlers.items():
            signal.signal(signum, handler)


def test_network_diagnostics_require_fresh_explicit_transport_recovery(
    tmp_path: Path,
) -> None:
    diagnostic_path = tmp_path / "behavior_evidence.navigation.json"

    def write_event(
        *,
        at: datetime,
        outcome: str = "network_paused",
        error: str = "ConnectTimeout: request failed",
        include_error: bool = True,
    ) -> None:
        event: dict[str, object] = {
            "at": at.isoformat(),
            "stage": "search",
            "outcome": outcome,
        }
        if include_error:
            event["error"] = error
        diagnostic_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "platform": "xhs",
                    "updated_at": at.isoformat(),
                    "events": [event],
                }
            ),
            encoding="utf-8",
        )

    write_event(
        at=NOW,
        error="ConnectTimeout: cookie=secret owner_token=forbidden",
    )
    assert mediacrawler_crawl.xhs_network_state_from_diagnostics(
        diagnostic_path,
        now=NOW,
    ) == ("network_paused", "transport_timeout")

    write_event(at=NOW, error="SMS Verification parameter error cookie=secret")
    assert mediacrawler_crawl.xhs_network_state_from_diagnostics(
        diagnostic_path,
        now=NOW,
    ) == ("unknown", "")

    write_event(at=NOW - timedelta(seconds=91))
    assert mediacrawler_crawl.xhs_network_state_from_diagnostics(
        diagnostic_path,
        now=NOW,
    ) == ("unknown", "")

    write_event(at=NOW, include_error=False)
    assert mediacrawler_crawl.xhs_network_state_from_diagnostics(
        diagnostic_path,
        now=NOW,
    ) == ("unknown", "")

    diagnostic_path.write_text("{", encoding="utf-8")
    assert mediacrawler_crawl.xhs_network_state_from_diagnostics(
        diagnostic_path,
        now=NOW,
    ) == ("unknown", "")


def test_network_timeout_diagnostic_is_fresh_explicit_terminal_observation(
    tmp_path: Path,
) -> None:
    diagnostic_path = tmp_path / "behavior_evidence.navigation.json"
    diagnostic_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "platform": "xhs",
                "updated_at": NOW.isoformat(),
                "events": [
                    {
                        "at": NOW.isoformat(),
                        "stage": "search",
                        "outcome": "network_recovery_timeout",
                        "error": "ConnectTimeout: request failed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert mediacrawler_crawl.xhs_network_state_from_diagnostics(
        diagnostic_path,
        now=NOW,
    ) == ("network_recovery_timeout", "transport_timeout")


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        ({"timeout_reason": "no_progress_timeout"}, "no_progress_timeout"),
        (
            {"timeout_reason": "parent_network_pause_timeout"},
            "parent_network_pause_timeout",
        ),
        (
            {"timeout_reason": "parent_network_terminal_unwind_timeout"},
            "parent_network_terminal_unwind_timeout",
        ),
        (
            {
                "timeout_reason": None,
                "network_terminal_observed": True,
                "network_terminal_reason": "network_recovery_timeout",
            },
            "network_recovery_timeout",
        ),
        (
            {
                "timeout_reason": None,
                "network_terminal_observed": False,
                "network_terminal_reason": "network_recovery_timeout",
            },
            "",
        ),
    ],
)
def test_runtime_watchdog_stop_detail_is_closed_and_explicit(
    run: dict[str, object],
    expected: str,
) -> None:
    assert mediacrawler_crawl.runtime_watchdog_stop_detail(run) == expected


def test_parent_network_pause_clock_freezes_remaining_without_resetting_it() -> None:
    clock = mediacrawler_crawl.XhsParentNetworkPauseClock(ceiling_seconds=10)

    assert clock.observe("network_paused", now=100) == (0.0, False)
    assert clock.observe("network_paused", now=104) == (4.0, False)
    # An untrusted gap neither earns credit nor impersonates recovery.
    assert clock.observe("unknown", now=107) == (0.0, False)
    assert clock.observe("network_paused", now=109) == (0.0, False)
    assert clock.observe("network_paused", now=115) == (6.0, True)
    assert clock.total_seconds == 10


def test_parent_network_pause_ceiling_resets_only_after_explicit_recovery() -> None:
    clock = mediacrawler_crawl.XhsParentNetworkPauseClock(ceiling_seconds=10)

    clock.observe("network_paused", now=0)
    assert clock.observe("network_paused", now=8) == (8.0, False)
    assert clock.observe("online", now=9) == (1.0, False)
    assert clock.observe("network_paused", now=20) == (0.0, False)
    assert clock.observe("network_paused", now=27) == (7.0, False)
    assert clock.total_seconds == 16


def test_network_diagnostics_never_count_as_business_progress(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    diagnostic_path = data_dir / "navigation.jsonl"
    business_path = data_dir / "xhs_contents.jsonl"
    diagnostic_path.write_text("diagnostic", encoding="utf-8")

    assert mediacrawler_crawl.progress_path_signature(
        [data_dir],
        excluded_paths=[diagnostic_path],
    ) == ()

    business_path.write_text("business", encoding="utf-8")
    signature = mediacrawler_crawl.progress_path_signature(
        [data_dir],
        excluded_paths=[diagnostic_path],
    )
    assert [Path(item[0]).name for item in signature] == ["xhs_contents.jsonl"]


def test_runtime_reporter_sequence_finalizing_and_no_background_keepalive(
    runtime_reporter: tuple[object, dict[str, Path], object],
) -> None:
    reporter, paths, identity = runtime_reporter
    assert reporter.checkpoint(
        phase="running",
        network_state="network_paused",
        network_reason="transport_timeout",
    )
    running = read_reporter_status(paths["status"], identity)
    assert running["sequence"] == 2
    assert running["network_reason"] == "transport_timeout"

    assert reporter.enter_finalizing()
    finalizing = read_reporter_status(paths["status"], identity)
    assert finalizing["sequence"] == 3
    assert finalizing["phase"] == "finalizing"
    assert finalizing["network_state"] == "unknown"
    assert not {
        "checkpoint",
        "completion_met",
        "source_exhausted",
    } & set(finalizing)

    time.sleep(0.03)
    unchanged = read_reporter_status(paths["status"], identity)
    assert unchanged["sequence"] == 3


def test_finalizing_summary_scan_keeps_advancing_authenticated_heartbeats(
    runtime_reporter: tuple[object, dict[str, Path], object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reporter, paths, identity = runtime_reporter
    reporter.enter_finalizing()
    data_dir = tmp_path / "xhs" / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "xhs_contents_1.jsonl").write_text(
        "\n".join(
            (
                json.dumps({"note_id": "one", "title": "青岛"}),
                "{malformed",
                json.dumps({"note_id": "two", "title": "青岛"}),
            )
        ),
        encoding="utf-8",
    )
    (data_dir / "image.jpg").write_bytes(b"image")
    clock = [0.0]

    def advancing_monotonic() -> float:
        clock[0] += 6.0
        return clock[0]

    monkeypatch.setattr(mediacrawler_crawl.time, "monotonic", advancing_monotonic)
    before = read_reporter_status(paths["status"], identity)["sequence"]

    output = mediacrawler_crawl.summarize_output(
        data_dir,
        "青岛",
        progress_callback=reporter.checkpoint,
    )

    after = read_reporter_status(paths["status"], identity)
    assert output["content_records"] == 3
    assert output["parse_errors"] == 1
    assert after["phase"] == "finalizing"
    assert int(after["sequence"]) >= int(before) + 6


def test_finalizing_summary_does_not_swallow_checkpoint_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "xhs" / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "xhs_contents_1.jsonl").write_text(
        json.dumps({"note_id": "one", "title": "青岛"}) + "\n",
        encoding="utf-8",
    )
    clock = [0.0]
    calls = [0]

    def advancing_monotonic() -> float:
        clock[0] += 6.0
        return clock[0]

    def fail_checkpoint() -> None:
        calls[0] += 1
        if calls[0] >= 2:
            raise mediacrawler_crawl.XhsRuntimeSupervisionError("synthetic_stale_writer")

    monkeypatch.setattr(mediacrawler_crawl.time, "monotonic", advancing_monotonic)

    with pytest.raises(
        mediacrawler_crawl.XhsRuntimeSupervisionError,
        match="synthetic_stale_writer",
    ):
        mediacrawler_crawl.summarize_output(
            data_dir,
            "青岛",
            progress_callback=fail_checkpoint,
        )
    assert calls[0] == 2


def test_streamed_summary_write_is_atomic_when_heartbeat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "summary.json"
    target.write_text('{"status":"old"}', encoding="utf-8")
    clock = [0.0]
    calls = [0]

    def advancing_monotonic() -> float:
        clock[0] += 6.0
        return clock[0]

    def fail_checkpoint() -> None:
        calls[0] += 1
        if calls[0] >= 2:
            raise mediacrawler_crawl.XhsRuntimeSupervisionError("heartbeat_write_failed")

    monkeypatch.setattr(mediacrawler_crawl.time, "monotonic", advancing_monotonic)
    payload = {"records": ["x" * (1024 * 1024 + 1), "tail"]}

    with pytest.raises(
        mediacrawler_crawl.XhsRuntimeSupervisionError,
        match="heartbeat_write_failed",
    ):
        mediacrawler_crawl.write_json_with_progress(
            target,
            payload,
            progress_callback=fail_checkpoint,
        )

    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "old"}
    assert not (tmp_path / f".summary.json.{os.getpid()}.tmp").exists()


def test_large_execution_state_read_propagates_heartbeat_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "execution.json"
    state_path.write_text(
        json.dumps({"events": [], "padding": "x" * (2 * 1024 * 1024)}),
        encoding="utf-8",
    )
    clock = [0.0]
    calls = [0]

    def advancing_monotonic() -> float:
        clock[0] += 6.0
        return clock[0]

    def fail_checkpoint() -> None:
        calls[0] += 1
        if calls[0] >= 2:
            raise mediacrawler_crawl.XhsRuntimeSupervisionError("state_heartbeat_failed")

    monkeypatch.setattr(mediacrawler_crawl.time, "monotonic", advancing_monotonic)

    with pytest.raises(
        mediacrawler_crawl.XhsRuntimeSupervisionError,
        match="state_heartbeat_failed",
    ):
        mediacrawler_crawl.load_pagination_evidence(
            state_path,
            progress_callback=fail_checkpoint,
        )
    assert calls[0] == 2


def test_terminal_stdout_envelope_never_duplicates_large_summary_payload(
    tmp_path: Path,
) -> None:
    summary = {
        "import_completion_met": False,
        "failure_reason": "network_recovery_timeout",
        "records": [{"large": "x" * 100_000}],
        "import_result": {"rows": list(range(10_000))},
    }

    envelope = mediacrawler_crawl.terminal_summary_envelope(
        summary_path=tmp_path / "summary.json",
        report_path=tmp_path / "summary.md",
        batch_dir=tmp_path,
        summary=summary,
    )

    assert envelope == {
        "summary": str(tmp_path / "summary.json"),
        "report": str(tmp_path / "summary.md"),
        "batch_dir": str(tmp_path),
        "status": "failed",
        "import_completion_met": False,
        "failure_reason": "network_recovery_timeout",
    }
    assert "records" not in envelope
    assert "import_result" not in envelope


def test_runtime_status_write_failure_is_terminal(
    runtime_reporter: tuple[object, dict[str, Path], object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reporter, paths, identity = runtime_reporter

    def fail_write(*_args: object, **_kwargs: object) -> object:
        raise OSError("synthetic write failure")

    monkeypatch.setattr(mediacrawler_crawl, "write_runtime_status_atomic", fail_write)
    with pytest.raises(
        mediacrawler_crawl.XhsRuntimeSupervisionError,
        match="runtime_status_write_failed",
    ):
        reporter.checkpoint(phase="running")

    status = read_reporter_status(paths["status"], identity)
    assert status["sequence"] == 1
    assert reporter.snapshot()["write_failures"] == 1


@pytest.mark.macos_process
def test_exporter_start_token_change_stops_heartbeat_and_terminates_process(
    runtime_reporter: tuple[object, dict[str, Path], object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reporter, paths, writer_identity = runtime_reporter
    registered: dict[str, object] = {}
    original_popen = mediacrawler_crawl.subprocess.Popen

    def capture_popen(*args: object, **kwargs: object) -> object:
        registered["child_env"] = dict(kwargs["env"])
        return original_popen(*args, **kwargs)

    def register_exporter(
        *,
        pid: int,
        environ: dict[str, str],
        **_kwargs: object,
    ) -> object:
        identity = process_identity(pid=pid, process_start_token="exporter-original")
        registered["identity"] = identity
        registered["registration_env"] = dict(environ)
        return identity

    def mark_exporter_exited(
        *,
        identity: object,
        environ: dict[str, str],
        **_kwargs: object,
    ) -> None:
        registered["marked_identity"] = identity
        registered["exit_env"] = dict(environ)

    class ChangedExporterInspector:
        def identity(self, pid: int) -> object:
            return process_identity(pid=pid, process_start_token="exporter-reused")

    monkeypatch.setattr(
        mediacrawler_crawl,
        "browser_launch_environment",
        reporter_environment,
    )
    monkeypatch.setattr(
        mediacrawler_crawl.subprocess,
        "Popen",
        capture_popen,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "register_lease_process_from_environment",
        register_exporter,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "mark_lease_process_exited_from_environment",
        mark_exporter_exited,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "SystemProcessInspector",
        ChangedExporterInspector,
    )
    started = time.monotonic()

    with pytest.raises(
        mediacrawler_crawl.XhsRuntimeSupervisionError,
        match="exporter_process_identity_changed",
    ):
        mediacrawler_crawl.run_command(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            tmp_path,
            5,
            tmp_path / "identity-logs",
            progress_paths=[],
            runtime_reporter=reporter,
            poll_seconds=0.02,
            cleanup_grace_seconds=1,
        )

    assert registered["identity"].process_start_token == "exporter-original"
    child_env = registered["child_env"]
    registration_env = registered["registration_env"]
    exit_env = registered["exit_env"]
    assert isinstance(child_env, dict)
    assert isinstance(registration_env, dict)
    assert isinstance(exit_env, dict)
    assert not set(PRIVATE_EXPORTER_ENV_KEYS) & set(child_env)
    for key, expected in reporter_environment(auth_key=None).items():
        assert registration_env[key] == expected
        assert exit_env[key] == expected
    assert registered["marked_identity"] == registered["identity"]
    assert time.monotonic() - started < 2
    assert read_reporter_status(paths["status"], writer_identity)["sequence"] == 1


@pytest.mark.macos_process
def test_no_progress_expiry_round_does_not_emit_another_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    exporter_identity = process_identity(pid=9911, process_start_token="exporter-1")
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 9911
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def communicate(self, *, timeout: float) -> tuple[bytes, bytes]:
            raise subprocess.TimeoutExpired(["fake"], timeout)

    class CountingReporter:
        def __init__(self, status_path: Path) -> None:
            self.calls = 0
            self.status_path = status_path

        def checkpoint(
            self,
            *,
            phase: str,
            network_state: str,
            network_reason: str,
        ) -> bool:
            assert phase == "running"
            assert network_state == "unknown"
            assert network_reason == ""
            self.calls += 1
            return True

        def snapshot(self) -> dict[str, object]:
            return {"enabled": True, "calls": self.calls}

    fake_process = FakeProcess()
    reporter = CountingReporter(tmp_path / "runtime_status.json")
    monotonic_values = iter((0.0, 0.0, 1.0, 1.0))

    def terminate(
        proc: FakeProcess,
        *,
        grace_seconds: float,
    ) -> tuple[bytes, bytes, bool]:
        assert grace_seconds == 1
        proc.returncode = -15
        return b"", b"", False

    class FakeGate:
        def __init__(self, process: FakeProcess) -> None:
            self.process = process

        def release(self) -> None:
            captured["released"] = True

        def cancel(self, *, grace_seconds: float) -> None:
            terminate(self.process, grace_seconds=grace_seconds)

    def spawn(*_args: object, **kwargs: object) -> FakeGate:
        captured["child_env"] = dict(kwargs["env"])
        return FakeGate(fake_process)

    def register_exporter(
        *,
        environ: dict[str, str],
        **_kwargs: object,
    ) -> object:
        captured["registration_env"] = dict(environ)
        return exporter_identity

    def mark_exporter_exited(
        *,
        identity: object,
        environ: dict[str, str],
        **_kwargs: object,
    ) -> None:
        captured["marked_identity"] = identity
        captured["exit_env"] = dict(environ)

    monkeypatch.setattr(
        mediacrawler_crawl,
        "browser_launch_environment",
        reporter_environment,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "spawn_gated_subprocess",
        spawn,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "register_lease_process_from_environment",
        register_exporter,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "mark_lease_process_exited_from_environment",
        mark_exporter_exited,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "SystemProcessInspector",
        lambda: StaticInspector(exporter_identity),
    )
    monkeypatch.setattr(mediacrawler_crawl, "terminate_managed_process", terminate)
    monkeypatch.setattr(
        mediacrawler_crawl.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    result = mediacrawler_crawl.run_command(
        ["fake"],
        tmp_path,
        0.5,
        tmp_path / "expiry-logs",
        progress_paths=[reporter.status_path],
        runtime_reporter=reporter,
        poll_seconds=0.1,
        cleanup_grace_seconds=1,
    )

    assert result["returncode"] == 124
    assert result["timeout_reason"] == "no_progress_timeout"
    assert result["progress_observed"] is False
    assert reporter.calls == 1
    child_env = captured["child_env"]
    registration_env = captured["registration_env"]
    exit_env = captured["exit_env"]
    assert isinstance(child_env, dict)
    assert isinstance(registration_env, dict)
    assert isinstance(exit_env, dict)
    assert not set(PRIVATE_EXPORTER_ENV_KEYS) & set(child_env)
    for key, expected in reporter_environment(auth_key=None).items():
        assert registration_env[key] == expected
        assert exit_env[key] == expected
    assert captured["marked_identity"] == exporter_identity


def test_runtime_reporter_requires_progress_tracking_before_popen(
    runtime_reporter: tuple[object, dict[str, Path], object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reporter, _, _ = runtime_reporter
    monkeypatch.setattr(
        mediacrawler_crawl,
        "browser_launch_environment",
        reporter_environment,
    )

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Popen must not run without progress paths")

    monkeypatch.setattr(mediacrawler_crawl.subprocess, "Popen", forbidden)
    with pytest.raises(
        mediacrawler_crawl.XhsRuntimeSupervisionError,
        match="requires progress tracking",
    ):
        mediacrawler_crawl.run_command(
            ["fake"],
            tmp_path,
            1,
            tmp_path / "missing-progress-logs",
            runtime_reporter=reporter,
        )


@pytest.mark.macos_process
def test_child_exit_then_main_control_flow_enters_finalizing(
    runtime_reporter: tuple[object, dict[str, Path], object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reporter, paths, writer_identity = runtime_reporter
    registered: dict[str, object] = {}

    def register_exporter(*, pid: int, **_kwargs: object) -> object:
        identity = process_identity(pid=pid, process_start_token="exporter-live")
        registered["identity"] = identity
        return identity

    class StableExporterInspector:
        def identity(self, _pid: int) -> object:
            return registered.get("identity")

    monkeypatch.setattr(
        mediacrawler_crawl,
        "browser_launch_environment",
        reporter_environment,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "register_lease_process_from_environment",
        register_exporter,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "mark_lease_process_exited_from_environment",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "SystemProcessInspector",
        StableExporterInspector,
    )

    result = mediacrawler_crawl.run_command(
        [sys.executable, "-c", "import time; time.sleep(0.08)"],
        tmp_path,
        1,
        tmp_path / "child-exit-logs",
        progress_paths=[],
        runtime_reporter=reporter,
        poll_seconds=0.01,
        cleanup_grace_seconds=1,
    )
    running = read_reporter_status(paths["status"], writer_identity)
    assert result["returncode"] == 0
    assert running["phase"] == "running"

    reporter.enter_finalizing()
    finalizing = read_reporter_status(paths["status"], writer_identity)
    assert finalizing["sequence"] > running["sequence"]
    assert finalizing["phase"] == "finalizing"


@pytest.mark.macos_process
def test_real_child_survives_no_progress_budget_during_network_pause_then_recovers(
    runtime_reporter: tuple[object, dict[str, Path], object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reporter, _, _ = runtime_reporter
    configure_real_supervised_exporter(monkeypatch)
    ready = tmp_path / "exporter-ready"
    recovered = tmp_path / "network-recovered"
    pause_started_at = None
    # 验证暂停不消耗预算，不承诺托管 VM 在 100ms 内完成真实进程调度。
    # 精确扣时和恢复边界由上面的纯时钟测试覆盖。
    inactivity_budget = 1.0

    def network_observation(_path: object) -> tuple[str, str]:
        nonlocal pause_started_at
        if not ready.exists():
            return "network_paused", "transport_timeout"
        if pause_started_at is None:
            pause_started_at = time.monotonic()
        if time.monotonic() - pause_started_at < 3 * inactivity_budget:
            return "network_paused", "transport_timeout"
        recovered.touch()
        return "online", ""

    monkeypatch.setattr(
        mediacrawler_crawl,
        "xhs_network_state_from_diagnostics",
        network_observation,
    )

    result = mediacrawler_crawl.run_command(
        [
            sys.executable,
            "-c",
            "from pathlib import Path\n"
            "import sys, time\n"
            "Path(sys.argv[1]).touch()\n"
            "deadline = time.monotonic() + 15\n"
            "while not Path(sys.argv[2]).exists():\n"
            "    if time.monotonic() >= deadline:\n"
            "        raise SystemExit(2)\n"
            "    time.sleep(0.005)\n"
            "time.sleep(0.02)\n"
            "print('recovered')\n",
            str(ready),
            str(recovered),
        ],
        tmp_path,
        inactivity_budget,
        tmp_path / "pause-recovery-logs",
        progress_paths=[ready],
        runtime_reporter=reporter,
        poll_seconds=0.01,
        cleanup_grace_seconds=1,
    )

    assert result["returncode"] == 0
    assert result["timed_out"] is False
    assert recovered.exists()
    assert result["stdout_tail"].strip() == "recovered"
    assert result["network_pause_observed"] is True
    assert result["network_pause_total_seconds"] > inactivity_budget


@pytest.mark.macos_process
def test_network_timeout_event_gets_one_fixed_unwind_grace_for_real_child(
    runtime_reporter: tuple[object, dict[str, Path], object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reporter, _, _ = runtime_reporter
    configure_real_supervised_exporter(monkeypatch)
    monkeypatch.setattr(
        mediacrawler_crawl,
        "XHS_CHILD_NETWORK_TERMINAL_GRACE_SECONDS",
        0.2,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "xhs_network_state_from_diagnostics",
        lambda _path: ("network_recovery_timeout", "transport_timeout"),
    )

    result = mediacrawler_crawl.run_command(
        [sys.executable, "-c", "import sys, time; time.sleep(0.08); sys.exit(7)"],
        tmp_path,
        0.01,
        tmp_path / "terminal-unwind-logs",
        progress_paths=[],
        runtime_reporter=reporter,
        poll_seconds=0.01,
        cleanup_grace_seconds=1,
    )

    assert result["returncode"] == 7
    assert result["timed_out"] is False
    assert result["timeout_reason"] is None
    assert result["elapsed_seconds"] >= 0.07
    assert result["network_terminal_observed"] is True
    assert result["network_terminal_reason"] == "network_recovery_timeout"


@pytest.mark.macos_process
def test_repeated_network_timeout_event_cannot_extend_unwind_grace(
    runtime_reporter: tuple[object, dict[str, Path], object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reporter, _, _ = runtime_reporter
    configure_real_supervised_exporter(monkeypatch)
    monkeypatch.setattr(
        mediacrawler_crawl,
        "XHS_CHILD_NETWORK_TERMINAL_GRACE_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "xhs_network_state_from_diagnostics",
        lambda _path: ("network_recovery_timeout", "transport_timeout"),
    )

    result = mediacrawler_crawl.run_command(
        [sys.executable, "-c", "import time; time.sleep(1)"],
        tmp_path,
        0.01,
        tmp_path / "terminal-unwind-timeout-logs",
        progress_paths=[],
        runtime_reporter=reporter,
        poll_seconds=0.01,
        cleanup_grace_seconds=1,
    )

    assert result["returncode"] == 124
    assert result["timed_out"] is True
    assert result["timeout_reason"] == "parent_network_terminal_unwind_timeout"
    assert result["elapsed_seconds"] < 0.5


@pytest.mark.macos_process
def test_durable_progress_allows_runtime_longer_than_watchdog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(mediacrawler_crawl, "browser_launch_environment", lambda: {})
    progress_path = tmp_path / "progress.json"
    child = (
        "from pathlib import Path; import sys, time; "
        "path = Path(sys.argv[1]); "
        "[(path.write_text(str(index)), time.sleep(0.1)) for index in range(7)]"
    )

    result = mediacrawler_crawl.run_command(
        [sys.executable, "-c", child, str(progress_path)],
        tmp_path,
        0.3,
        tmp_path / "logs",
        progress_paths=[progress_path],
        poll_seconds=0.02,
        cleanup_grace_seconds=1.0,
    )

    assert result["returncode"] == 0
    assert result["timed_out"] is False
    assert result["progress_observed"] is True
    assert result["elapsed_seconds"] > result["inactivity_timeout_seconds"]


@pytest.mark.macos_process
def test_no_progress_timeout_preserves_output_once_and_allows_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(mediacrawler_crawl, "browser_launch_environment", lambda: {})
    child = (
        "import signal, sys, time; "
        "signal.signal(signal.SIGTERM, lambda *_: (print('terminated', flush=True), sys.exit(0))); "
        "print('once', flush=True); time.sleep(10)"
    )

    result = mediacrawler_crawl.run_command(
        [sys.executable, "-c", child],
        tmp_path,
        0.15,
        tmp_path / "logs",
        progress_paths=[],
        poll_seconds=0.02,
        cleanup_grace_seconds=1.0,
    )

    stdout = (tmp_path / "logs" / "stdout.log").read_text(encoding="utf-8")
    assert result["returncode"] == 124
    assert result["timed_out"] is True
    assert result["timeout_reason"] == "no_progress_timeout"
    assert result["forced_termination"] is False
    assert stdout.count("once") == 1
    assert stdout.count("terminated") == 1


@pytest.mark.parametrize(
    "timeout_reason",
    [
        "no_progress_timeout",
        "parent_network_pause_timeout",
        "parent_network_terminal_unwind_timeout",
    ],
)
def test_timeout_with_staged_records_remains_runtime_failure(
    timeout_reason: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", raising=False)
    monkeypatch.setattr(
        mediacrawler_crawl,
        "behavior_environment",
        lambda *_: {},
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "load_behavior_evidence",
        lambda *_: {"status": "completed"},
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "behavior_evidence_valid",
        lambda *_: True,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "summarize_output",
        lambda *_: {
            "parse_errors": 0,
            "content_records": 5,
            "non_video_content_records": 5,
            "video_like_records": 0,
        },
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "run_command",
        lambda *_args, **_kwargs: {
            "returncode": 124,
            "timed_out": True,
            "timeout_reason": timeout_reason,
            "last_progress_age_seconds": 1200,
        },
    )
    args = SimpleNamespace(
        download_images=False,
        login_type="cookie",
        keyword="青岛太平角旅游",
        headed=False,
        start_page=42,
        start_offset=None,
        start_cursor="",
        db=str(tmp_path / "content.sqlite"),
        behavior_profile="social_high_risk",
        discovery_job_id=None,
        resume_identities_path=None,
        timeout_per_platform=1200,
    )

    result = mediacrawler_crawl._run_platform_without_policy(
        "weibo",
        args,
        tmp_path,
    )

    assert result["status"] == "runtime_failed"
    assert result["ok"] is False
    assert result["run"]["timeout_state_event"] == {
        "skipped": True,
        "reason": "execution_state_unavailable",
    }


def test_no_progress_timeout_appends_incomplete_terminal_audit_event(
    tmp_path: Path,
) -> None:
    frozen_input = tmp_path / "config.json"
    frozen_input.write_text("{}\n", encoding="utf-8")
    state_path = tmp_path / "state.json"
    state = FrozenExecutionState.create(
        state_path,
        run_id="run-1",
        job_key="job-1",
        site_key="weibo",
        job_kind="mediacrawler_search",
        plan={"keyword": "青岛太平角旅游"},
        frozen_inputs=[frozen_input],
    )
    state.append_event(
        "adaptive_batch_completed",
        {
            "platform": "weibo",
            "candidate_count": 150,
            "source_page": 41,
            "resume_page": 42,
            "source_has_more": True,
            "batch_complete": True,
            "stop_reason": "continue",
            "candidate_identities": ["post-1"],
        },
    )

    result = mediacrawler_crawl.append_runtime_watchdog_stop_event(
        state_path,
        timeout_reason="no_progress_timeout",
        platform_key="weibo",
        start_page=18,
        start_offset=None,
        start_cursor=None,
        inactivity_timeout_seconds=7200,
        last_progress_age_seconds=7200.25,
    )

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    terminal = payload["events"][-1]
    assert result["skipped"] is False
    assert terminal["type"] == "adaptive_search_stopped"
    assert terminal["details"]["stop_reason"] == "runtime_failed"
    assert terminal["details"]["stop_detail"] == "no_progress_timeout"
    assert terminal["details"]["resume_page"] == 42
    assert terminal["details"]["batch_complete"] is False
    assert terminal["details"]["candidate_identities"] == ["post-1"]


@pytest.mark.parametrize(
    "timeout_reason",
    [
        "network_recovery_timeout",
        "parent_network_pause_timeout",
        "parent_network_terminal_unwind_timeout",
    ],
)
def test_network_watchdog_stop_appends_exact_runtime_failed_terminal_event(
    timeout_reason: str,
    tmp_path: Path,
) -> None:
    frozen_input = tmp_path / "config.json"
    frozen_input.write_text("{}\n", encoding="utf-8")
    state_path = tmp_path / "state.json"
    FrozenExecutionState.create(
        state_path,
        run_id="run-network",
        job_key="job-network",
        site_key="xhs",
        job_kind="xhs_account_search",
        plan={"keyword": "青岛太平角旅游"},
        frozen_inputs=[frozen_input],
    )

    result = mediacrawler_crawl.append_runtime_watchdog_stop_event(
        state_path,
        timeout_reason=timeout_reason,
        platform_key="xhs",
        start_page=18,
        start_offset=None,
        start_cursor="search-id",
        inactivity_timeout_seconds=7200,
        last_progress_age_seconds=7199,
        network_pause_total_seconds=675,
        network_pause_ceiling_seconds=675,
        network_terminal_grace_seconds=20,
    )

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    terminal = payload["events"][-1]
    assert result["skipped"] is False
    assert terminal["type"] == "adaptive_search_stopped"
    assert terminal["details"]["stop_reason"] == "runtime_failed"
    assert terminal["details"]["stop_detail"] == timeout_reason
    assert terminal["details"]["resume_page"] == 18
    assert terminal["details"]["resume_cursor"] == "search-id"
    assert terminal["details"]["batch_complete"] is False
    assert terminal["details"]["network_pause_ceiling_seconds"] == 675
    assert terminal["details"]["network_terminal_grace_seconds"] == 20


def test_watchdog_does_not_replace_existing_terminal_event(tmp_path: Path) -> None:
    frozen_input = tmp_path / "config.json"
    frozen_input.write_text("{}\n", encoding="utf-8")
    state_path = tmp_path / "state.json"
    state = FrozenExecutionState.create(
        state_path,
        run_id="run-2",
        job_key="job-2",
        site_key="weibo",
        job_kind="mediacrawler_search",
        plan={"keyword": "青岛太平角旅游"},
        frozen_inputs=[frozen_input],
    )
    state.append_event(
        "adaptive_search_stopped",
        {
            "platform": "weibo",
            "stop_reason": "source_exhausted",
            "stop_detail": "empty_page",
            "source_page": 50,
            "resume_page": 50,
            "source_has_more": False,
            "batch_complete": True,
        },
    )

    result = mediacrawler_crawl.append_runtime_watchdog_stop_event(
        state_path,
        timeout_reason="no_progress_timeout",
        platform_key="weibo",
        start_page=18,
        start_offset=None,
        start_cursor=None,
        inactivity_timeout_seconds=7200,
        last_progress_age_seconds=7200,
    )

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert result == {"skipped": True, "reason": "terminal_event_already_present"}
    assert len(payload["events"]) == 1
    assert payload["events"][0]["details"]["stop_reason"] == "source_exhausted"
