"""Strict boundary tests for the ephemeral XHS watchdog status."""

from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from trippostcollect.xhs import runtime
from trippostcollect.xhs.leases import ProcessIdentity


NOW = datetime(2026, 9, 3, 6, 0, 0, tzinfo=timezone.utc)
AUTH_KEY = b"runtime-status-test-key-material-32"
WRONG_AUTH_KEY = b"wrong-runtime-status-key-material-32"
SUPERVISOR = ProcessIdentity(
    host_id="host-a",
    boot_id="boot-a",
    pid=1234,
    process_started_at="2026-09-03T05:59:00.000000+00:00",
    process_start_token="darwin:100:200",
    pgid=1234,
)


@pytest.fixture
def session_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Path]:
    monkeypatch.setattr(runtime, "XHS_SESSION_ROOT", tmp_path / "sessions")
    paths = runtime.prepare_runtime_session("run-1")
    return {**paths, "status": runtime.runtime_status_path("run-1")}


def unsigned_status_payload(
    *,
    sequence: int = 1,
    heartbeat_at: datetime | str = NOW,
    network_state: str = "online",
    network_reason: str = "",
    supervisor: ProcessIdentity = SUPERVISOR,
) -> dict[str, object]:
    heartbeat = (
        heartbeat_at.isoformat()
        if isinstance(heartbeat_at, datetime)
        else heartbeat_at
    )
    return {
        "schema_version": runtime.RUNTIME_STATUS_SCHEMA_VERSION,
        "run_id": "run-1",
        "account_id": "xhs-a01",
        "lease_id": "lease-1",
        "writer_role": "mediacrawler_supervisor",
        "writer_host_id": supervisor.host_id,
        "writer_boot_id": supervisor.boot_id,
        "writer_pid": supervisor.pid,
        "writer_process_started_at": supervisor.process_started_at,
        "writer_process_start_token": supervisor.process_start_token,
        "writer_pgid": supervisor.pgid,
        "sequence": sequence,
        "heartbeat_at": heartbeat,
        "phase": "running",
        "network_state": network_state,
        "network_reason": network_reason,
    }


def status_payload(**kwargs: object) -> dict[str, object]:
    return runtime.sign_runtime_status(
        unsigned_status_payload(**kwargs),
        auth_key=AUTH_KEY,
    )


def read_status(
    path: Path,
    *,
    auth_key: bytes = AUTH_KEY,
    supervisor: ProcessIdentity = SUPERVISOR,
    previous_sequence: int | None = None,
) -> dict[str, object]:
    return runtime.read_runtime_status(
        path,
        auth_key=auth_key,
        expected_run_id="run-1",
        expected_account_id="xhs-a01",
        expected_lease_id="lease-1",
        expected_writer_identity=supervisor,
        previous_sequence=previous_sequence,
    )


def write_status(path: Path, **kwargs: object) -> Path:
    return runtime.write_runtime_status_atomic(
        path,
        status_payload(**kwargs),
        auth_key=AUTH_KEY,
    )


def test_runtime_status_is_fixed_atomic_authenticated_0600_and_run_scoped(
    session_paths: dict[str, Path],
) -> None:
    assert set(runtime.runtime_session_paths("run-1")) == {"root", "profile"}
    assert not session_paths["status"].exists()

    written = write_status(session_paths["status"])

    assert written == session_paths["status"].absolute()
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert not list(session_paths["root"].glob(".runtime_status.json.*.tmp"))
    observed = read_status(written)
    assert observed["sequence"] == 1
    assert observed["auth_tag"] == json.loads(
        written.read_text(encoding="utf-8")
    )["auth_tag"]
    assert len(str(observed["auth_tag"])) == 64
    assert "owner_token" not in observed


