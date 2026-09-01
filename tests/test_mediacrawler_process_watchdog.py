"""TripPostCollect MediaCrawler process watchdog tests."""

from __future__ import annotations

import json
import sys
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

mediacrawler_crawl = import_module("mediacrawler_crawl")
FrozenExecutionState = import_module("execution_state").FrozenExecutionState


def test_durable_progress_allows_runtime_longer_than_watchdog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(mediacrawler_crawl, "browser_launch_environment", lambda: {})
    progress_path = tmp_path / "progress.json"
    child = (
        "from pathlib import Path; import sys, time; "
        "path = Path(sys.argv[1]); "
        "[(path.write_text(str(index)), time.sleep(0.1)) for index in range(7)]"
    )

    result = mediacrawler_crawl.run_command(
        [sys.executable, "-c", child, str(progress_path)],
        tmp_path,
        0.3,
        tmp_path / "logs",
        progress_paths=[progress_path],
        poll_seconds=0.02,
        cleanup_grace_seconds=1.0,
    )

    assert result["returncode"] == 0
    assert result["timed_out"] is False
    assert result["progress_observed"] is True
    assert result["elapsed_seconds"] > result["inactivity_timeout_seconds"]


def test_no_progress_timeout_preserves_output_once_and_allows_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(mediacrawler_crawl, "browser_launch_environment", lambda: {})
    child = (
        "import signal, sys, time; "
        "signal.signal(signal.SIGTERM, lambda *_: (print('terminated', flush=True), sys.exit(0))); "
        "print('once', flush=True); time.sleep(10)"
    )

    result = mediacrawler_crawl.run_command(
        [sys.executable, "-c", child],
        tmp_path,
        0.15,
        tmp_path / "logs",
        progress_paths=[],
        poll_seconds=0.02,
        cleanup_grace_seconds=1.0,
    )

    stdout = (tmp_path / "logs" / "stdout.log").read_text(encoding="utf-8")
    assert result["returncode"] == 124
    assert result["timed_out"] is True
    assert result["timeout_reason"] == "no_progress_timeout"
    assert result["forced_termination"] is False
    assert stdout.count("once") == 1
    assert stdout.count("terminated") == 1


def test_timeout_with_staged_records_remains_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", raising=False)
    monkeypatch.setattr(
        mediacrawler_crawl,
        "behavior_environment",
        lambda *_: {},
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "load_behavior_evidence",
        lambda *_: {"status": "completed"},
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "behavior_evidence_valid",
        lambda *_: True,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "summarize_output",
        lambda *_: {
            "parse_errors": 0,
            "content_records": 5,
            "non_video_content_records": 5,
            "video_like_records": 0,
        },
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "run_command",
        lambda *_args, **_kwargs: {
            "returncode": 124,
            "timed_out": True,
            "timeout_reason": "no_progress_timeout",
            "last_progress_age_seconds": 1200,
        },
    )
    args = SimpleNamespace(
        download_images=False,
        login_type="cookie",
        keyword="青岛太平角旅游",
        headed=False,
        start_page=42,
        start_offset=None,
        start_cursor="",
        db=str(tmp_path / "content.sqlite"),
        behavior_profile="social_high_risk",
        discovery_job_id=None,
        resume_identities_path=None,
        timeout_per_platform=1200,
    )

    result = mediacrawler_crawl._run_platform_without_policy(
        "weibo",
        args,
        tmp_path,
    )

    assert result["status"] == "runtime_failed"
    assert result["ok"] is False


def test_no_progress_timeout_appends_incomplete_terminal_audit_event(
    tmp_path: Path,
) -> None:
    frozen_input = tmp_path / "config.json"
    frozen_input.write_text("{}\n", encoding="utf-8")
    state_path = tmp_path / "state.json"
    state = FrozenExecutionState.create(
        state_path,
        run_id="run-1",
        job_key="job-1",
        site_key="weibo",
        job_kind="mediacrawler_search",
        plan={"keyword": "青岛太平角旅游"},
        frozen_inputs=[frozen_input],
    )
    state.append_event(
        "adaptive_batch_completed",
        {
            "platform": "weibo",
            "candidate_count": 150,
            "source_page": 41,
            "resume_page": 42,
            "source_has_more": True,
            "batch_complete": True,
            "stop_reason": "continue",
            "candidate_identities": ["post-1"],
        },
    )

    result = mediacrawler_crawl.append_no_progress_timeout_event(
        state_path,
        platform_key="weibo",
        start_page=18,
        start_offset=None,
        start_cursor=None,
        inactivity_timeout_seconds=7200,
        last_progress_age_seconds=7200.25,
    )

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    terminal = payload["events"][-1]
    assert result["skipped"] is False
    assert terminal["type"] == "adaptive_search_stopped"
    assert terminal["details"]["stop_reason"] == "runtime_failed"
    assert terminal["details"]["stop_detail"] == "no_progress_timeout"
    assert terminal["details"]["resume_page"] == 42
    assert terminal["details"]["batch_complete"] is False
    assert terminal["details"]["candidate_identities"] == ["post-1"]


def test_watchdog_does_not_replace_existing_terminal_event(tmp_path: Path) -> None:
    frozen_input = tmp_path / "config.json"
    frozen_input.write_text("{}\n", encoding="utf-8")
    state_path = tmp_path / "state.json"
    state = FrozenExecutionState.create(
        state_path,
        run_id="run-2",
        job_key="job-2",
        site_key="weibo",
        job_kind="mediacrawler_search",
        plan={"keyword": "青岛太平角旅游"},
        frozen_inputs=[frozen_input],
    )
    state.append_event(
        "adaptive_search_stopped",
        {
            "platform": "weibo",
            "stop_reason": "source_exhausted",
            "stop_detail": "empty_page",
            "source_page": 50,
            "resume_page": 50,
            "source_has_more": False,
            "batch_complete": True,
        },
    )

    result = mediacrawler_crawl.append_no_progress_timeout_event(
        state_path,
        platform_key="weibo",
        start_page=18,
        start_offset=None,
        start_cursor=None,
        inactivity_timeout_seconds=7200,
        last_progress_age_seconds=7200,
    )

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert result == {"skipped": True, "reason": "terminal_event_already_present"}
    assert len(payload["events"]) == 1
    assert payload["events"][0]["details"]["stop_reason"] == "source_exhausted"
