"""Run-scoped Xiaohongshu browser material and ephemeral watchdog status."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from trippostcollect.core.paths import XHS_SESSION_ROOT, ensure_dir
from trippostcollect.xhs.accounts import validate_account_id


RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}\Z")
LEASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}\Z")
RUNTIME_STATUS_SCHEMA_VERSION = 2
RUNTIME_STATUS_FILENAME = "runtime_status.json"
RUNTIME_STATUS_AUTH_KEY_ENV = "TRIPPOSTCOLLECT_XHS_RUNTIME_STATUS_AUTH_KEY_HEX"
RUNTIME_STATUS_MAX_BYTES = 16 * 1024
RUNTIME_STATUS_AUTH_KEY_MIN_BYTES = 32
RUNTIME_STATUS_WRITER_ROLES = frozenset({"mediacrawler_supervisor"})
RUNTIME_STATUS_PHASES = frozenset({"starting", "running", "finalizing"})
RUNTIME_NETWORK_STATES = frozenset({"unknown", "online", "network_paused"})
PROCESS_IDENTITY_FIELDS = frozenset(
    {
        "host_id",
        "boot_id",
        "pid",
        "process_started_at",
        "process_start_token",
        "pgid",
    }
)
RUNTIME_STATUS_SIGNED_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "account_id",
        "lease_id",
        "writer_role",
        "writer_host_id",
        "writer_boot_id",
        "writer_pid",
        "writer_process_started_at",
        "writer_process_start_token",
        "writer_pgid",
        "sequence",
        "heartbeat_at",
        "phase",
        "network_state",
        "network_reason",
    }
)
RUNTIME_STATUS_FIELDS = RUNTIME_STATUS_SIGNED_FIELDS | frozenset({"auth_tag"})
PUBLIC_RUNTIME_STATUS_FIELDS = (
    "writer_role",
    "sequence",
    "heartbeat_at",
    "phase",
    "network_state",
    "network_reason",
)
AUTH_TAG_RE = re.compile(r"[0-9a-f]{64}\Z")
SENSITIVE_REASON_RE = re.compile(
    r"authorization|cookie|owner[_ -]?token|storage[_ -]?state|web[_ -]?session",
    re.I,
)
RUNTIME_SESSION_CLAIM_SCHEMA_VERSION = 1
RUNTIME_SESSION_CLAIM_FILENAME = ".session_claim.json"
RUNTIME_SESSION_CLAIM_MAX_BYTES = 4 * 1024
RUNTIME_SESSION_CLAIM_SIGNED_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "account_id",
        "lease_id",
        "owner_token_sha256",
    }
)
RUNTIME_SESSION_CLAIM_FIELDS = RUNTIME_SESSION_CLAIM_SIGNED_FIELDS | frozenset(
    {"auth_tag"}
)


class RuntimeStatusValidationError(ValueError):
    """Raised when an ephemeral runtime status crosses its strict boundary."""


class RuntimeStatusNotFound(RuntimeStatusValidationError):
    """Raised only when the exact runtime status file does not yet exist."""


class RuntimeSessionClaimError(ValueError):
    """Raised when a run directory is not owned by the expected exact lease."""


def _validated_run_id(run_id: str) -> str:
    value = str(run_id).strip()
    if not RUN_ID_RE.fullmatch(value):
        raise ValueError("XHS run_id is not safe for a runtime session directory")
    return value


def _validated_lease_id(lease_id: object) -> str:
    if not isinstance(lease_id, str):
        raise RuntimeStatusValidationError("runtime status lease_id is invalid")
    value = lease_id.strip()
    if value != lease_id or not LEASE_ID_RE.fullmatch(value):
        raise RuntimeStatusValidationError("runtime status lease_id is invalid")
    return value


def _positive_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeStatusValidationError(
            f"runtime status {field} must be a positive integer"
        )
    return value


def _aware_timestamp(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeStatusValidationError(
            f"runtime status {field} must be a timezone-aware ISO timestamp"
        )
    text = value.strip()
    if text != value:
        raise RuntimeStatusValidationError(
            f"runtime status {field} must use its canonical scalar form"
        )
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
    return text


def _bounded_identity_string(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise RuntimeStatusValidationError(
            f"runtime status writer {field} must be a string"
        )
    text = value.strip()
    if (
        text != value
        or not text
        or len(text) > 256
        or any(ord(char) < 32 for char in text)
    ):
        raise RuntimeStatusValidationError(
            f"runtime status writer {field} must be a short non-empty scalar"
        )
    return text


def _validated_auth_key(auth_key: object) -> bytes:
    if not isinstance(auth_key, bytes) or len(auth_key) < RUNTIME_STATUS_AUTH_KEY_MIN_BYTES:
        raise RuntimeStatusValidationError(
            "runtime status requires a non-persistent authentication key"
        )
    return auth_key


def _validated_writer_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "host_id": _bounded_identity_string(
            payload["writer_host_id"],
            field="host_id",
        ),
        "boot_id": _bounded_identity_string(
            payload["writer_boot_id"],
            field="boot_id",
        ),
        "pid": _positive_int(payload["writer_pid"], field="writer_pid"),
        "process_started_at": _aware_timestamp(
            payload["writer_process_started_at"],
            field="writer_process_started_at",
        ),
        "process_start_token": _bounded_identity_string(
            payload["writer_process_start_token"],
            field="process_start_token",
        ),
        "pgid": _positive_int(payload["writer_pgid"], field="writer_pgid"),
    }


def _expected_writer_identity(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        fields = frozenset(value)
        if fields != PROCESS_IDENTITY_FIELDS:
            raise RuntimeStatusValidationError(
                "expected runtime supervisor identity must contain every exact ProcessIdentity scalar"
            )
        source = value
    else:
        try:
            source = {
                field: getattr(value, field)
                for field in PROCESS_IDENTITY_FIELDS
            }
        except AttributeError as exc:
            raise RuntimeStatusValidationError(
                "expected runtime supervisor identity must contain every exact ProcessIdentity scalar"
            ) from exc
    return _validated_writer_identity(
        {
            "writer_host_id": source["host_id"],
            "writer_boot_id": source["boot_id"],
            "writer_pid": source["pid"],
            "writer_process_started_at": source["process_started_at"],
            "writer_process_start_token": source["process_start_token"],
            "writer_pgid": source["pgid"],
        }
    )


def _canonical_status_bytes(payload: Mapping[str, Any]) -> bytes:
    signed_payload = {
        field: payload[field]
        for field in sorted(RUNTIME_STATUS_SIGNED_FIELDS)
    }
    return json.dumps(
        signed_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _runtime_status_auth_tag(payload: Mapping[str, Any], auth_key: bytes) -> str:
    return hmac.new(
        auth_key,
        _canonical_status_bytes(payload),
        hashlib.sha256,
    ).hexdigest()


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


def runtime_session_claim_path(run_id: str) -> Path:
    """Return the fixed ownership marker for one run-scoped session."""

    return runtime_session_paths(run_id)["root"] / RUNTIME_SESSION_CLAIM_FILENAME


def runtime_session_actually_absent(run_id: str) -> bool:
    """Return whether the exact run root has no filesystem directory entry."""

    root = runtime_session_paths(run_id)["root"]
    return not os.path.lexists(root)


def _validated_owner_token(owner_token: object) -> str:
    if not isinstance(owner_token, str):
        raise RuntimeSessionClaimError("runtime session owner token is invalid")
    value = owner_token.strip()
    if (
        value != owner_token
        or not value
        or len(value) > 1024
        or any(ord(char) < 32 for char in value)
    ):
        raise RuntimeSessionClaimError("runtime session owner token is invalid")
    return value


def _canonical_session_claim_bytes(payload: Mapping[str, Any]) -> bytes:
    signed_payload = {
        field: payload[field]
        for field in sorted(RUNTIME_SESSION_CLAIM_SIGNED_FIELDS)
    }
    return json.dumps(
        signed_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _session_claim_auth_tag(
    payload: Mapping[str, Any],
    *,
    owner_token: str,
) -> str:
    return hmac.new(
        owner_token.encode("utf-8"),
        _canonical_session_claim_bytes(payload),
        hashlib.sha256,
    ).hexdigest()


def build_runtime_session_claim(
    *,
    run_id: str,
    account_id: str,
    lease_id: str,
    owner_token: str,
) -> dict[str, Any]:
    """Build a marker that reveals no reusable exact lease credential."""

    try:
        exact_run_id = _validated_run_id(run_id)
        exact_account_id = validate_account_id(account_id)
        exact_lease_id = _validated_lease_id(lease_id)
    except (ValueError, RuntimeStatusValidationError) as exc:
        raise RuntimeSessionClaimError(str(exc)) from exc
    exact_owner_token = _validated_owner_token(owner_token)
    unsigned = {
        "schema_version": RUNTIME_SESSION_CLAIM_SCHEMA_VERSION,
        "run_id": exact_run_id,
        "account_id": exact_account_id,
        "lease_id": exact_lease_id,
        "owner_token_sha256": hashlib.sha256(
            exact_owner_token.encode("utf-8")
        ).hexdigest(),
    }
    return {
        **unsigned,
        "auth_tag": _session_claim_auth_tag(
            unsigned,
            owner_token=exact_owner_token,
        ),
    }


def validate_runtime_session_claim(
    payload: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_account_id: str,
    expected_lease_id: str,
    expected_owner_token: str,
) -> dict[str, Any]:
    """Validate that a marker belongs to one exact run and lease owner."""

    if not isinstance(payload, Mapping):
        raise RuntimeSessionClaimError("runtime session claim must be an object")
    fields = frozenset(payload)
    if fields != RUNTIME_SESSION_CLAIM_FIELDS:
        raise RuntimeSessionClaimError(
            "runtime session claim fields must match the fixed schema"
        )
    expected = build_runtime_session_claim(
        run_id=expected_run_id,
        account_id=expected_account_id,
        lease_id=expected_lease_id,
        owner_token=expected_owner_token,
    )
    schema_version = payload.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != RUNTIME_SESSION_CLAIM_SCHEMA_VERSION
    ):
        raise RuntimeSessionClaimError(
            "runtime session claim schema_version is unsupported"
        )
    for field in (
        "run_id",
        "account_id",
        "lease_id",
        "owner_token_sha256",
    ):
        if payload.get(field) != expected[field]:
            raise RuntimeSessionClaimError(
                "runtime session claim does not match the exact lease"
            )
    auth_tag = payload.get("auth_tag")
    if (
        not isinstance(auth_tag, str)
        or not AUTH_TAG_RE.fullmatch(auth_tag)
        or not hmac.compare_digest(auth_tag, expected["auth_tag"])
    ):
        raise RuntimeSessionClaimError(
            "runtime session claim authentication failed"
        )
    return dict(expected)


def _write_runtime_session_claim(path: Path, claim: Mapping[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise RuntimeSessionClaimError(
            "runtime session claim writes require O_NOFOLLOW"
        )
    flags |= no_follow
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        encoded = (
            json.dumps(claim, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        if len(encoded) > RUNTIME_SESSION_CLAIM_MAX_BYTES:
            raise RuntimeSessionClaimError(
                "runtime session claim exceeds the fixed size limit"
            )
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise RuntimeSessionClaimError(
                    "runtime session claim write made no progress"
                )
            offset += written
        os.fsync(descriptor)
    except RuntimeSessionClaimError:
        raise
    except OSError as exc:
        raise RuntimeSessionClaimError(
            f"runtime session claim cannot be written: {type(exc).__name__}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_runtime_session_claim(path: Path) -> dict[str, Any]:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    non_blocking = getattr(os, "O_NONBLOCK", None)
    if no_follow is None or non_blocking is None:
        raise RuntimeSessionClaimError(
            "runtime session claim reads require O_NOFOLLOW and O_NONBLOCK"
        )
    flags = os.O_RDONLY | no_follow | non_blocking
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise RuntimeSessionClaimError(
                "runtime session claim must be a regular file"
            )
        if file_stat.st_uid != os.getuid():
            raise RuntimeSessionClaimError(
                "runtime session claim must be owned by the current user"
            )
        if file_stat.st_nlink != 1:
            raise RuntimeSessionClaimError(
                "runtime session claim must have exactly one filesystem link"
            )
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise RuntimeSessionClaimError(
                "runtime session claim mode must be exactly 0600"
            )
        if file_stat.st_size > RUNTIME_SESSION_CLAIM_MAX_BYTES:
            raise RuntimeSessionClaimError(
                "runtime session claim exceeds the fixed size limit"
            )
        raw = os.read(descriptor, RUNTIME_SESSION_CLAIM_MAX_BYTES + 1)
        if len(raw) > RUNTIME_SESSION_CLAIM_MAX_BYTES:
            raise RuntimeSessionClaimError(
                "runtime session claim exceeds the fixed size limit"
            )
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise RuntimeSessionClaimError(
                "runtime session claim is not valid UTF-8"
            ) from exc
        try:
            payload = json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise RuntimeSessionClaimError(
                "runtime session claim is not valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeSessionClaimError(
                "runtime session claim must be an object"
            )
        return payload
    except RuntimeSessionClaimError:
        raise
    except OSError as exc:
        raise RuntimeSessionClaimError(
            f"runtime session claim cannot be read: {type(exc).__name__}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def verify_runtime_session_claim(
    profile_dir: Path,
    *,
    expected_run_id: str,
    expected_account_id: str,
    expected_lease_id: str,
    expected_owner_token: str,
) -> dict[str, Any]:
    """Read and verify the fixed marker without following session symlinks."""

    profile = canonical_runtime_profile_dir(expected_run_id, profile_dir)
    root = profile.parent
    if not root.is_dir() or root.is_symlink():
        raise RuntimeSessionClaimError(
            "runtime session claim requires the exact session directory"
        )
    payload = _read_runtime_session_claim(
        root / RUNTIME_SESSION_CLAIM_FILENAME
    )
    return validate_runtime_session_claim(
        payload,
        expected_run_id=expected_run_id,
        expected_account_id=expected_account_id,
        expected_lease_id=expected_lease_id,
        expected_owner_token=expected_owner_token,
    )


def canonical_runtime_profile_dir(run_id: str, profile_dir: str | Path) -> Path:
    """Validate the one fixed, non-symlinked profile path for ``run_id``."""

    candidate_input = Path(profile_dir).expanduser()
    if not candidate_input.is_absolute():
        raise ValueError("XHS runtime profile path must be absolute")
    candidate = Path(os.path.abspath(candidate_input))
    expected = Path(os.path.abspath(runtime_session_paths(run_id)["profile"]))
    if candidate != expected:
        raise ValueError(
            f"XHS runtime profile must match its exact run session: {expected}"
        )
    session_root = expected.parent
    configured_root = session_root.parent
    if (
        configured_root.is_symlink()
        or session_root.is_symlink()
        or expected.is_symlink()
    ):
        raise ValueError("XHS runtime profile path must not contain a session symlink")
    return expected


def runtime_status_path(run_id: str) -> Path:
    """Return the sole allowed watchdog status path for one runtime session."""

    return runtime_session_paths(run_id)["root"] / RUNTIME_STATUS_FILENAME


def prepare_runtime_session(
    run_id: str,
    *,
    account_id: str | None = None,
    lease_id: str | None = None,
    owner_token: str | None = None,
) -> dict[str, Path]:
    """Create a fresh browser profile for exactly one run."""

    paths = runtime_session_paths(run_id)
    claim_values = (account_id, lease_id, owner_token)
    if any(value is not None for value in claim_values) and not all(
        value is not None for value in claim_values
    ):
        raise RuntimeSessionClaimError(
            "runtime session claim requires account, lease, and owner identities"
        )
    claim = (
        build_runtime_session_claim(
            run_id=run_id,
            account_id=str(account_id),
            lease_id=str(lease_id),
            owner_token=str(owner_token),
        )
        if account_id is not None
        else None
    )
    ensure_dir(paths["root"].parent)
    if paths["root"].parent.is_symlink():
        raise RuntimeSessionClaimError(
            "XHS runtime session root must not be a symlink"
        )
    if os.path.lexists(paths["root"]):
        raise RuntimeError(f"XHS runtime session already exists: {paths['root']}")
    os.mkdir(paths["root"], mode=0o700)
    paths["root"].chmod(0o700)
    if claim is not None:
        _write_runtime_session_claim(
            paths["root"] / RUNTIME_SESSION_CLAIM_FILENAME,
            claim,
        )
    os.mkdir(paths["profile"], mode=0o700)
    paths["profile"].chmod(0o700)
    return paths


def _validate_unsigned_runtime_status(
    payload: Mapping[str, Any],
    *,
    expected_run_id: str | None = None,
    expected_account_id: str | None = None,
    expected_lease_id: str | None = None,
    expected_writer_identity: object | None = None,
    previous_sequence: int | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise RuntimeStatusValidationError("runtime status must be an object")
    fields = frozenset(payload)
    if fields != RUNTIME_STATUS_SIGNED_FIELDS:
        missing = sorted(
            str(field) for field in RUNTIME_STATUS_SIGNED_FIELDS - fields
        )
        unexpected = sorted(
            str(field) for field in fields - RUNTIME_STATUS_SIGNED_FIELDS
        )
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
    if run_id != payload["run_id"] or account_id != payload["account_id"]:
        raise RuntimeStatusValidationError(
            "runtime status run and account identities must use canonical scalar forms"
        )
    lease_id = _validated_lease_id(payload["lease_id"])
    writer_role = payload["writer_role"]
    if not isinstance(writer_role, str) or writer_role not in RUNTIME_STATUS_WRITER_ROLES:
        raise RuntimeStatusValidationError("runtime status writer_role is unsupported")
    writer_identity = _validated_writer_identity(payload)
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

    heartbeat_at = _aware_timestamp(payload["heartbeat_at"], field="heartbeat_at")

    expected_values = {
        "run_id": expected_run_id,
        "account_id": expected_account_id,
        "lease_id": expected_lease_id,
    }
    actual_values = {
        "run_id": run_id,
        "account_id": account_id,
        "lease_id": lease_id,
    }
    for field, expected in expected_values.items():
        if expected is not None and actual_values[field] != expected:
            raise RuntimeStatusValidationError(
                f"runtime status {field} does not match the exact run identity"
            )
    if expected_writer_identity is not None:
        expected_identity = _expected_writer_identity(expected_writer_identity)
        if writer_identity != expected_identity:
            raise RuntimeStatusValidationError(
                "runtime status writer identity does not match the exact supervisor process"
            )

    return {
        "schema_version": RUNTIME_STATUS_SCHEMA_VERSION,
        "run_id": run_id,
        "account_id": account_id,
        "lease_id": lease_id,
        "writer_role": writer_role,
        "writer_host_id": writer_identity["host_id"],
        "writer_boot_id": writer_identity["boot_id"],
        "writer_pid": writer_identity["pid"],
        "writer_process_started_at": writer_identity["process_started_at"],
        "writer_process_start_token": writer_identity["process_start_token"],
        "writer_pgid": writer_identity["pgid"],
        "sequence": sequence,
        "heartbeat_at": heartbeat_at,
        "phase": phase,
        "network_state": network_state,
        "network_reason": network_reason,
    }


def sign_runtime_status(
    payload: Mapping[str, Any],
    *,
    auth_key: bytes,
) -> dict[str, Any]:
    """Validate and authenticate one scalar-only supervisor status payload."""

    key = _validated_auth_key(auth_key)
    validated = _validate_unsigned_runtime_status(payload)
    return {
        **validated,
        "auth_tag": _runtime_status_auth_tag(validated, key),
    }


def validate_runtime_status(
    payload: Mapping[str, Any],
    *,
    auth_key: bytes,
    expected_run_id: str | None = None,
    expected_account_id: str | None = None,
    expected_lease_id: str | None = None,
    expected_writer_identity: object | None = None,
    previous_sequence: int | None = None,
) -> dict[str, Any]:
    """Validate the complete authenticated watchdog status contract."""

    key = _validated_auth_key(auth_key)
    if not isinstance(payload, Mapping):
        raise RuntimeStatusValidationError("runtime status must be an object")
    fields = frozenset(payload)
    if fields != RUNTIME_STATUS_FIELDS:
        missing = sorted(str(field) for field in RUNTIME_STATUS_FIELDS - fields)
        unexpected = sorted(str(field) for field in fields - RUNTIME_STATUS_FIELDS)
        raise RuntimeStatusValidationError(
            f"runtime status fields must match the fixed schema; missing={missing}, unexpected={unexpected}"
        )
    auth_tag = payload["auth_tag"]
    if not isinstance(auth_tag, str) or not AUTH_TAG_RE.fullmatch(auth_tag):
        raise RuntimeStatusValidationError(
            "runtime status auth_tag must be exactly 64 lowercase hexadecimal characters"
        )
    unsigned = {
        field: payload[field]
        for field in RUNTIME_STATUS_SIGNED_FIELDS
    }
    validated = _validate_unsigned_runtime_status(
        unsigned,
        expected_run_id=expected_run_id,
        expected_account_id=expected_account_id,
        expected_lease_id=expected_lease_id,
        expected_writer_identity=expected_writer_identity,
        previous_sequence=previous_sequence,
    )
    expected_tag = _runtime_status_auth_tag(validated, key)
    if not hmac.compare_digest(auth_tag, expected_tag):
        raise RuntimeStatusValidationError("runtime status authentication failed")
    return {**validated, "auth_tag": auth_tag}


def _read_runtime_status_file(target: Path) -> str:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    non_blocking = getattr(os, "O_NONBLOCK", None)
    if no_follow is None or non_blocking is None:
        raise RuntimeStatusValidationError(
            "runtime status secure reads require O_NOFOLLOW and O_NONBLOCK"
        )
    flags = os.O_RDONLY | non_blocking | no_follow
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = -1
    try:
        descriptor = os.open(target, flags)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise RuntimeStatusValidationError(
                "runtime status must be a regular file"
            )
        if file_stat.st_uid != os.getuid():
            raise RuntimeStatusValidationError(
                "runtime status must be owned by the current user"
            )
        if file_stat.st_nlink != 1:
            raise RuntimeStatusValidationError(
                "runtime status must have exactly one filesystem link"
            )
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise RuntimeStatusValidationError(
                "runtime status mode must be exactly 0600"
            )
        if file_stat.st_size > RUNTIME_STATUS_MAX_BYTES:
            raise RuntimeStatusValidationError(
                "runtime status exceeds the fixed size limit"
            )

        chunks: list[bytes] = []
        total = 0
        while total <= RUNTIME_STATUS_MAX_BYTES:
            chunk = os.read(
                descriptor,
                min(8192, RUNTIME_STATUS_MAX_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > RUNTIME_STATUS_MAX_BYTES:
            raise RuntimeStatusValidationError(
                "runtime status exceeds the fixed size limit"
            )
        try:
            return b"".join(chunks).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise RuntimeStatusValidationError(
                "runtime status is not valid UTF-8"
            ) from exc
    except RuntimeStatusValidationError:
        raise
    except FileNotFoundError as exc:
        raise RuntimeStatusNotFound("runtime status does not exist") from exc
    except OSError as exc:
        raise RuntimeStatusValidationError(
            f"runtime status cannot be read: {type(exc).__name__}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_runtime_status(
    path: str | Path,
    *,
    auth_key: bytes,
    expected_run_id: str,
    expected_writer_identity: object,
    expected_account_id: str | None = None,
    expected_lease_id: str | None = None,
    previous_sequence: int | None = None,
) -> dict[str, Any]:
    """Read one exact run status through a bounded, no-follow file descriptor."""

    key = _validated_auth_key(auth_key)
    target = _status_path_for_run(path, expected_run_id)
    raw = _read_runtime_status_file(target)
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise RuntimeStatusValidationError("runtime status is not valid JSON") from exc
    return validate_runtime_status(
        payload,
        auth_key=key,
        expected_run_id=expected_run_id,
        expected_account_id=expected_account_id,
        expected_lease_id=expected_lease_id,
        expected_writer_identity=expected_writer_identity,
        previous_sequence=previous_sequence,
    )


def read_runtime_status_if_present(
    path: str | Path,
    *,
    auth_key: bytes,
    expected_run_id: str,
    expected_writer_identity: object,
    expected_account_id: str | None = None,
    expected_lease_id: str | None = None,
) -> dict[str, Any] | None:
    """Return ``None`` only while the exact status file has not been created."""

    try:
        return read_runtime_status(
            path,
            auth_key=auth_key,
            expected_run_id=expected_run_id,
            expected_account_id=expected_account_id,
            expected_lease_id=expected_lease_id,
            expected_writer_identity=expected_writer_identity,
        )
    except RuntimeStatusNotFound:
        return None


def public_runtime_status(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project a validated runtime status onto its non-secret audit fields."""

    missing = [field for field in PUBLIC_RUNTIME_STATUS_FIELDS if field not in payload]
    if missing:
        raise RuntimeStatusValidationError(
            f"runtime status public projection is missing fields: {missing}"
        )
    return {field: payload[field] for field in PUBLIC_RUNTIME_STATUS_FIELDS}


