"""通用 runner 的操作人中断：真实 runner → 中间层 → 独立 worker 进程组贯通。

只使用临时 SQLite、临时配置与本地替身进程，不访问网络、不启动浏览器。
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.db.connection import connect_db
from trippostcollect.runtime.process import process_group_exists
from trippostcollect.scheduler.discovery import query_fingerprint, save_checkpoint, update_campaign


ROOT = Path(__file__).resolve().parents[1]
FAKES = ROOT / "tests" / "support" / "runner_interrupt_fakes.py"
WEIBO = "mc_weibo_qingdao_laoshan_guide_search"
DOUYIN = "mc_douyin_qingdao_laoshan_guide_search"
WEIBO_QUEUED = "mc_weibo_qingdao_laoshan_guide_search_queued"
HANDSHAKE_SECONDS = 90.0


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
        jobs = config["jobs"]
        self.config.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        checkpoint_plan: dict[str, dict] = {"job_ids": {}, "platforms": {}, "fingerprints": {}}
        with connect_db(self.db) as conn:
            bootstrap_connection(conn, config=config, sync_jobs=True)
            for job in jobs:
                row = conn.execute(
                    "SELECT id FROM crawl_jobs WHERE job_key=?", (job["job_key"],)
                ).fetchone()
                params = job["params"]
                fingerprint = query_fingerprint(params["platform"], params["keyword"], params)
                checkpoint_plan["job_ids"][job["job_key"]] = int(row[0])
                checkpoint_plan["platforms"][job["job_key"]] = params["platform"]
                checkpoint_plan["fingerprints"][job["job_key"]] = fingerprint
                # 已确认的历史前沿与 campaign：中断后必须原样保留。
                save_checkpoint(
                    conn,
                    job_id=int(row[0]),
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
                update_campaign(
                    conn,
                    job_id=int(row[0]),
                    query_fingerprint_value=fingerprint,
                    summary_path=None,
                    campaign_candidate_count=7,
                )
            conn.commit()
        (self.work / "checkpoint.json").write_text(
            json.dumps({"db": str(self.db), **checkpoint_plan}), encoding="utf-8"
        )
        self.plan = tmp_path / "plan.json"
        self.plan.write_text(
            json.dumps({"work": str(self.work), "modes": modes, "grace_seconds": grace_seconds}),
            encoding="utf-8",
        )
        self.before = self.snapshot()
        self.process: subprocess.Popen[bytes] | None = None

    def snapshot(self) -> dict[str, list[tuple]]:
        with sqlite3.connect(self.db) as conn:
            return {
                "checkpoints": conn.execute(
                    "SELECT job_id, resume_page, resume_offset, resume_cursor, status, "
                    "last_batch_complete, last_stop_reason, last_run_id, last_summary_path, "
                    "campaign_candidate_count, updated_at FROM crawl_discovery_checkpoints ORDER BY job_id"
                ).fetchall(),
                "seen": conn.execute(
                    "SELECT COUNT(*) FROM crawl_discovery_seen_candidates"
                ).fetchall(),
            }

    def start(self, *, max_parallel: int) -> None:
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
        deadline = time.monotonic() + HANDSHAKE_SECONDS
        while not predicate():
            assert self.process is not None
            if self.process.poll() is not None:
                pytest.fail(f"runner exited before {description}: {self.output()}")
            if time.monotonic() > deadline:
                pytest.fail(f"timed out waiting for {description}: {self.output()}")
            time.sleep(0.05)

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


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


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


def assert_once(text: str, needle: str) -> None:
    assert text.count(needle) == 1, (needle, text[-2000:])


@pytest.mark.macos_process
@pytest.mark.parametrize(
    ("signum", "terminal_group"),
    [(signal.SIGINT, True), (signal.SIGTERM, False)],
    ids=["sigint-terminal-group", "sigterm-runner"],
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
    evidence = assert_interrupted_state(run.state(WEIBO), signame, dispatched=True)
    assert evidence["forced_termination"] is False
    # 中间层只收到 runner 的一次 SIGTERM，并经可捕获路径以 143 退出。
    assert evidence["exit_code"] == 128 + signal.SIGTERM
    # 已确认批次事件保留在 execution state。
    events = run.state(WEIBO)["events"]
    assert [event["type"] for event in events] == ["adaptive_batch_completed"]

    # 进程树全部收束：中间层、worker 与其组内孙进程均不存在。
    for pid in (middle["pid"], worker["pid"], worker["grandchild_pid"]):
        assert not pid_alive(pid)
    assert not process_group_exists(middle["pgid"])
    assert not process_group_exists(worker["pgid"])

    # worker 只收到一次温和信号且清理完成，未走二次信号的 os._exit(130)。
    job_dir = run.work / WEIBO
    assert (job_dir / "worker-signals").read_text(encoding="utf-8") == "signal\n"
    assert (job_dir / "cleanup-done").is_file()

    # 两层日志均落盘：中断前输出与清理尾部输出各出现一次，stdout/stderr 分别断言。
    runner_logs = record["child_logs"]
    middle_stdout = Path(runner_logs["stdout_log"]).read_text(encoding="utf-8")
    middle_stderr = Path(runner_logs["stderr_log"]).read_text(encoding="utf-8")
    assert_once(middle_stdout, f"{WEIBO}:middle-partial-out")
    assert f"{WEIBO}:middle-partial-err" not in middle_stdout
    assert_once(middle_stderr, f"{WEIBO}:middle-partial-err")
    assert_once(middle_stderr, "runtime_failed:operator_interrupt:SIGTERM")
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
def test_parallel_lanes_keep_finished_results_and_stop_queued_jobs(tmp_path: Path) -> None:
    run = Run(tmp_path, {WEIBO: "hang", WEIBO_QUEUED: "hang", DOUYIN: "quick"})
    run.start(max_parallel=2)
    run.ready(WEIBO)
    run.attempt_finished(DOUYIN)

    run.interrupt(signal.SIGINT, terminal_group=True)
    exit_code = run.finish()

    assert exit_code == 128 + signal.SIGINT, run.output()
    summary = run.summary()
    records = {record["job_key"]: record for record in summary["records"]}
    assert [record["job_key"] for record in summary["records"]] == [WEIBO, WEIBO_QUEUED, DOUYIN]
    assert summary["interrupted_count"] == 2
    # 已完成平台的结果原样保留，不被标为中断。
    assert records[DOUYIN]["interrupt"] is None
    assert run.attempts(DOUYIN)[0]["status"] != "running"
    assert run.state(DOUYIN)["steps"]["command_executed"]["error"] != (
        "runtime_failed:operator_interrupt:SIGINT"
    )
    # 运行中的通道被收束，排队 job 不再派发：无租约、无 child、状态写成中断终态。
    assert_interrupted_state(run.state(WEIBO), "SIGINT", dispatched=True)
    assert_interrupted_state(run.state(WEIBO_QUEUED), "SIGINT", dispatched=False)
    assert run.attempts(WEIBO_QUEUED) == []
    assert run.job_row(WEIBO_QUEUED)["status"] == "pending"
    assert not (run.work / WEIBO_QUEUED).exists()
    assert records[WEIBO_QUEUED]["interrupt"]["signal"] == "SIGINT"
    worker = run.identity(WEIBO, "worker")
    assert not process_group_exists(worker["pgid"])
    assert (run.work / WEIBO / "cleanup-done").is_file()
    assert run.snapshot()["checkpoints"] == run.before["checkpoints"]


@pytest.mark.macos_process
def test_forced_kill_fallback_keeps_first_signal_as_cause(tmp_path: Path) -> None:
    run = Run(tmp_path, {WEIBO: "stubborn"}, grace_seconds=1.0)
    run.start(max_parallel=1)
    run.ready(WEIBO)
    middle = run.identity(WEIBO, "middle")

    run.interrupt(signal.SIGINT, terminal_group=True)
    run.interrupt(signal.SIGTERM, terminal_group=False)
    exit_code = run.finish()

    assert exit_code == 128 + signal.SIGINT, run.output()
    summary = run.summary()
    assert summary["interrupt"]["signal"] == "SIGINT"
    evidence = assert_interrupted_state(run.state(WEIBO), "SIGINT", dispatched=True)
    assert evidence["forced_termination"] is True
    assert evidence["exit_code"] == -signal.SIGKILL
    assert not pid_alive(middle["pid"])
    assert not process_group_exists(middle["pgid"])
    stdout = Path(summary["records"][0]["child_logs"]["stdout_log"]).read_text(encoding="utf-8")
    assert_once(stdout, f"{WEIBO}:middle-partial-out")
    assert run.snapshot() == run.before


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
def test_run_command_interrupt_writes_merged_cleaned_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    variant: str,
) -> None:
    from trippostcollect.records.sanitization import AUTHOR_AVATAR_LOG_REDACTION
    from trippostcollect.runtime import process

    ready = tmp_path / "ready"
    progress = tmp_path / "progress"
    progress.mkdir()
    original_signature = process.progress_path_signature

    def interrupt_once_ready(*args: object, **kwargs: object) -> object:
        if ready.is_file():
            raise KeyboardInterrupt
        return original_signature(*args, **kwargs)

    monkeypatch.setattr(process, "browser_launch_environment", lambda: dict(os.environ))
    monkeypatch.setattr(process, "progress_path_signature", interrupt_once_ready)
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


def test_merge_process_output_neither_duplicates_nor_drops() -> None:
    from trippostcollect.runtime.process import merge_process_output

    assert merge_process_output("a\n", "a\nb\n") == "a\nb\n"
    assert merge_process_output("a\nb\n", "") == "a\nb\n"
    assert merge_process_output("a\nb\n", "a\n") == "a\nb\n"
    assert merge_process_output("a\n", "c\n") == "a\nc\n"


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
