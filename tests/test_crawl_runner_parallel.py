"""Tests for the generic runner's platform-lane scheduler."""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.db.connection import connect_db


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

crawl_runner = import_module("crawl_runner")


def prepared_job(index: int, lane_key: str, job_key: str) -> crawl_runner.PreparedJob:
    row = {
        "id": index + 1,
        "job_key": job_key,
        "site_key": lane_key,
        "job_kind": "mediacrawler_search",
    }
    return crawl_runner.PreparedJob(
        selection_index=index,
        row=row,
        command=["child", job_key],
        discovery_plan=None,
        state_path=Path(f"{job_key}.json"),
        lane_key=lane_key,
    )


def test_parallel_platform_limit_is_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["crawl_runner.py"])

    args = crawl_runner.parse_args()

    assert args.max_parallel_platforms == 4


def test_different_platform_lanes_overlap_and_results_keep_selection_order() -> None:
    jobs = [
        prepared_job(0, "weibo", "weibo-job"),
        prepared_job(1, "douyin", "douyin-job"),
    ]
    both_started = threading.Barrier(2)

    def execute(job: crawl_runner.PreparedJob) -> dict[str, str]:
        both_started.wait(timeout=2)
        if job.lane_key == "weibo":
            time.sleep(0.05)
        return {"job_key": str(job.row["job_key"]), "status": "completed"}

    records = crawl_runner.execute_platform_lanes(
        jobs,
        max_parallel_platforms=2,
        execute_job=execute,
    )

    assert [record["job_key"] for record in records] == ["weibo-job", "douyin-job"]


def test_jobs_in_the_same_platform_lane_never_overlap() -> None:
    jobs = [
        prepared_job(0, "weibo", "weibo-first"),
        prepared_job(1, "weibo", "weibo-second"),
        prepared_job(2, "zhihu", "zhihu-job"),
    ]
    lock = threading.Lock()
    active_by_lane: dict[str, int] = {}
    peak_by_lane: dict[str, int] = {}

    def execute(job: crawl_runner.PreparedJob) -> dict[str, str]:
        with lock:
            active_by_lane[job.lane_key] = active_by_lane.get(job.lane_key, 0) + 1
            peak_by_lane[job.lane_key] = max(
                peak_by_lane.get(job.lane_key, 0),
                active_by_lane[job.lane_key],
            )
        time.sleep(0.03)
        with lock:
            active_by_lane[job.lane_key] -= 1
        return {"job_key": str(job.row["job_key"]), "status": "completed"}

    crawl_runner.execute_platform_lanes(
        jobs,
        max_parallel_platforms=3,
        execute_job=execute,
    )

    assert peak_by_lane["weibo"] == 1


def test_atomic_job_lease_rejects_a_second_runner(tmp_path: Path) -> None:
    db_path = tmp_path / "lease.sqlite"
    with connect_db(db_path) as conn:
        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        conn.execute(
            """
            INSERT INTO crawl_jobs (
                job_key, site_key, target_url, job_kind, next_run_at, params_json
            ) VALUES ('lease-test', 'weibo', '', 'mediacrawler_search', datetime('now'), '{}')
            """
        )
        conn.commit()
        row = dict(conn.execute("SELECT * FROM crawl_jobs WHERE job_key='lease-test'").fetchone())
        first_attempt = crawl_runner.insert_attempt(conn, row, "run-first", ["child"])

    with connect_db(db_path) as competing_conn:
        with pytest.raises(crawl_runner.JobLeaseConflict):
            crawl_runner.insert_attempt(competing_conn, row, "run-second", ["child"])

    with sqlite3.connect(db_path) as check_conn:
        attempt_count = check_conn.execute(
            "SELECT COUNT(*) FROM crawl_attempts WHERE job_id=?",
            (row["id"],),
        ).fetchone()[0]

    assert first_attempt > 0
    assert attempt_count == 1


def test_freshly_loaded_leased_job_cannot_start_another_attempt(tmp_path: Path) -> None:
    with connect_db(tmp_path / "lease.sqlite") as conn:
        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        conn.execute(
            "INSERT INTO crawl_jobs(job_key,site_key,target_url,job_kind,next_run_at) "
            "VALUES ('job','weibo','','mediacrawler_search',datetime('now'))"
        )
        conn.commit()
        row = dict(conn.execute("SELECT * FROM crawl_jobs").fetchone())
        crawl_runner.insert_attempt(conn, row, "first", ["child"])
        leased = dict(conn.execute("SELECT * FROM crawl_jobs").fetchone())
        with pytest.raises(crawl_runner.JobLeaseConflict):
            crawl_runner.insert_attempt(conn, leased, "second", ["child"])
        assert conn.execute("SELECT count(*) FROM crawl_attempts").fetchone()[0] == 1


def test_internal_job_error_becomes_an_isolated_retry_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen_input = tmp_path / "contract.md"
    frozen_input.write_text("contract", encoding="utf-8")
    state_path = tmp_path / "weibo-state.json"
    crawl_runner.FrozenExecutionState.create(
        state_path,
        run_id="run-isolated",
        job_key="weibo-job",
        site_key="weibo",
        job_kind="mediacrawler_search",
        plan={"scheduling": {"lane_key": "weibo"}},
        frozen_inputs=[frozen_input],
    )
    job = prepared_job(0, "weibo", "weibo-job")
    job = crawl_runner.PreparedJob(
        selection_index=job.selection_index,
        row=job.row,
        command=job.command,
        discovery_plan=job.discovery_plan,
        state_path=state_path,
        lane_key=job.lane_key,
    )

    def fail_to_lease(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("injected scheduler failure")

    monkeypatch.setattr(crawl_runner, "insert_attempt", fail_to_lease)

    record = crawl_runner.execute_prepared_job(
        job,
        args=SimpleNamespace(no_import=False),
        db_path=tmp_path / "runner.sqlite",
        config={"defaults": {"schedule_jitter_ratio": 0}},
        run_id="run-isolated",
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert record["status"] == "retry_wait"
    assert record["failure_type"] == "scheduler_internal_error"
    assert state["steps"]["command_executed"]["status"] == "failed"


def test_dry_run_freezes_and_reports_parallel_platform_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "dry-run.sqlite"
    run_root = tmp_path / "runs"
    state_root = tmp_path / "states"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "crawl_runner.py",
            "--db",
            str(db_path),
            "--config",
            str(ROOT / "config" / "crawl_targets.json"),
            "--run-root",
            str(run_root),
            "--execution-state-root",
            str(state_root),
            "--max-jobs",
            "4",
            "--dry-run",
        ],
    )

    assert crawl_runner.main() == 0
    summary_path = next(run_root.rglob("run_summary.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    states = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in state_root.rglob("*.json")
    ]
    with sqlite3.connect(db_path) as conn:
        attempt_count = conn.execute("SELECT COUNT(*) FROM crawl_attempts").fetchone()[0]

    assert summary["scheduling"] == {
        "mode": "parallel_platform_lanes",
        "max_parallel_platforms": 4,
        "platform_lane_count": 4,
        "planned_workers": 4,
        "effective_workers": 0,
        "parallel_execution": False,
        "execution_started": False,
        "lane_keys": ["weibo", "douyin", "bilibili", "zhihu"],
    }
    assert all(
        state["plan"]["scheduling"]["mode"] == "parallel_platform_lanes"
        for state in states
    )
    assert attempt_count == 0
