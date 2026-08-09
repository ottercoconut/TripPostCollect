from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sqlite3
import sys
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path

import pytest

from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.xhs import accounts
from trippostcollect.xhs.accounts import XhsAccountUnavailable
from trippostcollect.xhs.config import load_pool_config, load_target
from trippostcollect.xhs.config import XhsConfigError
from trippostcollect.xhs.sessions import decrypt_storage_state, encrypt_storage_state, load_snapshot_key


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


def pool_config(**overrides) -> dict:
    value = {
        "lease_seconds": 600,
    }
    value.update(overrides)
    return value


def test_default_xhs_target_budget() -> None:
    target = load_target("qingdao_travel")
    pool = load_pool_config()

    assert target["target_new_posts"] == 50
    assert target["candidate_hard_limit"] == 300
    assert target["max_stagnant_batches"] == 8
    assert target["top_refresh_max_pages"] == 5
    assert target["timeout_seconds"] == 7200
    assert target["local_image_storage_required"] is True
    assert "download_images" not in target
    assert pool["lease_seconds"] == 29100
    assert pool["lease_seconds"] >= target["timeout_seconds"] + 300


def test_exhaustive_xhs_target_budget() -> None:
    target = load_target("qingdao_free_travel_exhaustive")
    pool = load_pool_config()

    assert target["keyword"] == "青岛自由行"
    assert target["target_new_posts"] == 1000
    assert target["candidate_hard_limit"] == 1000
    assert target["max_stagnant_batches"] == 50
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
    states = [
        {
            "ok": False,
            "challenge_markers": ["安全验证"],
        },
        {
            "ok": True,
            "challenge_markers": [],
        },
    ]

    class FakePage:
        def __init__(self) -> None:
            self.brought_to_front = 0
            self.waits = 0

        async def bring_to_front(self) -> None:
            self.brought_to_front += 1

        async def wait_for_timeout(self, timeout_ms: int) -> None:
            assert timeout_ms == 2_000
            self.waits += 1

    async def fake_page_state(page: FakePage) -> dict:
        return states.pop(0)

    monkeypatch.setattr(xhs_login, "xhs_page_state", fake_page_state)
    page = FakePage()

    state = asyncio.run(
        xhs_login.wait_for_login(
            page,
            600,
            phase="测试登录",
        )
    )

    assert state["ok"] is True
    assert state["challenge_observed"] is True
    assert state["observed_challenge_markers"] == ["安全验证"]
    assert page.brought_to_front == 1
    assert page.waits == 1


def test_manual_xhs_login_challenge_overrides_stale_signed_in_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = [
        {
            "ok": True,
            "challenge_markers": ["安全验证"],
        },
        {
            "ok": True,
            "challenge_markers": [],
        },
    ]

    class FakePage:
        def __init__(self) -> None:
            self.waits = 0

        async def bring_to_front(self) -> None:
            return None

        async def wait_for_timeout(self, timeout_ms: int) -> None:
            assert timeout_ms == 2_000
            self.waits += 1

    async def fake_page_state(page: FakePage) -> dict:
        return states.pop(0)

    monkeypatch.setattr(xhs_login, "xhs_page_state", fake_page_state)
    page = FakePage()

    state = asyncio.run(
        xhs_login.wait_for_login(
            page,
            600,
            phase="测试登录",
        )
    )

    assert state["ok"] is True
    assert state["challenge_observed"] is True
    assert page.waits == 1


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


def test_account_lease_requires_explicit_account_and_only_blocks_same_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(accounts, "XHS_ACCOUNT_ROOT", tmp_path / "accounts")
    with open_db(tmp_path) as conn:
        for index in (1, 2):
            account_id = f"xhs-a0{index}"
            accounts.enroll_account(conn, account_id)
            accounts.mark_account_verified(conn, account_id, f"identity-{index}")
        first = accounts.acquire_account_lease(
            conn,
            run_id="run-1",
            pool_config=pool_config(),
            requested_account_id="xhs-a01",
            now=datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc),
        )
        assert first["account_id"] == "xhs-a01"
        second = accounts.acquire_account_lease(
            conn,
            run_id="run-2",
            pool_config=pool_config(),
            requested_account_id="xhs-a02",
            now=datetime(2026, 7, 14, 0, 1, tzinfo=timezone.utc),
        )
        assert second["account_id"] == "xhs-a02"
        with pytest.raises(XhsAccountUnavailable, match="busy"):
            accounts.acquire_account_lease(
                conn,
                run_id="run-3",
                pool_config=pool_config(),
                requested_account_id="xhs-a01",
                now=datetime(2026, 7, 14, 0, 2, tzinfo=timezone.utc),
            )
        accounts.release_account_lease(conn, account_id="xhs-a01", run_id="run-1", outcome="completed")
        leased_again = accounts.acquire_account_lease(
            conn,
            run_id="run-4",
            pool_config=pool_config(),
            requested_account_id="xhs-a01",
            now=datetime(2026, 7, 14, 0, 3, tzinfo=timezone.utc),
        )
        assert leased_again["account_id"] == "xhs-a01"
        accounts.release_account_lease(conn, account_id="xhs-a02", run_id="run-2", outcome="failed")
        assert accounts.get_account(conn, "xhs-a01")["status"] == "active"
        assert accounts.get_account(conn, "xhs-a02")["status"] == "active"