def test_runtime_session_removal_naturally_removes_status(
    session_paths: dict[str, Path],
) -> None:
    write_status(session_paths["status"])

    assert runtime.remove_runtime_session(session_paths["root"]) is True
    assert not session_paths["root"].exists()


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "owner_token",
        "auth_key",
        "cookie",
        "storage_state",
        "checkpoint",
        "resume_page",
        "completion_met",
        "source_exhausted",
    ],
)
def test_runtime_status_rejects_secret_checkpoint_and_completion_fields(
    forbidden_field: str,
) -> None:
    payload = status_payload()
    payload[forbidden_field] = "forbidden"

    with pytest.raises(
        runtime.RuntimeStatusValidationError,
        match="unexpected",
    ):
        runtime.validate_runtime_status(payload, auth_key=AUTH_KEY)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 1, "schema_version"),
        ("run_id", "../escape", "run_id"),
        ("account_id", "account-1", "account_id"),
        ("lease_id", "../lease", "lease_id"),
        ("writer_role", "browser", "writer_role"),
        ("writer_host_id", "", "host_id"),
        ("writer_boot_id", "line\nbreak", "boot_id"),
        ("writer_pid", 0, "writer_pid"),
        ("writer_process_started_at", "2026-09-03T05:59:00", "timezone-aware"),
        ("writer_process_start_token", "", "process_start_token"),
        ("writer_pgid", 0, "writer_pgid"),
        ("sequence", 0, "sequence"),
        ("phase", "completed", "phase"),
        ("network_state", "source_exhausted", "network_state"),
    ],
)
def test_runtime_status_rejects_invalid_identity_and_enums(
    field: str,
    value: object,
    message: str,
) -> None:
    payload = unsigned_status_payload()
    payload[field] = value

    with pytest.raises(runtime.RuntimeStatusValidationError, match=message):
        runtime.sign_runtime_status(payload, auth_key=AUTH_KEY)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host_id", "host-b"),
        ("boot_id", "boot-b"),
        ("pid", 4321),
        ("process_started_at", "2026-09-03T05:59:01+00:00"),
        ("process_start_token", "darwin:100:201"),
        ("pgid", 4321),
    ],
)
def test_reader_requires_every_exact_supervisor_identity_scalar(
    session_paths: dict[str, Path],
    field: str,
    value: object,
) -> None:
    write_status(session_paths["status"])
    expected = SUPERVISOR.public()
    expected[field] = value

    with pytest.raises(
        runtime.RuntimeStatusValidationError,
        match="exact supervisor process",
    ):
        runtime.read_runtime_status(
            session_paths["status"],
            auth_key=AUTH_KEY,
            expected_run_id="run-1",
            expected_writer_identity=expected,
        )


def test_reader_rejects_pid_reuse_with_same_pid_and_new_start_token(
    session_paths: dict[str, Path],
) -> None:
    write_status(session_paths["status"])
    reused = ProcessIdentity(
        **{
            **SUPERVISOR.public(),
            "process_start_token": "darwin:999:999",
        }
    )

    with pytest.raises(
        runtime.RuntimeStatusValidationError,
        match="exact supervisor process",
    ):
        read_status(session_paths["status"], supervisor=reused)


def test_reader_rejects_partial_expected_process_identity(
    session_paths: dict[str, Path],
) -> None:
    write_status(session_paths["status"])

    with pytest.raises(
        runtime.RuntimeStatusValidationError,
        match="every exact ProcessIdentity scalar",
    ):
        runtime.read_runtime_status(
            session_paths["status"],
            auth_key=AUTH_KEY,
            expected_run_id="run-1",
            expected_writer_identity={"pid": SUPERVISOR.pid},
        )


@pytest.mark.parametrize(
    ("expected_field", "expected_value"),
    [
        ("expected_run_id", "run-2"),
        ("expected_account_id", "xhs-a02"),
        ("expected_lease_id", "lease-2"),
    ],
)
def test_runtime_status_requires_exact_run_identity(
    expected_field: str,
    expected_value: str,
) -> None:
    with pytest.raises(
        runtime.RuntimeStatusValidationError,
        match="exact run identity",
    ):
        runtime.validate_runtime_status(
            status_payload(),
            auth_key=AUTH_KEY,
            **{expected_field: expected_value},
        )


def test_sequence_is_strict_but_heartbeat_wall_clock_is_audit_only(
    session_paths: dict[str, Path],
) -> None:
    write_status(session_paths["status"], sequence=1, heartbeat_at=NOW)

    repeated_sequence = status_payload(
        sequence=1,
        heartbeat_at=NOW + timedelta(days=10),
    )
    with pytest.raises(runtime.RuntimeStatusValidationError, match="sequence"):
        runtime.write_runtime_status_atomic(
            session_paths["status"],
            repeated_sequence,
            auth_key=AUTH_KEY,
        )

    write_status(session_paths["status"], sequence=2, heartbeat_at=NOW)
    write_status(
        session_paths["status"],
        sequence=3,
        heartbeat_at=NOW - timedelta(days=30),
    )
    write_status(
        session_paths["status"],
        sequence=4,
        heartbeat_at=NOW + timedelta(days=30),
    )
    assert read_status(session_paths["status"], previous_sequence=3)[
        "sequence"
    ] == 4


