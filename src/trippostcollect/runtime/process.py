"""子进程监督、网络暂停预算与严格 watchdog 事件。"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from trippostcollect.application.contracts import XhsRuntimeSupervisionError
from trippostcollect.core.execution_state import FrozenExecutionState
from trippostcollect.core.paths import ensure_dir
from trippostcollect.records.sanitization import (
    AUTHOR_AVATAR_LOG_REDACTION,
    redact_author_avatar_text,
)
from trippostcollect.runtime.browser_runtime import browser_launch_environment
from trippostcollect.xhs.leases import (
    DeferredTerminationSignals,
    GatedSubprocess,
    LEASE_DB_ENV,
    LEASE_ID_ENV,
    LEASE_OWNER_TOKEN_ENV,
    SystemProcessInspector,
    mark_lease_process_exited_from_environment,
    register_lease_process_from_environment,
    spawn_gated_subprocess,
)
from trippostcollect.xhs.runtime import RUNTIME_STATUS_AUTH_KEY_ENV

if TYPE_CHECKING:
    from trippostcollect.xhs.supervision import XhsSupervisorRuntimeReporter


PROCESS_PROGRESS_POLL_SECONDS = 5.0
PROCESS_CLEANUP_GRACE_SECONDS = 20.0
PROCESS_FINAL_REAP_SECONDS = 5.0
XHS_NETWORK_DIAGNOSTIC_MAX_AGE_SECONDS = 90.0
XHS_NETWORK_DIAGNOSTIC_MAX_BYTES = 512 * 1024
# 子侧拥有固定的 600 秒网络恢复预算；父侧只留观察终态的额外时间。
XHS_PARENT_NETWORK_PAUSE_CEILING_SECONDS = 675.0
XHS_CHILD_NETWORK_TERMINAL_GRACE_SECONDS = PROCESS_CLEANUP_GRACE_SECONDS
SUPERVISOR_RUNTIME_TIMEOUT_REASONS = frozenset(
    {
        "no_progress_timeout",
        "parent_network_pause_timeout",
        "parent_network_terminal_unwind_timeout",
    }
)
RUNTIME_WATCHDOG_STOP_DETAILS = SUPERVISOR_RUNTIME_TIMEOUT_REASONS | {
    "network_recovery_timeout"
}


class XhsParentNetworkPauseClock:
    """Account only supervisor-observed, fresh XHS transport pauses."""

    def __init__(self, *, ceiling_seconds: float) -> None:
        if ceiling_seconds <= 0:
            raise ValueError("network pause ceiling must be positive")
        self.ceiling_seconds = float(ceiling_seconds)
        self.active = False
        self.last_observed_at: float | None = None
        self.episode_seconds = 0.0
        self.total_seconds = 0.0
        self.observed = False

    def observe(self, network_state: str, *, now: float) -> tuple[float, bool]:
        accounted_delta = 0.0
        if network_state == "network_paused":
            self.observed = True
            if self.active and self.last_observed_at is not None:
                accounted_delta = max(0.0, now - self.last_observed_at)
                self.episode_seconds += accounted_delta
                self.total_seconds += accounted_delta
            self.active = True
            self.last_observed_at = now
        elif network_state == "online" and self.active and self.last_observed_at is not None:
            accounted_delta = max(0.0, now - self.last_observed_at)
            self.episode_seconds += accounted_delta
            self.total_seconds += accounted_delta
            self.active = False
            self.last_observed_at = None
            self.episode_seconds = 0.0
            return accounted_delta, False
        elif network_state == "online":
            self.episode_seconds = 0.0
            self.active = False
            self.last_observed_at = None
            return 0.0, False
        else:
            # Unknown includes stale, malformed, and non-transport diagnostics.
            # None receives watchdog credit, and none can impersonate an
            # explicit recovery that resets the current episode ceiling.
            self.active = False
            self.last_observed_at = None
        return accounted_delta, self.episode_seconds >= self.ceiling_seconds


def decode_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def tail(value: str, limit: int = 4000) -> str:
    return value[-limit:] if len(value) > limit else value


_XHS_TRANSPORT_MARKER_REASONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "internet_disconnected",
        (
            "net::err_internet_disconnected",
            "network is unreachable",
            "no route to host",
        ),
    ),
    (
        "dns_unreachable",
        (
            "net::err_name_not_resolved",
            "temporary failure in name resolution",
            "name or service not known",
        ),
    ),
    ("network_changed", ("net::err_network_changed",)),
    (
        "connection_reset",
        (
            "net::err_connection_reset",
            "net::err_connection_closed",
            "connection reset by peer",
        ),
    ),
    (
        "connection_refused",
        ("net::err_connection_refused", "connection refused"),
    ),
    (
        "address_unreachable",
        ("net::err_address_unreachable",),
    ),
    (
        "proxy_unreachable",
        (
            "net::err_proxy_connection_failed",
            "net::err_tunnel_connection_failed",
        ),
    ),
    ("transport_timeout", ("net::err_timed_out",)),
)


_XHS_TRANSPORT_ERROR_TYPE_REASONS = {
    "ConnectError": "internet_disconnected",
    "ConnectTimeout": "transport_timeout",
    "NetworkError": "internet_disconnected",
    "PlaywrightTimeoutError": "transport_timeout",
    "PoolTimeout": "transport_timeout",
    "ProxyError": "proxy_unreachable",
    "ReadError": "connection_reset",
    "ReadTimeout": "transport_timeout",
    "RemoteProtocolError": "connection_reset",
    "TimeoutError": "transport_timeout",
    "WriteError": "connection_reset",
    "WriteTimeout": "transport_timeout",
}


def _diagnostic_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _fresh_diagnostic_timestamp(
    value: object,
    *,
    now: datetime,
    max_age_seconds: float,
) -> bool:
    parsed = _diagnostic_timestamp(value)
    return bool(
        parsed is not None
        and parsed <= now + timedelta(seconds=5)
        and parsed >= now - timedelta(seconds=max_age_seconds)
    )


def _sanitized_transport_reason(error: str) -> str:
    detail = error.casefold()
    for reason, markers in _XHS_TRANSPORT_MARKER_REASONS:
        if any(marker in detail for marker in markers):
            return reason
    error_type, separator, _ = error.partition(":")
    if not separator:
        return ""
    return _XHS_TRANSPORT_ERROR_TYPE_REASONS.get(error_type.strip(), "")


def xhs_network_state_from_diagnostics(
    path: str | Path | None,
    *,
    now: datetime | None = None,
    max_age_seconds: float = XHS_NETWORK_DIAGNOSTIC_MAX_AGE_SECONDS,
) -> tuple[str, str]:
    """Return only a fresh, explicit XHS transport observation."""

    if path is None or max_age_seconds <= 0:
        return "unknown", ""
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("network diagnostic reference time must be timezone-aware")
    observed_at = observed_at.astimezone(timezone.utc)
    candidate = Path(path).expanduser()
    try:
        if candidate.is_symlink():
            return "unknown", ""
        file_stat = candidate.stat()
        if not candidate.is_file() or file_stat.st_size > XHS_NETWORK_DIAGNOSTIC_MAX_BYTES:
            return "unknown", ""
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return "unknown", ""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "platform", "updated_at", "events"}
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
        or payload.get("platform") != "xhs"
        or not _fresh_diagnostic_timestamp(
            payload.get("updated_at"),
            now=observed_at,
            max_age_seconds=max_age_seconds,
        )
    ):
        return "unknown", ""
    events = payload.get("events")
    if (
        not isinstance(events, list)
        or not events
        or len(events) > 30
        or not isinstance(events[-1], dict)
    ):
        return "unknown", ""
    event = events[-1]
    if not isinstance(event.get("stage"), str) or not event["stage"].strip():
        return "unknown", ""
    if not _fresh_diagnostic_timestamp(
        event.get("at"),
        now=observed_at,
        max_age_seconds=max_age_seconds,
    ):
        return "unknown", ""
    outcome = event.get("outcome")
    error = event.get("error")
    if outcome == "network_recovered" and error in (None, ""):
        return "online", ""
    if (
        outcome not in {"network_paused", "network_recovery_timeout"}
        or not isinstance(error, str)
        or not error
        or len(error) > 500
    ):
        return "unknown", ""
    reason = _sanitized_transport_reason(error)
    return (str(outcome), reason) if reason else ("unknown", "")


def progress_path_signature(
    paths: Iterable[Path],
    *,
    excluded_paths: Iterable[Path] = (),
) -> tuple[tuple[str, int, int], ...]:
    excluded = {
        Path(path).expanduser().absolute()
        for path in excluded_paths
    }
    files: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        try:
            if path.is_dir():
                files.update(
                    candidate
                    for candidate in path.rglob("*.jsonl")
                    if candidate.is_file()
                    and candidate.expanduser().absolute() not in excluded
                )
            elif path.is_file():
                if path.expanduser().absolute() not in excluded:
                    files.add(path)
        except OSError:
            continue
    signature: list[tuple[str, int, int]] = []
    for path in sorted(files):
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append((str(path), int(stat.st_size), int(stat.st_mtime_ns)))
    return tuple(signature)


def process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_managed_process(
    proc: subprocess.Popen[bytes],
    *,
    grace_seconds: float,
) -> tuple[bytes | None, bytes | None, bool]:
    partial_stdout: bytes | None = None
    partial_stderr: bytes | None = None
    complete_stdout: bytes | None = None
    complete_stderr: bytes | None = None
    forced = False
    deadline = time.monotonic() + max(0.0, grace_seconds)
    if proc.poll() is None:
        proc.terminate()
    remaining = max(0.01, deadline - time.monotonic())
    try:
        complete_stdout, complete_stderr = proc.communicate(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        partial_stdout = exc.stdout
        partial_stderr = exc.stderr
    while process_group_exists(proc.pid) and time.monotonic() < deadline:
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    if process_group_exists(proc.pid):
        forced = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if complete_stdout is None or complete_stderr is None:
        try:
            final_stdout, final_stderr = proc.communicate(
                timeout=PROCESS_FINAL_REAP_SECONDS
            )
        except subprocess.TimeoutExpired as exc:
            final_stdout = exc.stdout
            final_stderr = exc.stderr
        complete_stdout = final_stdout if final_stdout is not None else partial_stdout
        complete_stderr = final_stderr if final_stderr is not None else partial_stderr
    return complete_stdout, complete_stderr, forced


def runtime_watchdog_stop_detail(run: Mapping[str, Any]) -> str:
    timeout_reason = str(run.get("timeout_reason") or "")
    if timeout_reason in SUPERVISOR_RUNTIME_TIMEOUT_REASONS:
        return timeout_reason
    if (
        run.get("network_terminal_observed") is True
        and run.get("network_terminal_reason") == "network_recovery_timeout"
    ):
        return "network_recovery_timeout"
    return ""


def append_runtime_watchdog_stop_event(
    state_path: str | Path | None,
    *,
    timeout_reason: str,
    platform_key: str,
    start_page: int,
    start_offset: int | None,
    start_cursor: str | None,
    inactivity_timeout_seconds: float,
    last_progress_age_seconds: float,
    network_pause_total_seconds: float = 0.0,
    network_pause_ceiling_seconds: float | None = None,
    network_terminal_grace_seconds: float | None = None,
) -> dict[str, Any]:
    if timeout_reason not in RUNTIME_WATCHDOG_STOP_DETAILS:
        raise ValueError(f"unsupported runtime watchdog stop detail: {timeout_reason}")
    if not state_path:
        return {"skipped": True, "reason": "execution_state_unavailable"}
    state = FrozenExecutionState(state_path)
    try:
        payload = state.load()
        events = [event for event in payload.get("events") or [] if isinstance(event, dict)]
        if any(event.get("type") == "adaptive_search_stopped" for event in events):
            return {"skipped": True, "reason": "terminal_event_already_present"}
        latest_batch: dict[str, Any] = {}
        for event in events:
            if event.get("type") != "adaptive_batch_completed":
                continue
            details = event.get("details")
            if isinstance(details, dict):
                latest_batch = details
        details = {
            key: latest_batch.get(key)
            for key in PAGINATION_EVENT_FIELDS
            if key in latest_batch
        }
        details.update(
            {
                "platform": platform_key,
                "source_page": latest_batch.get("source_page", start_page),
                "source_offset": latest_batch.get("source_offset", start_offset),
                "source_cursor": latest_batch.get("source_cursor", start_cursor),
                "resume_page": latest_batch.get("resume_page", start_page),
                "resume_offset": latest_batch.get("resume_offset", start_offset),
                "resume_cursor": latest_batch.get("resume_cursor", start_cursor),
                "source_has_more": True,
                "batch_complete": False,
                "stop_reason": "runtime_failed",
                "stop_detail": timeout_reason,
                "inactivity_timeout_seconds": round(inactivity_timeout_seconds, 3),
                "last_progress_age_seconds": round(last_progress_age_seconds, 3),
                "network_pause_total_seconds": round(
                    network_pause_total_seconds,
                    3,
                ),
                "network_pause_ceiling_seconds": network_pause_ceiling_seconds,
                "network_terminal_grace_seconds": network_terminal_grace_seconds,
            }
        )
        state.append_event("adaptive_search_stopped", details)
    except Exception as exc:
        return {"skipped": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"skipped": False, "event": "adaptive_search_stopped", "details": details}


def run_command(
    cmd: list[str],
    cwd: Path,
    timeout: float,
    log_dir: Path,
    *,
    extra_env: dict[str, str] | None = None,
    progress_paths: Iterable[Path] | None = None,
    runtime_reporter: XhsSupervisorRuntimeReporter | None = None,
    network_diagnostics_path: str | Path | None = None,
    startup_grace_seconds: float = 0.0,
    poll_seconds: float = PROCESS_PROGRESS_POLL_SECONDS,
    cleanup_grace_seconds: float = PROCESS_CLEANUP_GRACE_SECONDS,
) -> dict[str, Any]:
    log_dir = ensure_dir(log_dir)
    env = browser_launch_environment()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra_env:
        env.update(extra_env)
    env.pop(RUNTIME_STATUS_AUTH_KEY_ENV, None)
    registration_env = dict(env)
    child_env = dict(registration_env)
    for private_lease_key in (LEASE_DB_ENV, LEASE_ID_ENV, LEASE_OWNER_TOKEN_ENV):
        child_env.pop(private_lease_key, None)
    started = time.monotonic()
    stdout = ""
    stderr = ""
    returncode = 0
    timed_out = False
    timeout_reason: str | None = None
    forced_termination = False
    progress_observed = False
    last_progress_at = started
    last_progress_age_seconds = 0.0
    network_pause_clock = XhsParentNetworkPauseClock(
        ceiling_seconds=XHS_PARENT_NETWORK_PAUSE_CEILING_SECONDS
    )
    network_terminal_grace_started_at: float | None = None
    tracked_paths = tuple(progress_paths) if progress_paths is not None else None
    excluded_progress_paths: set[Path] = set()
    if runtime_reporter is not None and tracked_paths is not None:
        runtime_status_file = runtime_reporter.status_path.expanduser().absolute()
        excluded_progress_paths.add(runtime_status_file)
        if network_diagnostics_path is not None:
            excluded_progress_paths.add(
                Path(network_diagnostics_path).expanduser().absolute()
            )
        tracked_paths = tuple(
            path
            for path in tracked_paths
            if Path(path).expanduser().absolute() != runtime_status_file
        )
    progress_signature = (
        progress_path_signature(
            tracked_paths,
            excluded_paths=excluded_progress_paths,
        )
        if tracked_paths is not None
        else ()
    )
    proc: subprocess.Popen[bytes] | None = None
    gated: GatedSubprocess | None = None
    lease_process_identity = None
    exporter_identity_mismatch = False
    lease_registration_enabled = all(
        str(registration_env.get(key) or "")
        for key in (LEASE_DB_ENV, LEASE_ID_ENV, LEASE_OWNER_TOKEN_ENV)
    )
    if runtime_reporter is not None and (
        tracked_paths is None or not lease_registration_enabled
    ):
        raise XhsRuntimeSupervisionError(
            "XHS runtime reporter requires progress tracking and lease registration"
        )
    process_inspector = (
        SystemProcessInspector()
        if lease_registration_enabled or runtime_reporter is not None
        else None
    )
    try:
        if not lease_registration_enabled:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        else:
            deferred_signals = DeferredTerminationSignals()
            deferred_signals.install()
            try:
                gated = spawn_gated_subprocess(
                    cmd,
                    cwd=cwd,
                    env=child_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                proc = gated.process
                if deferred_signals.signal_received is not None:
                    raise InterruptedError(
                        "termination signal received before exporter registration"
                    )
                lease_process_identity = register_lease_process_from_environment(
                    pid=proc.pid,
                    process_role="exporter",
                    environ=registration_env,
                    inspector=process_inspector,
                )
                if runtime_reporter is not None and lease_process_identity is None:
                    raise XhsRuntimeSupervisionError(
                        "XHS runtime reporter requires an exact registered exporter identity"
                    )
                if deferred_signals.signal_received is not None:
                    raise InterruptedError(
                        "termination signal received before exporter gate release"
                    )
                gated.release()
                if deferred_signals.signal_received is not None:
                    raise InterruptedError(
                        "termination signal received during exporter gate release"
                    )
            except BaseException:
                try:
                    if gated is not None:
                        gated.cancel(grace_seconds=cleanup_grace_seconds)
                    if (
                        lease_process_identity is not None
                        and proc is not None
                        and proc.poll() is not None
                    ):
                        mark_lease_process_exited_from_environment(
                            identity=lease_process_identity,
                            process_role="exporter",
                            environ=registration_env,
                        )
                        lease_process_identity = None
                finally:
                    deferred_signals.restore()
                    if deferred_signals.signal_received is not None:
                        deferred_signals.replay()
                raise
            deferred_signals.restore()
            if deferred_signals.signal_received is not None:
                deferred_signals.replay()
        assert proc is not None
        while True:
            now = time.monotonic()
            progress_advanced = False
            if tracked_paths is not None:
                current_signature = progress_path_signature(
                    tracked_paths,
                    excluded_paths=excluded_progress_paths,
                )
                if current_signature != progress_signature:
                    progress_signature = current_signature
                    progress_observed = True
                    progress_advanced = True
                    last_progress_at = now
                current_budget = timeout + (
                    0.0 if progress_observed else max(0.0, startup_grace_seconds)
                )
                remaining = current_budget - (now - last_progress_at)
                communicate_timeout = min(max(0.01, poll_seconds), max(0.01, remaining))
            else:
                remaining = timeout
                communicate_timeout = timeout
            network_state = "unknown"
            network_reason = ""
            if runtime_reporter is not None:
                exporter_alive = proc.poll() is None
                exporter_identity_current = bool(
                    exporter_alive
                    and lease_process_identity is not None
                    and process_inspector is not None
                    and process_inspector.identity(proc.pid) == lease_process_identity
                )
                if exporter_identity_current:
                    network_state, network_reason = xhs_network_state_from_diagnostics(
                        network_diagnostics_path
                    )
                    runtime_network_state = network_state
                    runtime_network_reason = network_reason
                    if network_state == "network_recovery_timeout":
                        runtime_network_state = "unknown"
                        runtime_network_reason = ""
                        if network_terminal_grace_started_at is None:
                            network_terminal_grace_started_at = now
                    # Ordinary expiry rounds must not manufacture another
                    # heartbeat.  A fresh explicit pause is the sole exception:
                    # it has to be published before the timeout decision below.
                    # A fresh terminal event gets one final heartbeat while the
                    # child commits its checkpoint and unwinds.
                    if remaining > 0 or network_state in {
                        "network_paused",
                        "network_recovery_timeout",
                    }:
                        runtime_reporter.checkpoint(
                            phase="running",
                            network_state=runtime_network_state,
                            network_reason=runtime_network_reason,
                        )
                elif exporter_alive:
                    exporter_identity_mismatch = True
                    terminate_managed_process(
                        proc,
                        grace_seconds=cleanup_grace_seconds,
                    )
                    raise XhsRuntimeSupervisionError(
                        "xhs_exporter_process_identity_changed"
                    )

            pause_ceiling_expired = False
            if tracked_paths is not None:
                pause_delta, pause_ceiling_expired = network_pause_clock.observe(
                    network_state,
                    now=now,
                )
                if not progress_advanced:
                    last_progress_at += pause_delta
            if tracked_paths is not None and network_state == "network_paused":
                current_budget = timeout + (
                    0.0 if progress_observed else max(0.0, startup_grace_seconds)
                )
                remaining = current_budget - (now - last_progress_at)
                communicate_timeout = max(0.01, poll_seconds)
            else:
                if tracked_paths is not None:
                    current_budget = timeout + (
                        0.0 if progress_observed else max(0.0, startup_grace_seconds)
                    )
                    remaining = current_budget - (now - last_progress_at)
                    communicate_timeout = min(
                        max(0.01, poll_seconds), max(0.01, remaining)
                    )

            expiration_reason: str | None = None
            terminal_grace_elapsed = (
                max(0.0, now - network_terminal_grace_started_at)
                if network_terminal_grace_started_at is not None
                else None
            )
            if terminal_grace_elapsed is not None:
                if (
                    terminal_grace_elapsed
                    >= XHS_CHILD_NETWORK_TERMINAL_GRACE_SECONDS
                ):
                    expiration_reason = "parent_network_terminal_unwind_timeout"
            elif pause_ceiling_expired:
                expiration_reason = "parent_network_pause_timeout"
            elif (
                remaining <= 0
                and network_state != "network_paused"
                and network_terminal_grace_started_at is None
            ):
                expiration_reason = (
                    "no_progress_timeout"
                    if tracked_paths is not None
                    else "wall_clock_timeout"
                )
            if expiration_reason is not None:
                # Prefer a child terminal result that won the boundary race.
                # In particular, do not rewrite its network_recovery_timeout as
                # a parent watchdog failure merely because both clocks expired
                # in the same polling round.
                observed_returncode = proc.poll()
                if observed_returncode is not None:
                    stdout_data, stderr_data = proc.communicate()
                    stdout = decode_text(stdout_data)
                    stderr = decode_text(stderr_data)
                    returncode = int(observed_returncode)
                    last_progress_age_seconds = max(0.0, now - last_progress_at)
                    break
                timed_out = True
                returncode = 124
                timeout_reason = expiration_reason
                last_progress_age_seconds = max(0.0, now - last_progress_at)
                stdout_data, stderr_data, forced_termination = terminate_managed_process(
                    proc,
                    grace_seconds=cleanup_grace_seconds,
                )
                stdout = decode_text(stdout_data)
                stderr = decode_text(stderr_data)
                break
            if network_terminal_grace_started_at is not None:
                communicate_timeout = min(
                    max(0.01, poll_seconds),
                    max(
                        0.01,
                        XHS_CHILD_NETWORK_TERMINAL_GRACE_SECONDS
                        - float(terminal_grace_elapsed or 0.0),
                    ),
                )
            try:
                stdout_data, stderr_data = proc.communicate(timeout=communicate_timeout)
            except subprocess.TimeoutExpired:
                if tracked_paths is None:
                    timed_out = True
                    returncode = 124
                    timeout_reason = "wall_clock_timeout"
                    last_progress_age_seconds = max(
                        0.0,
                        time.monotonic() - last_progress_at,
                    )
                    (
                        stdout_data,
                        stderr_data,
                        forced_termination,
                    ) = terminate_managed_process(
                        proc,
                        grace_seconds=cleanup_grace_seconds,
                    )
                    stdout = decode_text(stdout_data)
                    stderr = decode_text(stderr_data)
                    break
                continue
            stdout = decode_text(stdout_data)
            stderr = decode_text(stderr_data)
            returncode = int(proc.returncode or 0)
            last_progress_age_seconds = max(0.0, time.monotonic() - last_progress_at)
            break
    except BaseException:
        if proc is not None and getattr(proc, "poll", lambda: proc.returncode)() is None:
            try:
                terminate_managed_process(
                    proc,
                    grace_seconds=cleanup_grace_seconds,
                )
            except Exception:
                pass
        raise
    finally:
        if lease_process_identity is not None and proc is not None and proc.returncode is not None:
            mark_lease_process_exited_from_environment(
                identity=lease_process_identity,
                process_role="exporter",
                environ=registration_env,
            )

    _, avatar_output_detected = redact_author_avatar_text(f"{stdout}\n{stderr}")
    if avatar_output_detected:
        stdout = AUTHOR_AVATAR_LOG_REDACTION
        stderr = AUTHOR_AVATAR_LOG_REDACTION
    stdout_log = log_dir / "stdout.log"
    stderr_log = log_dir / "stderr.log"
    command_log = log_dir / "command.txt"
    stdout_log.write_text(stdout, encoding="utf-8")
    stderr_log.write_text(stderr, encoding="utf-8")
    command_log.write_text(shlex.join(cmd), encoding="utf-8")
    return {
        "command": cmd,
        "command_text": shlex.join(cmd),
        "returncode": returncode,
        "timed_out": timed_out,
        "timeout_reason": timeout_reason,
        "inactivity_timeout_seconds": timeout if tracked_paths is not None else None,
        "startup_grace_seconds": (
            startup_grace_seconds if tracked_paths is not None else None
        ),
        "progress_observed": progress_observed,
        "last_progress_age_seconds": round(last_progress_age_seconds, 2),
        "network_pause_observed": network_pause_clock.observed,
        "network_pause_total_seconds": round(network_pause_clock.total_seconds, 2),
        "network_pause_ceiling_seconds": (
            XHS_PARENT_NETWORK_PAUSE_CEILING_SECONDS
            if runtime_reporter is not None and tracked_paths is not None
            else None
        ),
        "network_terminal_grace_seconds": (
            XHS_CHILD_NETWORK_TERMINAL_GRACE_SECONDS
            if runtime_reporter is not None and tracked_paths is not None
            else None
        ),
        "network_terminal_observed": network_terminal_grace_started_at is not None,
        "network_terminal_reason": (
            "network_recovery_timeout"
            if network_terminal_grace_started_at is not None
            else None
        ),
        "forced_termination": forced_termination,
        "cleanup_grace_seconds": cleanup_grace_seconds,
        "exporter_identity_mismatch": exporter_identity_mismatch,
        "runtime_status": (
            runtime_reporter.snapshot() if runtime_reporter is not None else None
        ),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "command_log": str(command_log),
        "stdout_tail": tail(stdout),
        "stderr_tail": tail(stderr),
    }


def skipped_command(cmd: list[str], log_dir: Path, reason: str) -> dict[str, Any]:
    log_dir = ensure_dir(log_dir)
    stdout_log = log_dir / "stdout.log"
    stderr_log = log_dir / "stderr.log"
    command_log = log_dir / "command.txt"
    stdout_log.write_text("", encoding="utf-8")
    stderr_log.write_text(reason, encoding="utf-8")
    command_log.write_text(shlex.join(cmd), encoding="utf-8")
    return {
        "command": cmd,
        "command_text": shlex.join(cmd),
        "returncode": 1,
        "timed_out": False,
        "elapsed_seconds": 0,
        "skipped": True,
        "reason": reason,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "command_log": str(command_log),
        "stdout_tail": "",
        "stderr_tail": reason,
    }


PAGINATION_EVENT_FIELDS = (
    "platform",
    "batch_no",
    "candidate_count",
    "valid_new_count",
    "valid_existing_count",
    "batch_new_count",
    "batch_candidate_identity_count",
    "source_page",
    "source_offset",
    "source_cursor",
    "next_cursor",
    "resume_page",
    "resume_offset",
    "resume_cursor",
    "batch_complete",
    "discovery_phase",
    "source_has_more",
    "raw_batch_count",
    "raw_response_count",
    "stagnant_batches",
    "stagnation_basis",
    "stop_reason",
    "stop_detail",
    "candidate_identities",
    "skipped_candidate_count",
    "skipped_candidate_failures",
)
