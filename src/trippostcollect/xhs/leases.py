"""Exact-owner Xiaohongshu leases and safe orphan reconciliation."""

from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import hashlib
import json
import math
import os
import secrets
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import FrameType
from typing import Any, Callable, Mapping, Sequence

from trippostcollect.core.paths import ensure_dir
from trippostcollect.xhs.accounts import (
    XhsAccountUnavailable,
    account_lock_path,
    ensure_xhs_schema,
    get_account,
    iso,
    parse_iso,
    record_event,
    validate_account_id,
)
from trippostcollect.xhs.runtime import (
    RUNTIME_STATUS_AUTH_KEY_ENV,
    RuntimeStatusValidationError,
    canonical_runtime_profile_dir,
    prepare_runtime_session as create_runtime_session,
    public_runtime_status,
    read_runtime_status_if_present,
    remove_runtime_session_for_profile,
    runtime_session_actually_absent,
    runtime_status_path,
    verify_runtime_session_claim,
)


LEASE_IDENTITY_VERSION = 1
DEFAULT_CHILD_SHUTDOWN_BUDGET_SECONDS = 30
DEFAULT_ROOT_FINALIZE_BUDGET_SECONDS = 270
LEASE_DB_ENV = "TRIPPOSTCOLLECT_XHS_LEASE_DB"
LEASE_ID_ENV = "TRIPPOSTCOLLECT_XHS_LEASE_ID"
LEASE_OWNER_TOKEN_ENV = "TRIPPOSTCOLLECT_XHS_LEASE_OWNER_TOKEN"
_IDENTITY_PROBE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
IDENTITY_PROBE_TIMEOUT_SECONDS = 2.0
SECOND_SIGNAL_FINALIZE_GRACE_SECONDS = 1.0
# 操作人中断已转发给 child（中间层）后，child 关闭预算中留给 SIGKILL 后回收的上限；其余全部是
# child 自行收束 exporter、落盘日志并登记 exporter 退出的宽限，期间不再发第二次温和信号。
CHILD_INTERRUPT_KILL_REAP_SECONDS = 5
# child 已退出后回收其管道输出的上限：后代仍占着管道时不无限等待，残留进程交给登记进程组收束。
CHILD_EXIT_PIPE_DRAIN_SECONDS = 5.0
_GATED_SUBPROCESS_RELEASE = b"G"
_GATED_SUBPROCESS_WRAPPER = """
import os
import signal
import sys

gate_fd = int(sys.argv[1])
try:
    release = os.read(gate_fd, 1)
finally:
    os.close(gate_fd)
if release != b"G":
    os._exit(125)
for signum in (signal.SIGINT, signal.SIGTERM):
    signal.signal(signum, signal.SIG_DFL)
os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
"""


class XhsLeaseOwnershipError(RuntimeError):
    pass


class XhsLeaseProcessesAlive(RuntimeError):
    pass


class XhsIdentityProbeTimeout(RuntimeError):
    pass


class XhsOrphanLeaseRecoveryRefused(RuntimeError):
    pass


class XhsLeaseSignal(BaseException):

    def __init__(self, signum: int):
        self.signum = int(signum)
        super().__init__(f"XHS lease owner interrupted by signal {self.signum}")


class DeferredTerminationSignals:
    """Latch termination signals while a gated child is not yet registered."""

    def __init__(
        self,
        signums: Sequence[int] = (signal.SIGINT, signal.SIGTERM),
    ) -> None:
        self.signal_received: int | None = None
        self._signums = tuple(signums)
        self._previous_handlers: dict[int, Any] = {}
        self._installed = False

    def __enter__(self) -> DeferredTerminationSignals:
        return self.install()

    def install(self) -> DeferredTerminationSignals:
        if self._installed:
            raise RuntimeError("termination signal deferral is already installed")
        signums = set(self._signums)
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, signums)
        try:
            for signum in self._signums:
                try:
                    previous = signal.getsignal(signum)
                    if previous == signal.SIG_IGN:
                        continue
                    signal.signal(signum, self._handle)
                except (ValueError, OSError):
                    continue
                self._previous_handlers[signum] = previous
            self._installed = True
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: FrameType | None,
    ) -> bool:
        self.restore()
        return False

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        if self.signal_received is None:
            self.signal_received = int(signum)

    def restore(self) -> None:
        if not self._installed:
            return
        signums = set(self._signums)
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, signums)
        try:
            for signum, handler in self._previous_handlers.items():
                try:
                    signal.signal(signum, handler)
                except (ValueError, OSError):
                    pass
            self._installed = False
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    def replay(self) -> None:
        signum = self.signal_received
        if signum is None:
            return
        previous = self._previous_handlers.get(signum, signal.SIG_DFL)
        self.restore()
        if previous == signal.SIG_IGN:
            return
        if previous == signal.SIG_DFL:
            signal.raise_signal(signum)
            return
        previous(signum, None)


@dataclass
class GatedSubprocess:
    """A new process group whose target command cannot exec before release."""

    process: subprocess.Popen[Any]
    _release_fd: int | None
    released: bool = False

    def release(self) -> None:
        if self.released or self._release_fd is None:
            raise RuntimeError("gated subprocess has no releasable gate")
        release_fd = self._release_fd
        self._release_fd = None
        try:
            written = os.write(release_fd, _GATED_SUBPROCESS_RELEASE)
        finally:
            os.close(release_fd)
        if written != len(_GATED_SUBPROCESS_RELEASE):
            raise RuntimeError("gated subprocess release was incomplete")
        self.released = True

    def cancel(self, *, grace_seconds: float) -> None:
        if self._release_fd is not None:
            os.close(self._release_fd)
            self._release_fd = None
        proc = self.process
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        first_budget = max(0.01, float(grace_seconds) / 2)
        try:
            proc.communicate(timeout=first_budget)
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.communicate(timeout=max(0.01, float(grace_seconds) - first_budget))


def spawn_gated_subprocess(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdout: Any,
    stderr: Any,
    text: bool = False,
) -> GatedSubprocess:
    """Spawn a blocked wrapper; the target command starts only after ``release``."""

    if os.name != "posix":
        raise RuntimeError("gated subprocesses require POSIX pass_fds and process groups")
    normalized_command = [os.fspath(part) for part in command]
    if not normalized_command:
        raise ValueError("gated subprocess command must not be empty")
    interpreter = os.path.realpath(sys.executable)
    if not os.path.isabs(interpreter) or not os.access(interpreter, os.X_OK):
        raise RuntimeError("current Python interpreter is not an executable absolute path")
    read_fd, write_fd = os.pipe()
    try:
        proc = subprocess.Popen(
            [
                interpreter,
                "-c",
                _GATED_SUBPROCESS_WRAPPER,
                str(read_fd),
                *normalized_command,
            ],
            cwd=str(cwd),
            env=dict(env),
            stdout=stdout,
            stderr=stderr,
            text=text,
            start_new_session=True,
            pass_fds=(read_fd,),
        )
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    os.close(read_fd)
    return GatedSubprocess(process=proc, _release_fd=write_fd)


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


