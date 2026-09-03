"""Shared crash-conscious terminalization for XHS leased runs.

The database token is the linearization point for the business outcome.  File
publication and exact lease cleanup happen on either side of that point, but a
signal or exception can always determine whether the commit happened by
probing ``xhs_runs.report_json`` from a new connection.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, Callable, Mapping, Protocol


class XhsTerminalSignal(BaseException):
    """A termination signal received outside the LeaseGuard handler."""

    def __init__(self, signum: int):
        self.signum = int(signum)
        super().__init__(f"XHS terminalization interrupted by signal {self.signum}")


class XhsTerminalCommitUnconfirmed(RuntimeError):
    """The terminal database transaction cannot be proven committed or rolled back."""


@dataclass(frozen=True)
class TerminalCommit:
    token: str
    outcome: str
    committed: bool


PhaseHook = Callable[[str, "XhsRunTerminalizer"], None]
CommitMutation = Callable[[sqlite3.Connection, str], None]


class TerminalLeaseGuard(Protocol):
    signal_received: int | None

    def set_outcome(self, outcome: str) -> None: ...

    def begin_terminalization(self) -> None: ...

    def terminate_owned_processes(self) -> dict[str, Any]: ...

    def close(self) -> bool: ...

    def runtime_session_cleanup_evidence(self) -> dict[str, bool]: ...


@dataclass(frozen=True)
class TerminalFinishResult:
    summary: dict[str, Any]
    lease_released: bool
    confirmed: bool
    error: str | None


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    """Replace a JSON document atomically and never leave a truncated target."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


