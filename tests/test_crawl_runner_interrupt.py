"""通用 runner 的操作人中断：真实 runner → 中间层 → 独立 worker 进程组贯通。

只使用临时 SQLite、临时配置与本地替身进程，不访问网络、不启动浏览器。替身覆盖范围见
``tests/support/runner_interrupt_fakes.py``：批次事件、checkpoint/seen 提交与 campaign 更新走生产代码，
平台抓取、正文导入与图片物化不在本文件证明范围内。
"""

from __future__ import annotations

import ast
import copy
from importlib import import_module
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace
import time

import pytest

from support.signal_driver import isolated_signal_test
from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.db.connection import connect_db
from trippostcollect.runtime.process import process_group_exists
from trippostcollect.scheduler.discovery import (
    query_fingerprint,
    save_checkpoint,
    save_seen_candidates,
    update_campaign,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
FAKES = ROOT / "tests" / "support" / "runner_interrupt_fakes.py"
WEIBO = "mc_weibo_qingdao_laoshan_guide_search"
DOUYIN = "mc_douyin_qingdao_laoshan_guide_search"
WEIBO_QUEUED = "mc_weibo_qingdao_laoshan_guide_search_queued"
HANDSHAKE_SECONDS = 90.0
CONFIRMED = ["confirmed-1", "confirmed-2"]
TAIL = "tail-1"

crawl_runner = import_module("crawl_runner")


def _config(keys: list[str]) -> dict:
    source = json.loads((ROOT / "config" / "crawl_targets.json").read_text(encoding="utf-8"))
    by_key = {job["job_key"]: job for job in source["jobs"]}
    jobs = []
    for key in keys:
        if key == WEIBO_QUEUED:
            job = copy.deepcopy(by_key[WEIBO])
            job["job_key"] = WEIBO_QUEUED
            job["priority"] = int(job["priority"]) + 1
        else:
            job = copy.deepcopy(by_key[key])
        jobs.append(job)
    return {**source, "jobs": jobs}


def wait_until(predicate, description: str, *, seconds: float = HANDSHAKE_SECONDS) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out waiting for {description}")
        time.sleep(0.02)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def assert_gone(pid: int) -> None:
    # 被强杀进程的僵尸由其父或 init 回收；只等待回收，不放宽“不得残留”。
    wait_until(lambda: not pid_alive(pid), f"pid {pid} reaped", seconds=15)


class Run:
    def __init__(self, tmp_path: Path, modes: dict[str, str], *, grace_seconds: float | None = None):
        self.tmp = tmp_path
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.db = tmp_path / "runner.sqlite"
        self.config = tmp_path / "crawl_targets.json"
        self.run_root = tmp_path / "runs"
        self.state_root = tmp_path / "states"
        self.modes = modes
        config = _config(list(modes))
        self.config.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        with connect_db(self.db) as conn:
            bootstrap_connection(conn, config=config, sync_jobs=True)
            for job in config["jobs"]:
                job_id = int(
                    conn.execute(
                        "SELECT id FROM crawl_jobs WHERE job_key=?", (job["job_key"],)
                    ).fetchone()[0]
                )
                params = job["params"]
                fingerprint = query_fingerprint(params["platform"], params["keyword"], params)
                # 历史已确认前沿、seen 与 campaign：中断不得改动。
                save_checkpoint(
                    conn,
                    job_id=job_id,
                    platform_key=params["platform"],
                    keyword=params["keyword"],
                    query_fingerprint_value=fingerprint,
                    resume_page=3,
                    resume_offset=None,
                    resume_cursor=None,
                    source_has_more=True,
                    last_batch_complete=True,
                    last_stop_reason="runtime_failed",
                    last_run_id="confirmed-run",
                )
                save_seen_candidates(
                    conn,
                    job_id=job_id,
                    platform_key=params["platform"],
                    query_fingerprint_value=fingerprint,
                    platform_post_ids=["old-1"],
                    run_id="confirmed-run",
                )
                update_campaign(
                    conn,
                    job_id=job_id,
                    query_fingerprint_value=fingerprint,
                    summary_path=None,
                    campaign_candidate_count=7,
                )
            conn.commit()
        self.plan = tmp_path / "plan.json"
        self.plan.write_text(
            json.dumps({"work": str(self.work), "modes": modes, "grace_seconds": grace_seconds}),
            encoding="utf-8",
        )
        self.before = self.snapshot()
        self.process: subprocess.Popen[bytes] | None = None

    def snapshot(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        with sqlite3.connect(self.db) as conn:
            for job_key in self.modes:
                row = conn.execute(
                    "SELECT c.resume_page, c.resume_offset, c.resume_cursor, c.status, "
                    "c.last_batch_complete, c.last_stop_reason, c.last_run_id, "
                    "c.last_summary_path, c.campaign_candidate_count, c.updated_at, c.job_id "
                    "FROM crawl_discovery_checkpoints c JOIN crawl_jobs j ON j.id=c.job_id "
                    "WHERE j.job_key=?",
                    (job_key,),
                ).fetchone()
                seen = sorted(
                    value
                    for (value,) in conn.execute(
                        "SELECT platform_post_id FROM crawl_discovery_seen_candidates WHERE job_id=?",
                        (row[-1],),
                    )
                )
                result[job_key] = {
                    "checkpoint": row[:-1],
                    "resume_page": row[0],
                    "last_run_id": row[6],
                    "last_summary_path": row[7],
                    "campaign_candidate_count": row[8],
                    "seen": seen,
                }
        return result

    def start(self, *, max_parallel: int, extra_args: tuple[str, ...] = ()) -> None:
        output = (self.tmp / "runner.out").open("wb")
        self.process = subprocess.Popen(
            [
                sys.executable, str(FAKES), "runner", str(self.plan), "--",
                "--db", str(self.db),
                "--config", str(self.config),
                "--run-root", str(self.run_root),
                "--execution-state-root", str(self.state_root),
                "--max-jobs", "5",
                "--max-parallel-platforms", str(max_parallel),
                "--no-sync-config",
                *extra_args,
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        output.close()

    def output(self) -> str:
        return (self.tmp / "runner.out").read_text(encoding="utf-8", errors="replace")[-4000:]

    def wait_for(self, predicate, description: str) -> None:
        def ready() -> bool:
            assert self.process is not None
            if predicate():
                return True
            if self.process.poll() is not None:
                pytest.fail(f"runner exited before {description}: {self.output()}")
            return False

        wait_until(ready, description)

    def ready(self, job_key: str) -> None:
        self.wait_for(lambda: (self.work / job_key / "ready").is_file(), f"{job_key} ready")

    def attempt_finished(self, job_key: str) -> None:
        def finished() -> bool:
            with sqlite3.connect(self.db) as conn:
                row = conn.execute(
                    "SELECT a.status FROM crawl_attempts a JOIN crawl_jobs j ON j.id=a.job_id "
                    "WHERE j.job_key=?",
                    (job_key,),
                ).fetchone()
            return bool(row and row[0] != "running")

        self.wait_for(finished, f"{job_key} attempt finalized")

    def interrupt(self, signum: int, *, terminal_group: bool) -> None:
        assert self.process is not None
        if terminal_group:
            # 等价于终端 Ctrl+C：发给 runner 所在前台进程组。
            os.killpg(self.process.pid, signum)
        else:
            os.kill(self.process.pid, signum)

    def finish(self) -> int:
        assert self.process is not None
        return self.process.wait(timeout=120)

    def summary(self) -> dict:
        return json.loads(next(self.run_root.rglob("run_summary.json")).read_text(encoding="utf-8"))

    def report(self) -> str:
        return next(self.run_root.rglob("run_summary.md")).read_text(encoding="utf-8")

    def state(self, job_key: str) -> dict:
        return json.loads(next(self.state_root.rglob(f"{job_key}.json")).read_text(encoding="utf-8"))

    def identity(self, job_key: str, name: str) -> dict:
        return json.loads((self.work / job_key / f"{name}.json").read_text(encoding="utf-8"))

    def job_row(self, job_key: str) -> dict:
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return dict(conn.execute("SELECT * FROM crawl_jobs WHERE job_key=?", (job_key,)).fetchone())

    def attempts(self, job_key: str) -> list[dict]:
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT a.* FROM crawl_attempts a JOIN crawl_jobs j ON j.id=a.job_id WHERE j.job_key=?",
                    (job_key,),
                )
            ]


def assert_interrupted_state(state: dict, signame: str, *, dispatched: bool) -> dict:
    error = f"runtime_failed:operator_interrupt:{signame}"
    command = state["steps"]["command_executed"]
    assert state["status"] == "failed"
    assert command["status"] == "failed"
    assert command["error"] == error
    assert command["evidence"]["dispatched"] is dispatched
    assert command["evidence"]["interrupt"]["signal"] == signame
    for step in ("artifacts_verified", "persistence_verified"):
        assert state["steps"][step]["status"] == "frozen"
    assert state["steps"]["task_finalized"]["status"] == "failed"
    assert state["steps"]["task_finalized"]["error"] == error
    stops = [event for event in state["events"] if event["type"] == "adaptive_search_stopped"]
    assert stops == []
    return command["evidence"]


def assert_confirmed_batch_only(state: dict) -> None:
    batches = [event["details"] for event in state["events"] if event["type"] == "adaptive_batch_completed"]
    assert len(batches) == 1
    assert batches[0]["batch_complete"] is True
    assert batches[0]["candidate_identities"] == CONFIRMED
    assert TAIL not in json.dumps(state["events"])


def assert_once(text: str, needle: str) -> None:
    assert text.count(needle) == 1, (needle, text[-2000:])


def assert_worker_stopped_once(run: Run, job_key: str) -> None:
    worker = run.identity(job_key, "worker")
    for pid in (worker["pid"], worker["grandchild_pid"]):
        assert_gone(pid)
    assert not process_group_exists(worker["pgid"])
    job_dir = run.work / job_key
    assert (job_dir / "worker-signals").read_text(encoding="utf-8") == "signal\n"
    assert (job_dir / "cleanup-done").is_file()


@pytest.mark.macos_process
@pytest.mark.parametrize(
    ("signum", "terminal_group"),
    [(signal.SIGINT, True), (signal.SIGTERM, False), (signal.SIGHUP, False)],
    ids=["sigint-terminal-group", "sigterm-runner", "sighup-runner"],
)
def test_operator_signal_stops_runner_middle_and_worker_group_once(
    tmp_path: Path,
    signum: int,
    terminal_group: bool,
) -> None:
    signame = signal.Signals(signum).name
    run = Run(tmp_path, {WEIBO: "hang"})
    run.start(max_parallel=1)
    run.ready(WEIBO)
    runner = json.loads((run.work / "runner.json").read_text(encoding="utf-8"))
    middle = run.identity(WEIBO, "middle")
    worker = run.identity(WEIBO, "worker")
    # 中间层与 worker 各自独立会话：终端信号只到 runner。
    assert len({runner["pgid"], middle["pgid"], worker["pgid"]}) == 3

    run.interrupt(signum, terminal_group=terminal_group)
    exit_code = run.finish()

    assert exit_code == 128 + signum, run.output()
    summary = run.summary()
    assert summary["interrupt"]["signal"] == signame
    assert summary["interrupt"]["error"] == f"runtime_failed:operator_interrupt:{signame}"
    assert summary["interrupted_count"] == 1
    assert f"操作人中断：`{signame}`" in run.report()
    [record] = summary["records"]
    assert record["status"] == "retry_wait"
    assert record["failure_type"] == "runtime_failed"
    assert record["interrupt"]["signal"] == signame
    state = run.state(WEIBO)
    evidence = assert_interrupted_state(state, signame, dispatched=True)
    assert evidence["forced_termination"] is False
    assert evidence["killed_child_process_groups"] == []
    # 中间层只收到 runner 的一次 SIGTERM，并经可捕获路径以 143 退出。
    assert evidence["exit_code"] == 128 + signal.SIGTERM
    # 已确认批次事件保留在 execution state；未确认尾批没有事件。
    assert_confirmed_batch_only(state)

    # 进程树全部收束；worker 只收到一次温和信号且清理完成，未走二次信号的 os._exit(130)。
    assert_gone(middle["pid"])
    assert not process_group_exists(middle["pgid"])
    assert_worker_stopped_once(run, WEIBO)

    # 两层日志均落盘：中断前输出与清理尾部输出各出现一次，stdout/stderr 分别断言。
    runner_logs = record["child_logs"]
    middle_stdout = Path(runner_logs["stdout_log"]).read_text(encoding="utf-8")
    middle_stderr = Path(runner_logs["stderr_log"]).read_text(encoding="utf-8")
    assert_once(middle_stdout, f"{WEIBO}:middle-partial-out")
    assert f"{WEIBO}:middle-partial-err" not in middle_stdout
    assert_once(middle_stderr, f"{WEIBO}:middle-partial-err")
    assert_once(middle_stderr, "runtime_failed:operator_interrupt:SIGTERM")
    job_dir = run.work / WEIBO
    worker_stdout = (job_dir / "worker-logs" / "stdout.log").read_text(encoding="utf-8")
    worker_stderr = (job_dir / "worker-logs" / "stderr.log").read_text(encoding="utf-8")
    assert_once(worker_stdout, f"{WEIBO}:worker-partial-out")
    assert_once(worker_stdout, f"{WEIBO}:worker-cleanup-out")
    assert_once(worker_stdout, "Received interrupt signal 15")
    assert "again" not in worker_stdout
    assert_once(worker_stderr, f"{WEIBO}:worker-partial-err")
    assert_once(worker_stderr, f"{WEIBO}:worker-cleanup-err")
    assert f"{WEIBO}:worker-cleanup-err" not in worker_stdout

    # 临时 SQLite：checkpoint/seen/campaign 未推进，租约释放且中断不计入失败次数。
    assert run.snapshot() == run.before
    job = run.job_row(WEIBO)
    assert job["status"] == "retry_wait"
    assert job["consecutive_failures"] == 0
    assert job["last_failure_type"] == "runtime_failed"
    [attempt] = run.attempts(WEIBO)
    assert attempt["status"] == "retry_wait"
    assert json.loads(attempt["classification_json"])["reason"] == "operator_interrupt"
    with sqlite3.connect(run.db) as conn:
        report_status = conn.execute("SELECT status FROM crawl_run_reports").fetchone()[0]
    assert report_status == "failed"


@pytest.mark.macos_process
def test_uninterrupted_chain_commits_confirmed_batch_and_campaign(tmp_path: Path) -> None:
    """对照：同一替身链不被中断时，生产提交路径会推进前沿、seen 与 campaign。"""

    run = Run(tmp_path, {WEIBO: "complete"})
    run.start(max_parallel=1)
    exit_code = run.finish()

    assert exit_code == 1, run.output()
    summary = run.summary()
    assert summary["interrupt"] is None
    assert summary["records"][0]["interrupt"] is None
    after = run.snapshot()[WEIBO]
    assert after["resume_page"] == 4
    assert after["last_run_id"] == summary["run_id"]
    assert after["seen"] == sorted(["old-1", *CONFIRMED])
    assert after["last_summary_path"] == str((run.work / WEIBO / "summary.json").resolve())
    assert after["campaign_candidate_count"] == len(CONFIRMED)


@pytest.mark.macos_process
def test_signal_after_middle_commit_keeps_committed_frontier_without_campaign(
    tmp_path: Path,
) -> None:
    """中间层已提交 checkpoint/seen 后才收到信号：提交保留，runner 不更新 campaign。"""

    run = Run(tmp_path, {WEIBO: "persist-hang"})
    run.start(max_parallel=1)
    run.wait_for(lambda: (run.work / WEIBO / "persisted.json").is_file(), "checkpoint committed")

    run.interrupt(signal.SIGTERM, terminal_group=False)
    exit_code = run.finish()

    assert exit_code == 128 + signal.SIGTERM, run.output()
    summary = run.summary()
    after = run.snapshot()[WEIBO]
    before = run.before[WEIBO]
    assert after["resume_page"] == 4
    assert after["last_run_id"] == summary["run_id"]
    assert after["seen"] == sorted(["old-1", *CONFIRMED])
    assert TAIL not in after["seen"]
    assert after["last_summary_path"] == before["last_summary_path"]
    assert after["campaign_candidate_count"] == before["campaign_candidate_count"]
    assert_interrupted_state(run.state(WEIBO), "SIGTERM", dispatched=True)


@pytest.mark.macos_process
def test_both_active_parallel_lanes_are_stopped(tmp_path: Path) -> None:
    run = Run(tmp_path, {WEIBO: "hang", DOUYIN: "hang"})
    run.start(max_parallel=2)
    run.ready(WEIBO)
    run.ready(DOUYIN)

    run.interrupt(signal.SIGTERM, terminal_group=False)
    exit_code = run.finish()

    assert exit_code == 128 + signal.SIGTERM, run.output()
    summary = run.summary()
    assert summary["scheduling"]["effective_workers"] == 2
    assert summary["interrupted_count"] == 2
    for job_key in (WEIBO, DOUYIN):
        assert_interrupted_state(run.state(job_key), "SIGTERM", dispatched=True)
        assert_worker_stopped_once(run, job_key)
        assert run.job_row(job_key)["status"] == "retry_wait"
    assert run.snapshot() == run.before


@pytest.mark.macos_process
def test_finished_success_lane_is_kept_and_queued_job_is_not_dispatched(tmp_path: Path) -> None:
    run = Run(tmp_path, {WEIBO: "hang", WEIBO_QUEUED: "hang", DOUYIN: "success"})
    run.start(max_parallel=2, extra_args=("--no-import",))
    run.ready(WEIBO)
    run.attempt_finished(DOUYIN)

    run.interrupt(signal.SIGINT, terminal_group=True)
    exit_code = run.finish()

    assert exit_code == 128 + signal.SIGINT, run.output()
    summary = run.summary()
    records = {record["job_key"]: record for record in summary["records"]}
    assert [record["job_key"] for record in summary["records"]] == [WEIBO, WEIBO_QUEUED, DOUYIN]
    assert summary["interrupted_count"] == 2
    assert summary["completed_count"] == 1
    # 信号前已成功完成的平台原样保留为成功。
    assert records[DOUYIN]["status"] == "completed"
    assert records[DOUYIN]["interrupt"] is None
    douyin_state = run.state(DOUYIN)
    assert douyin_state["status"] == "completed"
    assert douyin_state["steps"]["task_finalized"]["status"] == "completed"
    assert run.job_row(DOUYIN)["status"] == "completed"
    # 运行中的通道被收束，排队 job 不再派发：无租约、无 child、状态写成中断终态。
    assert_interrupted_state(run.state(WEIBO), "SIGINT", dispatched=True)
    assert_interrupted_state(run.state(WEIBO_QUEUED), "SIGINT", dispatched=False)
    assert run.attempts(WEIBO_QUEUED) == []
    assert run.job_row(WEIBO_QUEUED)["status"] == "pending"
    assert records[WEIBO_QUEUED]["status"] == "retry_wait"
    assert not (run.work / WEIBO_QUEUED).exists()
    assert_worker_stopped_once(run, WEIBO)


@pytest.mark.macos_process
def test_forced_kill_fallback_stops_middle_and_recorded_worker_group(tmp_path: Path) -> None:
    run = Run(tmp_path, {WEIBO: "stubborn"}, grace_seconds=1.0)
    run.start(max_parallel=1)
    run.ready(WEIBO)
    middle = run.identity(WEIBO, "middle")
    worker = run.identity(WEIBO, "worker")

    run.interrupt(signal.SIGINT, terminal_group=True)
    run.interrupt(signal.SIGTERM, terminal_group=False)
    exit_code = run.finish()

    # 重复信号与强杀兜底都不改变首因。
    assert exit_code == 128 + signal.SIGINT, run.output()
    summary = run.summary()
    assert summary["interrupt"]["signal"] == "SIGINT"
    evidence = assert_interrupted_state(run.state(WEIBO), "SIGINT", dispatched=True)
    assert evidence["forced_termination"] is True
    assert evidence["exit_code"] == -signal.SIGKILL
    [killed] = evidence["killed_child_process_groups"]
    assert killed["pgid"] == worker["pgid"]
    assert killed["matched"] is True and killed["killed"] is True
    assert killed["group_exists"] is False
    for pid in (middle["pid"], worker["pid"], worker["grandchild_pid"]):
        assert_gone(pid)
    assert not process_group_exists(middle["pgid"])
    assert not process_group_exists(worker["pgid"])
    stdout = Path(summary["records"][0]["child_logs"]["stdout_log"]).read_text(encoding="utf-8")
    assert_once(stdout, f"{WEIBO}:middle-partial-out")
    assert run.snapshot() == run.before


@pytest.mark.macos_process
def test_child_exit_observed_after_latch_is_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    started = tmp_path / "started"
    release = tmp_path / "release"
    child = (
        "import pathlib, time\n"
        f"pathlib.Path({str(started)!r}).write_text('started')\n"
        f"while not pathlib.Path({str(release)!r}).exists():\n"
        "    time.sleep(0.01)\n"
        "print('child-done', flush=True)\n"
    )
    latched: list[int] = []

    def latch_then_release() -> None:
        wait_until(started.is_file, "child started")
        latched.append(int(signal.SIGINT))
        release.write_text("release", encoding="utf-8")

    # 轮询间隔远大于 child 寿命：退出只能在正常 communicate 完成边界被观察到。
    monkeypatch.setattr(crawl_runner, "RUNNER_CHILD_POLL_SECONDS", 60.0)
    helper = threading.Thread(target=latch_then_release)
    helper.start()
    result = crawl_runner.run_child_command(
        [sys.executable, "-c", child],
        env=dict(os.environ),
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.json",
        interrupt_signal=lambda: latched[0] if latched else None,
    )
    helper.join(timeout=30)

    assert result.interrupt_signum == signal.SIGINT
    assert result.launched is True
    assert result.forced_termination is False
    assert result.completed.returncode == 0
    assert (tmp_path / "logs" / "stdout.log").read_text(encoding="utf-8") == "child-done\n"


def test_latched_signal_before_launch_skips_child(tmp_path: Path) -> None:
    marker = tmp_path / "launched"
    result = crawl_runner.run_child_command(
        [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
        env=dict(os.environ),
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.json",
        interrupt_signal=lambda: int(signal.SIGTERM),
    )

    assert result.interrupt_signum == signal.SIGTERM
    assert result.launched is False
    assert result.completed.returncode is None
    assert not marker.exists()
    assert (tmp_path / "logs" / "stdout.log").read_text(encoding="utf-8") == ""


CHILD_WITH_CLEANUP = r"""
import pathlib, signal, sys, time

ready = pathlib.Path(sys.argv[1])
avatar = sys.argv[2] == "avatar"

def cleanup(_signum, _frame):
    print("child-cleanup-out", flush=True)
    if avatar:
        print("author_avatar=https://img.example.invalid/a.jpg", file=sys.stderr, flush=True)
    else:
        print("child-cleanup-err", file=sys.stderr, flush=True)
    sys.exit(0)

signal.signal(signal.SIGTERM, cleanup)
print("https://img.example.invalid/a.jpg" if avatar else "child-partial-out", flush=True)
print("child-partial-err", file=sys.stderr, flush=True)
ready.write_text("ready")
while True:
    time.sleep(60)
"""


@pytest.mark.macos_process
@pytest.mark.parametrize("variant", ["plain", "avatar"])
def test_run_command_interrupt_writes_cumulative_cleaned_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    variant: str,
) -> None:
    """中断前已被 communicate 读走的输出与收束尾部输出合并写盘，各一次。"""

    from trippostcollect.records.sanitization import AUTHOR_AVATAR_LOG_REDACTION
    from trippostcollect.runtime import process

    ready = tmp_path / "ready"
    progress = tmp_path / "progress"
    progress.mkdir()
    original_signature = process.progress_path_signature
    observations = {"ready": 0}

    def interrupt_after_a_full_read(*args: object, **kwargs: object) -> object:
        # 第二次看到 ready 时，至少已有一轮 communicate 在 ready 写出后运行，读走了先写出的输出。
        if ready.is_file():
            observations["ready"] += 1
            if observations["ready"] >= 2:
                raise KeyboardInterrupt
        return original_signature(*args, **kwargs)

    monkeypatch.setattr(process, "browser_launch_environment", lambda: dict(os.environ))
    monkeypatch.setattr(process, "progress_path_signature", interrupt_after_a_full_read)
    log_dir = tmp_path / "logs"
    with pytest.raises(KeyboardInterrupt):
        process.run_command(
            [sys.executable, "-c", CHILD_WITH_CLEANUP, str(ready), variant],
            tmp_path,
            3600,
            log_dir,
            progress_paths=[progress],
            poll_seconds=0.05,
            cleanup_grace_seconds=10,
        )

    stdout = (log_dir / "stdout.log").read_text(encoding="utf-8")
    stderr = (log_dir / "stderr.log").read_text(encoding="utf-8")
    assert (log_dir / "command.txt").is_file()
    if variant == "avatar":
        # 头像证据只在 stderr 尾部出现，stdout 里的同值 URL 也必须随两路整体清除。
        assert stdout == AUTHOR_AVATAR_LOG_REDACTION
        assert stderr == AUTHOR_AVATAR_LOG_REDACTION
        assert "img.example.invalid" not in stdout + stderr
    else:
        assert stdout == "child-partial-out\nchild-cleanup-out\n"
        assert stderr == "child-partial-err\nchild-cleanup-err\n"


@pytest.mark.macos_process
@isolated_signal_test
def test_sigterm_during_worker_popen_is_replayed_after_proc_is_owned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from trippostcollect.runtime import process

    spawned: dict[str, int] = {}
    original_popen = process.subprocess.Popen

    def popen_then_signal(*args: object, **kwargs: object) -> object:
        proc = original_popen(*args, **kwargs)
        spawned["pid"] = proc.pid
        # 落在 Popen 返回与 proc 赋值之间：必须推迟到 proc 归属后再重放。
        os.kill(os.getpid(), signal.SIGTERM)
        return proc

    monkeypatch.setattr(process, "browser_launch_environment", lambda: dict(os.environ))
    monkeypatch.setattr(process.subprocess, "Popen", popen_then_signal)
    log_dir = tmp_path / "logs"
    with process.sigterm_raises_operator_interrupt():
        with pytest.raises(process.OperatorInterrupt) as raised:
            process.run_command(
                [sys.executable, "-c", "import time\ntime.sleep(3600)"],
                tmp_path,
                3600,
                log_dir,
            )

    assert raised.value.signum == signal.SIGTERM
    assert_gone(spawned["pid"])
    assert not process_group_exists(spawned["pid"])
    assert (log_dir / "stdout.log").is_file()
    assert (log_dir / "stderr.log").is_file()


@pytest.mark.macos_process
@isolated_signal_test
def test_signals_during_finalize_keep_first_cause_and_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "crawl_targets.json"
    config.write_text(json.dumps(_config([WEIBO]), ensure_ascii=False), encoding="utf-8")
    original_planned = crawl_runner.planned_record
    original_finish = crawl_runner.finish_run_report
    printed: list[str] = []

    def planned_with_first_signal(job: object) -> dict:
        os.kill(os.getpid(), signal.SIGTERM)
        return original_planned(job)

    def finish_with_second_signal(*args: object, **kwargs: object) -> None:
        os.kill(os.getpid(), signal.SIGINT)
        original_finish(*args, **kwargs)

    def print_with_third_signal(*args: object, **_kwargs: object) -> None:
        os.kill(os.getpid(), signal.SIGHUP)
        printed.append(" ".join(map(str, args)))

    monkeypatch.setattr(crawl_runner, "planned_record", planned_with_first_signal)
    monkeypatch.setattr(crawl_runner, "finish_run_report", finish_with_second_signal)
    monkeypatch.setattr(crawl_runner, "print", print_with_third_signal, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "crawl_runner.py",
            "--db", str(tmp_path / "runner.sqlite"),
            "--config", str(config),
            "--run-root", str(tmp_path / "runs"),
            "--execution-state-root", str(tmp_path / "states"),
            "--dry-run",
        ],
    )
    handlers = {signum: signal.getsignal(signum) for signum in crawl_runner.RUNNER_INTERRUPT_SIGNALS}

    assert crawl_runner.main() == 128 + signal.SIGTERM
    summary = json.loads(next((tmp_path / "runs").rglob("run_summary.json")).read_text(encoding="utf-8"))
    assert summary["interrupt"]["signal"] == "SIGTERM"
    assert printed
    assert {signum: signal.getsignal(signum) for signum in handlers} == handlers


def test_sigterm_conversion_is_scoped_to_generic_middle_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trippostcollect.runtime import process
    from trippostcollect.xhs.leases import LEASE_DB_ENV, LEASE_ID_ENV, LEASE_OWNER_TOKEN_ENV

    original = signal.getsignal(signal.SIGTERM)
    for key in (LEASE_DB_ENV, LEASE_ID_ENV, LEASE_OWNER_TOKEN_ENV):
        monkeypatch.delenv(key, raising=False)
    with process.sigterm_raises_operator_interrupt():
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler) and handler is not original
        with pytest.raises(process.OperatorInterrupt) as raised:
            handler(signal.SIGTERM, None)
        assert raised.value.signum == signal.SIGTERM
        # 首个 SIGTERM 之后的重复信号不再打断收束。
        handler(signal.SIGTERM, None)
    assert signal.getsignal(signal.SIGTERM) is original
    assert process.operator_interrupt_error(signal.SIGTERM) == (
        "runtime_failed:operator_interrupt:SIGTERM"
    )

    # 小红书租约进程由 LeaseGuard 精确转发，保持原处理，避免对 exporter 发第二次温和信号。
    for key in (LEASE_DB_ENV, LEASE_ID_ENV, LEASE_OWNER_TOKEN_ENV):
        monkeypatch.setenv(key, "lease-value")
    with process.sigterm_raises_operator_interrupt():
        assert signal.getsignal(signal.SIGTERM) is original


