"""Strict boundary tests for the ephemeral XHS watchdog status."""

from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trippostcollect.xhs import runtime


NOW = datetime(2026, 9, 3, 6, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def session_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Path]:
    monkeypatch.setattr(runtime, "XHS_SESSION_ROOT", tmp_path / "sessions")
    paths = runtime.prepare_runtime_session("run-1")
    return {**paths, "status": runtime.runtime_status_path("run-1")}


def status_payload(
    *,
    sequence: int = 1,
    heartbeat_at: datetime = NOW,
    network_state: str = "online",
    network_reason: str = "",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": "run-1",
        "account_id": "xhs-a01",
        "lease_id": "lease-1",
        "writer_role": "mediacrawler_supervisor",
        "writer_pid": 1234,
        "sequence": sequence,
        "heartbeat_at": heartbeat_at.isoformat(),
        "phase": "running",
        "network_state": network_state,
        "network_reason": network_reason,
    }


def test_runtime_status_is_fixed_atomic_0600_and_run_scoped(
    session_paths: dict[str, Path],
) -> None:
    assert set(runtime.runtime_session_paths("run-1")) == {"root", "profile"}
    assert not session_paths["status"].exists()

    written = runtime.write_runtime_status_atomic(
        session_paths["status"],
        status_payload(),
        now=NOW,
    )

    assert written == session_paths["status"].absolute()
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert not list(session_paths["root"].glob(".runtime_status.json.*.tmp"))
    assert runtime.read_runtime_status(
        written,
        expected_run_id="run-1",
        expected_account_id="xhs-a01",
        expected_lease_id="lease-1",
        expected_writer_pid=1234,
        now=NOW,
        max_age_seconds=1,
    )["sequence"] == 1


def test_runtime_session_removal_naturally_removes_status(
    session_paths: dict[str, Path],
) -> None:
    runtime.write_runtime_status_atomic(
        session_paths["status"],
        status_payload(),
        now=NOW,
    )

    assert runtime.remove_runtime_session(session_paths["root"]) is True
    assert not session_paths["root"].exists()


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "owner_token",
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
        runtime.validate_runtime_status(payload, now=NOW)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 2, "schema_version"),
        ("run_id", "../escape", "run_id"),
        ("account_id", "account-1", "account_id"),
        ("lease_id", "../lease", "lease_id"),
        ("writer_role", "browser", "writer_role"),
        ("writer_pid", 0, "writer_pid"),
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
    payload = status_payload()
    payload[field] = value

    with pytest.raises(runtime.RuntimeStatusValidationError, match=message):
        runtime.validate_runtime_status(payload, now=NOW)


@pytest.mark.parametrize(
    "expected_field,expected_value",
    [
        ("expected_run_id", "run-2"),
        ("expected_account_id", "xhs-a02"),
        ("expected_lease_id", "lease-2"),
        ("expected_writer_pid", 4321),
    ],
)
def test_runtime_status_requires_exact_expected_identity(
    expected_field: str,
    expected_value: object,
) -> None:
    with pytest.raises(
        runtime.RuntimeStatusValidationError,
        match="exact run identity",
    ):
        runtime.validate_runtime_status(
            status_payload(),
            now=NOW,
            **{expected_field: expected_value},
        )


def test_runtime_status_requires_strictly_increasing_sequence_and_time(
    session_paths: dict[str, Path],
) -> None:
    runtime.write_runtime_status_atomic(
        session_paths["status"],
        status_payload(),
        now=NOW,
    )
    repeated_sequence = status_payload(
        sequence=1,
        heartbeat_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(runtime.RuntimeStatusValidationError, match="sequence"):
        runtime.write_runtime_status_atomic(
            session_paths["status"],
            repeated_sequence,
            now=NOW + timedelta(seconds=1),
        )

    repeated_time = status_payload(sequence=2, heartbeat_at=NOW)
    with pytest.raises(runtime.RuntimeStatusValidationError, match="strictly increase"):
        runtime.write_runtime_status_atomic(
            session_paths["status"],
            repeated_time,
            now=NOW + timedelta(seconds=1),
        )

    updated = status_payload(
        sequence=2,
        heartbeat_at=NOW + timedelta(seconds=1),
    )
    runtime.write_runtime_status_atomic(
        session_paths["status"],
        updated,
        now=NOW + timedelta(seconds=1),
    )
    assert json.loads(session_paths["status"].read_text(encoding="utf-8"))[
        "sequence"
    ] == 2


def test_runtime_status_rejects_naive_future_and_stale_timestamps(
    session_paths: dict[str, Path],
) -> None:
    naive = status_payload()
    naive["heartbeat_at"] = "2026-09-03T06:00:00"
    with pytest.raises(runtime.RuntimeStatusValidationError, match="timezone-aware"):
        runtime.validate_runtime_status(naive, now=NOW)

    future = status_payload(heartbeat_at=NOW + timedelta(seconds=6))
    with pytest.raises(runtime.RuntimeStatusValidationError, match="future"):
        runtime.validate_runtime_status(future, now=NOW)

    runtime.write_runtime_status_atomic(
        session_paths["status"],
        status_payload(),
        now=NOW,
    )
    with pytest.raises(runtime.RuntimeStatusValidationError, match="stale"):
        runtime.read_runtime_status(
            session_paths["status"],
            expected_run_id="run-1",
            now=NOW + timedelta(seconds=11),
            max_age_seconds=10,
        )


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
        runtime.validate_runtime_status(
            status_payload(
                network_state=network_state,
                network_reason=network_reason,
            ),
            now=NOW,
        )


def test_runtime_status_rejects_wrong_path_symlink_and_loose_mode(
    session_paths: dict[str, Path],
    tmp_path: Path,
) -> None:
    with pytest.raises(runtime.RuntimeStatusValidationError, match="fixed file"):
        runtime.write_runtime_status_atomic(
            tmp_path / "runtime_status.json",
            status_payload(),
            now=NOW,
        )

    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(status_payload()), encoding="utf-8")
    outside.chmod(0o600)
    session_paths["status"].symlink_to(outside)
    with pytest.raises(runtime.RuntimeStatusValidationError, match="symlink"):
        runtime.read_runtime_status(
            session_paths["status"],
            expected_run_id="run-1",
            now=NOW,
        )
    session_paths["status"].unlink()

    runtime.write_runtime_status_atomic(
        session_paths["status"],
        status_payload(),
        now=NOW,
    )
    session_paths["status"].chmod(0o640)
    with pytest.raises(runtime.RuntimeStatusValidationError, match="0600"):
        runtime.read_runtime_status(
            session_paths["status"],
            expected_run_id="run-1",
            now=NOW,
        )


def test_failed_atomic_replace_preserves_previous_status(
    session_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.write_runtime_status_atomic(
        session_paths["status"],
        status_payload(),
        now=NOW,
    )
    before = session_paths["status"].read_bytes()

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(runtime.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        runtime.write_runtime_status_atomic(
            session_paths["status"],
            status_payload(
                sequence=2,
                heartbeat_at=NOW + timedelta(seconds=1),
            ),
            now=NOW + timedelta(seconds=1),
        )

    assert session_paths["status"].read_bytes() == before
    assert not list(session_paths["root"].glob(".runtime_status.json.*.tmp"))
