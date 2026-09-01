"""TripPostCollect tests for xhs pool."""

from __future__ import annotations

import argparse
import asyncio
import base64
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
from trippostcollect.xhs.sessions import (
    capture_context_state,
    decrypt_storage_state,
    encrypt_storage_state,
    load_snapshot_key,
    refresh_encrypted_storage_state,
    restore_context_state,
    snapshot_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
xhs_runner = import_module("xhs_runner")
crawl_runner = import_module("crawl_runner")
mediacrawler_crawl = import_module("mediacrawler_crawl")
xhs_login = import_module("xhs_login")


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


def test_manual_xhs_login_keeps_one_existing_tab() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.closed = False

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            self.closed = True

    class FakeContext:
        def __init__(self, pages: list[FakePage]) -> None:
            self.pages = pages

        async def new_page(self) -> FakePage:
            page = FakePage()
            self.pages.append(page)
            return page

    first, second, third = FakePage(), FakePage(), FakePage()
    context = FakeContext([first, second, third])

    page = asyncio.run(xhs_login.single_login_page(context))

    assert page is first
    assert not first.closed
    assert second.closed
    assert third.closed


def test_manual_xhs_login_waits_through_visible_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xhs_login, "LOGIN_STABILITY_CONFIRMATIONS", 2)
    monkeypatch.setattr(xhs_login, "LOGIN_STABILITY_POLL_MS", 1)
    states = [
        {
            "ok": False,
            "challenge_markers": ["安全验证"],
            "continuity": {"ready": False, "_token": ""},
        },
        {
            "ok": True,
            "challenge_markers": [],
            "profile_ids": ["profile-1"],
            "continuity": {"ready": True, "_token": "stable"},
        },
        {
            "ok": True,
            "challenge_markers": [],
            "profile_ids": ["profile-1"],
            "continuity": {"ready": True, "_token": "stable"},
        },
    ]

    class FakePage:
        def __init__(self) -> None:
            self.brought_to_front = 0
            self.waits = 0

        async def bring_to_front(self) -> None:
            self.brought_to_front += 1

        async def wait_for_timeout(self, timeout_ms: int) -> None:
            assert timeout_ms == 1
            self.waits += 1

    async def fake_page_state(page: FakePage) -> dict:
        return states.pop(0)

    monkeypatch.setattr(xhs_login, "xhs_page_state", fake_page_state)
    page = FakePage()

    _, state = asyncio.run(
        xhs_login.wait_for_login(
            page,
            600,
            phase="测试登录",
        )
    )

    assert state["ok"] is True
    assert state["challenge_observed"] is True
    assert state["observed_challenge_markers"] == ["安全验证"]
    assert state["stability_confirmations"] == 2
    assert page.brought_to_front == 1
    assert page.waits == 2


def test_manual_xhs_login_challenge_overrides_stale_signed_in_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xhs_login, "LOGIN_STABILITY_CONFIRMATIONS", 2)
    monkeypatch.setattr(xhs_login, "LOGIN_STABILITY_POLL_MS", 1)
    states = [
        {
            "ok": True,
            "challenge_markers": ["安全验证"],
            "profile_ids": ["profile-1"],
            "continuity": {"ready": True, "_token": "stable"},
        },
        {
            "ok": True,
            "challenge_markers": [],
            "profile_ids": ["profile-1"],
            "continuity": {"ready": True, "_token": "stable"},
        },
        {
            "ok": True,
            "challenge_markers": [],
            "profile_ids": ["profile-1"],
            "continuity": {"ready": True, "_token": "stable"},
        },
    ]

    class FakePage:
        def __init__(self) -> None:
            self.waits = 0

        async def bring_to_front(self) -> None:
            return None

        async def wait_for_timeout(self, timeout_ms: int) -> None:
            assert timeout_ms == 1
            self.waits += 1

    async def fake_page_state(page: FakePage) -> dict:
        return states.pop(0)

    monkeypatch.setattr(xhs_login, "xhs_page_state", fake_page_state)
    page = FakePage()

    _, state = asyncio.run(
        xhs_login.wait_for_login(
            page,
            600,
            phase="测试登录",
        )
    )

    assert state["ok"] is True
    assert state["challenge_observed"] is True
    assert page.waits == 2


