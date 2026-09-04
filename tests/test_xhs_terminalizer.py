"""Adversarial tests for the shared XHS terminal commit and publisher."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
from argparse import Namespace
from importlib import import_module
from pathlib import Path
from typing import Any, Mapping

import pytest

from trippostcollect.xhs.terminal import (
    XhsRunTerminalizer,
    XhsTerminalCommitUnconfirmed,
    atomic_write_json,
)
from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.xhs import accounts
from trippostcollect.xhs.discovery import (
    save_checkpoint,
    update_campaign,
    xhs_query_fingerprint,
)
from trippostcollect.xhs.leases import execution_terminal_assessment
from trippostcollect.xhs.leases import LeaseSubprocessResult

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

execution_state = import_module("execution_state")
xhs_runner = import_module("xhs_runner")


def terminal_db(path: Path, run_id: str = "run-1") -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE xhs_runs(
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                report_json TEXT NOT NULL
            );
            CREATE TABLE mutation_log(value TEXT NOT NULL);
            CREATE TABLE checkpoint(position INTEGER NOT NULL);
            INSERT INTO checkpoint(position) VALUES (44);
            """
        )
        conn.execute(
            "INSERT INTO xhs_runs(run_id, status, report_json) VALUES (?, 'running', '{}')",
            (run_id,),
        )


def terminal_mutation(
    run_id: str,
    outcome: str,
    *,
    advance_checkpoint: bool = True,
    raise_after_write: BaseException | None = None,
):
    def mutate(conn: sqlite3.Connection, token: str) -> None:
        conn.execute("INSERT INTO mutation_log(value) VALUES ('terminal_event')")
        if advance_checkpoint:
            conn.execute("UPDATE checkpoint SET position=45")
        report = {
            "terminal_commit": {
                "token": token,
                "outcome": outcome,
                "committed": True,
                "finalization_confirmed": False,
            }
        }
        conn.execute(
            "UPDATE xhs_runs SET status=?, report_json=? WHERE run_id=?",
            (outcome, json.dumps(report), run_id),
        )
        if raise_after_write is not None:
            raise raise_after_write

    return mutate


@pytest.mark.parametrize(
    "phase",
    ["before_terminal_commit", "before_terminal_sqlite_commit"],
)
def test_interrupt_before_linearization_rolls_back_event_and_checkpoint(
    tmp_path: Path,
    phase: str,
) -> None:
    db_path = tmp_path / "control.sqlite"
    terminal_db(db_path)

    def interrupt(name: str, _terminalizer: XhsRunTerminalizer) -> None:
        if name == phase:
            raise KeyboardInterrupt

    terminalizer = XhsRunTerminalizer(
        db_path=db_path,
        run_id="run-1",
        phase_hook=interrupt,
    )
    with pytest.raises(KeyboardInterrupt):
        terminalizer.linearize(
            outcome="completed",
            mutation=terminal_mutation("run-1", "completed"),
        )

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT position FROM checkpoint").fetchone()[0] == 44
        assert conn.execute("SELECT COUNT(*) FROM mutation_log").fetchone()[0] == 0
        status, report = conn.execute(
            "SELECT status, report_json FROM xhs_runs WHERE run_id='run-1'"
        ).fetchone()
    assert status == "running"
    assert json.loads(report) == {}
    assert terminalizer.business_committed is False


