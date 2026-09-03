"""TripPostCollect tests for xhs pool."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path

import pytest

from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.xhs import accounts
from trippostcollect.xhs.config import load_pool_config, load_target
from trippostcollect.xhs.config import XhsConfigError
from trippostcollect.xhs.runtime import (
    prepare_runtime_session,
    remove_runtime_session,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
xhs_runner = import_module("xhs_runner")
crawl_runner = import_module("crawl_runner")
mediacrawler_crawl = import_module("mediacrawler_crawl")


def open_db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "pool.sqlite"
    bootstrap_database(path, sync_jobs=False)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    accounts.ensure_xhs_schema(conn)
    return conn


def test_default_xhs_target_uses_exhaustion_schema() -> None:
    target = load_target("qingdao_travel")
    pool = load_pool_config()

    assert "target_new_posts" not in target
    assert "candidate_hard_limit" not in target
    assert "max_stagnant_batches" not in target
    assert target["top_refresh_max_pages"] == 5
    assert target["timeout_seconds"] == 7200
    assert target["local_image_storage_required"] is True
    assert "download_images" not in target
    assert pool["lease_seconds"] == 29100
    assert pool["lease_seconds"] >= target["timeout_seconds"] + 300


def test_long_xhs_target_uses_time_budget_only() -> None:
    target = load_target("qingdao_free_travel_exhaustive")
    pool = load_pool_config()

    assert target["keyword"] == "青岛自由行"
    assert target["top_refresh_max_pages"] == 5
    assert target["timeout_seconds"] == 28800
    assert pool["lease_seconds"] >= target["timeout_seconds"] + 300


def test_xhs_runtime_session_has_only_profile_and_is_removable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trippostcollect.xhs.runtime.XHS_SESSION_ROOT",
        tmp_path / "sessions",
    )
    paths = prepare_runtime_session("run-1")

    assert set(paths) == {"root", "profile"}
    assert paths["profile"].is_dir()
    assert not (paths["root"] / "storage_state.json").exists()
    assert remove_runtime_session(paths["root"]) is True
    assert not paths["root"].exists()


def test_xhs_guarded_runtime_session_delegates_creation_to_lease_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trippostcollect.xhs.runtime.XHS_SESSION_ROOT",
        tmp_path / "sessions",
    )

    class FakeGuard:
        run_id = "run-2"

        def prepare_runtime_session(self) -> dict[str, Path]:
            return prepare_runtime_session(self.run_id)

    with xhs_runner.guarded_runtime_session("run-2", FakeGuard()) as paths:
        (paths["profile"] / "Cookies").write_text("secret", encoding="utf-8")
        root = paths["root"]
        assert root.exists()
        assert not (root / "storage_state.json").exists()

    assert root.exists()
    assert remove_runtime_session(root) is True


@pytest.mark.parametrize("interrupt_kind", ["lease_signal", "keyboard_interrupt"])
def test_xhs_operator_interrupt_finalizes_state_summary_and_exact_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_kind: str,
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
    run_id = "operator-interrupt-run"
    runtime_root = tmp_path / "runtime"
    execution_root = tmp_path / "execution"
    session_root = tmp_path / "sessions"
    monkeypatch.setattr(xhs_runner, "XHS_RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(xhs_runner, "XHS_EXECUTION_STATE_ROOT", execution_root)
    monkeypatch.setattr(xhs_runner, "XHS_RUNS_OUTPUT", tmp_path / "outputs")
    monkeypatch.setattr(xhs_runner, "utc_stamp", lambda: run_id)
    monkeypatch.setattr(
        "trippostcollect.xhs.runtime.XHS_SESSION_ROOT",
        session_root,
    )
    monkeypatch.setattr(accounts, "XHS_LOCK_ROOT", tmp_path / "locks")
    watchdogs = []
    monkeypatch.setattr(
        accounts,
        "XHS_LEGACY_ACCOUNT_ROOT",
        tmp_path / "legacy-accounts",
    )
    previous_handlers = {
        signum: xhs_runner.signal.getsignal(signum)
        for signum in (xhs_runner.signal.SIGINT, xhs_runner.signal.SIGTERM)
    }
    secondary_signals = []
    original_fail_open_step = xhs_runner.fail_open_step
    original_terminate_owned_processes = (
        xhs_runner.LeaseGuard.terminate_owned_processes
    )

    def interrupt_child(self, *_args, **_kwargs):
        watchdogs.append(_kwargs.get("runtime_watchdog"))
        if interrupt_kind == "lease_signal":
            self._signal_handler(xhs_runner.signal.SIGINT, None)
        raise KeyboardInterrupt

    def fail_open_with_secondary_signal(*args, **kwargs):
        guard = xhs_runner._ACTIVE_LEASE_GUARD
        assert guard is not None
        guard._signal_handler(xhs_runner.signal.SIGTERM, None)
        secondary_signals.append(("terminal_write", guard.signal_received))
        return original_fail_open_step(*args, **kwargs)

    def terminate_with_secondary_signal(self):
        assert self._closing is True
        self._signal_handler(xhs_runner.signal.SIGTERM, None)
        secondary_signals.append(("close", self.signal_received))
        return original_terminate_owned_processes(self)

    monkeypatch.setattr(xhs_runner.LeaseGuard, "run_subprocess", interrupt_child)
    monkeypatch.setattr(xhs_runner, "fail_open_step", fail_open_with_secondary_signal)
    monkeypatch.setattr(
        xhs_runner.LeaseGuard,
        "terminate_owned_processes",
        terminate_with_secondary_signal,
    )
    args = argparse.Namespace(
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

    assert xhs_runner._run_main(args) == 130
    assert secondary_signals == [
        ("terminal_write", int(xhs_runner.signal.SIGINT)),
        ("close", int(xhs_runner.signal.SIGINT)),
    ]
    assert {
        signum: xhs_runner.signal.getsignal(signum)
        for signum in (xhs_runner.signal.SIGINT, xhs_runner.signal.SIGTERM)
    } == previous_handlers
    assert len(watchdogs) == 1
    assert watchdogs[0].startup_grace_seconds == 120.0
    assert watchdogs[0].stale_after_seconds == 60.0
    assert watchdogs[0].poll_seconds == 5.0
    assert watchdogs[0].resume_grace_seconds == 30.0

    summary_path = runtime_root / "runs" / run_id / "run_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert summary["failure_type"] == "runtime_failed"
    assert summary["stop_reason"] == "runtime_failed"
    assert summary["reason"] == "operator_interrupt"
    assert summary["interrupt"] == {
        "reason": "operator_interrupt",
        "source": interrupt_kind,
        "signum": int(xhs_runner.signal.SIGINT),
        "signal": "SIGINT",
        "exit_code": 130,
    }
    assert summary["runtime_session_removed"] is True
    assert summary["lease_released"] is True
    assert summary["lease_cleanup"]["ok"] is True
    assert summary["lease_cleanup"]["event_type"] == "lease_released"
    assert summary["lease_cleanup"]["signal"] == int(xhs_runner.signal.SIGINT)
    assert summary["lease_cleanup"]["process_check"]["safe_to_release"] is True
    assert not (session_root / run_id).exists()

    state_path = execution_root / run_id / "test.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert (
        state["steps"]["command_executed"]["error"]
        == "xhs_runtime_failed:operator_interrupt:SIGINT"
    )
    assert state["steps"]["command_executed"]["evidence"]["interrupt"] == summary[
        "interrupt"
    ]
    assert "adaptive_search_stopped" not in {
        event.get("type") for event in state.get("events", [])
    }

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM xhs_lease_processes").fetchone()[0] == 0
        row = conn.execute(
            "SELECT status, finished_at, report_json FROM xhs_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == "failed"
        assert row[1]
        assert json.loads(row[2])["lease_cleanup"]["ok"] is True


def test_formal_xhs_operator_login_wait_matches_documented_window() -> None:
    assert mediacrawler_crawl.XHS_OPERATOR_LOGIN_WAIT_SECONDS == 600


def test_xhs_low_level_cli_has_no_storage_state_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["mediacrawler_crawl.py"])

    args = mediacrawler_crawl.parse_args()

    assert not hasattr(args, "xhs_storage_state")


def test_xhs_low_level_executor_uses_profile_contract_without_legacy_login_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_command(*args: object, **kwargs: object) -> dict[str, object]:
        captured["extra_env"] = kwargs["extra_env"]
        return {
            "returncode": 0,
            "timed_out": False,
            "stdout_tail": "",
            "stderr_tail": "",
        }

    monkeypatch.setattr(mediacrawler_crawl, "run_command", fake_run_command)
    monkeypatch.setattr(mediacrawler_crawl, "discover_cdp_browser_path", lambda: None)
    monkeypatch.setattr(
        mediacrawler_crawl,
        "summarize_output",
        lambda *_args: {
            "parse_errors": 0,
            "content_records": 0,
            "non_video_content_records": 0,
            "video_like_records": 0,
        },
    )

    args = argparse.Namespace(
        behavior_profile="xhs_guarded",
        db=str(tmp_path / "content.sqlite"),
        discovery_job_id=None,
        discovery_source_exhausted=False,
        download_images=True,
        headed=True,
        keyword="青岛旅游",
        login_type="qrcode",
        post_repair_detail_targets=[],
        resume_identities_path=None,
        start_cursor="",
        start_offset=0,
        start_page=1,
        timeout_per_platform=7200,
        top_refresh_max_pages=5,
        xhs_account_id="xhs-a01",
        xhs_detail_urls=[],
        xhs_discovery_query_fingerprint="fingerprint",
        xhs_discovery_target_key="target",
        xhs_post_interaction="none",
        xhs_profile_dir=str(tmp_path / "profile"),
        xhs_repair=False,
        xhs_repair_batch_size=5,
        zhihu_detail_urls=[],
    )

    result = mediacrawler_crawl._run_platform_without_policy("xhs", args, tmp_path)

    extra_env = captured["extra_env"]
    assert isinstance(extra_env, dict)
    assert extra_env["TRIPPOSTCOLLECT_XHS_PROFILE_DIR"] == str(
        (tmp_path / "profile").resolve()
    )
    assert extra_env["TRIPPOSTCOLLECT_XHS_ACCOUNT_ID"] == "xhs-a01"
    assert "TRIPPOSTCOLLECT_XHS_RUN_SCOPED_LOGIN" not in extra_env
    assert "TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH" not in extra_env
    assert not hasattr(mediacrawler_crawl, "xhs_storage_snapshot_info")
    assert result["login_state"] is None


def test_xhs_runner_reads_login_required_from_structured_child_summary() -> None:
    child_summary = {
        "records": [
            {
                "failure_classification": {
                    "status": "login_required",
                    "failure_type": "login_required",
                    "reason": "login_or_profile_refresh_required",
                },
                "behavior_evidence": {
                    "status": "failed",
                    "visible_markers": {"login_required": True},
                },
            }
        ]
    }

    assert xhs_runner._login_reason("unrelated" * 1000, "", child_summary) == "login_required"


def test_xhs_runner_reads_challenge_from_structured_child_summary() -> None:
    child_summary = {
        "records": [
            {
                "failure_classification": {
                    "status": "captcha_detected",
                    "failure_type": "captcha_detected",
                },
                "behavior_evidence": {
                    "status": "failed",
                    "visible_markers": {"captcha": True},
                },
            }
        ]
    }

    assert xhs_runner._challenge_reason("", "", child_summary) == "captcha"


def test_xhs_runner_classifies_platform_security_limit_300011() -> None:
    child_summary = {
        "records": [
            {
                "failure_classification": {
                    "status": "blocked",
                    "failure_type": "visible_page_blocked",
                },
                "behavior_evidence": {
                    "status": "failed",
                    "visible_markers": {"platform_security_limit": True},
                    "visible_text_sample": "安全限制 Account exception, please retry later 300011",
                },
            }
        ]
    }

    assert xhs_runner._challenge_reason("", "", child_summary) == "platform_security_limit_300011"


def test_xhs_runner_classifies_platform_security_limit_from_current_record_error() -> None:
    child_summary = {
        "records": [
            {
                "failure_classification": {
                    "status": "blocked",
                    "failure_type": "platform_security_limit",
                    "reason": "platform_security_limit_300011",
                },
                "behavior_evidence": {
                    "status": "failed",
                    "error": "xhs_creator_profile_visible_block:platform_security_limit",
                    "visible_markers": {},
                },
            }
        ]
    }

    assert xhs_runner._challenge_reason("", "", child_summary) == "platform_security_limit_300011"


def _prepare_300011_retry_database(
    tmp_path: Path,
    *,
    finished_at: datetime,
) -> tuple[Path, Path]:
    db_path = tmp_path / "retry.sqlite"
    bootstrap_database(db_path, sync_jobs=False)
    child_summary_path = tmp_path / "child_summary.json"
    child_summary_path.write_text(
        json.dumps(
            {
                "pagination_evidence": {
                    "stopped": True,
                    "stop_reason": "runtime_failed",
                    "stop_detail": "platform_security_limit_300011",
                    "stop_event": {
                        "source_page": 42,
                        "resume_page": 42,
                        "batch_complete": False,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    run_id = "security-limit-run"
    report = {
        "status": "failed",
        "run_id": run_id,
        "account_id": "xhs-a01",
        "lease_id": "lease-security-limit",
        "runtime_session_removed": True,
        "challenge": "platform_security_limit_300011",
        "child_summary": str(child_summary_path),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "discovery": {
            "last_stop_reason": "runtime_failed",
            "resume_page": 42,
        },
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO xhs_accounts(
                account_id, status, created_at, updated_at
            ) VALUES ('xhs-a01', 'active', ?, ?)
            """,
            (
                finished_at.isoformat(timespec="seconds"),
                finished_at.isoformat(timespec="seconds"),
            ),
        )
        conn.execute(
            """
            INSERT INTO xhs_runs(
                run_id, target_key, account_id, status, started_at, finished_at,
                execution_state_path, child_summary_path, report_json
            ) VALUES (?, 'target', 'xhs-a01', 'failed', ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                (finished_at - timedelta(minutes=1)).isoformat(timespec="seconds"),
                finished_at.isoformat(timespec="seconds"),
                str(tmp_path / "state.json"),
                str(child_summary_path),
                json.dumps(report),
            ),
        )
        conn.execute(
            """
            INSERT INTO xhs_discovery_checkpoints(
                target_key, account_id, keyword, query_fingerprint,
                resume_page, resume_search_id, last_batch_complete,
                last_stop_reason, last_run_id, last_summary_path,
                campaign_candidate_count
            ) VALUES (
                'target', 'xhs-a01', '青岛旅游', 'fingerprint',
                42, 'cursor-42', 0, 'runtime_failed', ?, ?, 577
            )
            """,
            (run_id, str(child_summary_path)),
        )
        conn.execute(
            """
            INSERT INTO xhs_account_events(
                account_id, run_id, event_type, details_json, created_at
            ) VALUES ('xhs-a01', ?, 'lease_released', ?, ?)
            """,
            (
                run_id,
                json.dumps(
                    {
                        "lease_id": "lease-security-limit",
                        "owner_token_sha256": "owner-token-digest",
                        "outcome": "failed",
                        "process_check": {
                            "safe_to_release": True,
                            "checks": [],
                            "blocking": [],
                        },
                    }
                ),
                finished_at.isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    return db_path, child_summary_path


def _retry_args(db_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        db=str(db_path),
        target_key="target",
        account_id="xhs-a01",
        retry_on_300011=True,
        dry_run=False,
    )


def test_xhs_runner_retries_only_complete_300011_terminal(tmp_path: Path) -> None:
    finished_at = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
    db_path, child_summary_path = _prepare_300011_retry_database(
        tmp_path,
        finished_at=finished_at,
    )
    report = xhs_runner._latest_terminal_xhs_run(
        db_path,
        target_key="target",
        account_id="xhs-a01",
    )
    assert report is not None
    assert xhs_runner._security_limit_retry_evidence(report) == (
        True,
        "complete_300011_terminal",
    )
    assert xhs_runner._security_limit_retry_checkpoint_ready(
        db_path,
        target_key="target",
        account_id="xhs-a01",
        report=report,
    ) == (True, "safe_checkpoint_ready")
    assert xhs_runner._security_limit_retry_due_at(report) == finished_at + timedelta(
        minutes=30
    )

    child_summary_path.write_text(
        json.dumps(
            {
                "pagination_evidence": {
                    "stopped": True,
                    "stop_reason": "runtime_failed",
                    "stop_detail": "platform_security_limit_300011",
                    "stop_event": {"batch_complete": True},
                }
            }
        ),
        encoding="utf-8",
    )
    assert xhs_runner._security_limit_retry_evidence(report) == (
        False,
        "triggering_run_incomplete_boundary_missing",
    )


def test_xhs_runner_300011_retry_requires_exact_release_audit(tmp_path: Path) -> None:
    db_path, _ = _prepare_300011_retry_database(
        tmp_path,
        finished_at=datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc),
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE xhs_account_events
            SET details_json=?
            WHERE event_type='lease_released'
            """,
            (
                json.dumps(
                    {
                        "lease_id": "wrong-lease",
                        "owner_token_sha256": "owner-token-digest",
                        "process_check": {
                            "safe_to_release": True,
                            "blocking": [],
                        },
                    }
                ),
            ),
        )
        conn.commit()
    report = xhs_runner._latest_terminal_xhs_run(
        db_path,
        target_key="target",
        account_id="xhs-a01",
    )
    assert report is not None
    assert report["lease_released"] is False
    assert xhs_runner._security_limit_retry_evidence(report) == (
        False,
        "triggering_run_lease_not_released",
    )