def test_manual_xhs_login_platform_security_limit_stops_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class FakePage:
        def __init__(self) -> None:
            self.brought_to_front = 0
            self.waits = 0

        async def bring_to_front(self) -> None:
            self.brought_to_front += 1

        async def wait_for_timeout(self, timeout_ms: int) -> None:
            self.waits += 1

    async def fake_page_state(page: FakePage) -> dict:
        nonlocal calls
        calls += 1
        return {
            "ok": False,
            "platform_security_limit": True,
            "challenge_markers": ["安全限制", "300011"],
        }

    monkeypatch.setattr(xhs_login, "xhs_page_state", fake_page_state)
    page = FakePage()

    _, state = asyncio.run(
        xhs_login.wait_for_login(
            page,
            600,
            phase="测试登录",
        )
    )

    assert state["platform_security_limit"] is True
    assert state["challenge_observed"] is True
    assert state["observed_challenge_markers"] == ["300011", "安全限制"]
    assert calls == 1
    assert page.brought_to_front == 1
    assert page.waits == 0


def test_manual_xhs_login_requires_selfinfo_before_stable_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xhs_login, "LOGIN_STABILITY_CONFIRMATIONS", 1)
    monkeypatch.setattr(xhs_login, "LOGIN_STABILITY_POLL_MS", 1)

    class FakePage:
        def __init__(self) -> None:
            self.waits = 0

        async def wait_for_timeout(self, timeout_ms: int) -> None:
            assert timeout_ms == 1
            self.waits += 1

    class FakeMonitor:
        def __init__(self) -> None:
            self.calls = 0

        def public(self) -> dict:
            self.calls += 1
            return {"ok": self.calls >= 2, "observed": self.calls}

    async def fake_page_state(page: FakePage) -> dict:
        return {
            "ok": True,
            "profile_ids": ["profile-1"],
            "challenge_markers": [],
            "continuity": {"ready": True, "_token": "stable"},
        }

    monkeypatch.setattr(xhs_login, "xhs_page_state", fake_page_state)
    page = FakePage()
    _, state = asyncio.run(
        xhs_login.wait_for_login(
            page,
            600,
            phase="测试登录",
            self_info_monitor=FakeMonitor(),
        )
    )

    assert state["ok"] is True
    assert state["self_info"]["ok"] is True
    assert page.waits == 1


def test_manual_xhs_login_adopts_replacement_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xhs_login, "LOGIN_STABILITY_CONFIRMATIONS", 1)

    class FakePage:
        def __init__(self, url: str, *, closed: bool) -> None:
            self.url = url
            self.closed = closed

        def is_closed(self) -> bool:
            return self.closed

        async def wait_for_timeout(self, timeout_ms: int) -> None:
            raise AssertionError(timeout_ms)

    class FakeContext:
        def __init__(self, pages: list[FakePage]) -> None:
            self.pages = pages

    async def fake_page_state(page: FakePage) -> dict:
        assert page.url.endswith("/replacement")
        return {
            "ok": True,
            "profile_ids": ["profile-1"],
            "challenge_markers": [],
            "continuity": {"ready": True, "_token": "stable"},
        }

    monkeypatch.setattr(xhs_login, "xhs_page_state", fake_page_state)
    original = FakePage("https://www.xiaohongshu.com/explore", closed=True)
    replacement = FakePage("https://www.xiaohongshu.com/replacement", closed=False)
    selected, state = asyncio.run(
        xhs_login.wait_for_login(
            original,
            600,
            phase="测试登录",
            context=FakeContext([original, replacement]),
        )
    )

    assert selected is replacement
    assert state["ok"] is True
    assert state["page_replacements"] == 1