def test_middle_layer_script_entry_runs_under_operator_interrupt_wrapper() -> None:
    tree = ast.parse((ROOT / "scripts" / "mediacrawler_crawl.py").read_text(encoding="utf-8"))
    entry = tree.body[-1]
    assert isinstance(entry, ast.If) and ast.unparse(entry.test) == "__name__ == '__main__'"
    assert [ast.unparse(statement) for statement in entry.body] == [
        "raise SystemExit(run_main_with_operator_interrupt(main))"
    ]
    imported = {
        alias.name: node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert imported["run_main_with_operator_interrupt"] == "trippostcollect.runtime.process"


def test_runner_cli_keeps_signal_latch_until_process_exit() -> None:
    tree = ast.parse((SCRIPTS / "crawl_runner.py").read_text(encoding="utf-8"))
    entry = tree.body[-1]
    assert [ast.unparse(statement) for statement in entry.body] == ["raise SystemExit(run_cli())"]
    assert crawl_runner.RUNNER_INTERRUPT_SIGNALS == (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@pytest.mark.macos_process
@pytest.mark.parametrize("shape", ["gather-return-exceptions", "background-task"])
@isolated_signal_test
def test_operator_interrupt_escapes_asyncio_tasks(tmp_path: Path, shape: str) -> None:
    """B站在中间层进程内跑 asyncio：信号落在子任务步进内也必须向外抛，不能被 Task 吞掉。"""

    import asyncio

    from trippostcollect.runtime import process

    async def busy_child() -> str:
        os.kill(os.getpid(), signal.SIGTERM)
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            pass
        return "child-finished"

    async def main() -> str:
        if shape == "gather-return-exceptions":
            await asyncio.gather(busy_child(), return_exceptions=True)
        else:
            asyncio.get_running_loop().create_task(busy_child())
        await asyncio.sleep(0.5)
        return "main-finished"

    assert issubclass(process.OperatorInterrupt, KeyboardInterrupt)
    with process.sigterm_raises_operator_interrupt():
        with pytest.raises(process.OperatorInterrupt):
            asyncio.run(main())


def _leased_job(tmp_path: Path) -> tuple[object, Path]:
    db_path = tmp_path / "runner.sqlite"
    config = _config([WEIBO])
    with connect_db(db_path) as conn:
        bootstrap_connection(conn, config=config, sync_jobs=True)
        row = dict(conn.execute("SELECT * FROM crawl_jobs WHERE job_key=?", (WEIBO,)).fetchone())
    contract = tmp_path / "contract.md"
    contract.write_text("contract", encoding="utf-8")
    state_path = tmp_path / "states" / f"{WEIBO}.json"
    crawl_runner.FrozenExecutionState.create(
        state_path,
        run_id="run-interrupt",
        job_key=WEIBO,
        site_key="weibo",
        job_kind="mediacrawler_search",
        plan={"scheduling": {"lane_key": "weibo"}},
        frozen_inputs=[contract],
    )
    job = crawl_runner.PreparedJob(
        selection_index=0,
        row=row,
        command=["child"],
        discovery_plan=None,
        state_path=state_path,
        lane_key="weibo",
    )
    return job, db_path


def test_error_after_interrupt_decision_keeps_operator_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job, db_path = _leased_job(tmp_path)

    def disk_full(*_args: object, **_kwargs: object) -> object:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(crawl_runner, "run_child_command", disk_full)
    record = crawl_runner.execute_prepared_job(
        job,
        args=SimpleNamespace(no_import=False),
        db_path=db_path,
        config={"defaults": {"schedule_jitter_ratio": 0}},
        run_id="run-interrupt",
        run_dir=tmp_path / "run",
        interrupt_signal=lambda: int(signal.SIGTERM),
    )

    assert record["status"] == "retry_wait"
    assert record["failure_type"] == "runtime_failed"
    assert record["interrupt"]["signal"] == "SIGTERM"
    assert "No space left on device" in record["reason"]
    state = json.loads(job.state_path.read_text(encoding="utf-8"))
    assert state["steps"]["command_executed"]["error"] == "runtime_failed:operator_interrupt:SIGTERM"
    assert state["steps"]["task_finalized"]["status"] == "failed"
    with sqlite3.connect(db_path) as conn:
        status, failures = conn.execute(
            "SELECT status, consecutive_failures FROM crawl_jobs WHERE job_key=?", (WEIBO,)
        ).fetchone()
    assert (status, failures) == ("retry_wait", 0)


def test_recorded_group_kill_permission_error_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from trippostcollect.runtime import process

    recorded = {
        "host_id": "host",
        "boot_id": "boot",
        "pid": 424242,
        "process_started_at": "2026-10-08T00:00:00+00:00",
        "process_start_token": "token",
        "pgid": 424242,
    }

    class Identity:
        def public(self) -> dict:
            return dict(recorded)

    class Inspector:
        def identity(self, _pid: int) -> Identity:
            return Identity()

    def killpg(_pgid: int, signum: int) -> None:
        if signum == 0:
            raise ProcessLookupError
        raise PermissionError(1, "Operation not permitted")

    state_path = tmp_path / "job.json"
    process.child_process_groups_path(state_path).write_text(json.dumps(recorded) + "\n", encoding="utf-8")
    monkeypatch.setattr(process, "SystemProcessInspector", Inspector)
    monkeypatch.setattr(process.os, "killpg", killpg)

    [item] = process.kill_recorded_child_process_groups(state_path, wait_seconds=0)

    assert item["matched"] is True
    assert item["killed"] is False
    assert item["error"].startswith("PermissionError")


GRANDCHILD_HOLDING_PIPES = r"""
import os, pathlib, sys, time
parent = int(sys.argv[2])
while os.getppid() == parent:
    time.sleep(0.01)
pathlib.Path(sys.argv[1]).write_text("parent-exited")
time.sleep(60)
"""

CHILD_LEAVING_GRANDCHILD = r"""
import os, pathlib, subprocess, sys
print("child-out", flush=True)
print("child-err", file=sys.stderr, flush=True)
grandchild = subprocess.Popen(
    [sys.executable, "-c", sys.argv[3], sys.argv[1], str(os.getpid())],
    start_new_session=True,
)
pathlib.Path(sys.argv[2]).write_text(str(grandchild.pid))
"""


@pytest.mark.macos_process
@pytest.mark.parametrize("layer", ["run_command", "runner"])
def test_exception_after_child_exit_keeps_output_while_grandchild_holds_pipes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    layer: str,
) -> None:
    from trippostcollect.runtime import process

    exited = tmp_path / "child-exited"
    pidfile = tmp_path / "grandchild.pid"
    command = [
        sys.executable, "-c", CHILD_LEAVING_GRANDCHILD,
        str(exited), str(pidfile), GRANDCHILD_HOLDING_PIPES,
    ]
    log_dir = tmp_path / "logs"

    def fail_after_child_exit(*args: object, **kwargs: object) -> object:
        if exited.is_file():
            raise RuntimeError("injected failure after child exit")
        return original(*args, **kwargs) if original is not None else None

    try:
        if layer == "run_command":
            original = process.progress_path_signature
            progress = tmp_path / "progress"
            progress.mkdir()
            monkeypatch.setattr(process, "browser_launch_environment", lambda: dict(os.environ))
            monkeypatch.setattr(process, "PROCESS_FINAL_REAP_SECONDS", 0.3)
            monkeypatch.setattr(process, "progress_path_signature", fail_after_child_exit)
            with pytest.raises(RuntimeError):
                process.run_command(
                    command, tmp_path, 3600, log_dir, progress_paths=[progress], poll_seconds=0.05,
                )
        else:
            original = None
            monkeypatch.setattr(crawl_runner, "RUNNER_CHILD_POLL_SECONDS", 0.05)
            monkeypatch.setattr(crawl_runner, "PROCESS_FINAL_REAP_SECONDS", 0.3)
            with pytest.raises(RuntimeError):
                crawl_runner.run_child_command(
                    command,
                    env=dict(os.environ),
                    log_dir=log_dir,
                    state_path=tmp_path / "state.json",
                    interrupt_signal=fail_after_child_exit,
                )
    finally:
        if pidfile.is_file():
            try:
                os.killpg(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert (log_dir / "stdout.log").read_text(encoding="utf-8") == "child-out\n"
    assert (log_dir / "stderr.log").read_text(encoding="utf-8") == "child-err\n"


@pytest.mark.macos_process
@pytest.mark.parametrize(
    ("signum", "fail_finish", "broken_stderr"),
    [
        (signal.SIGHUP, False, False),
        (signal.SIGTERM, True, False),
        (signal.SIGTERM, True, True),
    ],
    ids=["sighup-stdout-hung-up", "uncaught-after-latch", "uncaught-stdout-stderr-hung-up"],
)
def test_exit_code_stays_first_signal_when_output_is_hung_up(
    tmp_path: Path,
    signum: int,
    fail_finish: bool,
    broken_stderr: bool,
) -> None:
    """终端挂断后输出端失效：真实解释器退出时的 flush 不得把退出码改成 120。"""

    work = tmp_path / "work"
    work.mkdir()
    config = tmp_path / "crawl_targets.json"
    config.write_text(json.dumps(_config([WEIBO]), ensure_ascii=False), encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "work": str(work),
                "modes": {WEIBO: "hang"},
                "self_signal_while_planning": int(signum),
                "fail_finish_run_report": fail_finish,
            }
        ),
        encoding="utf-8",
    )
    # 读端在启动后立即关闭：写入得到 EPIPE（解释器忽略 SIGPIPE），可靠复现输出端失效。
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    stderr_file = (tmp_path / "stderr.log").open("wb")
    try:
        process = subprocess.Popen(
            [
                sys.executable, str(FAKES), "runner", str(plan), "--",
                "--db", str(tmp_path / "runner.sqlite"),
                "--config", str(config),
                "--run-root", str(tmp_path / "runs"),
                "--execution-state-root", str(tmp_path / "states"),
                "--dry-run",
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stdout_write,
            stderr=stderr_write if broken_stderr else stderr_file,
            start_new_session=True,
        )
    finally:
        for descriptor in (stdout_read, stdout_write, stderr_read, stderr_write):
            os.close(descriptor)
        stderr_file.close()
    exit_code = process.wait(timeout=120)

    stderr_text = (tmp_path / "stderr.log").read_text(encoding="utf-8", errors="replace")
    assert exit_code == 128 + signum, stderr_text[-3000:]
    summary = json.loads(next((tmp_path / "runs").rglob("run_summary.json")).read_text(encoding="utf-8"))
    assert summary["interrupt"]["signal"] == signal.Signals(signum).name
    if fail_finish and not broken_stderr:
        # stderr 可写时 traceback 照常可见。
        assert "injected finish failure" in stderr_text


def test_unrelated_error_after_child_result_keeps_original_classification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """对照：child 已正常结束后出现与中断无关的错误，即使已锁存信号也不改记为中断。"""

    job, db_path = _leased_job(tmp_path)
    logs = {"stdout": "", "stderr": "", "stdout_log": "", "stderr_log": "", "command_log": ""}

    def completed_child(*_args: object, **_kwargs: object) -> object:
        return crawl_runner.ChildRun(
            completed=subprocess.CompletedProcess(["child"], 0, "", ""),
            raw_stdout="",
            raw_stderr="",
            logs=logs,
            interrupt_signum=None,
            launched=True,
            forced_termination=False,
            killed_child_process_groups=[],
        )

    def database_locked(*_args: object, **_kwargs: object) -> object:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(crawl_runner, "run_child_command", completed_child)
    monkeypatch.setattr(crawl_runner, "find_artifact_paths", database_locked)
    record = crawl_runner.execute_prepared_job(
        job,
        args=SimpleNamespace(no_import=False),
        db_path=db_path,
        config={"defaults": {"schedule_jitter_ratio": 0}},
        run_id="run-interrupt",
        run_dir=tmp_path / "run",
        interrupt_signal=lambda: int(signal.SIGINT),
    )

    assert record["failure_type"] == "scheduler_internal_error"
    assert record["interrupt"] is None
    assert "database is locked" in record["reason"]
    state = json.loads(job.state_path.read_text(encoding="utf-8"))
    assert state["steps"]["command_executed"]["error"] == "scheduler_internal_error"