@pytest.mark.parametrize("runtime_session_state", [False, "missing"])
def test_xhs_runner_300011_retry_requires_removed_runtime_session(
    tmp_path: Path,
    runtime_session_state: bool | str,
) -> None:
    db_path, _ = _prepare_300011_retry_database(
        tmp_path,
        finished_at=datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc),
    )
    report = xhs_runner._latest_terminal_xhs_run(
        db_path,
        target_key="target",
        account_id="xhs-a01",
    )
    assert report is not None
    if runtime_session_state == "missing":
        report.pop("runtime_session_removed")
    else:
        report["runtime_session_removed"] = runtime_session_state

    assert xhs_runner._security_limit_retry_evidence(report) == (
        False,
        "triggering_run_runtime_session_not_removed",
    )
    assert xhs_runner._terminal_release_ready(
        db_path,
        account_id="xhs-a01",
        report=report,
    ) == (False, "terminal_run_runtime_session_not_removed")


def test_xhs_runner_300011_controller_waits_without_lease_then_stops_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finished_at = datetime.now(timezone.utc).replace(microsecond=0)
    db_path, _ = _prepare_300011_retry_database(
        tmp_path,
        finished_at=finished_at,
    )
    monkeypatch.setattr(xhs_runner, "XHS_RETRY_STATE_ROOT", tmp_path / "retry_states")
    observed_due: list[datetime] = []

    def complete_externally(due_at: datetime) -> None:
        observed_due.append(due_at)
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 0
            completed_at = (finished_at + timedelta(seconds=1)).isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO xhs_runs(
                    run_id, target_key, account_id, status, started_at, finished_at,
                    execution_state_path, report_json
                ) VALUES (
                    'completed-run', 'target', 'xhs-a01', 'completed',
                    ?, ?, ?, ?
                )
                """,
                (
                    completed_at,
                    completed_at,
                    str(tmp_path / "completed-state.json"),
                    json.dumps(
                        {
                            "status": "completed",
                            "run_id": "completed-run",
                            "lease_id": "completed-lease",
                            "runtime_session_removed": True,
                            "finished_at": completed_at,
                        }
                    ),
                ),
            )
            conn.execute(
                """
                INSERT INTO xhs_account_events(
                    account_id, run_id, event_type, details_json, created_at
                ) VALUES ('xhs-a01', 'completed-run', 'lease_released', ?, ?)
                """,
                (
                    json.dumps(
                        {
                            "lease_id": "completed-lease",
                            "owner_token_sha256": "completed-owner-digest",
                            "outcome": "completed",
                            "process_check": {
                                "safe_to_release": True,
                                "checks": [],
                                "blocking": [],
                            },
                        }
                    ),
                    completed_at,
                ),
            )
            conn.commit()

    monkeypatch.setattr(xhs_runner, "_sleep_until", complete_externally)
    monkeypatch.setattr(
        xhs_runner,
        "_run_main",
        lambda _args: pytest.fail("controller must not run before the 30-minute timer"),
    )

    assert xhs_runner._run_security_limit_retry_controller(_retry_args(db_path)) == 0
    assert observed_due == [finished_at + timedelta(minutes=30)]
    state_files = list((tmp_path / "retry_states").glob("**/*.json"))
    assert len(state_files) == 1
    state = json.loads(state_files[0].read_text(encoding="utf-8"))
    assert state["status"] == "completed"
    assert state["attempt_count"] == 0
    with sqlite3.connect(db_path) as conn:
        event_types = {
            row[0]
            for row in conn.execute(
                """
                SELECT event_type FROM xhs_account_events
                WHERE event_type LIKE 'security_limit_retry_%'
                """
            )
        }
    assert event_types == {
        "security_limit_retry_scheduled",
        "security_limit_retry_completed",
    }


def test_xhs_runner_300011_controller_retries_repeated_limit_until_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    db_path, _ = _prepare_300011_retry_database(
        tmp_path,
        finished_at=now - timedelta(minutes=95),
    )
    monkeypatch.setattr(xhs_runner, "XHS_RETRY_STATE_ROOT", tmp_path / "retry_states")
    monkeypatch.setattr(
        xhs_runner,
        "_sleep_until",
        lambda _due_at: pytest.fail("both synthetic retry deadlines are already due"),
    )
    attempts: list[int] = []

    def run_attempt(_args: argparse.Namespace) -> int:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            run_id = "second-security-limit-run"
            lease_id = "second-security-limit-lease"
            finished_at = now - timedelta(minutes=65)
            child_summary = tmp_path / "second_child_summary.json"
            child_summary.write_text(
                json.dumps(
                    {
                        "pagination_evidence": {
                            "stopped": True,
                            "stop_reason": "runtime_failed",
                            "stop_detail": "platform_security_limit_300011",
                            "stop_event": {"batch_complete": False},
                        }
                    }
                ),
                encoding="utf-8",
            )
            report = {
                "status": "failed",
                "run_id": run_id,
                "lease_id": lease_id,
                "runtime_session_removed": True,
                "challenge": "platform_security_limit_300011",
                "child_summary": str(child_summary),
                "finished_at": finished_at.isoformat(timespec="seconds"),
                "discovery": {"last_stop_reason": "runtime_failed"},
            }
            status = "failed"
            exit_code = 2
        else:
            run_id = "completed-retry-run"
            lease_id = "completed-retry-lease"
            finished_at = now - timedelta(minutes=1)
            child_summary = None
            report = {
                "status": "completed",
                "run_id": run_id,
                "lease_id": lease_id,
                "runtime_session_removed": True,
                "finished_at": finished_at.isoformat(timespec="seconds"),
            }
            status = "completed"
            exit_code = 0
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO xhs_runs(
                    run_id, target_key, account_id, status, started_at,
                    finished_at, execution_state_path, child_summary_path,
                    report_json
                ) VALUES (?, 'target', 'xhs-a01', ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    status,
                    finished_at.isoformat(timespec="seconds"),
                    finished_at.isoformat(timespec="seconds"),
                    str(tmp_path / f"{run_id}-state.json"),
                    str(child_summary) if child_summary else None,
                    json.dumps(report),
                ),
            )
            if child_summary is not None:
                conn.execute(
                    """
                    UPDATE xhs_discovery_checkpoints
                    SET last_run_id=?, last_summary_path=?,
                        last_batch_complete=0, last_stop_reason='runtime_failed'
                    WHERE target_key='target' AND account_id='xhs-a01'
                    """,
                    (run_id, str(child_summary)),
                )
            conn.execute(
                """
                INSERT INTO xhs_account_events(
                    account_id, run_id, event_type, details_json, created_at
                ) VALUES ('xhs-a01', ?, 'lease_released', ?, ?)
                """,
                (
                    run_id,
                    json.dumps(
                        {
                            "lease_id": lease_id,
                            "owner_token_sha256": f"{lease_id}-owner-digest",
                            "outcome": status,
                            "process_check": {
                                "safe_to_release": True,
                                "checks": [],
                                "blocking": [],
                            },
                        }
                    ),
                    finished_at.isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
        return exit_code

    monkeypatch.setattr(xhs_runner, "_run_main", run_attempt)
    assert xhs_runner._run_security_limit_retry_controller(_retry_args(db_path)) == 0
    assert attempts == [1, 2]
    state_file = next((tmp_path / "retry_states").glob("**/*.json"))
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["status"] == "completed"
    assert state["attempt_count"] == 2
    with sqlite3.connect(db_path) as conn:
        attempt_events = conn.execute(
            """
            SELECT COUNT(*) FROM xhs_account_events
            WHERE event_type='security_limit_retry_attempt_started'
            """
        ).fetchone()[0]
    assert attempt_events == 2


def test_xhs_runner_300011_controller_rejects_duplicate_timer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, _ = _prepare_300011_retry_database(
        tmp_path,
        finished_at=datetime.now(timezone.utc).replace(microsecond=0),
    )
    monkeypatch.setattr(xhs_runner, "XHS_RETRY_STATE_ROOT", tmp_path / "retry_states")
    args = _retry_args(db_path)
    state_path = xhs_runner._retry_state_path(args)
    lock = xhs_runner.AccountLeaseFileLock(state_path.with_suffix(".lock"))
    lock.acquire()
    try:
        with pytest.raises(SystemExit, match="retry controller is already active"):
            xhs_runner._run_security_limit_retry_controller(args)
    finally:
        lock.release()


def test_xhs_runner_rejects_retry_timer_in_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "xhs_runner.py",
            "--target-key",
            "target",
            "--account-id",
            "xhs-a01",
            "--dry-run",
            "--retry-on-300011",
        ],
    )
    with pytest.raises(SystemExit):
        xhs_runner.parse_args()


def test_xhs_runner_ignores_prior_resume_challenge_when_latest_record_is_clean() -> None:
    child_summary = {
        "records": [
            {
                "failure_classification": {"status": "captcha_detected"},
                "behavior_evidence": {
                    "status": "failed",
                    "visible_markers": {"captcha_or_verify": True},
                },
            },
            {
                "failure_classification": {"status": "completed", "failure_type": "success"},
                "behavior_evidence": {
                    "status": "completed",
                    "challenge": "",
                    "visible_markers": {
                        "platform_security_limit": False,
                        "captcha_or_verify": False,
                        "rate_limited": False,
                        "blocked": False,
                        "login_required": False,
                    },
                },
            },
        ]
    }

    assert xhs_runner._challenge_reason("", "", child_summary) == ""


def test_xhs_runner_does_not_treat_false_marker_names_as_failures() -> None:
    child_summary = {
        "records": [
            {
                "failure_classification": {
                    "status": "completed",
                    "failure_type": "success",
                    "reason": "completed",
                },
                "behavior_evidence": {
                    "status": "completed",
                    "challenge": "",
                    "error": "",
                    "initial_visible_markers": {
                        "captcha_or_verify": False,
                        "login_required": False,
                    },
                    "visible_markers": {
                        "captcha_or_verify": False,
                        "rate_limited": False,
                        "blocked": False,
                        "login_required": False,
                    },
                },
            }
        ]
    }
    stdout = json.dumps(child_summary, ensure_ascii=False)

    assert xhs_runner._challenge_reason(stdout, "", child_summary) == ""
    assert xhs_runner._login_reason(stdout, "", child_summary) == ""


def test_xhs_runner_uses_raw_failure_text_without_structured_records() -> None:
    assert xhs_runner._challenge_reason("请完成验证", "", {}) == "请完成验证"
    assert xhs_runner._login_reason("", "扫码登录", {}) == "扫码登录"


def test_config_and_child_command_use_run_scoped_login_paths(tmp_path: Path) -> None:
    target_path = tmp_path / "targets.json"
    pool_path = tmp_path / "pool.json"
    target_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "targets": [
                    {
                        "target_key": "test",
                        "keyword": "青岛旅游",
                        "top_refresh_max_pages": 3,
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
    target = load_target("test", target_path)
    pool = load_pool_config(pool_path)
    command = xhs_runner.build_child_command(
        target=target,
        pool=pool,
        account={"account_id": "xhs-a01"},
        profile_dir=tmp_path / "profile",
        db_path=tmp_path / "db.sqlite",
        output_root=tmp_path / "output",
        no_import=False,
        post_interaction="comment-scroll",
        discovery={
            "resume_page": 7,
            "resume_search_id": "saved-search-id",
            "source_exhausted": False,
            "top_refresh_max_pages": 3,
            "campaign_summary_path": "",
            "target_key": "test",
            "query_fingerprint": "test-fingerprint",
        },
    )

    assert command[command.index("--xhs-account-id") + 1] == "xhs-a01"
    assert command[command.index("--xhs-discovery-target-key") + 1] == "test"
    assert (
        command[command.index("--xhs-discovery-query-fingerprint") + 1]
        == "test-fingerprint"
    )
    assert command[command.index("--behavior-profile") + 1] == "xhs_guarded"
    assert command[command.index("--login-type") + 1] == "qrcode"
    assert command[command.index("--xhs-profile-dir") + 1] == str(
        tmp_path / "profile"
    )
    assert "--xhs-storage-state" not in command
    assert command[command.index("--xhs-post-interaction") + 1] == "comment-scroll"
    assert "--completion-mode" not in command
    assert "--target-new-posts" not in command
    assert "--candidate-hard-limit" not in command
    assert "--max-stagnant-batches" not in command
    assert command[command.index("--start-page") + 1] == "7"
    assert command[command.index("--start-cursor") + 1] == "saved-search-id"
    assert command[command.index("--top-refresh-max-pages") + 1] == "3"
    assert "--download-images" in command
    assert command[command.index("--media-root") + 1] == str(
        xhs_runner.LOCAL_MEDIA_ROOT.resolve()
    )
    assert "--get-media" not in command


def test_xhs_pool_requires_headed_browser(tmp_path: Path) -> None:
    pool_path = tmp_path / "pool.json"
    pool_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "lease_seconds": 2400,
                "behavior_profile": "xhs_guarded",
                "headed": False,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(XhsConfigError, match="headed=true"):
        load_pool_config(pool_path)


def test_xhs_schema_removes_persistent_fields_without_erasing_history_or_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(accounts, "XHS_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(
        accounts,
        "XHS_LEGACY_ACCOUNT_ROOT",
        tmp_path / "legacy-accounts-root",
    )
    db_path = tmp_path / "legacy.sqlite"
    legacy_profile = tmp_path / "legacy-account" / "profile"
    legacy_profile.mkdir(parents=True)
    legacy_cookie = legacy_profile / "Cookies"
    legacy_cookie.write_bytes(b"legacy-cookie-material")
    legacy_state = tmp_path / "legacy-account" / "storage_state.enc"
    legacy_state.write_bytes(b"legacy-encrypted-state")
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        conn.execute(
            """
            CREATE TABLE xhs_accounts(
                account_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                profile_dir TEXT NOT NULL UNIQUE,
                encrypted_state_path TEXT NOT NULL UNIQUE,
                identity_hash TEXT UNIQUE,
                last_verified_at TEXT,
                last_used_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO xhs_accounts VALUES(
                'xhs-a01', 'login_required', ?, ?,
                'identity', NULL, NULL, datetime('now'), datetime('now')
            )
            """,
            (str(legacy_profile), str(legacy_state)),
        )
        conn.execute(
            """
            CREATE TABLE xhs_account_events(
                id INTEGER PRIMARY KEY,
                account_id TEXT REFERENCES xhs_accounts(account_id) ON DELETE SET NULL,
                run_id TEXT,
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO xhs_account_events(
                id, account_id, run_id, event_type, details_json, created_at
            ) VALUES (
                41, 'xhs-a01', 'legacy-login-run', 'login_persisted',
                '{"identity_hash":"legacy-identity"}', '2026-08-01T00:00:00+00:00'
            )
            """
        )
        conn.execute(
            "CREATE TABLE xhs_platform_state(site_key TEXT PRIMARY KEY, status TEXT, daily_runs INTEGER)"
        )
        accounts.ensure_xhs_schema(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(xhs_accounts)")}
        assert columns == {
            "account_id",
            "status",
            "last_used_at",
            "created_at",
            "updated_at",
        }
        assert conn.execute(
            "SELECT status FROM xhs_accounts WHERE account_id='xhs-a01'"
        ).fetchone()[0] == "active"
        conn.commit()
        accounts.ensure_xhs_schema(conn)
        assert conn.execute(
            """
            SELECT id, account_id, run_id, event_type, details_json, created_at
            FROM xhs_account_events WHERE id=41
            """
        ).fetchone() == (
            41,
            "xhs-a01",
            "legacy-login-run",
            "login_persisted",
            '{"identity_hash":"legacy-identity"}',
            "2026-08-01T00:00:00+00:00",
        )
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='xhs_platform_state'"
        ).fetchone() is None
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert legacy_cookie.read_bytes() == b"legacy-cookie-material"
    assert legacy_state.read_bytes() == b"legacy-encrypted-state"


def test_pool_config_rejects_removed_automatic_controls(tmp_path: Path) -> None:
    path = tmp_path / "pool.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "lease_seconds": 2400,
                "behavior_profile": "xhs_guarded",
                "headed": True,
                "global_daily_runs": 3,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(XhsConfigError, match="removed automatic XHS controls"):
        load_pool_config(path)


def test_xhs_config_rejects_removed_enabled_gates(tmp_path: Path) -> None:
    pool_path = tmp_path / "pool.json"
    pool_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "enabled": False,
                "lease_seconds": 2400,
                "behavior_profile": "xhs_guarded",
                "headed": True,
            }
        ),
        encoding="utf-8",
    )
    target_path = tmp_path / "targets.json"
    target_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "targets": [{"target_key": "test", "enabled": False}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(XhsConfigError, match="removed automatic XHS controls"):
        load_pool_config(pool_path)
    with pytest.raises(XhsConfigError, match="removed XHS target enabled gate"):
        load_target("test", target_path)


def test_xhs_config_rejects_legacy_schema(tmp_path: Path) -> None:
    pool_path = tmp_path / "pool.json"
    pool_path.write_text(
        json.dumps({"schema_version": 1}),
        encoding="utf-8",
    )

    with pytest.raises(XhsConfigError, match="unsupported XHS config schema"):
        load_pool_config(pool_path)


def test_xhs_target_rejects_removed_download_images_option(tmp_path: Path) -> None:
    target_path = tmp_path / "targets.json"
    target_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "targets": [
                    {
                        "target_key": "test",
                        "keyword": "青岛旅游",
                        "top_refresh_max_pages": 0,
                        "timeout_seconds": 30,
                        "required_fields_profile": "image_post_with_followers_v1",
                        "followers_policy": "required",
                        "download_images": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(XhsConfigError, match="removed XHS target download_images"):
        load_target("test", target_path)


def test_xhs_target_rejects_removed_quantity_fields(tmp_path: Path) -> None:
    target_path = tmp_path / "targets.json"
    target_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "targets": [
                    {
                        "target_key": "test",
                        "keyword": "青岛旅游",
                        "top_refresh_max_pages": 0,
                        "timeout_seconds": 30,
                        "required_fields_profile": "image_post_with_followers_v1",
                        "followers_policy": "required",
                        "target_new_posts": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(XhsConfigError, match="removed quantity fields"):
        load_target("test", target_path)


def test_failed_child_summary_remains_available_for_reporting(tmp_path: Path) -> None:
    path = tmp_path / "summary.json"
    path.write_text(
        json.dumps(
            {
                "behavior_validation": {
                    "platforms": {
                        "xhs": {
                            "post_interaction_ok": True,
                            "post_interactions": [{"selected_mode": "comment-scroll", "status": "completed"}],
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = xhs_runner.load_child_summary(str(path))

    assert loaded["behavior_validation"]["platforms"]["xhs"]["post_interaction_ok"] is True


def test_generic_entrypoints_do_not_select_xhs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["mediacrawler_crawl.py", "--no-import"])
    assert "xhs" not in mediacrawler_crawl.parse_args().platforms

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT 'legacy-xhs' AS job_key, 'xhs' AS site_key, '' AS target_url,
                   'mediacrawler_search' AS job_kind,
                   ? AS params_json, '{}' AS behavior_profile_json
            """,
            (json.dumps({"platform": "xhs"}),),
        ).fetchone()
        with pytest.raises(ValueError, match="xhs_runner"):
            crawl_runner.build_command(row, object())


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--candidate-hard-limit", "1"),
        ("--target-new-posts", "1"),
        ("--max-stagnant-batches", "1"),
        ("--completion-mode", "source-exhausted"),
    ],
)
def test_mediacrawler_rejects_removed_quantity_cli(
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    value: str,
) -> None:
    monkeypatch.setattr(sys, "argv", ["mediacrawler_crawl.py", flag, value])
    with pytest.raises(SystemExit):
        mediacrawler_crawl.parse_args()