def test_xhs_login_does_not_treat_bare_retry_later_as_security_limit() -> None:
    assert xhs_login.CHALLENGE_RE.search("Please retry later") is None
    assert xhs_login.CHALLENGE_RE.search("Account exception, please retry later") is not None


@pytest.mark.asyncio
async def test_xhs_selfinfo_monitor_requires_successful_backend_result() -> None:
    class FakeResponse:
        url = "https://edith.xiaohongshu.com/api/sns/web/v1/user/selfinfo"
        status = 200

        async def json(self) -> dict:
            return {"data": {"result": {"success": True}}}

    monitor = xhs_login.SelfInfoMonitor()
    await monitor._consume(FakeResponse())

    assert monitor.public()["ok"] is True
    assert monitor.public()["observed"] == 1


def test_manual_xhs_login_lease_covers_both_operator_waits() -> None:
    assert xhs_login.login_lease_seconds(600) == 1_500


def test_formal_xhs_operator_login_wait_matches_documented_window() -> None:
    assert mediacrawler_crawl.XHS_OPERATOR_LOGIN_WAIT_SECONDS == 600


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
                account_id, status, profile_dir, encrypted_state_path,
                created_at, updated_at
            ) VALUES ('xhs-a01', 'active', ?, ?, ?, ?)
            """,
            (
                str(tmp_path / "profile"),
                str(tmp_path / "state.enc"),
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
    assert xhs_runner._login_reason("", "missing_xhs_storage_state", {}) == "missing_xhs_storage_state"


def test_xhs_runner_does_not_promote_target_closed_session_snapshot() -> None:
    child_summary = {
        "pagination_evidence": {
            "stop_reason": "runtime_failed",
            "stop_detail": "browser_context_closed",
        }
    }

    assert xhs_runner._storage_refresh_block_reason(
        exit_code=2,
        stdout="",
        stderr="TargetClosedError: context or browser has been closed",
        child_summary=child_summary,
    ) == "runtime:browser_context_closed"


def test_xhs_runner_can_promote_verified_session_after_content_failure() -> None:
    child_summary = {
        "pagination_evidence": {
            "stop_reason": "runtime_failed",
            "stop_detail": "search_or_detail_request_failed",
        }
    }

    assert xhs_runner._storage_refresh_block_reason(
        exit_code=2,
        stdout="",
        stderr="detail request failed",
        child_summary=child_summary,
    ) == ""


def test_storage_state_encryption_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = b"k" * 32
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_SNAPSHOT_KEY", base64.urlsafe_b64encode(key).decode("ascii"))
    loaded = load_snapshot_key()
    path = tmp_path / "state.enc"
    state = {"cookies": [{"name": "web_session", "value": "secret"}], "origins": []}

    encrypt_storage_state(state, path, account_id="xhs-a01", key=loaded)

    assert decrypt_storage_state(path, account_id="xhs-a01", key=loaded) == state
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(Exception):
        decrypt_storage_state(path, account_id="xhs-a02", key=loaded)


def test_restore_state_keeps_profile_cookie_and_scopes_session_device_id_to_primary_page() -> None:
    class FakePage:
        url = "about:blank"

        def __init__(self) -> None:
            self.init_script = ""

        async def add_init_script(self, script: str) -> None:
            self.init_script = script

    class FakeContext:
        def __init__(self) -> None:
            self.added_cookies: list[dict] = []
            self.init_script = ""

        async def cookies(self) -> list[dict]:
            return [{"name": "web_session", "domain": ".xiaohongshu.com", "path": "/"}]

        async def add_cookies(self, cookies: list[dict]) -> None:
            self.added_cookies = cookies

        async def add_init_script(self, script: str) -> None:
            self.init_script = script

    context = FakeContext()
    page = FakePage()
    asyncio.run(
        restore_context_state(
            context,
            {
                "cookies": [
                    {
                        "name": "web_session",
                        "value": "stale",
                        "domain": ".xiaohongshu.com",
                        "path": "/",
                    },
                    {
                        "name": "a1",
                        "value": "fallback",
                        "domain": ".xiaohongshu.com",
                        "path": "/",
                    },
                ],
                "origins": [],
                "trippostcollect": {
                    "runtime_storage": [
                        {
                            "origin": "https://www.xiaohongshu.com",
                            "page_role": "primary",
                            "localStorage": {},
                            "sessionStorage": {"XHS_TAB_DEVICE_ID": "device-1"},
                        }
                    ]
                },
                },
                primary_page=page,
            )
        )

    assert [cookie["name"] for cookie in context.added_cookies] == ["a1"]
    assert "XHS_TAB_DEVICE_ID" not in context.init_script
    assert "XHS_TAB_DEVICE_ID" in page.init_script
    assert "sessionStorage.getItem(key) === null" in page.init_script
    assert context.init_script.strip().startswith("(() =>")
    assert page.init_script.strip().startswith("(() =>")


def test_restore_state_does_not_merge_two_tab_device_ids() -> None:
    class FakePage:
        url = "https://www.xiaohongshu.com/explore"

        def __init__(self) -> None:
            self.init_script = ""

        async def add_init_script(self, script: str) -> None:
            self.init_script = script

    class FakeContext:
        def __init__(self) -> None:
            self.init_script = ""

        async def cookies(self) -> list[dict]:
            return []

        async def add_cookies(self, cookies: list[dict]) -> None:
            raise AssertionError(cookies)

        async def add_init_script(self, script: str) -> None:
            self.init_script = script

    page = FakePage()
    context = FakeContext()
    asyncio.run(
        restore_context_state(
            context,
            {
                "cookies": [],
                "origins": [],
                "trippostcollect": {
                    "runtime_storage": [
                        {
                            "origin": "https://www.xiaohongshu.com",
                            "page_role": "primary",
                            "localStorage": {},
                            "sessionStorage": {"XHS_TAB_DEVICE_ID": "device-primary"},
                        },
                        {
                            "origin": "https://www.xiaohongshu.com",
                            "page_role": "secondary",
                            "localStorage": {},
                            "sessionStorage": {"XHS_TAB_DEVICE_ID": "device-secondary"},
                        },
                    ]
                },
            },
            primary_page=page,
        )
    )

    assert "device-primary" in page.init_script
    assert "device-secondary" not in page.init_script
    assert "XHS_TAB_DEVICE_ID" not in context.init_script


def test_capture_and_refresh_storage_state_preserves_runtime_device_identity(
    tmp_path: Path,
) -> None:
    class FakePage:
        url = "https://www.xiaohongshu.com/explore"

        def is_closed(self) -> bool:
            return False

        async def evaluate(self, script: str) -> dict:
            assert "sessionStorage" in script
            return {
                "origin": "https://www.xiaohongshu.com",
                "url": self.url,
                "localStorage": {"b1": "stable-browser"},
                "sessionStorage": {
                    "XHS_RWP_FINGERPRINT": "fingerprint-1",
                    "XHS_TAB_DEVICE_ID": "device-1",
                },
            }

    class FakeContext:
        pages = [FakePage()]

        async def storage_state(self) -> dict:
            return {
                "cookies": [
                    {"name": "a1", "value": "a", "domain": ".xiaohongshu.com"},
                    {"name": "webId", "value": "w", "domain": ".xiaohongshu.com"},
                    {
                        "name": "web_session",
                        "value": "s",
                        "domain": ".xiaohongshu.com",
                    },
                ],
                "origins": [],
            }

    key = b"r" * 32
    encrypted_path = tmp_path / "state.enc"
    runtime_path = tmp_path / "state.json"
    context = FakeContext()
    old_state = asyncio.run(
        capture_context_state(
            context,
            account_id="xhs-a01",
            identity_hash="identity-1",
            primary_page=context.pages[0],
        )
    )
    encrypt_storage_state(old_state, encrypted_path, account_id="xhs-a01", key=key)
    refreshed = json.loads(json.dumps(old_state))
    refreshed["cookies"][2]["value"] = "s-refreshed"
    refreshed["trippostcollect"]["session_verification"] = {
        "status": "verified",
        "run_id": "run-1",
        "source": "xhs_selfinfo",
    }
    runtime_path.write_text(json.dumps(refreshed), encoding="utf-8")

    assert refresh_encrypted_storage_state(
        runtime_path,
        encrypted_path,
        account_id="xhs-a01",
        identity_hash="identity-1",
        expected_run_id="run-1",
        key=key,
    )
    saved = decrypt_storage_state(encrypted_path, account_id="xhs-a01", key=key)
    assert saved["trippostcollect"]["schema_version"] == 3
    assert saved["trippostcollect"]["account_id"] == "xhs-a01"
    assert saved["trippostcollect"]["runtime_storage"][0]["sessionStorage"] == {
        "XHS_RWP_FINGERPRINT": "fingerprint-1",
        "XHS_TAB_DEVICE_ID": "device-1",
    }
    assert saved["trippostcollect"]["runtime_storage"][0]["page_role"] == "primary"


def test_refresh_storage_state_rejects_unverified_failed_run(tmp_path: Path) -> None:
    key = b"s" * 32
    encrypted_path = tmp_path / "state.enc"
    runtime_path = tmp_path / "state.json"
    existing = {
        "cookies": [
            {"name": "a1", "value": "a", "domain": ".xiaohongshu.com"},
            {"name": "webId", "value": "w", "domain": ".xiaohongshu.com"},
            {"name": "web_session", "value": "last-good", "domain": ".xiaohongshu.com"},
        ],
        "origins": [],
        "trippostcollect": {
            "schema_version": 3,
            "account_id": "xhs-a01",
            "identity_hash": "identity-1",
            "session_verification": {
                "status": "verified",
                "run_id": "last-good-run",
            },
        },
    }
    encrypt_storage_state(existing, encrypted_path, account_id="xhs-a01", key=key)
    before = snapshot_sha256(encrypted_path)
    failed = json.loads(json.dumps(existing))
    failed["cookies"][2]["value"] = "failed-run"
    failed["trippostcollect"]["session_verification"] = {
        "status": "verified",
        "run_id": "other-run",
    }
    runtime_path.write_text(json.dumps(failed), encoding="utf-8")

    with pytest.raises(RuntimeError, match="belongs to another run"):
        refresh_encrypted_storage_state(
            runtime_path,
            encrypted_path,
            account_id="xhs-a01",
            identity_hash="identity-1",
            expected_run_id="failed-run",
            key=key,
        )

    assert snapshot_sha256(encrypted_path) == before
    assert decrypt_storage_state(
        encrypted_path,
        account_id="xhs-a01",
        key=key,
    )["cookies"][2]["value"] == "last-good"


def test_config_and_child_command_freeze_account_paths(tmp_path: Path) -> None:
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
        account={"account_id": "xhs-a01", "profile_dir": str(tmp_path / "profile")},
        storage_state=tmp_path / "runtime-state.json",
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


def test_xhs_schema_migrates_automatic_budget_and_breaker_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        legacy_schema = (ROOT / "db" / "xhs_control.sql").read_text(encoding="utf-8")
        legacy_schema = legacy_schema.replace(
            "identity_hash TEXT UNIQUE,",
            "identity_hash TEXT UNIQUE, health_score INTEGER, consecutive_failures INTEGER, "
            "daily_date TEXT, daily_runs INTEGER, cooldown_until TEXT,",
        )
        conn.executescript(legacy_schema)
        conn.execute(
            "CREATE TABLE xhs_platform_state(site_key TEXT PRIMARY KEY, status TEXT, daily_runs INTEGER)"
        )
        accounts.ensure_xhs_schema(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(xhs_accounts)")}
        assert "daily_runs" not in columns
        assert "cooldown_until" not in columns
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='xhs_platform_state'"
        ).fetchone() is None


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