def test_login_lease_and_crawl_lease_are_mutually_exclusive(tmp_path: Path) -> None:
    with open_db(tmp_path) as conn:
        accounts.enroll_account(conn, "xhs-a01")
        accounts.mark_account_verified(conn, "xhs-a01", "identity-1")
        login_lease = accounts.acquire_account_login_lease(
            conn,
            run_id="login-1",
            requested_account_id="xhs-a01",
            lease_seconds=900,
            now=datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc),
        )
        assert login_lease["account_id"] == "xhs-a01"
        with pytest.raises(XhsAccountUnavailable, match="busy"):
            accounts.acquire_account_lease(
                conn,
                run_id="crawl-1",
                pool_config=pool_config(),
                requested_account_id="xhs-a01",
                now=datetime(2026, 7, 14, 0, 1, tzinfo=timezone.utc),
            )
        accounts.release_account_lease(conn, account_id="xhs-a01", run_id="login-1", outcome="completed")
        crawl_lease = accounts.acquire_account_lease(
            conn,
            run_id="crawl-2",
            pool_config=pool_config(),
            requested_account_id="xhs-a01",
            now=datetime(2026, 7, 14, 0, 2, tzinfo=timezone.utc),
        )
        assert crawl_lease["account_id"] == "xhs-a01"
        with pytest.raises(XhsAccountUnavailable, match="busy"):
            accounts.acquire_account_login_lease(
                conn,
                run_id="login-2",
                requested_account_id="xhs-a01",
                lease_seconds=900,
                now=datetime(2026, 7, 14, 0, 3, tzinfo=timezone.utc),
            )
        accounts.release_account_lease(conn, account_id="xhs-a01", run_id="crawl-2", outcome="completed")


def test_login_lease_accepts_login_pending_account(tmp_path: Path) -> None:
    with open_db(tmp_path) as conn:
        accounts.enroll_account(conn, "xhs-a01")
        lease = accounts.acquire_account_login_lease(
            conn,
            run_id="login-pending",
            requested_account_id="xhs-a01",
            lease_seconds=600,
            now=datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc),
        )

        assert lease["status"] == "login_pending"
        assert conn.execute(
            "SELECT event_type FROM xhs_account_events WHERE run_id=?",
            ("login-pending",),
        ).fetchone()["event_type"] == "login_lease_acquired"


def test_busy_login_returns_blocked_without_changing_account_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "pool.sqlite"
    with open_db(tmp_path) as conn:
        accounts.enroll_account(conn, "xhs-a01")
        accounts.mark_account_verified(conn, "xhs-a01", "identity-1")
        accounts.acquire_account_lease(
            conn,
            run_id="active-crawl",
            pool_config=pool_config(lease_seconds=3600),
            requested_account_id="xhs-a01",
            now=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(
        xhs_login,
        "parse_args",
        lambda: argparse.Namespace(
            account_id="xhs-a01",
            db=str(db_path),
            timeout_seconds=600,
            browser_path=None,
        ),
    )
    monkeypatch.setattr(xhs_login, "XHS_LOGIN_OUTPUT", tmp_path / "login-output")

    assert xhs_login.main() == 2
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        assert accounts.get_account(conn, "xhs-a01")["status"] == "active"


def test_xhs_dry_run_account_preflight_rejects_active_lease(tmp_path: Path) -> None:
    with open_db(tmp_path) as conn:
        accounts.enroll_account(conn, "xhs-a01")
        accounts.mark_account_verified(conn, "xhs-a01", "identity-1")
        accounts.acquire_account_lease(
            conn,
            run_id="active-run",
            pool_config=pool_config(lease_seconds=3600),
            requested_account_id="xhs-a01",
            now=datetime.now(timezone.utc),
        )

        with pytest.raises(XhsAccountUnavailable, match="busy"):
            xhs_runner._eligible_account_for_plan(conn, "xhs-a01")


def test_config_and_child_command_freeze_account_paths(tmp_path: Path) -> None:
    target_path = tmp_path / "targets.json"
    pool_path = tmp_path / "pool.json"
    target_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "targets": [
                    {
                        "target_key": "test",
                        "keyword": "青岛旅游",
                        "target_new_posts": 5,
                        "candidate_hard_limit": 50,
                        "max_stagnant_batches": 3,
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
        completion_mode="source-exhausted",
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
    assert command[command.index("--completion-mode") + 1] == "source-exhausted"
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
                "schema_version": 2,
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
                "schema_version": 2,
                "targets": [
                    {
                        "target_key": "test",
                        "keyword": "青岛旅游",
                        "target_new_posts": 1,
                        "candidate_hard_limit": 1,
                        "max_stagnant_batches": 1,
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
    monkeypatch.setattr(sys, "argv", ["mediacrawler_crawl.py", "--candidate-hard-limit", "1", "--no-import"])
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
            "--candidate-hard-limit",
            "1",
            "--no-import",
        ],
    )
    monkeypatch.setattr(mediacrawler_crawl, "ensure_prerequisites", lambda: None)

    with pytest.raises(SystemExit, match="xhs-account-id"):
        mediacrawler_crawl.main()
