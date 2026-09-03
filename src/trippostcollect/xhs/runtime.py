"""Run-scoped Xiaohongshu browser material and ephemeral watchdog status."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from trippostcollect.core.paths import XHS_SESSION_ROOT, ensure_dir
from trippostcollect.xhs.accounts import validate_account_id


RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}\Z")
LEASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}\Z")
RUNTIME_STATUS_SCHEMA_VERSION = 1
RUNTIME_STATUS_FILENAME = "runtime_status.json"
RUNTIME_STATUS_WRITER_ROLES = frozenset({"mediacrawler_supervisor"})
RUNTIME_STATUS_PHASES = frozenset({"starting", "running", "finalizing"})
RUNTIME_NETWORK_STATES = frozenset({"unknown", "online", "network_paused"})
RUNTIME_STATUS_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "account_id",
        "lease_id",
        "writer_role",
        "writer_pid",
        "sequence",
        "heartbeat_at",
        "phase",
        "network_state",
        "network_reason",
    }
)
SENSITIVE_REASON_RE = re.compile(
    r"authorization|cookie|owner[_ -]?token|storage[_ -]?state|web[_ -]?session",
    re.I,
)


class RuntimeStatusValidationError(ValueError):
    """Raised when an ephemeral runtime status crosses its strict boundary."""


def _validated_run_id(run_id: str) -> str:
    value = str(run_id).strip()
    if not RUN_ID_RE.fullmatch(value):
        raise ValueError("XHS run_id is not safe for a runtime session directory")
    return value


def _validated_lease_id(lease_id: object) -> str:
    if not isinstance(lease_id, str):
        raise RuntimeStatusValidationError("runtime status lease_id is invalid")
    value = lease_id.strip()
    if not LEASE_ID_RE.fullmatch(value):
        raise RuntimeStatusValidationError("runtime status lease_id is invalid")
    return value


def _positive_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeStatusValidationError(
            f"runtime status {field} must be a positive integer"
        )
    return value


def _aware_utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeStatusValidationError(
            f"runtime status {field} must be a timezone-aware ISO timestamp"
        )
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeStatusValidationError(
            f"runtime status {field} must be a timezone-aware ISO timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeStatusValidationError(
            f"runtime status {field} must be a timezone-aware ISO timestamp"
        )
    return parsed.astimezone(timezone.utc)


def _status_path_for_run(path: str | Path, run_id: str) -> Path:
    candidate = Path(path).expanduser().absolute()
    expected = runtime_status_path(run_id).expanduser().absolute()
    if candidate != expected:
        raise RuntimeStatusValidationError(
            "runtime status path must be the fixed file inside its exact run session"
        )
    return candidate


def runtime_session_paths(run_id: str) -> dict[str, Path]:
    value = _validated_run_id(run_id)
    root = XHS_SESSION_ROOT / value
    return {
        "root": root,
        "profile": root / "profile",
    }


def runtime_status_path(run_id: str) -> Path:
    """Return the sole allowed watchdog status path for one runtime session."""

    return runtime_session_paths(run_id)["root"] / RUNTIME_STATUS_FILENAME


def prepare_runtime_session(run_id: str) -> dict[str, Path]:
    """Create a fresh browser profile for exactly one run."""

    paths = runtime_session_paths(run_id)
    if paths["root"].exists():
        raise RuntimeError(f"XHS runtime session already exists: {paths['root']}")
    ensure_dir(paths["profile"])
    paths["root"].chmod(0o700)
    paths["profile"].chmod(0o700)
    return paths


def validate_runtime_status(
    payload: Mapping[str, Any],
    *,
    expected_run_id: str | None = None,
    expected_account_id: str | None = None,
    expected_lease_id: str | None = None,
    expected_writer_pid: int | None = None,
    previous_sequence: int | None = None,
    previous_heartbeat_at: str | None = None,
    now: datetime | None = None,
    max_age_seconds: float | None = None,
    max_future_skew_seconds: float = 5.0,
) -> dict[str, Any]:
    """Validate the complete, scalar-only watchdog status contract."""

    if not isinstance(payload, Mapping):
        raise RuntimeStatusValidationError("runtime status must be an object")
    fields = frozenset(payload)
    if fields != RUNTIME_STATUS_FIELDS:
        missing = sorted(str(field) for field in RUNTIME_STATUS_FIELDS - fields)
        unexpected = sorted(str(field) for field in fields - RUNTIME_STATUS_FIELDS)
        raise RuntimeStatusValidationError(
            f"runtime status fields must match the fixed schema; missing={missing}, unexpected={unexpected}"
        )
    schema_version = payload["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != RUNTIME_STATUS_SCHEMA_VERSION
    ):
        raise RuntimeStatusValidationError("runtime status schema_version is unsupported")

    if not isinstance(payload["run_id"], str):
        raise RuntimeStatusValidationError("runtime status run_id must be a string")
    if not isinstance(payload["account_id"], str):
        raise RuntimeStatusValidationError("runtime status account_id must be a string")
    try:
        run_id = _validated_run_id(payload["run_id"])
        account_id = validate_account_id(payload["account_id"])
    except ValueError as exc:
        raise RuntimeStatusValidationError(str(exc)) from exc
    lease_id = _validated_lease_id(payload["lease_id"])
    writer_role = payload["writer_role"]
    if not isinstance(writer_role, str) or writer_role not in RUNTIME_STATUS_WRITER_ROLES:
        raise RuntimeStatusValidationError("runtime status writer_role is unsupported")
    writer_pid = _positive_int(payload["writer_pid"], field="writer_pid")
    sequence = _positive_int(payload["sequence"], field="sequence")
    if previous_sequence is not None and sequence <= previous_sequence:
        raise RuntimeStatusValidationError(
            "runtime status sequence must strictly increase"
        )

    phase = payload["phase"]
    if not isinstance(phase, str) or phase not in RUNTIME_STATUS_PHASES:
        raise RuntimeStatusValidationError("runtime status phase is unsupported")
    network_state = payload["network_state"]
    if not isinstance(network_state, str) or network_state not in RUNTIME_NETWORK_STATES:
        raise RuntimeStatusValidationError("runtime status network_state is unsupported")
    network_reason = payload["network_reason"]
    if not isinstance(network_reason, str):
        raise RuntimeStatusValidationError("runtime status network_reason must be a string")
    if len(network_reason) > 240 or any(ord(char) < 32 for char in network_reason):
        raise RuntimeStatusValidationError(
            "runtime status network_reason must be a short single-line string"
        )
    if SENSITIVE_REASON_RE.search(network_reason):
        raise RuntimeStatusValidationError(
            "runtime status network_reason contains forbidden sensitive material"
        )
    if network_state == "network_paused" and not network_reason:
        raise RuntimeStatusValidationError(
            "runtime status network_paused requires a sanitized reason"
        )
    if network_state != "network_paused" and network_reason:
        raise RuntimeStatusValidationError(
            "runtime status network_reason is only allowed while network_paused"
        )

    heartbeat = _aware_utc(payload["heartbeat_at"], field="heartbeat_at")
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if max_future_skew_seconds < 0:
        raise ValueError("max_future_skew_seconds cannot be negative")
    if heartbeat > observed_at + timedelta(seconds=max_future_skew_seconds):
        raise RuntimeStatusValidationError("runtime status heartbeat_at is in the future")
    if max_age_seconds is not None:
        if max_age_seconds < 0:
            raise ValueError("max_age_seconds cannot be negative")
        if heartbeat < observed_at - timedelta(seconds=max_age_seconds):
            raise RuntimeStatusValidationError("runtime status heartbeat_at is stale")
    if previous_heartbeat_at is not None:
        previous_heartbeat = _aware_utc(
            previous_heartbeat_at,
            field="previous_heartbeat_at",
        )
        if heartbeat <= previous_heartbeat:
            raise RuntimeStatusValidationError(
                "runtime status heartbeat_at must strictly increase"
            )

    expected_values = {
        "run_id": expected_run_id,
        "account_id": expected_account_id,
        "lease_id": expected_lease_id,
        "writer_pid": expected_writer_pid,
    }
    actual_values = {
        "run_id": run_id,
        "account_id": account_id,
        "lease_id": lease_id,
        "writer_pid": writer_pid,
    }
    for field, expected in expected_values.items():
        if expected is not None and actual_values[field] != expected:
            raise RuntimeStatusValidationError(
                f"runtime status {field} does not match the exact run identity"
            )

    return {
        "schema_version": RUNTIME_STATUS_SCHEMA_VERSION,
        "run_id": run_id,
        "account_id": account_id,
        "lease_id": lease_id,
        "writer_role": writer_role,
        "writer_pid": writer_pid,
        "sequence": sequence,
        "heartbeat_at": str(payload["heartbeat_at"]).strip(),
        "phase": phase,
        "network_state": network_state,
        "network_reason": network_reason,
    }


def read_runtime_status(
    path: str | Path,
    *,
    expected_run_id: str,
    expected_account_id: str | None = None,
    expected_lease_id: str | None = None,
    expected_writer_pid: int | None = None,
    previous_sequence: int | None = None,
    now: datetime | None = None,
    max_age_seconds: float | None = None,
) -> dict[str, Any]:
    """Read one exact run status, rejecting aliases, loose modes, and stale identity."""

    target = _status_path_for_run(path, expected_run_id)
    if target.is_symlink():
        raise RuntimeStatusValidationError("runtime status must not be a symlink")
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeStatusValidationError(
            f"runtime status cannot be read: {type(exc).__name__}"
        ) from exc
    if mode != 0o600:
        raise RuntimeStatusValidationError("runtime status mode must be exactly 0600")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeStatusValidationError("runtime status is not valid JSON") from exc
    return validate_runtime_status(
        payload,
        expected_run_id=expected_run_id,
        expected_account_id=expected_account_id,
        expected_lease_id=expected_lease_id,
        expected_writer_pid=expected_writer_pid,
        previous_sequence=previous_sequence,
        now=now,
        max_age_seconds=max_age_seconds,
    )


def write_runtime_status_atomic(
    path: str | Path,
    payload: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> Path:
    """Validate and atomically replace one run-scoped status with mode 0600."""

    validated = validate_runtime_status(payload, now=now)
    target = _status_path_for_run(path, validated["run_id"])
    session_root = target.parent
    if not session_root.is_dir() or session_root.is_symlink():
        raise RuntimeStatusValidationError(
            "runtime status requires an existing exact run session"
        )

    if target.exists():
        previous = read_runtime_status(
            target,
            expected_run_id=validated["run_id"],
            expected_account_id=validated["account_id"],
            expected_lease_id=validated["lease_id"],
            expected_writer_pid=validated["writer_pid"],
            now=now,
        )
        validated = validate_runtime_status(
            validated,
            expected_run_id=previous["run_id"],
            expected_account_id=previous["account_id"],
            expected_lease_id=previous["lease_id"],
            expected_writer_pid=previous["writer_pid"],
            previous_sequence=int(previous["sequence"]),
            previous_heartbeat_at=str(previous["heartbeat_at"]),
            now=now,
        )
    elif target.is_symlink():
        raise RuntimeStatusValidationError("runtime status must not be a symlink")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=session_root,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(validated, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def remove_runtime_session(session_root: Path) -> bool:
    """Remove one exact run directory after its guarded process tree is dead."""

    root = Path(session_root).expanduser().resolve()
    expected_parent = XHS_SESSION_ROOT.expanduser().resolve()
    if root.parent != expected_parent or not RUN_ID_RE.fullmatch(root.name):
        raise ValueError(f"refusing to remove non-XHS runtime session path: {root}")
    if not root.exists():
        return True
    shutil.rmtree(root)
    return not root.exists()


def remove_runtime_session_for_profile(profile_dir: Path) -> bool:
    profile = Path(profile_dir).expanduser().resolve()
    if profile.name != "profile":
        raise ValueError(f"invalid XHS runtime profile path: {profile}")
    return remove_runtime_session(profile.parent)