def interrupted_child_kill_reap_seconds(budget: LeaseBudget) -> int:
    return min(CHILD_INTERRUPT_KILL_REAP_SECONDS, budget.child_shutdown_seconds // 2)


def interrupted_child_grace_seconds(budget: LeaseBudget) -> int:
    """自向 child 转发操作人中断起，child 自行退出的宽限；之后才 SIGKILL 兜底。"""

    return budget.child_shutdown_seconds - interrupted_child_kill_reap_seconds(budget)


def close_process_pipes(proc: subprocess.Popen[Any]) -> None:
    """显式关闭父侧管道，不等 GC；仍持有写端的后代此后写入得到 EPIPE。"""

    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _text_output(proc: subprocess.Popen[Any], value: Any) -> Any:
    # TimeoutExpired 携带的累计输出始终是 bytes；文本模式下按 Popen 的编码语义还原。
    if isinstance(value, bytes) and getattr(proc, "text_mode", False):
        return value.decode("utf-8", errors="replace")
    return value


def drain_exited_child_output(
    proc: subprocess.Popen[Any],
    *,
    timeout: float = CHILD_EXIT_PIPE_DRAIN_SECONDS,
) -> tuple[Any, Any]:
    """有界回收 child 输出；超时说明后代仍占着管道，保留累计输出并显式关闭管道。"""

    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        close_process_pipes(proc)
        return _text_output(proc, exc.stdout), _text_output(proc, exc.stderr)


def crawl_lease_budget(*, timeout_seconds: int, configured_lease_seconds: int) -> LeaseBudget:
    budget = LeaseBudget(runtime_seconds=int(timeout_seconds))
    if int(configured_lease_seconds) < budget.lease_seconds:
        raise ValueError(
            "configured XHS lease ceiling does not cover runtime, child shutdown, and root finalization"
        )
    return budget


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


class _DarwinProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


@dataclass(frozen=True)
class LeaseSubprocessResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    termination_reason: str | None = None
    runtime_status: dict[str, Any] | None = None
    watchdog_resume_grace_used: bool = False


@dataclass(frozen=True)
class RuntimeStatusWatchdogPolicy:
    """Parent-side liveness bounds; status is never durable crawl progress."""

    startup_grace_seconds: float
    stale_after_seconds: float
    poll_seconds: float = 5.0
    resume_grace_seconds: float = 0.0

    def __post_init__(self) -> None:
        values = {
            "startup_grace_seconds": self.startup_grace_seconds,
            "stale_after_seconds": self.stale_after_seconds,
            "poll_seconds": self.poll_seconds,
            "resume_grace_seconds": self.resume_grace_seconds,
        }
        normalized: dict[str, float] = {}
        for field, raw_value in values.items():
            if (
                isinstance(raw_value, bool)
                or not isinstance(raw_value, (int, float))
                or not math.isfinite(float(raw_value))
            ):
                raise ValueError(f"{field} must be a finite number")
            normalized[field] = float(raw_value)
            object.__setattr__(self, field, normalized[field])
        if normalized["startup_grace_seconds"] < 0:
            raise ValueError("startup_grace_seconds cannot be negative")
        if normalized["stale_after_seconds"] <= 0:
            raise ValueError("stale_after_seconds must be positive")
        if normalized["poll_seconds"] <= 0:
            raise ValueError("poll_seconds must be positive")
        if normalized["poll_seconds"] > normalized["stale_after_seconds"]:
            raise ValueError("poll_seconds cannot exceed stale_after_seconds")
        if not 0 <= normalized["resume_grace_seconds"] <= normalized["stale_after_seconds"]:
            raise ValueError(
                "resume_grace_seconds must be between zero and stale_after_seconds"
            )


@dataclass
class _ParentRuntimeWatchdogState:
    policy: RuntimeStatusWatchdogPolicy
    started_at: float
    last_poll_at: float
    last_sequence: int | None = None
    last_sequence_received_at: float | None = None
    last_status: dict[str, Any] | None = None
    resume_granted_sequence: int | None = None
    resume_deadline: float | None = None

    def observe(
        self,
        status: Mapping[str, Any] | None,
        *,
        received_at: float,
    ) -> str | None:
        if received_at < self.last_poll_at:
            return "parent_monotonic_regressed"
        poll_gap = received_at - self.last_poll_at
        self.last_poll_at = received_at
        if status is None and self.last_sequence is not None:
            return "runtime_status_missing_after_observation"
        if status is not None:
            sequence = int(status["sequence"])
            observed = dict(status)
            if self.last_sequence is None or sequence > self.last_sequence:
                self.last_sequence = sequence
                self.last_sequence_received_at = received_at
                self.last_status = observed
                self.resume_deadline = None
                return None
            if sequence < self.last_sequence:
                return "runtime_status_sequence_regressed"
            if observed != self.last_status:
                return "runtime_status_sequence_reused"

        deadline = (
            self.started_at + self.policy.startup_grace_seconds
            if self.last_sequence_received_at is None
            else self.last_sequence_received_at + self.policy.stale_after_seconds
        )
        if received_at <= deadline:
            return None
        current_sequence = self.last_sequence or 0
        if (
            self.policy.resume_grace_seconds > 0
            and poll_gap > self.policy.stale_after_seconds
            and self.resume_granted_sequence != current_sequence
        ):
            self.resume_granted_sequence = current_sequence
            self.resume_deadline = received_at + self.policy.resume_grace_seconds
            return None
        if self.resume_deadline is not None and received_at <= self.resume_deadline:
            return None
        return (
            "runtime_status_startup_timeout"
            if self.last_sequence is None
            else "runtime_status_stale"
        )

    def public_status(self) -> dict[str, Any] | None:
        return (
            public_runtime_status(self.last_status)
            if self.last_status is not None
            else None
        )


def _read_nonempty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _identity_probe_environment() -> dict[str, str]:
    return {
        "LC_ALL": "C",
        "PATH": _IDENTITY_PROBE_PATH,
    }


def _sysctl_value(name: str) -> str:
    try:
        result = subprocess.run(
            ["sysctl", "-n", name],
            check=True,
            capture_output=True,
            text=True,
            env=_identity_probe_environment(),
            timeout=IDENTITY_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _darwin_platform_uuid() -> str:
    try:
        result = subprocess.run(
            ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            check=True,
            capture_output=True,
            text=True,
            env=_identity_probe_environment(),
            timeout=IDENTITY_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in result.stdout.splitlines():
        key, separator, raw_value = line.partition("=")
        if separator and key.strip() == '"IOPlatformUUID"':
            return raw_value.strip().strip('"')
    return ""


def system_host_id() -> str:
    value = _read_nonempty(Path("/etc/machine-id")) or _read_nonempty(
        Path("/var/lib/dbus/machine-id")
    )
    if not value and sys.platform == "darwin":
        value = _darwin_platform_uuid()
    if value:
        return value.lower()
    raise RuntimeError("cannot determine a stable host identity for exact XHS leases")


def system_boot_id() -> str:
    value = _read_nonempty(Path("/proc/sys/kernel/random/boot_id"))
    if not value and sys.platform == "darwin":
        value = _sysctl_value("kern.bootsessionuuid")
    if value:
        return value.lower()
    boot_marker = _sysctl_value("kern.boottime")
    if boot_marker:
        return f"fallback-{hashlib.sha256(boot_marker.encode('utf-8')).hexdigest()}"
    raise RuntimeError("cannot determine a boot identity for exact XHS leases")


def _wall_time_iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat(timespec="microseconds")


class SystemProcessInspector:

    def __init__(self) -> None:
        self.host_id = system_host_id()
        self.boot_id = system_boot_id()
        self._identity_probe_deadline: float | None = None
        self._darwin_libproc: Any = None
        if sys.platform == "darwin":
            try:
                library = ctypes.CDLL(
                    ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib",
                    use_errno=True,
                )
                library.proc_pidinfo.argtypes = [
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_uint64,
                    ctypes.c_void_p,
                    ctypes.c_int,
                ]
                library.proc_pidinfo.restype = ctypes.c_int
                self._darwin_libproc = library
            except (AttributeError, OSError):
                self._darwin_libproc = None

    def _identity_probe_timeout(self) -> float:
        deadline = getattr(self, "_identity_probe_deadline", None)
        if deadline is None:
            return IDENTITY_PROBE_TIMEOUT_SECONDS
        remaining = float(deadline) - time.monotonic()
        if remaining <= 0:
            raise XhsIdentityProbeTimeout(
                "root finalize deadline expired before identity probe"
            )
        return min(IDENTITY_PROBE_TIMEOUT_SECONDS, remaining)

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

    def process_presence(self, pid: int) -> bool | None:
        if sys.platform.startswith("linux") and Path("/proc").is_dir():
            try:
                raw = (Path("/proc") / str(int(pid)) / "stat").read_text(encoding="utf-8")
                close_paren = raw.rfind(")")
                state = raw[close_paren + 2 :].split()[0]
            except FileNotFoundError:
                return False
            except PermissionError:
                return None
            except (OSError, IndexError):
                return None
            return state != "Z"
        try:
            result = subprocess.run(
                ["ps", "-p", str(int(pid)), "-o", "state="],
                check=False,
                capture_output=True,
                text=True,
                env=_identity_probe_environment(),
                timeout=self._identity_probe_timeout(),
            )
        except subprocess.TimeoutExpired as exc:
            raise XhsIdentityProbeTimeout(
                "process presence probe exceeded its bounded timeout"
            ) from exc
        except OSError:
            result = None
        if result is not None and result.returncode == 0:
            state = result.stdout.strip()
            return bool(state) and not state.startswith("Z")
        if result is not None and result.returncode == 1:
            return False
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None
        return True

    def _darwin_identity(self, pid: int) -> ProcessIdentity | None:
        if self._darwin_libproc is None:
            return None
        info = _DarwinProcBsdInfo()
        received = self._darwin_libproc.proc_pidinfo(
            int(pid),
            3,
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if received != ctypes.sizeof(info) or int(info.pbi_pid) != int(pid):
            return None
        if int(info.pbi_status) == 5:
            return None
        started_epoch = float(info.pbi_start_tvsec) + float(info.pbi_start_tvusec) / 1_000_000
        return ProcessIdentity(
            host_id=self.host_id,
            boot_id=self.boot_id,
            pid=int(info.pbi_pid),
            process_started_at=_wall_time_iso(started_epoch),
            process_start_token=(
                f"darwin:{int(info.pbi_start_tvsec)}:{int(info.pbi_start_tvusec)}"
            ),
            pgid=int(info.pbi_pgid),
        )

    def identity(self, pid: int) -> ProcessIdentity | None:
        if sys.platform.startswith("linux") and Path("/proc").is_dir():
            return self._linux_identity(int(pid))
        if sys.platform == "darwin":
            return self._darwin_identity(int(pid))
        return None

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
                ["ps", "-ww", "-axo", "pid=,state=,command="],
                check=True,
                capture_output=True,
                text=True,
                env=_identity_probe_environment(),
                timeout=self._identity_probe_timeout(),
            )
        except subprocess.TimeoutExpired as exc:
            raise XhsIdentityProbeTimeout(
                "process snapshot probe exceeded its bounded timeout"
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("cannot enumerate processes for XHS lease safety") from exc
        snapshots: list[ProcessSnapshot] = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(maxsplit=2)
            if len(parts) < 2 or parts[1].startswith("Z"):
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            command = parts[2] if len(parts) == 3 else ""
            try:
                argv = tuple(shlex.split(command))
            except ValueError:
                argv = (command,)
            identity = self.identity(pid)
            if identity is None:
                continue
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

    def __init__(self, path: Path | Sequence[Path]):
        values = (path,) if isinstance(path, Path) else tuple(path)
        if not values:
            raise ValueError("at least one XHS lock path is required")
        unique: list[Path] = []
        for value in values:
            normalized = Path(value).expanduser().absolute()
            if normalized not in unique:
                unique.append(normalized)
        self.paths = tuple(unique)
        self.path = self.paths[-1]
        self._handles: list[Any] = []

    def acquire(self) -> None:
        if self._handles:
            raise RuntimeError("XHS account lock is already acquired")
        try:
            for path in self.paths:
                ensure_dir(path.parent)
                handle = path.open("a+b")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BaseException:
                    handle.close()
                    raise
                self._handles.append(handle)
        except BlockingIOError:
            self.release()
            raise XhsAccountUnavailable(
                "requested_xhs_account_local_lock_busy"
            ) from None
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        first_error: BaseException | None = None
        while self._handles:
            handle = self._handles.pop()
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            finally:
                try:
                    handle.close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
        if first_error is not None:
            raise first_error


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
    runtime_profile_dir: Path,
    budget: LeaseBudget,
    owner: ProcessIdentity,
    now: datetime | None = None,
    lease_id: str | None = None,
    owner_token: str | None = None,
) -> dict[str, Any]:
    if lease_kind not in {"crawl", "repair"}:
        raise ValueError(f"unsupported XHS lease kind: {lease_kind}")
    if not run_id.strip():
        raise ValueError("run_id must not be empty")
    current = now or datetime.now(timezone.utc)
    current_iso = iso(current)
    requested = validate_account_id(requested_account_id)
    exact_lease_id = lease_id or uuid.uuid4().hex
    exact_owner_token = owner_token or secrets.token_urlsafe(32)
    expires_at = iso(current + timedelta(seconds=budget.lease_seconds))
    resolved_runtime_profile = canonical_runtime_profile_dir(
        run_id,
        runtime_profile_dir,
    )

    conn.execute("BEGIN IMMEDIATE")
    try:
        account = conn.execute(
            "SELECT * FROM xhs_accounts WHERE account_id=?",
            (requested,),
        ).fetchone()
        if not account:
            raise XhsAccountUnavailable("requested_xhs_account_not_active")
        account_data = dict(account)
        if account_data["status"] != "active":
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
                runtime_profile_dir, acquired_at, heartbeat_at, expires_at, lease_duration_seconds,
                child_shutdown_budget_seconds, root_finalize_budget_seconds,
                identity_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                str(resolved_runtime_profile),
                current_iso,
                current_iso,
                expires_at,
                budget.lease_seconds,
                budget.child_shutdown_seconds,
                budget.root_finalize_seconds,
                LEASE_IDENTITY_VERSION,
            ),
        )
        conn.execute(
            "UPDATE xhs_accounts SET last_used_at=?, updated_at=datetime('now') WHERE account_id=?",
            (current_iso, requested),
        )
        record_event(
            conn,
            account_id=requested,
            run_id=run_id,
            event_type="lease_acquired",
            details={
                "lease_id": exact_lease_id,
                "lease_kind": lease_kind,
                "owner_token_sha256": _owner_token_digest(exact_owner_token),
                "owner": owner.public(),
                "execution_state_path": str(execution_state_path.expanduser().resolve()),
                "runtime_profile_dir": str(resolved_runtime_profile),
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
            "runtime_profile_dir": str(resolved_runtime_profile),
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
        presence_reader = getattr(inspector, "process_presence", None)
        presence = presence_reader(stored.pid) if callable(presence_reader) else None
        evidence["pid_presence"] = presence
        if presence is False:
            return {**evidence, "reason": "pid_absent"}
        return {**evidence, "live": None, "reason": "exact_process_identity_unavailable"}
    evidence["observed"] = observed.public()
    if (
        observed.process_start_token != stored.process_start_token
        or observed.process_started_at != stored.process_started_at
        or observed.pgid != stored.pgid
    ):
        return {**evidence, "reason": "pid_reused_or_identity_changed"}
    return {**evidence, "live": True, "reason": "exact_process_identity_alive"}


def _query_dicts(conn: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    cursor = conn.execute(query)
    columns = [str(item[0]) for item in cursor.description or ()]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def assess_legacy_lease_cutover(
    conn: sqlite3.Connection,
    *,
    account_profiles: Mapping[str, Path],
    inspector: SystemProcessInspector | None = None,
) -> dict[str, Any]:
    """Prove that pre-run-scoped owners and browser processes are gone."""

    process_inspector = inspector or SystemProcessInspector()
    checks: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = []
    lease_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(xhs_account_leases)")
    }
    lease_rows = (
        _query_dicts(conn, "SELECT * FROM xhs_account_leases ORDER BY rowid")
        if lease_columns
        else []
    )
    owner_columns = {
        "account_id",
        "lease_id",
        "owner_host_id",
        "owner_boot_id",
        "owner_pid",
        "owner_process_started_at",
        "owner_process_start_token",
        "owner_pgid",
        "identity_version",
    }
    process_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(xhs_lease_processes)")
    }
    required_process_columns = {
        "lease_id",
        "process_role",
        "host_id",
        "boot_id",
        "pid",
        "process_started_at",
        "process_start_token",
        "pgid",
        "exited_at",
    }

    if lease_rows and not owner_columns.issubset(lease_columns):
        blocking.append(
            {
                "role": "legacy_lease_schema",
                "live": None,
                "reason": "legacy_owner_identity_unavailable",
                "missing_columns": sorted(owner_columns - lease_columns),
            }
        )
    elif lease_rows and not required_process_columns.issubset(process_columns):
        blocking.append(
            {
                "role": "legacy_process_registry",
                "live": None,
                "reason": "legacy_process_registry_unavailable",
                "missing_columns": sorted(required_process_columns - process_columns),
            }
        )
    else:
        for lease in lease_rows:
            try:
                if int(lease.get("identity_version") or 0) != LEASE_IDENTITY_VERSION:
                    raise ValueError("unsupported identity version")
                stored = _stored_identity(lease, prefix="owner_")
                if not all(
                    (
                        stored.host_id,
                        stored.boot_id,
                        stored.process_started_at,
                        stored.process_start_token,
                    )
                ) or stored.pid <= 0 or stored.pgid <= 0:
                    raise ValueError("incomplete owner identity")
            except (KeyError, TypeError, ValueError) as exc:
                blocking.append(
                    {
                        "role": "legacy_lease_owner",
                        "account_id": lease.get("account_id"),
                        "lease_id": lease.get("lease_id"),
                        "live": None,
                        "reason": "legacy_owner_identity_invalid",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            owner_check = {
                "role": "legacy_lease_owner",
                "account_id": lease["account_id"],
                "lease_id": lease["lease_id"],
                **_identity_assessment(stored, process_inspector),
            }
            checks.append(owner_check)
            if owner_check["live"] is not False:
                blocking.append(owner_check)

        if lease_rows and not blocking:
            for row in _query_dicts(
                conn,
                "SELECT * FROM xhs_lease_processes ORDER BY lease_id, process_role, pid",
            ):
                try:
                    stored = _stored_identity(row)
                    if not all(
                        (
                            stored.host_id,
                            stored.boot_id,
                            stored.process_started_at,
                            stored.process_start_token,
                        )
                    ) or stored.pid <= 0 or stored.pgid <= 0:
                        raise ValueError("incomplete process identity")
                except (KeyError, TypeError, ValueError) as exc:
                    blocking.append(
                        {
                            "role": "legacy_registered_process",
                            "lease_id": row.get("lease_id"),
                            "live": None,
                            "reason": "legacy_process_identity_invalid",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue
                process_check = {
                    "role": str(row["process_role"]),
                    "lease_id": row["lease_id"],
                    "exited_at": row.get("exited_at"),
                    **_identity_assessment(stored, process_inspector),
                }
                checks.append(process_check)
                if process_check["live"] is not False:
                    blocking.append(process_check)
                    continue
                if row["process_role"] in {"child", "exporter"}:
                    group_reader = getattr(process_inspector, "group_members", None)
                    if not callable(group_reader):
                        blocking.append(
                            {
                                "role": f"{row['process_role']}_process_group",
                                "lease_id": row["lease_id"],
                                "live": None,
                                "reason": "legacy_process_group_unverifiable",
                            }
                        )
                        continue
                    try:
                        members = list(group_reader(stored.pgid))
                    except (OSError, RuntimeError) as exc:
                        blocking.append(
                            {
                                "role": f"{row['process_role']}_process_group",
                                "lease_id": row["lease_id"],
                                "live": None,
                                "reason": "legacy_process_group_unverifiable",
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        continue
                    if members:
                        blocking.append(
                            {
                                "role": f"{row['process_role']}_process_group",
                                "lease_id": row["lease_id"],
                                "live": True,
                                "reason": "legacy_process_group_has_members",
                                "pgid": stored.pgid,
                            }
                        )

    profile_reader = getattr(process_inspector, "profile_processes", None)
    for account_id, profile_dir in sorted(account_profiles.items()):
        if not callable(profile_reader):
            blocking.append(
                {
                    "role": "legacy_profile_chrome",
                    "account_id": account_id,
                    "live": None,
                    "reason": "legacy_profile_scan_unavailable",
                }
            )
            continue
        try:
            profile_processes = list(profile_reader(profile_dir))
        except (OSError, RuntimeError) as exc:
            blocking.append(
                {
                    "role": "legacy_profile_chrome",
                    "account_id": account_id,
                    "live": None,
                    "reason": "legacy_profile_scan_unavailable",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        for snapshot in profile_processes:
            blocking.append(
                {
                    "role": "legacy_profile_chrome",
                    "account_id": account_id,
                    "live": True,
                    "reason": "legacy_profile_chrome_alive",
                    "observed": snapshot.identity.public(),
                }
            )

    return {
        "safe_to_cutover": not blocking,
        "current_host_id": process_inspector.host_id,
        "current_boot_id": process_inspector.boot_id,
        "checks": checks,
        "blocking": blocking,
    }


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
            row["process_role"] in {"child", "exporter"}
            and stored.host_id == inspector.host_id
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
            "role": "runtime_profile_chrome",
            "live": True,
            "reason": "exact_runtime_profile_argument_alive",
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
    value = validate_account_id(account_id)
    if not run_id.strip() or not lease_id.strip():
        raise ValueError("run_id and lease_id must not be empty")
    account = get_account(conn, value)
    if not account:
        raise XhsOrphanLeaseRecoveryRefused(
            "requested XHS coordination slot does not exist"
        )
    lock = AccountLeaseFileLock(account_lock_path(value))
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
        try:
            runtime_profile = canonical_runtime_profile_dir(
                run_id,
                str(lease["runtime_profile_dir"]),
            )
        except ValueError as exc:
            raise XhsOrphanLeaseRecoveryRefused(
                "lease runtime profile is not the exact run-scoped path"
            ) from exc
        first_process_check = assess_lease_runtime(
            conn,
            lease=lease,
            profile_dir=runtime_profile,
            inspector=process_inspector,
            include_owner=True,
        )
        if not first_process_check["safe_to_release"]:
            raise XhsOrphanLeaseRecoveryRefused(
                "exact owner, child, exporter, or runtime-profile Chrome is still live"
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
            try:
                locked_runtime_profile = canonical_runtime_profile_dir(
                    run_id,
                    str(locked_lease["runtime_profile_dir"]),
                )
            except ValueError as exc:
                raise XhsOrphanLeaseRecoveryRefused(
                    "lease runtime profile changed to a noncanonical path"
                ) from exc
            second_process_check = assess_lease_runtime(
                conn,
                lease=locked_lease,
                profile_dir=locked_runtime_profile,
                inspector=process_inspector,
                include_owner=True,
            )
            if not second_process_check["safe_to_release"]:
                raise XhsOrphanLeaseRecoveryRefused(
                    "runtime process appeared during orphan reconciliation"
                )
            runtime_session_cleanup_required = not runtime_session_actually_absent(
                run_id
            )
            runtime_session_owned = False
            runtime_session_removed = False
            if runtime_session_cleanup_required:
                try:
                    verify_runtime_session_claim(
                        locked_runtime_profile,
                        expected_run_id=run_id,
                        expected_account_id=value,
                        expected_lease_id=lease_id,
                        expected_owner_token=str(locked_lease["owner_token"]),
                    )
                    runtime_session_owned = True
                    runtime_session_removed = remove_runtime_session_for_profile(
                        locked_runtime_profile,
                        expected_run_id=run_id,
                        expected_account_id=value,
                        expected_lease_id=lease_id,
                        expected_owner_token=str(locked_lease["owner_token"]),
                    )
                except (OSError, ValueError) as exc:
                    raise XhsOrphanLeaseRecoveryRefused(
                        "runtime session is not owned by the exact lease"
                    ) from exc
            runtime_session_absent = runtime_session_actually_absent(run_id)
            runtime_session_cleanup_complete = runtime_session_absent
            if not runtime_session_cleanup_complete:
                raise XhsOrphanLeaseRecoveryRefused(
                    "runtime session still exists after cleanup"
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
                "runtime_session_owned": runtime_session_owned,
                "runtime_session_cleanup_required": runtime_session_cleanup_required,
                "runtime_session_actually_absent": runtime_session_absent,
                "runtime_session_cleanup_complete": runtime_session_cleanup_complete,
                "runtime_session_removed": runtime_session_removed,
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
                "runtime_profile_dir": locked_lease["runtime_profile_dir"],
                "lease_acquired_at": locked_lease["acquired_at"],
                **audit,
            }
        except Exception:
            conn.rollback()
            raise
    finally:
        lock.release()


class LeaseGuard:

    def __init__(
        self,
        *,
        db_path: Path,
        account_id: str,
        run_id: str,
        lease_kind: str,
        execution_state_path: Path,
        runtime_profile_dir: Path,
        budget: LeaseBudget,
        inspector: SystemProcessInspector | None = None,
    ):
        self.db_path = db_path.expanduser().resolve()
        self.account_id = validate_account_id(account_id)
        self.run_id = run_id
        self.lease_kind = lease_kind
        self.execution_state_path = execution_state_path.expanduser().resolve()
        self.runtime_profile_dir = canonical_runtime_profile_dir(
            run_id,
            runtime_profile_dir,
        )
        self.budget = budget
        self.inspector = inspector or SystemProcessInspector()
        self.file_lock = AccountLeaseFileLock(account_lock_path(self.account_id))
        self.account: dict[str, Any] | None = None
        self.lease_id = ""
        self.owner_token = ""
        self.owner: ProcessIdentity | None = None
        self.outcome = "failed"
        self.signal_received: int | None = None
        self._previous_handlers: dict[int, Any] = {}
        self._closed = False
        self._closing = False
        self._released = False
        self._retain_file_lock = False
        self._finalize_started_at: float | None = None
        self._finalize_deadline: float | None = None
        self._finalize_deadline_shortened = False
        self._finalize_timeout_reason = ""
        self._finalize_timeout_stage = ""
        self._last_safe_assessment: dict[str, Any] | None = None
        self.runtime_session_owned = False
        self.runtime_session_actually_absent = runtime_session_actually_absent(
            self.run_id
        )
        self.runtime_session_cleanup_required = (
            not self.runtime_session_actually_absent
        )
        self.runtime_session_cleanup_complete = self.runtime_session_actually_absent
        self.runtime_session_removed = False
        self._runtime_session_prepare_attempted = False
        self._signal_sink: Callable[[int], Any] | None = None
        self._active_child_identity: ProcessIdentity | None = None
        # 已向 child 转发操作人中断的时刻：child 负责只向 exporter 转发一次温和信号，
        # 此后的兜底对 exporter 只发 SIGKILL。
        self._interrupt_forwarded_at: float | None = None

    def route_signals_to(self, sink: Callable[[int], Any]) -> None:
        """Route signals to a run-level first-wins latch before acquisition."""

        if self.account is not None:
            raise RuntimeError("XHS signal routing must be configured before acquire")
        self._signal_sink = sink

    def acquire(self) -> dict[str, Any]:
        if self.account is not None:
            raise RuntimeError("XHS LeaseGuard is already acquired")
        # A legacy schema cutover owns its historical locks internally. Perform
        # that one-time transition before taking the current runtime lock so the
        # migration cannot deadlock against this process.
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_xhs_schema(conn, cutover_inspector=self.inspector)
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
                    runtime_profile_dir=self.runtime_profile_dir,
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
        try:
            released = self.close()
        except BaseException as cleanup_error:
            if exc is None:
                raise
            try:
                # Only trusted type labels: exception messages and even custom
                # class names can contain credentials or other private data.
                cleanup_type = next(
                    kind.__name__
                    for kind in (
                        XhsLeaseSignal,
                        XhsIdentityProbeTimeout,
                        XhsLeaseOwnershipError,
                        XhsLeaseProcessesAlive,
                        KeyboardInterrupt,
                        SystemExit,
                        OSError,
                        RuntimeError,
                        ValueError,
                        Exception,
                        BaseException,
                    )
                    if isinstance(cleanup_error, kind)
                )
                BaseException.add_note(
                    exc,
                    f"LeaseGuard.__exit__: close raised {cleanup_type}; "
                    "primary exception preserved; lease release unconfirmed.",
                )
            except BaseException:
                # Malformed __notes__ or custom attribute hooks can fail even
                # with the base implementation. Diagnostic failure must never
                # replace the active exception, including an interrupt.
                return False
            # Let the with statement propagate the original object/traceback;
            # do not reverse the cleanup exception's implicit context chain.
            return False
        if not released and exc is None:
            raise XhsLeaseProcessesAlive(
                "XHS lease retained because guarded runtime processes are still live"
            )
        return False

    def set_outcome(self, outcome: str) -> None:
        self.outcome = outcome

    def begin_terminalization(self) -> None:
        """Latch later signals without raising while terminal evidence is published."""

        if self.account is None:
            raise RuntimeError("XHS LeaseGuard is not acquired")
        self._closing = True

    def child_environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        if not self.lease_id or not self.owner_token:
            raise RuntimeError("XHS LeaseGuard is not acquired")
        result = dict(base or os.environ)
        result[LEASE_DB_ENV] = str(self.db_path)
        result[LEASE_ID_ENV] = self.lease_id
        result[LEASE_OWNER_TOKEN_ENV] = self.owner_token
        return result

    def prepare_runtime_session(self) -> dict[str, Path]:
        """Create and claim the fresh session that this exact lease may remove."""

        if not self.account:
            raise RuntimeError("XHS LeaseGuard is not acquired")
        if self._runtime_session_prepare_attempted:
            raise RuntimeError("XHS runtime session preparation was already attempted")
        self._runtime_session_prepare_attempted = True
        session_root = self.runtime_profile_dir.parent
        existed_before = os.path.lexists(session_root)
        if existed_before:
            self.runtime_session_cleanup_required = True
            self.runtime_session_actually_absent = False
            self.runtime_session_cleanup_complete = False
        try:
            paths = create_runtime_session(
                self.run_id,
                account_id=self.account_id,
                lease_id=self.lease_id,
                owner_token=self.owner_token,
            )
        except BaseException:
            self._refresh_runtime_session_cleanup_state()
            if not existed_before and not self.runtime_session_actually_absent:
                try:
                    verify_runtime_session_claim(
                        self.runtime_profile_dir,
                        expected_run_id=self.run_id,
                        expected_account_id=self.account_id,
                        expected_lease_id=self.lease_id,
                        expected_owner_token=self.owner_token,
                    )
                except (OSError, ValueError):
                    self.runtime_session_owned = False
                else:
                    self.runtime_session_owned = True
            raise
        verify_runtime_session_claim(
            self.runtime_profile_dir,
            expected_run_id=self.run_id,
            expected_account_id=self.account_id,
            expected_lease_id=self.lease_id,
            expected_owner_token=self.owner_token,
        )
        self.runtime_session_owned = True
        self.runtime_session_cleanup_required = True
        self.runtime_session_actually_absent = False
        self.runtime_session_cleanup_complete = False
        if paths["profile"] != self.runtime_profile_dir:
            raise RuntimeError("prepared XHS runtime profile changed unexpectedly")
        return paths

    def _refresh_runtime_session_cleanup_state(self) -> None:
        self.runtime_session_actually_absent = runtime_session_actually_absent(
            self.run_id
        )
        if not self.runtime_session_actually_absent:
            self.runtime_session_cleanup_required = True
        self.runtime_session_cleanup_complete = self.runtime_session_actually_absent

    def runtime_session_cleanup_evidence(self) -> dict[str, bool]:
        """Return non-secret cleanup facts without turning absence into deletion.

        ``cleanup_required`` is latched once any exact root is observed, while
        ``actually_absent`` and ``cleanup_complete`` describe the last refresh.
        ``removed`` is true only after this guard's verified removal is observed.
        """

        return {
            "runtime_session_owned": self.runtime_session_owned,
            "runtime_session_cleanup_required": self.runtime_session_cleanup_required,
            "runtime_session_actually_absent": self.runtime_session_actually_absent,
            "runtime_session_cleanup_complete": self.runtime_session_cleanup_complete,
            "runtime_session_removed": self.runtime_session_removed,
        }

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
        if self.signal_received is not None:
            if self._closing:
                self._shorten_finalize_deadline()
            return
        self.signal_received = int(signum)
        if self._signal_sink is not None:
            self._signal_sink(int(signum))
            identity = self._active_child_identity
            if identity is not None:
                try:
                    self._signal_registered_group(identity, signal.SIGTERM)
                    self._interrupt_forwarded_at = time.monotonic()
                except (OSError, RuntimeError):
                    pass
            return
        if self._closing:
            return
        raise XhsLeaseSignal(signum)

    def _begin_finalize_deadline(self) -> None:
        if self._finalize_started_at is not None:
            return
        started_at = time.monotonic()
        self._finalize_started_at = started_at
        self._finalize_deadline = started_at + self.budget.root_finalize_seconds

    def _shorten_finalize_deadline(self) -> None:
        if self._finalize_deadline is None:
            return
        shortened = time.monotonic() + SECOND_SIGNAL_FINALIZE_GRACE_SECONDS
        if shortened < self._finalize_deadline:
            self._finalize_deadline = shortened
            self._finalize_deadline_shortened = True

    def _finalize_deadline_expired(self) -> bool:
        if self._finalize_deadline is None:
            return False
        if self.budget.root_finalize_seconds == 0:
            return True
        return time.monotonic() > self._finalize_deadline

    def _finalize_remaining_seconds(self) -> float:
        if self._finalize_deadline is None:
            return math.inf
        return max(0.0, self._finalize_deadline - time.monotonic())

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
        for snapshot in self.inspector.profile_processes(self.runtime_profile_dir):
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
        runtime_watchdog: RuntimeStatusWatchdogPolicy | None = None,
        progress_callback: Callable[[], None] | None = None,
    ) -> LeaseSubprocessResult:
        if self.signal_received is not None:
            raise XhsLeaseSignal(self.signal_received)
        child_env = self.child_environment(env)
        runtime_auth_key: bytes | None = None
        if runtime_watchdog is not None:
            runtime_auth_key = secrets.token_bytes(32)
            if type(runtime_auth_key) is not bytes or len(runtime_auth_key) != 32:
                raise RuntimeError("runtime watchdog key generation failed")
            child_env[RUNTIME_STATUS_AUTH_KEY_ENV] = runtime_auth_key.hex()
        gated: GatedSubprocess | None = None
        identity: ProcessIdentity | None = None
        deferred_signals = DeferredTerminationSignals()
        deferred_signals.install()
        try:
            gated = spawn_gated_subprocess(
                command,
                cwd=cwd,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            proc = gated.process
            watchdog_started_at = time.monotonic()
            if deferred_signals.signal_received is not None:
                raise XhsLeaseSignal(deferred_signals.signal_received)
            identity = self.register_process(proc.pid, "child")
            self._active_child_identity = identity
            if deferred_signals.signal_received is not None:
                raise XhsLeaseSignal(deferred_signals.signal_received)
            gated.release()
            if deferred_signals.signal_received is not None:
                raise XhsLeaseSignal(deferred_signals.signal_received)
        except BaseException:
            try:
                if gated is not None:
                    gated.cancel(grace_seconds=self.budget.child_shutdown_seconds)
                if (
                    identity is not None
                    and gated is not None
                    and gated.process.poll() is not None
                ):
                    try:
                        self.mark_process_exited(identity, "child")
                    except XhsLeaseOwnershipError:
                        pass
            finally:
                if self._active_child_identity == identity:
                    self._active_child_identity = None
                deferred_signals.restore()
                if deferred_signals.signal_received is not None:
                    deferred_signals.replay()
            raise
        assert gated is not None
        assert identity is not None
        proc = gated.process
        watchdog_state = (
            _ParentRuntimeWatchdogState(
                policy=runtime_watchdog,
                started_at=watchdog_started_at,
                last_poll_at=watchdog_started_at,
            )
            if runtime_watchdog is not None
            else None
        )
        timed_out = False
        termination_reason: str | None = None
        stdout = ""
        stderr = ""
        returncode = 1
        try:
            deferred_signals.restore()
            if deferred_signals.signal_received is not None:
                deferred_signals.replay()
            if runtime_watchdog is None:
                try:
                    stdout, stderr = proc.communicate(timeout=int(timeout_seconds))
                    returncode = int(proc.returncode or 0)
                    if self.signal_received is not None:
                        raise XhsLeaseSignal(self.signal_received)
                except subprocess.TimeoutExpired as exc:
                    timed_out = True
                    termination_reason = "absolute_timeout"
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
                    self.terminate_owned_processes()
            else:
                assert runtime_auth_key is not None
                assert watchdog_state is not None
                status_path = runtime_status_path(self.run_id)
                while True:
                    if self.signal_received is not None:
                        if self._interrupt_forwarded_at is not None:
                            stdout, stderr = self._await_interrupted_subprocess(
                                proc,
                                identity,
                            )
                        else:
                            stdout, stderr = self._terminate_registered_subprocess(
                                proc,
                                identity,
                            )
                        raise XhsLeaseSignal(self.signal_received)
                    if proc.poll() is not None:
                        stdout, stderr = drain_exited_child_output(proc)
                        returncode = int(proc.returncode or 0)
                        break
                    if progress_callback is not None:
                        progress_callback()
                    observed_identity = self.inspector.identity(proc.pid)
                    if observed_identity is None:
                        # A platform probe may briefly lose sight of a live
                        # child, and a just-exited child may already be a
                        # zombie before ``Popen.poll`` reaps it.  The Popen
                        # handle still names our exact child, so first give it
                        # one bounded interval to exit.  If it remains live,
                        # its authenticated status must still pass below.
                        try:
                            stdout, stderr = proc.communicate(
                                timeout=runtime_watchdog.poll_seconds
                            )
                        except subprocess.TimeoutExpired:
                            pass
                        else:
                            returncode = int(proc.returncode or 0)
                            break
                    elif observed_identity != identity:
                        # The exact process probe can observe the child as a
                        # zombie just before ``Popen.poll`` reaps it.  Give
                        # that exit race one bounded watchdog interval before
                        # deciding that the PID belongs to a different live
                        # process.  This does not grant liveness: a still-live
                        # child must prove the original exact identity again.
                        try:
                            stdout, stderr = proc.communicate(
                                timeout=runtime_watchdog.poll_seconds
                            )
                        except subprocess.TimeoutExpired:
                            reproved_identity = self.inspector.identity(proc.pid)
                            if (
                                reproved_identity is not None
                                and reproved_identity != identity
                            ):
                                termination_reason = "child_process_identity_changed"
                                self.terminate_owned_processes()
                                break
                        else:
                            returncode = int(proc.returncode or 0)
                            break
                    try:
                        status = read_runtime_status_if_present(
                            status_path,
                            auth_key=runtime_auth_key,
                            expected_run_id=self.run_id,
                            expected_account_id=self.account_id,
                            expected_lease_id=self.lease_id,
                            expected_writer_identity=identity,
                        )
                    except RuntimeStatusValidationError:
                        termination_reason = "runtime_status_invalid"
                        returncode = 1
                        stdout, stderr = self._terminate_registered_subprocess(
                            proc,
                            identity,
                        )
                        self.terminate_owned_processes()
                        break
                    termination_reason = watchdog_state.observe(
                        status,
                        received_at=time.monotonic(),
                    )
                    if termination_reason is not None:
                        if termination_reason in {
                            "runtime_status_startup_timeout",
                            "runtime_status_stale",
                        }:
                            observed_returncode = proc.poll()
                            if observed_returncode is not None:
                                stdout, stderr = drain_exited_child_output(proc)
                                returncode = int(observed_returncode)
                                termination_reason = None
                                break
                        timed_out = termination_reason in {
                            "runtime_status_startup_timeout",
                            "runtime_status_stale",
                        }
                        returncode = 124 if timed_out else 1
                        stdout, stderr = self._terminate_registered_subprocess(
                            proc,
                            identity,
                        )
                        self.terminate_owned_processes()
                        break
                    try:
                        stdout, stderr = proc.communicate(
                            timeout=runtime_watchdog.poll_seconds
                        )
                    except subprocess.TimeoutExpired:
                        continue
                    returncode = int(proc.returncode or 0)
                    break
                if termination_reason is None and self.signal_received is not None:
                    # 锁存之后才观察到 child 正常退出（中间层收束 worker 后自行退出）：按中断处理，
                    # 与无 watchdog 分支一致，不把中断后的退出码当作 child 结果。
                    raise XhsLeaseSignal(self.signal_received)
        except BaseException:
            self.terminate_owned_processes()
            raise
        finally:
            if self._active_child_identity == identity:
                self._active_child_identity = None
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
            termination_reason=termination_reason,
            runtime_status=(
                watchdog_state.public_status()
                if watchdog_state is not None
                else None
            ),
            watchdog_resume_grace_used=bool(
                watchdog_state is not None
                and watchdog_state.resume_granted_sequence is not None
            ),
        )

    def _terminate_registered_subprocess(
        self,
        proc: subprocess.Popen[str],
        identity: ProcessIdentity,
    ) -> tuple[str, str]:
        if proc.poll() is not None:
            stdout, stderr = drain_exited_child_output(proc)
            return str(stdout or ""), str(stderr or "")
        if self.inspector.identity(proc.pid) != identity:
            return "", ""
        self._signal_registered_group(identity, signal.SIGTERM)
        half_budget = max(1, self.budget.child_shutdown_seconds // 2)
        try:
            stdout, stderr = proc.communicate(timeout=half_budget)
        except subprocess.TimeoutExpired:
            self._signal_registered_group(identity, signal.SIGKILL)
            stdout, stderr = drain_exited_child_output(proc, timeout=half_budget)
        return str(stdout or ""), str(stderr or "")

    def _await_interrupted_subprocess(
        self,
        proc: subprocess.Popen[str],
        identity: ProcessIdentity,
    ) -> tuple[str, str]:
        """操作人中断已转发给 child：不再发第二次温和信号，只等待其自行收束，超时才 SIGKILL。"""

        assert self._interrupt_forwarded_at is not None
        deadline = self._interrupt_forwarded_at + interrupted_child_grace_seconds(self.budget)
        try:
            stdout, stderr = proc.communicate(
                timeout=max(0.01, deadline - time.monotonic())
            )
        except subprocess.TimeoutExpired:
            if proc.poll() is None and self.inspector.identity(proc.pid) == identity:
                self._signal_registered_group(identity, signal.SIGKILL)
            stdout, stderr = drain_exited_child_output(
                proc,
                timeout=max(1, interrupted_child_kill_reap_seconds(self.budget)),
            )
        return str(stdout or ""), str(stderr or "")

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
                profile_dir=self.runtime_profile_dir,
                inspector=self.inspector,
                include_owner=False,
            )

    def _finalize_timeout_assessment(
        self,
        *,
        reason: str,
        stage: str,
    ) -> dict[str, Any]:
        self._finalize_timeout_reason = reason
        self._finalize_timeout_stage = stage
        return {
            "safe_to_release": False,
            "checks": list((self._last_safe_assessment or {}).get("checks") or []),
            "blocking": [
                {
                    "role": "root_finalize",
                    "live": None,
                    "reason": reason,
                    "stage": stage,
                }
            ],
            "finalize_timeout": True,
            "finalize_timeout_reason": reason,
            "finalize_timeout_stage": stage,
        }

    def _bounded_runtime_assessment(self, *, stage: str) -> dict[str, Any]:
        if self._finalize_remaining_seconds() <= 0:
            return self._finalize_timeout_assessment(
                reason="root_finalize_deadline_exhausted",
                stage=stage,
            )
        probe_deadline_supported = isinstance(self.inspector, SystemProcessInspector)
        previous_probe_deadline = (
            getattr(self.inspector, "_identity_probe_deadline", None)
            if probe_deadline_supported
            else None
        )
        if probe_deadline_supported:
            self.inspector._identity_probe_deadline = self._finalize_deadline
        try:
            assessment = self._runtime_assessment()
        except XhsIdentityProbeTimeout:
            reason = (
                "root_finalize_deadline_exhausted"
                if self._finalize_remaining_seconds() <= 0
                else "identity_probe_timeout"
            )
            return self._finalize_timeout_assessment(
                reason=reason,
                stage=stage,
            )
        finally:
            if probe_deadline_supported:
                self.inspector._identity_probe_deadline = previous_probe_deadline
        self._last_safe_assessment = assessment
        if self._finalize_deadline_expired():
            return self._finalize_timeout_assessment(
                reason="root_finalize_deadline_exhausted",
                stage=stage,
            )
        return assessment

    def _finalize_timeout_details(
        self,
        *,
        process_check: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "signal": self.signal_received,
            "process_check": dict(process_check),
            "runtime_profile_dir": str(self.runtime_profile_dir),
            **self.runtime_session_cleanup_evidence(),
            "root_finalize_budget_seconds": self.budget.root_finalize_seconds,
            "root_finalize_started_monotonic": self._finalize_started_at,
            "root_finalize_deadline_monotonic": self._finalize_deadline,
            "root_finalize_deadline_shortened": self._finalize_deadline_shortened,
            "finalize_timeout_reason": self._finalize_timeout_reason,
            "finalize_timeout_stage": self._finalize_timeout_stage,
            "last_safe_assessment": self._last_safe_assessment,
        }

    def _defer_finalize_timeout(self, process_check: Mapping[str, Any]) -> bool:
        self._retain_file_lock = True
        self._closed = True
        try:
            with sqlite3.connect(self.db_path, timeout=0.0) as conn:
                conn.row_factory = sqlite3.Row
                ensure_xhs_schema(conn)
                record_event(
                    conn,
                    account_id=self.account_id,
                    run_id=self.run_id,
                    event_type="lease_release_deferred_finalize_timeout",
                    details=self._finalize_timeout_details(
                        process_check=process_check,
                    ),
                )
                conn.commit()
        except Exception:
            # A best-effort audit write must never turn a fail-closed timeout
            # into an exact lease or file-lock release.
            pass
        return False

    def terminate_owned_processes(self) -> dict[str, Any]:
        if not self.account:
            return {"safe_to_release": True, "checks": [], "blocking": []}
        initial = self._bounded_runtime_assessment(stage="initial_assessment")
        if initial.get("finalize_timeout"):
            return initial
        if initial["safe_to_release"]:
            return initial

        # 操作人中断已转发给 child 时，exporter 已由 child 收到唯一一次温和信号；兜底只能 SIGKILL。
        exporter_gentle_signal_forwarded = self._interrupt_forwarded_at is not None

        def targets(
            assessment: Mapping[str, Any],
            *,
            gentle: bool = False,
        ) -> set[tuple[str, int]]:
            result: set[tuple[str, int]] = set()
            for item in assessment["blocking"]:
                if (
                    gentle
                    and exporter_gentle_signal_forwarded
                    and str(item.get("role") or "").startswith("exporter")
                ):
                    continue
                observed = item.get("observed") if isinstance(item, dict) else None
                if item.get("role") in {
                    "child",
                    "exporter",
                    "child_process_group",
                    "exporter_process_group",
                }:
                    pgid = int(item.get("pgid") or (observed or {}).get("pgid") or 0)
                    if pgid and (self.owner is None or pgid != self.owner.pgid):
                        result.add(("pgid", pgid))
                elif observed and int(observed.get("pid") or 0) != os.getpid():
                    result.add(("pid", int(observed["pid"])))
                for member in item.get("members") or []:
                    pgid = int(member.get("pgid") or 0)
                    if pgid and (self.owner is None or pgid != self.owner.pgid):
                        result.add(("pgid", pgid))
            return result

        def send(signum: int, assessment: Mapping[str, Any]) -> bool:
            gentle = signum != signal.SIGKILL
            for target_type, value in sorted(targets(assessment, gentle=gentle)):
                if self._finalize_remaining_seconds() <= 0:
                    return False
                try:
                    if target_type == "pgid":
                        os.killpg(value, signum)
                    else:
                        os.kill(value, signum)
                except ProcessLookupError:
                    continue
            return self._finalize_remaining_seconds() > 0

        half_budget = max(1, self.budget.child_shutdown_seconds // 2)
        if not send(signal.SIGTERM, initial):
            return self._finalize_timeout_assessment(
                reason="root_finalize_deadline_exhausted",
                stage="send_sigterm",
            )
        term_deadline = min(
            self._finalize_deadline or math.inf,
            time.monotonic() + half_budget,
        )
        assessment = self._bounded_runtime_assessment(stage="after_sigterm")
        while not assessment["safe_to_release"] and not assessment.get(
            "finalize_timeout"
        ):
            now = time.monotonic()
            if now >= term_deadline:
                break
            time.sleep(min(0.1, term_deadline - now))
            assessment = self._bounded_runtime_assessment(stage="wait_after_sigterm")
        if assessment.get("finalize_timeout"):
            return assessment
        if assessment["safe_to_release"]:
            return assessment
        if not send(signal.SIGKILL, assessment):
            return self._finalize_timeout_assessment(
                reason="root_finalize_deadline_exhausted",
                stage="send_sigkill",
            )
        kill_deadline = min(
            self._finalize_deadline or math.inf,
            time.monotonic()
            + max(1, self.budget.child_shutdown_seconds - half_budget),
        )
        assessment = self._bounded_runtime_assessment(stage="after_sigkill")
        while not assessment["safe_to_release"] and not assessment.get(
            "finalize_timeout"
        ):
            now = time.monotonic()
            if now >= kill_deadline:
                break
            time.sleep(min(0.1, kill_deadline - now))
            assessment = self._bounded_runtime_assessment(stage="wait_after_sigkill")
        return assessment

    def close(self) -> bool:
        if self._closed:
            return self._released
        self._closing = True
        self._begin_finalize_deadline()
        try:
            if self.account is None:
                self._closed = True
                self._released = True
                return True
            process_check = self.terminate_owned_processes()
            if process_check.get("finalize_timeout"):
                return self._defer_finalize_timeout(process_check)
            if not process_check["safe_to_release"]:
                if self._finalize_remaining_seconds() <= 0:
                    return self._defer_finalize_timeout(
                        self._finalize_timeout_assessment(
                            reason="root_finalize_deadline_exhausted",
                            stage="after_process_termination",
                        )
                    )
                self._refresh_runtime_session_cleanup_state()
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
                            "runtime_profile_dir": str(self.runtime_profile_dir),
                            **self.runtime_session_cleanup_evidence(),
                        },
                    )
                    conn.commit()
                self._closed = True
                return False
            if self._finalize_deadline_expired():
                return self._defer_finalize_timeout(
                    self._finalize_timeout_assessment(
                        reason="root_finalize_deadline_exhausted",
                        stage="before_runtime_session_refresh",
                    )
                )
            cleanup_error = ""
            self._refresh_runtime_session_cleanup_state()
            if self._finalize_deadline_expired():
                return self._defer_finalize_timeout(
                    self._finalize_timeout_assessment(
                        reason="root_finalize_deadline_exhausted",
                        stage="after_runtime_session_refresh",
                    )
                )
            if (
                self.runtime_session_cleanup_required
                and not self.runtime_session_actually_absent
                and self.runtime_session_owned
            ):
                if self._finalize_deadline_expired():
                    return self._defer_finalize_timeout(
                        self._finalize_timeout_assessment(
                            reason="root_finalize_deadline_exhausted",
                            stage="before_runtime_session_removal",
                        )
                    )
                try:
                    removal_reported = remove_runtime_session_for_profile(
                        self.runtime_profile_dir,
                        expected_run_id=self.run_id,
                        expected_account_id=self.account_id,
                        expected_lease_id=self.lease_id,
                        expected_owner_token=self.owner_token,
                    )
                except (OSError, ValueError) as exc:
                    cleanup_error = f"{type(exc).__name__}: {exc}"
                    self.runtime_session_removed = False
                    self._refresh_runtime_session_cleanup_state()
                else:
                    self._refresh_runtime_session_cleanup_state()
                    self.runtime_session_removed = bool(
                        removal_reported and self.runtime_session_actually_absent
                    )
                if self._finalize_deadline_expired():
                    return self._defer_finalize_timeout(
                        self._finalize_timeout_assessment(
                            reason="root_finalize_deadline_exhausted",
                            stage="after_runtime_session_removal",
                        )
                    )
            elif not self.runtime_session_actually_absent:
                cleanup_error = "runtime_session_not_owned"
            if not self.runtime_session_cleanup_complete:
                if self._finalize_remaining_seconds() <= 0:
                    return self._defer_finalize_timeout(
                        self._finalize_timeout_assessment(
                            reason="root_finalize_deadline_exhausted",
                            stage="after_runtime_session_cleanup",
                        )
                    )
                with sqlite3.connect(self.db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    ensure_xhs_schema(conn)
                    record_event(
                        conn,
                        account_id=self.account_id,
                        run_id=self.run_id,
                        event_type=(
                            "lease_release_deferred_runtime_session_cleanup"
                        ),
                        details={
                            "lease_id": self.lease_id,
                            "signal": self.signal_received,
                            "process_check": process_check,
                            "runtime_profile_dir": str(self.runtime_profile_dir),
                            **self.runtime_session_cleanup_evidence(),
                            "cleanup_error": cleanup_error
                            or "runtime_session_still_exists",
                        },
                    )
                    conn.commit()
                self._closed = True
                return False
            if self._finalize_deadline_expired():
                return self._defer_finalize_timeout(
                    self._finalize_timeout_assessment(
                        reason="root_finalize_deadline_exhausted",
                        stage="before_lease_release",
                    )
                )
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
                        "runtime_profile_dir": str(self.runtime_profile_dir),
                        **self.runtime_session_cleanup_evidence(),
                    },
                )
            self._closed = True
            self._released = True
            return True
        finally:
            self._restore_signal_handlers()
            self._closing = False
            if self._closed and not self._retain_file_lock:
                self.file_lock.release()