class XhsRunTerminalizer:
    """First-wins signal latch and one-shot terminal DB commit coordinator."""

    def __init__(
        self,
        *,
        db_path: Path,
        run_id: str,
        phase_hook: PhaseHook | None = None,
    ) -> None:
        self.db_path = db_path.expanduser().resolve()
        self.run_id = run_id
        self.phase_hook = phase_hook
        self.token = secrets.token_hex(32)
        self.signal_received: int | None = None
        self.interrupt_source: str | None = None
        self.business_committed = False
        self.business_outcome: str | None = None
        self.finalization_confirmed = False
        self._previous_handlers: dict[int, Any] = {}
        self._installed = False
        self._finish_result: Any = None

    def install(self) -> None:
        if self._installed:
            return
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._signal_handler)
            except (OSError, ValueError):
                continue
        self._installed = True

    def restore(self) -> None:
        if not self._installed:
            return
        for signum, handler in self._previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass
        self._previous_handlers.clear()
        self._installed = False

    def _signal_handler(self, signum: int, _frame: FrameType | None) -> None:
        self.latch_interrupt(signum, source="terminal_signal")

    def bind_guard(self, guard: Any) -> None:
        """Route Guard signals into this latch before the lease is acquired."""

        guard.route_signals_to(
            lambda signum: self.latch_interrupt(signum, source="lease_signal")
        )

    def latch_interrupt(self, signum: int, *, source: str) -> int:
        if self.signal_received is None:
            self.signal_received = int(signum)
            self.interrupt_source = source
        return self.signal_received

    def capture_interrupt(self, exc: BaseException, *, guard_signal: int | None = None) -> int:
        if guard_signal is not None:
            return self.latch_interrupt(guard_signal, source="lease_signal")
        signum = getattr(exc, "signum", None)
        if signum is not None:
            return self.latch_interrupt(int(signum), source="lease_signal")
        if isinstance(exc, KeyboardInterrupt):
            return self.latch_interrupt(int(signal.SIGINT), source="keyboard_interrupt")
        raise TypeError(f"not an interrupt: {type(exc).__name__}")

    @staticmethod
    def is_interrupt(exc: BaseException) -> bool:
        return isinstance(exc, (KeyboardInterrupt, XhsTerminalSignal)) or (
            type(exc).__name__ == "XhsLeaseSignal" and hasattr(exc, "signum")
        )

    def phase(self, name: str) -> None:
        if self.phase_hook is None:
            return
        try:
            self.phase_hook(name, self)
        except BaseException as exc:
            if self.is_interrupt(exc) and self.business_committed:
                self.capture_interrupt(exc)
                return
            raise

    def raise_if_interrupted(self) -> None:
        if self.signal_received is not None and not self.business_committed:
            raise XhsTerminalSignal(self.signal_received)

    def _probe(self) -> TerminalCommit | None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT status, report_json FROM xhs_runs WHERE run_id=?",
                    (self.run_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise XhsTerminalCommitUnconfirmed(
                f"cannot probe XHS terminal token: {type(exc).__name__}: {exc}"
            ) from exc
        if row is None:
            return None
        try:
            report = json.loads(str(row[1] or "{}"))
        except json.JSONDecodeError as exc:
            raise XhsTerminalCommitUnconfirmed("XHS terminal report_json is unreadable") from exc
        terminal = report.get("terminal_commit") if isinstance(report, dict) else None
        if not isinstance(terminal, dict) or terminal.get("token") != self.token:
            return None
        outcome = str(terminal.get("outcome") or "")
        if outcome not in {"completed", "failed"}:
            raise XhsTerminalCommitUnconfirmed("XHS terminal token has invalid outcome")
        return TerminalCommit(token=self.token, outcome=outcome, committed=True)

    def linearize(
        self,
        *,
        outcome: str,
        mutation: CommitMutation,
        allow_interrupted_failure: bool = False,
    ) -> TerminalCommit:
        """Run the unique terminal transaction and prove its result after any raise."""

        if outcome not in {"completed", "failed"}:
            raise ValueError(f"invalid XHS terminal outcome: {outcome}")
        if self.business_committed:
            if self.business_outcome != outcome:
                raise RuntimeError("XHS terminal outcome cannot change after commit")
            return TerminalCommit(self.token, outcome, True)
        def commit_phase(name: str) -> None:
            try:
                self.phase(name)
            except BaseException as exc:
                if (
                    allow_interrupted_failure
                    and outcome == "failed"
                    and self.is_interrupt(exc)
                ):
                    self.capture_interrupt(exc)
                    return
                raise

        commit_phase("before_terminal_commit")
        if not (allow_interrupted_failure and outcome == "failed"):
            self.raise_if_interrupted()
        raised: BaseException | None = None
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                mutation(conn, self.token)
                commit_phase("before_terminal_sqlite_commit")
                if not (allow_interrupted_failure and outcome == "failed"):
                    self.raise_if_interrupted()
                conn.commit()
                commit_phase("after_terminal_sqlite_commit")
        except BaseException as exc:
            raised = exc
        probed = self._probe()
        if probed is not None:
            if probed.outcome != outcome:
                raise XhsTerminalCommitUnconfirmed(
                    "XHS terminal token committed with a different outcome"
                )
            self.business_committed = True
            self.business_outcome = outcome
            self.phase("terminal_commit_confirmed")
            return probed
        if raised is not None:
            raise raised
        raise XhsTerminalCommitUnconfirmed("XHS terminal transaction returned without its token")

    def terminal_report(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "outcome": self.business_outcome,
            "committed": self.business_committed,
            "finalization_confirmed": self.finalization_confirmed,
        }

    def mark_finalization_confirmed(self) -> None:
        self.finalization_confirmed = True

    def finish(
        self,
        *,
        outcome: str,
        summary: Mapping[str, Any],
        summary_path: Path,
        state_publish: Callable[[], None],
        run_publish: Callable[[Mapping[str, Any]], None],
        guard: TerminalLeaseGuard,
        cleanup_evidence: Callable[[], Mapping[str, Any]],
    ) -> TerminalFinishResult:
        """Publish terminal evidence, clean exactly, then publish cleanup evidence.

        Any failure before exact close only terminates registered processes; it
        intentionally preserves the claimed session and SQLite lease for audited
        recovery.  Calling this method again returns the first result and cannot
        duplicate terminal events or cleanup.
        """

        if self._finish_result is not None:
            return self._finish_result
        if not self.business_committed or self.business_outcome != outcome:
            raise RuntimeError("XHS business outcome must be committed before finish")
        document = {
            **dict(summary),
            "status": outcome,
            "terminal_commit": self.terminal_report(),
            "lease_released": False,
            "lease_cleanup": {
                "ok": False,
                "event_type": "pending",
                "reason": "exact_cleanup_pending",
            },
        }
        try:
            guard.begin_terminalization()
            self.phase("before_state_publish")
            state_publish()
            self.phase("after_state_publish")
            atomic_write_json(summary_path, document)
            self.phase("after_provisional_summary_publish")
            run_publish(document)
            self.phase("after_provisional_run_publish")
        except BaseException as exc:
            try:
                guard.terminate_owned_processes()
            except BaseException:
                pass
            document["terminal_commit"] = self.terminal_report()
            document["finalization_error"] = f"{type(exc).__name__}: {exc}"
            document["lease_retained_for_recovery"] = True
            result = TerminalFinishResult(
                summary=document,
                lease_released=False,
                confirmed=False,
                error=str(document["finalization_error"]),
            )
            self._finish_result = result
            return result

        guard.set_outcome(outcome)
        try:
            self.phase("before_exact_close")
            released = bool(guard.close())
            self.phase("after_exact_close")
            cleanup = dict(cleanup_evidence())
            document.update(guard.runtime_session_cleanup_evidence())
            document["lease_released"] = released
            document["lease_cleanup"] = cleanup
            if not released:
                document["status"] = "failed"
                document["cleanup_error"] = str(
                    cleanup.get("event_type") or "exact_cleanup_deferred"
                )
            self.phase("before_final_summary_publish")
            atomic_write_json(summary_path, document)
            self.phase("after_final_summary_publish")
            run_publish(document)
            self.phase("after_final_run_publish")
        except BaseException as exc:
            # A valid provisional document and run row already carry the terminal
            # token.  Never retry close blindly: its commit may have succeeded.
            if not document.get("lease_released"):
                try:
                    guard.terminate_owned_processes()
                except BaseException:
                    pass
                document["lease_retained_for_recovery"] = True
            document["terminal_commit"] = self.terminal_report()
            document["finalization_error"] = f"{type(exc).__name__}: {exc}"
            result = TerminalFinishResult(
                summary=document,
                lease_released=bool(document.get("lease_released")),
                confirmed=False,
                error=str(document["finalization_error"]),
            )
            self._finish_result = result
            return result
        self.mark_finalization_confirmed()
        document["terminal_commit"] = self.terminal_report()
        # Publish the confirmation bit last.  Failure preserves the preceding
        # complete cleanup document and exact release event.
        try:
            atomic_write_json(summary_path, document)
            run_publish(document)
        except BaseException as exc:
            document["finalization_error"] = f"{type(exc).__name__}: {exc}"
            result = TerminalFinishResult(
                summary=document,
                lease_released=released,
                confirmed=False,
                error=str(document["finalization_error"]),
            )
            self._finish_result = result
            return result
        result = TerminalFinishResult(
            summary=document,
            lease_released=released,
            confirmed=True,
            error=None,
        )
        self._finish_result = result
        return result