def test_heartbeat_at_still_requires_an_aware_timestamp() -> None:
    payload = unsigned_status_payload(heartbeat_at="2026-09-03T06:00:00")
    with pytest.raises(runtime.RuntimeStatusValidationError, match="timezone-aware"):
        runtime.sign_runtime_status(payload, auth_key=AUTH_KEY)


def test_runtime_status_requires_auth_key_and_rejects_wrong_key(
    session_paths: dict[str, Path],
) -> None:
    unsigned = unsigned_status_payload()
    with pytest.raises(runtime.RuntimeStatusValidationError, match="authentication key"):
        runtime.sign_runtime_status(unsigned, auth_key=None)  # type: ignore[arg-type]

    payload = status_payload()
    with pytest.raises(runtime.RuntimeStatusValidationError, match="authentication key"):
        runtime.write_runtime_status_atomic(
            session_paths["status"],
            payload,
            auth_key=None,  # type: ignore[arg-type]
        )
    with pytest.raises(runtime.RuntimeStatusValidationError, match="authentication failed"):
        runtime.write_runtime_status_atomic(
            session_paths["status"],
            payload,
            auth_key=WRONG_AUTH_KEY,
        )

    runtime.write_runtime_status_atomic(
        session_paths["status"],
        payload,
        auth_key=AUTH_KEY,
    )
    with pytest.raises(runtime.RuntimeStatusValidationError, match="authentication key"):
        runtime.read_runtime_status(
            session_paths["status"],
            auth_key=None,  # type: ignore[arg-type]
            expected_run_id="run-1",
            expected_writer_identity=SUPERVISOR,
        )
    with pytest.raises(runtime.RuntimeStatusValidationError, match="authentication failed"):
        read_status(session_paths["status"], auth_key=WRONG_AUTH_KEY)


def test_runtime_status_rejects_bad_tag_and_signed_field_tampering(
    session_paths: dict[str, Path],
) -> None:
    bad_tag = status_payload()
    bad_tag["auth_tag"] = "0" * 63
    with pytest.raises(runtime.RuntimeStatusValidationError, match="64 lowercase"):
        runtime.write_runtime_status_atomic(
            session_paths["status"],
            bad_tag,
            auth_key=AUTH_KEY,
        )

    tampered = status_payload()
    tampered["phase"] = "finalizing"
    with pytest.raises(runtime.RuntimeStatusValidationError, match="authentication failed"):
        runtime.write_runtime_status_atomic(
            session_paths["status"],
            tampered,
            auth_key=AUTH_KEY,
        )

    write_status(session_paths["status"])
    on_disk = json.loads(session_paths["status"].read_text(encoding="utf-8"))
    on_disk["network_state"] = "unknown"
    session_paths["status"].write_text(
        json.dumps(on_disk),
        encoding="utf-8",
    )
    with pytest.raises(runtime.RuntimeStatusValidationError, match="authentication failed"):
        read_status(session_paths["status"])


@pytest.mark.parametrize(
    ("network_state", "network_reason", "message"),
    [
        ("network_paused", "", "requires a sanitized reason"),
        ("online", "dns_unreachable", "only allowed"),
        ("network_paused", "cookie was rejected", "sensitive material"),
        ("network_paused", "line one\nline two", "single-line"),
    ],
)
def test_runtime_status_restricts_network_reason(
    network_state: str,
    network_reason: str,
    message: str,
) -> None:
    with pytest.raises(runtime.RuntimeStatusValidationError, match=message):
        runtime.sign_runtime_status(
            unsigned_status_payload(
                network_state=network_state,
                network_reason=network_reason,
            ),
            auth_key=AUTH_KEY,
        )


def test_runtime_status_rejects_wrong_path_symlink_and_loose_mode(
    session_paths: dict[str, Path],
    tmp_path: Path,
) -> None:
    with pytest.raises(runtime.RuntimeStatusValidationError, match="fixed file"):
        runtime.write_runtime_status_atomic(
            tmp_path / "runtime_status.json",
            status_payload(),
            auth_key=AUTH_KEY,
        )

    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(status_payload()), encoding="utf-8")
    outside.chmod(0o600)
    session_paths["status"].symlink_to(outside)
    with pytest.raises(runtime.RuntimeStatusValidationError, match="cannot be read"):
        read_status(session_paths["status"])
    session_paths["status"].unlink()

    write_status(session_paths["status"])
    session_paths["status"].chmod(0o640)
    with pytest.raises(runtime.RuntimeStatusValidationError, match="0600"):
        read_status(session_paths["status"])