def test_raise_after_sqlite_commit_is_probed_as_known_and_never_replayed(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "control.sqlite"
    terminal_db(db_path)

    def interrupt_after_commit(name: str, _terminalizer: XhsRunTerminalizer) -> None:
        if name == "after_terminal_sqlite_commit":
            os.kill(os.getpid(), signal.SIGTERM)

    terminalizer = XhsRunTerminalizer(
        db_path=db_path,
        run_id="run-1",
        phase_hook=interrupt_after_commit,
    )
    terminalizer.install()
    try:
        result = terminalizer.linearize(
            outcome="completed",
            mutation=terminal_mutation("run-1", "completed"),
        )
        repeated = terminalizer.linearize(
            outcome="completed",
            mutation=lambda _conn, _token: pytest.fail("mutation replayed"),
        )
    finally:
        terminalizer.restore()

    assert result == repeated
    assert terminalizer.business_outcome == "completed"
    assert terminalizer.signal_received == signal.SIGTERM
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT position FROM checkpoint").fetchone()[0] == 45
        assert conn.execute("SELECT COUNT(*) FROM mutation_log").fetchone()[0] == 1


def test_mutation_raise_is_known_rollback_not_a_false_terminal_commit(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "control.sqlite"
    terminal_db(db_path)
    terminalizer = XhsRunTerminalizer(db_path=db_path, run_id="run-1")

    with pytest.raises(RuntimeError, match="before commit"):
        terminalizer.linearize(
            outcome="completed",
            mutation=terminal_mutation(
                "run-1",
                "completed",
                raise_after_write=RuntimeError("before commit"),
            ),
        )

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT position FROM checkpoint").fetchone()[0] == 44
        assert conn.execute("SELECT COUNT(*) FROM mutation_log").fetchone()[0] == 0


def test_commit_probe_rejects_missing_or_foreign_token(tmp_path: Path) -> None:
    db_path = tmp_path / "control.sqlite"
    terminal_db(db_path)
    terminalizer = XhsRunTerminalizer(db_path=db_path, run_id="run-1")

    def commit_foreign_token(conn: sqlite3.Connection, _token: str) -> None:
        conn.execute(
            "UPDATE xhs_runs SET status='completed', report_json=? WHERE run_id='run-1'",
            (
                json.dumps(
                    {
                        "terminal_commit": {
                            "token": "foreign",
                            "outcome": "completed",
                        }
                    }
                ),
            ),
        )

    with pytest.raises(XhsTerminalCommitUnconfirmed, match="without its token"):
        terminalizer.linearize(outcome="completed", mutation=commit_foreign_token)


def test_first_interrupt_wins_and_committed_outcome_is_immutable(tmp_path: Path) -> None:
    db_path = tmp_path / "control.sqlite"
    terminal_db(db_path)
    terminalizer = XhsRunTerminalizer(db_path=db_path, run_id="run-1")

    assert terminalizer.latch_interrupt(signal.SIGTERM, source="terminal_signal") == signal.SIGTERM
    assert (
        terminalizer.capture_interrupt(KeyboardInterrupt(), guard_signal=None)
        == signal.SIGTERM
    )
    terminalizer.linearize(
        outcome="failed",
        mutation=terminal_mutation(
            "run-1",
            "failed",
            advance_checkpoint=False,
        ),
        allow_interrupted_failure=True,
    )
    terminalizer._signal_handler(signal.SIGINT, None)
    assert terminalizer.signal_received == signal.SIGTERM
    assert terminalizer.business_outcome == "failed"
    with pytest.raises(RuntimeError, match="cannot change"):
        terminalizer.linearize(
            outcome="completed",
            mutation=terminal_mutation("run-1", "completed"),
        )


class FakeGuard:
    def __init__(self, *, close_result: bool = True) -> None:
        self.signal_received: int | None = None
        self.close_result = close_result
        self.calls: list[str] = []
        self.session_exists = True

    def begin_terminalization(self) -> None:
        self.calls.append("begin_terminalization")

    def set_outcome(self, outcome: str) -> None:
        self.calls.append(f"set_outcome:{outcome}")

    def terminate_owned_processes(self) -> dict[str, Any]:
        self.calls.append("terminate")
        return {"safe_to_release": True}

    def close(self) -> bool:
        self.calls.append("close")
        if self.close_result:
            self.session_exists = False
        return self.close_result

    def runtime_session_cleanup_evidence(self) -> dict[str, bool]:
        return {
            "runtime_session_cleanup_complete": not self.session_exists,
            "runtime_session_actually_absent": not self.session_exists,
            "runtime_session_removed": not self.session_exists,
        }


def committed_terminalizer(
    db_path: Path,
    *,
    phase_hook=None,
) -> XhsRunTerminalizer:
    terminalizer = XhsRunTerminalizer(
        db_path=db_path,
        run_id="run-1",
        phase_hook=phase_hook,
    )
    terminalizer.linearize(
        outcome="completed",
        mutation=terminal_mutation("run-1", "completed"),
    )
    return terminalizer


@pytest.mark.parametrize(
    "phase",
    [
        "before_state_publish",
        "after_state_publish",
        "after_provisional_summary_publish",
        "after_provisional_run_publish",
        "before_exact_close",
    ],
)
def test_preclose_publish_failure_terminates_but_retains_session_and_lease(
    tmp_path: Path,
    phase: str,
) -> None:
    db_path = tmp_path / "control.sqlite"
    terminal_db(db_path)

    def fail(name: str, _terminalizer: XhsRunTerminalizer) -> None:
        if name == phase:
            raise OSError(f"fault:{phase}")

    terminalizer = committed_terminalizer(db_path, phase_hook=fail)
    guard = FakeGuard()
    published: list[dict[str, Any]] = []
    state_calls: list[str] = []
    result = terminalizer.finish(
        outcome="completed",
        summary={"run_id": "run-1"},
        summary_path=tmp_path / "run_summary.json",
        state_publish=lambda: state_calls.append("state"),
        run_publish=lambda value: published.append(dict(value)),
        guard=guard,
        cleanup_evidence=lambda: {"ok": True, "event_type": "lease_released"},
    )

    assert result.confirmed is False
    assert result.lease_released is False
    assert result.summary["lease_retained_for_recovery"] is True or phase == "before_exact_close"
    assert guard.session_exists is True
    assert "terminate" in guard.calls
    assert "close" not in guard.calls
    assert terminalizer.finish(
        outcome="completed",
        summary={},
        summary_path=tmp_path / "different.json",
        state_publish=lambda: pytest.fail("finish replayed"),
        run_publish=lambda _value: pytest.fail("finish replayed"),
        guard=guard,
        cleanup_evidence=lambda: pytest.fail("finish replayed"),
    ) is result


def test_success_publishes_matching_token_state_summary_and_cleanup_once(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "control.sqlite"
    terminal_db(db_path)
    terminalizer = committed_terminalizer(db_path)
    guard = FakeGuard()
    published: list[dict[str, Any]] = []
    state_tokens: list[str] = []
    summary_path = tmp_path / "run_summary.json"

    result = terminalizer.finish(
        outcome="completed",
        summary={"run_id": "run-1"},
        summary_path=summary_path,
        state_publish=lambda: state_tokens.append(terminalizer.token),
        run_publish=lambda value: published.append(dict(value)),
        guard=guard,
        cleanup_evidence=lambda: {
            "ok": True,
            "event_type": "lease_released",
            "process_check": {"safe_to_release": True},
        },
    )

    document = json.loads(summary_path.read_text(encoding="utf-8"))
    assert result.confirmed is True
    assert result.lease_released is True
    assert state_tokens == [terminalizer.token]
    assert document["terminal_commit"]["token"] == terminalizer.token
    assert document["terminal_commit"]["outcome"] == "completed"
    assert document["terminal_commit"]["finalization_confirmed"] is True
    assert document["lease_cleanup"]["ok"] is True
    assert guard.calls.count("close") == 1
    assert len(published) == 3
    assert published[-1]["terminal_commit"] == document["terminal_commit"]


def test_deferred_close_is_published_as_failed_without_claiming_release(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "control.sqlite"
    terminal_db(db_path)
    terminalizer = committed_terminalizer(db_path)
    guard = FakeGuard(close_result=False)
    published: list[dict[str, Any]] = []
    summary_path = tmp_path / "run_summary.json"

    result = terminalizer.finish(
        outcome="completed",
        summary={"run_id": "run-1"},
        summary_path=summary_path,
        state_publish=lambda: None,
        run_publish=lambda value: published.append(dict(value)),
        guard=guard,
        cleanup_evidence=lambda: {
            "ok": False,
            "event_type": "lease_release_deferred_live_processes",
        },
    )

    assert result.confirmed is True
    assert result.lease_released is False
    assert result.summary["status"] == "failed"
    assert result.summary["lease_cleanup"]["ok"] is False
    assert guard.session_exists is True
    assert guard.calls.count("close") == 1


def test_final_run_publish_failure_does_not_repeat_exact_close(tmp_path: Path) -> None:
    db_path = tmp_path / "control.sqlite"
    terminal_db(db_path)
    terminalizer = committed_terminalizer(db_path)
    guard = FakeGuard()
    publish_count = 0

    def fail_second_publish(_value: Mapping[str, Any]) -> None:
        nonlocal publish_count
        publish_count += 1
        if publish_count == 2:
            raise sqlite3.OperationalError("second upsert failed")

    result = terminalizer.finish(
        outcome="completed",
        summary={"run_id": "run-1"},
        summary_path=tmp_path / "run_summary.json",
        state_publish=lambda: None,
        run_publish=fail_second_publish,
        guard=guard,
        cleanup_evidence=lambda: {"ok": True, "event_type": "lease_released"},
    )

    assert result.confirmed is False
    assert result.lease_released is True
    assert "second upsert failed" in str(result.error)
    assert guard.calls.count("close") == 1
    assert terminalizer.finish(
        outcome="completed",
        summary={},
        summary_path=tmp_path / "other.json",
        state_publish=lambda: pytest.fail("replayed"),
        run_publish=lambda _value: pytest.fail("replayed"),
        guard=guard,
        cleanup_evidence=lambda: pytest.fail("replayed"),
    ) is result


def test_atomic_json_failure_preserves_old_target_and_removes_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "run_summary.json"
    target.write_text('{"old": true}\n', encoding="utf-8")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        atomic_write_json(target, {"new": True})

    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert list(tmp_path.glob(".*.tmp")) == []


def test_execution_state_atomic_failure_preserves_previous_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"status": "running"}\n', encoding="utf-8")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("state replace failed")

    monkeypatch.setattr(execution_state.os, "replace", fail_replace)
    with pytest.raises(OSError, match="state replace failed"):
        execution_state._atomic_write(target, {"status": "completed"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "running"}
    assert list(tmp_path.glob(".*.tmp")) == []


def test_failed_state_finalization_preserves_first_failure_and_enables_recovery(
    tmp_path: Path,
) -> None:
    frozen_input = tmp_path / "input.json"
    frozen_input.write_text("{}\n", encoding="utf-8")
    state_path = tmp_path / "state.json"
    state = execution_state.FrozenExecutionState.create(
        state_path,
        run_id="run-1",
        job_key="target",
        site_key="xhs",
        job_kind="xhs_account_search",
        plan={"account_id": "xhs-a01"},
        frozen_inputs=[frozen_input],
        dry_run=False,
    )
    state.fail(
        "command_executed",
        error="original_child_failure",
        evidence={"stderr_tail": "first evidence"},
    )

    first = state.finalize_failure(
        error="terminalized_failed_run",
        evidence={"terminal_token": "token-1"},
    )
    second = state.finalize_failure(
        error="must_not_replace",
        evidence={"terminal_token": "token-2"},
    )

    assert first == second
    assert second["steps"]["command_executed"]["error"] == "original_child_failure"
    assert second["steps"]["command_executed"]["evidence"] == {
        "stderr_tail": "first evidence"
    }
    assert second["steps"]["task_finalized"]["status"] == "failed"
    assert second["steps"]["task_finalized"]["error"] == "terminalized_failed_run"
    assert second["steps"]["task_finalized"]["evidence"] == {
        "terminal_token": "token-1"
    }
    assessment = execution_terminal_assessment(
        state_path,
        expected_run_id="run-1",
        expected_account_id="xhs-a01",
    )
    assert assessment["terminal_complete"] is True
    assert assessment["reason"] == "terminal_state_complete"


def test_handler_restore_is_exact_even_after_first_wins_signal(tmp_path: Path) -> None:
    db_path = tmp_path / "control.sqlite"
    terminal_db(db_path)
    before = {value: signal.getsignal(value) for value in (signal.SIGINT, signal.SIGTERM)}
    terminalizer = XhsRunTerminalizer(db_path=db_path, run_id="run-1")
    terminalizer.install()
    try:
        terminalizer._signal_handler(signal.SIGTERM, None)
        terminalizer._signal_handler(signal.SIGINT, None)
        assert terminalizer.signal_received == signal.SIGTERM
    finally:
        terminalizer.restore()
    assert {
        value: signal.getsignal(value) for value in (signal.SIGINT, signal.SIGTERM)
    } == before


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_formal_os_signal_immediately_after_acquire_has_one_failed_terminal_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signum: int,
) -> None:
    target_path = tmp_path / "targets.json"
    target_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "targets": [
                    {
                        "target_key": "test",
                        "keyword": "青岛旅游",
                        "top_refresh_max_pages": 1,
                        "timeout_seconds": 1800,
                        "required_fields_profile": "image_post_with_followers_v1",
                        "followers_policy": "required",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    pool_path = tmp_path / "pool.json"
    pool_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "lease_seconds": 2400,
                "behavior_profile": "xhs_guarded",
                "headed": True,
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "content.sqlite"
    bootstrap_database(db_path, sync_jobs=False)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        accounts.ensure_xhs_schema(conn)
        accounts.register_account_slot(conn, "xhs-a01")

    run_id = "interrupt-after-acquire"
    monkeypatch.setattr(xhs_runner, "XHS_RUNTIME_ROOT", tmp_path / "runtime")
    monkeypatch.setattr(xhs_runner, "XHS_EXECUTION_STATE_ROOT", tmp_path / "states")
    monkeypatch.setattr(xhs_runner, "XHS_RUNS_OUTPUT", tmp_path / "outputs")
    monkeypatch.setattr(xhs_runner, "utc_stamp", lambda: run_id)
    monkeypatch.setattr(accounts, "XHS_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(
        "trippostcollect.xhs.runtime.XHS_SESSION_ROOT",
        tmp_path / "sessions",
    )
    phases: list[str] = []

    def interrupt_after_acquire(
        phase: str,
        _terminalizer: XhsRunTerminalizer,
    ) -> None:
        phases.append(phase)
        if phase == "after_acquire":
            os.kill(os.getpid(), signum)

    monkeypatch.setattr(xhs_runner, "_TERMINAL_PHASE_HOOK", interrupt_after_acquire)
    args = Namespace(
        target_key="test",
        account_id="xhs-a01",
        post_interaction="none",
        db=str(db_path),
        target_config=str(target_path),
        pool_config=str(pool_path),
        dry_run=False,
        no_import=False,
        retry_on_300011=False,
    )

    assert xhs_runner._run_main(args) == 128 + signum
    assert phases.count("after_acquire") == 1
    state = json.loads(
        (tmp_path / "states" / run_id / "test.json").read_text(encoding="utf-8")
    )
    summary = json.loads(
        (tmp_path / "runtime" / "runs" / run_id / "run_summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["status"] == "failed"
    assert state["steps"]["task_finalized"]["status"] == "failed"
    assert summary["reason"] == "operator_interrupt"
    assert summary["terminal_commit"]["outcome"] == "failed"
    assert summary["terminal_commit"]["finalization_confirmed"] is True
    assert summary["lease_released"] is True
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM xhs_discovery_checkpoints").fetchone()[0] == 0
        assert conn.execute(
            """
            SELECT COUNT(*) FROM xhs_account_events
            WHERE run_id=? AND event_type='formal_run_finished'
            """,
            (run_id,),
        ).fetchone()[0] == 1


def test_sms_terminal_before_pagination_keeps_precise_reason_and_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_path = tmp_path / "targets.json"
    target_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "targets": [
                    {
                        "target_key": "test",
                        "keyword": "青岛旅游",
                        "top_refresh_max_pages": 1,
                        "timeout_seconds": 1800,
                        "required_fields_profile": "image_post_with_followers_v1",
                        "followers_policy": "required",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    pool_path = tmp_path / "pool.json"
    pool_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "lease_seconds": 2400,
                "behavior_profile": "xhs_guarded",
                "headed": True,
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "content.sqlite"
    bootstrap_database(db_path, sync_jobs=False)
    campaign_summary = tmp_path / "campaign_summary.json"
    campaign_summary.write_text('{"records": []}', encoding="utf-8")
    target = {"target_key": "test", "keyword": "青岛旅游"}
    fingerprint = xhs_query_fingerprint(target)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        accounts.ensure_xhs_schema(conn)
        accounts.register_account_slot(conn, "xhs-a01")
        save_checkpoint(
            conn,
            target_key="test",
            account_id="xhs-a01",
            keyword="青岛旅游",
            query_fingerprint_value=fingerprint,
            resume_page=44,
            resume_search_id="safe-search-id",
            source_has_more=True,
            last_batch_complete=True,
            last_stop_reason="continue",
            last_run_id="safe-run",
        )
        update_campaign(
            conn,
            target_key="test",
            account_id="xhs-a01",
            query_fingerprint_value=fingerprint,
            summary_path=str(campaign_summary),
            candidate_count=579,
        )
        conn.commit()

    child_summary_path = tmp_path / "child_summary.json"
    child_summary_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "platform": "xhs",
                        "failure_classification": {
                            "status": "blocked",
                            "failure_type": "sms_verification_terminal",
                            "retryable": False,
                            "reason": "xhs_sms_verification_parameter_error",
                        },
                        "behavior_evidence": {"status": "missing"},
                    }
                ],
                "pagination_evidence": {
                    "available": False,
                    "stopped": False,
                    "batches": [],
                    "stop_event": None,
                },
                "formal_validation": {
                    "completion_met": False,
                    "source_exhausted_met": False,
                    "stop_reason": "runtime_failed",
                    "stop_detail": "xhs_sms_verification_parameter_error",
                },
                "behavior_validation": {"platforms": {}},
                "import_completion_met": False,
                "import_result": {
                    "skipped": True,
                    "reason": "sms_verification_terminal",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    run_id = "sms-terminal-run"
    runtime_root = tmp_path / "runtime"
    execution_root = tmp_path / "execution"
    session_root = tmp_path / "sessions"
    monkeypatch.setattr(xhs_runner, "XHS_RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(xhs_runner, "XHS_EXECUTION_STATE_ROOT", execution_root)
    monkeypatch.setattr(xhs_runner, "XHS_RUNS_OUTPUT", tmp_path / "outputs")
    monkeypatch.setattr(xhs_runner, "LOCAL_MEDIA_ROOT", tmp_path / "media")
    monkeypatch.setattr(xhs_runner, "utc_stamp", lambda: run_id)
    monkeypatch.setattr(accounts, "XHS_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(
        "trippostcollect.xhs.runtime.XHS_SESSION_ROOT",
        session_root,
    )

    def failed_child(*_args: Any, **_kwargs: Any) -> LeaseSubprocessResult:
        return LeaseSubprocessResult(
            args=["mediacrawler_crawl.py"],
            returncode=2,
            stdout=json.dumps({"summary": str(child_summary_path)}),
            stderr=(
                "RuntimeError: "
                "xhs_login_verification_terminal:Parameter error"
            ),
            timed_out=False,
        )

    monkeypatch.setattr(xhs_runner, "run_supervised_xhs_subprocess", failed_child)
    args = Namespace(
        target_key="test",
        account_id="xhs-a01",
        post_interaction="none",
        db=str(db_path),
        target_config=str(target_path),
        pool_config=str(pool_path),
        dry_run=False,
        no_import=False,
        retry_on_300011=False,
    )

    assert xhs_runner._run_main(args) == 2
    summary = json.loads(
        (runtime_root / "runs" / run_id / "run_summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["failure_type"] == "sms_verification_terminal"
    assert summary["stop_reason"] == "runtime_failed"
    assert summary["stop_detail"] == "xhs_sms_verification_parameter_error"
    assert summary["reason"] == "xhs_sms_verification_parameter_error"
    assert summary["challenge"] == "xhs_sms_verification_parameter_error"
    assert summary["login_reason"] == ""
    assert summary["exit_code"] == 2
    assert summary["discovery"] == {
        "skipped": True,
        "reason": "runtime_blocked_before_pagination",
        "checkpoint_preserved": True,
        "failure_type": "sms_verification_terminal",
        "stop_reason": "runtime_failed",
        "stop_detail": "xhs_sms_verification_parameter_error",
    }
    assert summary["lease_released"] is True
    assert summary["runtime_session_removed"] is True
    assert "terminal_commit_failed" not in json.dumps(summary)

    with sqlite3.connect(db_path) as conn:
        checkpoint = conn.execute(
            """
            SELECT resume_page, resume_search_id, last_run_id,
                   campaign_candidate_count
            FROM xhs_discovery_checkpoints
            WHERE target_key='test' AND account_id='xhs-a01'
            """
        ).fetchone()
        event_row = conn.execute(
            """
            SELECT details_json
            FROM xhs_account_events
            WHERE run_id=? AND event_type='formal_run_finished'
            """,
            (run_id,),
        ).fetchone()
    assert checkpoint == (44, "safe-search-id", "safe-run", 579)
    event = json.loads(event_row[0])
    assert event["failure_type"] == "sms_verification_terminal"
    assert event["stop_reason"] == "runtime_failed"
    assert event["stop_detail"] == "xhs_sms_verification_parameter_error"
    assert not (session_root / run_id).exists()