def test_crawl_runner_rejects_removed_completion_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["crawl_runner.py", "--completion-mode", "source-exhausted"],
    )

    with pytest.raises(SystemExit):
        crawl_runner.parse_args()


def test_xhs_runner_rejects_removed_completion_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "xhs_runner.py",
            "--target-key",
            "qingdao_travel",
            "--account-id",
            "xhs-a01",
            "--completion-mode",
            "source-exhausted",
        ],
    )

    with pytest.raises(SystemExit):
        xhs_runner.parse_args()


@pytest.mark.parametrize(
    "config",
    [
        {"schema_version": 1, "jobs": []},
        {
            "schema_version": 2,
            "jobs": [
                {
                    "job_key": "removed-quantity-field",
                    "job_kind": "mediacrawler_search",
                    "params": {"target_new_posts": 1},
                }
            ],
        },
    ],
)
def test_crawl_config_rejects_pre_exhaustion_schema_and_fields(
    tmp_path: Path,
    config: dict,
) -> None:
    with pytest.raises(ValueError):
        crawl_runner.validate_crawl_config(config, tmp_path / "crawl.json")


def test_xhs_runner_requires_operator_selected_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["xhs_runner.py", "--target-key", "qingdao_travel", "--dry-run"])

    with pytest.raises(SystemExit):
        xhs_runner.parse_args()


def test_low_level_xhs_rejects_missing_account_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mediacrawler_crawl.py",
                "--platforms",
                "xhs",
                "--no-import",
        ],
    )
    monkeypatch.setattr(mediacrawler_crawl, "ensure_prerequisites", lambda: None)

    with pytest.raises(SystemExit, match="xhs-account-id"):
        mediacrawler_crawl.main()