@pytest.mark.skipif(os.name != "posix", reason="POSIX special files required")
def test_fifo_is_rejected_without_blocking(session_paths: dict[str, Path]) -> None:
    os.mkfifo(session_paths["status"], mode=0o600)
    started = time.monotonic()

    with pytest.raises(runtime.RuntimeStatusValidationError, match="regular file"):
        read_status(session_paths["status"])

    assert time.monotonic() - started < 1.0


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket required")
def test_socket_is_rejected_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="xhsrt-", dir="/tmp") as temporary:
        monkeypatch.setattr(runtime, "XHS_SESSION_ROOT", Path(temporary) / "s")
        paths = runtime.prepare_runtime_session("run-1")
        status_path = runtime.runtime_status_path("run-1")
        unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            unix_socket.bind(str(status_path))
            status_path.chmod(0o600)
            started = time.monotonic()
            with pytest.raises(runtime.RuntimeStatusValidationError):
                read_status(status_path)
            assert time.monotonic() - started < 1.0
        finally:
            unix_socket.close()
        assert paths["root"].is_dir()


def test_hardlink_and_wrong_owner_are_rejected(
    session_paths: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_status(session_paths["status"])
    os.link(session_paths["status"], tmp_path / "second-link.json")
    with pytest.raises(runtime.RuntimeStatusValidationError, match="one filesystem link"):
        read_status(session_paths["status"])

    (tmp_path / "second-link.json").unlink()
    real_fstat = runtime.os.fstat

    def wrong_owner(descriptor: int) -> SimpleNamespace:
        observed = real_fstat(descriptor)
        return SimpleNamespace(
            st_mode=observed.st_mode,
            st_uid=observed.st_uid + 1,
            st_nlink=observed.st_nlink,
            st_size=observed.st_size,
        )

    monkeypatch.setattr(runtime.os, "fstat", wrong_owner)
    with pytest.raises(runtime.RuntimeStatusValidationError, match="current user"):
        read_status(session_paths["status"])


def test_oversized_and_invalid_utf8_files_are_rejected(
    session_paths: dict[str, Path],
) -> None:
    session_paths["status"].write_bytes(
        b" " * (runtime.RUNTIME_STATUS_MAX_BYTES + 1)
    )
    session_paths["status"].chmod(0o600)
    with pytest.raises(runtime.RuntimeStatusValidationError, match="size limit"):
        read_status(session_paths["status"])

    session_paths["status"].write_bytes(b"\xff")
    session_paths["status"].chmod(0o600)
    with pytest.raises(runtime.RuntimeStatusValidationError, match="UTF-8"):
        read_status(session_paths["status"])


def test_reader_uses_one_file_descriptor_not_path_stat_or_read_text(
    session_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_status(session_paths["status"])

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("path-based status access is forbidden")

    monkeypatch.setattr(Path, "stat", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    assert read_status(session_paths["status"])["sequence"] == 1


def test_reader_fails_closed_when_no_follow_is_unavailable(
    session_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_status(session_paths["status"])
    monkeypatch.delattr(runtime.os, "O_NOFOLLOW")

    with pytest.raises(runtime.RuntimeStatusValidationError, match="O_NOFOLLOW"):
        read_status(session_paths["status"])


def test_same_sequence_replay_and_recreated_sequence_rollback_are_rejected(
    session_paths: dict[str, Path],
) -> None:
    write_status(session_paths["status"], sequence=1)
    with pytest.raises(runtime.RuntimeStatusValidationError, match="sequence"):
        read_status(session_paths["status"], previous_sequence=1)

    session_paths["status"].unlink()
    write_status(session_paths["status"], sequence=1)
    with pytest.raises(runtime.RuntimeStatusValidationError, match="sequence"):
        read_status(session_paths["status"], previous_sequence=1)


def test_failed_atomic_replace_preserves_previous_status(
    session_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_status(session_paths["status"])
    before = session_paths["status"].read_bytes()

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(runtime.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        write_status(
            session_paths["status"],
            sequence=2,
            heartbeat_at=NOW + timedelta(seconds=1),
        )

    assert session_paths["status"].read_bytes() == before
    assert not list(session_paths["root"].glob(".runtime_status.json.*.tmp"))