def write_runtime_status_atomic(
    path: str | Path,
    payload: Mapping[str, Any],
    *,
    auth_key: bytes,
) -> Path:
    """Validate and atomically replace one run-scoped status with mode 0600."""

    validated = validate_runtime_status(payload, auth_key=auth_key)
    target = _status_path_for_run(path, validated["run_id"])
    session_root = target.parent
    if not session_root.is_dir() or session_root.is_symlink():
        raise RuntimeStatusValidationError(
            "runtime status requires an existing exact run session"
        )

    if target.exists():
        previous = read_runtime_status(
            target,
            auth_key=auth_key,
            expected_run_id=validated["run_id"],
            expected_account_id=validated["account_id"],
            expected_lease_id=validated["lease_id"],
            expected_writer_identity={
                "host_id": validated["writer_host_id"],
                "boot_id": validated["writer_boot_id"],
                "pid": validated["writer_pid"],
                "process_started_at": validated["writer_process_started_at"],
                "process_start_token": validated["writer_process_start_token"],
                "pgid": validated["writer_pgid"],
            },
        )
        validated = validate_runtime_status(
            validated,
            auth_key=auth_key,
            expected_run_id=previous["run_id"],
            expected_account_id=previous["account_id"],
            expected_lease_id=previous["lease_id"],
            expected_writer_identity={
                "host_id": previous["writer_host_id"],
                "boot_id": previous["writer_boot_id"],
                "pid": previous["writer_pid"],
                "process_started_at": previous["writer_process_started_at"],
                "process_start_token": previous["writer_process_start_token"],
                "pgid": previous["writer_pgid"],
            },
            previous_sequence=int(previous["sequence"]),
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

    root_input = Path(session_root).expanduser()
    if not root_input.is_absolute():
        raise ValueError("XHS runtime session path must be absolute")
    root = Path(os.path.abspath(root_input))
    expected_parent = Path(os.path.abspath(XHS_SESSION_ROOT.expanduser()))
    if expected_parent.is_symlink():
        raise ValueError("refusing to remove through a symlinked XHS session root")
    if root.parent != expected_parent or not RUN_ID_RE.fullmatch(root.name):
        raise ValueError(f"refusing to remove non-XHS runtime session path: {root}")
    if not os.path.lexists(root):
        return True
    if root.is_symlink():
        raise ValueError("refusing to remove a symlinked XHS runtime session")
    shutil.rmtree(root)
    return not os.path.lexists(root)


def remove_runtime_session_for_profile(
    profile_dir: Path,
    *,
    expected_run_id: str,
    expected_account_id: str | None = None,
    expected_lease_id: str | None = None,
    expected_owner_token: str | None = None,
) -> bool:
    profile = canonical_runtime_profile_dir(expected_run_id, profile_dir)
    claim_values = (
        expected_account_id,
        expected_lease_id,
        expected_owner_token,
    )
    if any(value is not None for value in claim_values):
        if not all(value is not None for value in claim_values):
            raise RuntimeSessionClaimError(
                "runtime session cleanup requires every exact lease identity"
            )
        verify_runtime_session_claim(
            profile,
            expected_run_id=expected_run_id,
            expected_account_id=str(expected_account_id),
            expected_lease_id=str(expected_lease_id),
            expected_owner_token=str(expected_owner_token),
        )
    return remove_runtime_session(profile.parent)
