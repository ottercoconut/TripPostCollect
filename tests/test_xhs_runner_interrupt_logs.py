"""小红书租约路径的操作人中断：真实 xhs_runner → 中间层 → 独立 worker 进程组贯通（#58）。

只使用临时 SQLite、临时运行根与本地替身进程，不访问网络、不启动浏览器。替身覆盖范围见
``tests/support/xhs_interrupt_fakes.py``。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.records.sanitization import AUTHOR_AVATAR_LOG_REDACTION
from trippostcollect.runtime.process import process_group_exists
from trippostcollect.xhs import accounts


ROOT = Path(__file__).resolve().parents[1]
FAKES = ROOT / "tests" / "support" / "xhs_interrupt_fakes.py"
RUN_ID = "xhs-interrupt-logs"
HANDSHAKE_SECONDS = 90.0


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
    wait_until(lambda: not pid_alive(pid), f"pid {pid} reaped", seconds=15)


def assert_once(text: str, needle: str) -> None:
    assert text.count(needle) == 1, (needle, text[-2000:])


def _setup(tmp_path: Path, worker_mode: str) -> tuple[Path, dict]:
    work = tmp_path / "work"
    work.mkdir()
    target_config = tmp_path / "targets.json"
    target_config.write_text(
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
    pool_config = tmp_path / "pool.json"
    pool_config.write_text(
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
    original_lock_root = accounts.XHS_LOCK_ROOT
    accounts.XHS_LOCK_ROOT = tmp_path / "locks"
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            accounts.ensure_xhs_schema(conn)
            accounts.register_account_slot(conn, "xhs-a01")
    finally:
        accounts.XHS_LOCK_ROOT = original_lock_root
    plan = {
        "run_id": RUN_ID,
        "db": str(db_path),
        "target_config": str(target_config),
        "pool_config": str(pool_config),
        "runtime_root": str(tmp_path / "runtime"),
        "execution_root": str(tmp_path / "execution"),
        "output_root": str(tmp_path / "outputs"),
        "session_root": str(tmp_path / "sessions"),
        "lock_root": str(tmp_path / "locks"),
        "worker_mode": worker_mode,
    }
    (work / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    return work, plan


def _discovery_snapshot(db_path: str) -> dict[str, list]:
    with sqlite3.connect(db_path) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("xhs_discovery_checkpoints", "xhs_discovery_seen_candidates")
        }


def _run_interrupted(tmp_path: Path, worker_mode: str, **plan_overrides: object) -> dict:
    """启动真实 xhs_runner 替身链，worker 就绪后向 runner 发 SIGTERM，返回观测结果。"""

    work, plan = _setup(tmp_path, worker_mode)
    plan.update(plan_overrides)
    (work / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    before = _discovery_snapshot(plan["db"])
    output = (tmp_path / "runner.out").open("wb")
    runner = subprocess.Popen(
        [sys.executable, str(FAKES), "runner", str(work)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    output.close()

    def runner_output() -> str:
        return (tmp_path / "runner.out").read_text(encoding="utf-8", errors="replace")[-4000:]

    try:
        def ready() -> bool:
            if (work / "ready").is_file():
                return True
            if runner.poll() is not None:
                pytest.fail(f"runner exited before worker ready: {runner_output()}")
            return False

        wait_until(ready, "worker ready")
        runner_identity = json.loads((work / "runner.json").read_text(encoding="utf-8"))
        middle = json.loads((work / "middle.json").read_text(encoding="utf-8"))
        worker = json.loads((work / "worker.json").read_text(encoding="utf-8"))
        # 中间层与 worker 各自独立会话：操作人信号只到 runner。
        assert len({runner_identity["pgid"], middle["pgid"], worker["pgid"]}) == 3

        os.kill(runner.pid, signal.SIGTERM)
        exit_code = runner.wait(timeout=120)
    finally:
        if runner.poll() is None:
            os.killpg(runner.pid, signal.SIGKILL)
            runner.wait(timeout=10)
        for name in ("worker.json", "middle.json"):
            identity = work / name
            if identity.is_file():
                try:
                    os.killpg(json.loads(identity.read_text(encoding="utf-8"))["pgid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    assert exit_code == 128 + signal.SIGTERM, runner_output()
    return {
        "work": work,
        "plan": plan,
        "before": before,
        "middle": middle,
        "worker": worker,
    }


def _assert_single_gentle_signal(observed: dict) -> None:
    work = observed["work"]
    middle = observed["middle"]
    worker = observed["worker"]
    # worker 只收到中间层转发的一次温和信号；第二次会留下 second-signal / forced-exit 标记。
    assert (work / "worker-signals").read_text(encoding="utf-8") == "signal\n"
    assert not (work / "second-signal").exists()
    assert not (work / "forced-exit").exists()
    for pid in (middle["pid"], worker["pid"], worker["grandchild_pid"]):
        assert_gone(pid)
    assert not process_group_exists(middle["pgid"])
    assert not process_group_exists(worker["pgid"])


def _assert_interrupt_terminal_and_cleanup(observed: dict) -> None:
    # 终态、租约与 session 清理语义不变；discovery 不推进。
    plan = observed["plan"]
    summary = json.loads(
        (Path(plan["runtime_root"]) / "runs" / RUN_ID / "run_summary.json").read_text(encoding="utf-8")
    )
    assert summary["status"] == "failed"
    assert summary["failure_type"] == "runtime_failed"
    assert summary["reason"] == "operator_interrupt"
    assert summary["interrupt"]["signal"] == "SIGTERM"
    assert summary["interrupt"]["exit_code"] == 128 + signal.SIGTERM
    assert summary["lease_released"] is True
    assert summary["runtime_session_removed"] is True
    assert summary["lease_cleanup"]["process_check"]["safe_to_release"] is True
    state = json.loads(
        (Path(plan["execution_root"]) / RUN_ID / "test.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "failed"
    assert state["steps"]["command_executed"]["error"] == "xhs_runtime_failed:operator_interrupt:SIGTERM"
    assert "adaptive_search_stopped" not in {event.get("type") for event in state.get("events", [])}
    assert not (Path(plan["session_root"]) / RUN_ID).exists()
    with sqlite3.connect(plan["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM xhs_lease_processes").fetchone()[0] == 0
    assert _discovery_snapshot(plan["db"]) == observed["before"]


def test_lease_interrupt_budgets_are_nested() -> None:
    """中间层等待 exporter 的上限加收尾余量严格小于 LeaseGuard 给中间层的中断宽限，且不加长总预算。"""

    from trippostcollect.runtime import process
    from trippostcollect.xhs import leases

    budget = leases.crawl_lease_budget(timeout_seconds=1800, configured_lease_seconds=2400)
    assert budget.child_shutdown_seconds == leases.DEFAULT_CHILD_SHUTDOWN_BUDGET_SECONDS
    middle_ceiling = (
        process.XHS_LEASE_INTERRUPT_EXPORTER_GRACE_SECONDS
        + process.PROCESS_FINAL_REAP_SECONDS
        + process.XHS_LEASE_INTERRUPT_FINALIZE_MARGIN_SECONDS
    )
    assert middle_ceiling < leases.interrupted_child_grace_seconds(budget)
    assert (
        leases.interrupted_child_grace_seconds(budget)
        + leases.interrupted_child_kill_reap_seconds(budget)
        == budget.child_shutdown_seconds
    )
    # 余量覆盖 exporter 退出登记在 SQLite 默认 5 秒 busy 等待下的上限。
    assert process.XHS_LEASE_INTERRUPT_FINALIZE_MARGIN_SECONDS > 5.0
    assert process.XHS_LEASE_INTERRUPT_EXPORTER_GRACE_SECONDS <= process.PROCESS_CLEANUP_GRACE_SECONDS


@pytest.mark.macos_process
@pytest.mark.parametrize("worker_mode", ["plain", "avatar"])
def test_xhs_lease_interrupt_persists_cleaned_child_logs_and_signals_worker_once(
    tmp_path: Path,
    worker_mode: str,
) -> None:
    observed = _run_interrupted(tmp_path, worker_mode)
    work = observed["work"]

    # 中间层经可捕获路径以 128+SIGTERM 退出，worker 只收到中间层转发的一次温和信号并完成清理。
    assert json.loads((work / "middle-exit.json").read_text(encoding="utf-8")) == {
        "code": 128 + signal.SIGTERM
    }
    assert (work / "cleanup-done").is_file()
    _assert_single_gentle_signal(observed)

    # child（exporter）日志经同一头像清洗后落盘。
    stdout = (work / "worker-logs" / "stdout.log").read_text(encoding="utf-8")
    stderr = (work / "worker-logs" / "stderr.log").read_text(encoding="utf-8")
    assert (work / "worker-logs" / "command.txt").is_file()
    if worker_mode == "avatar":
        assert stdout == AUTHOR_AVATAR_LOG_REDACTION
        assert stderr == AUTHOR_AVATAR_LOG_REDACTION
    else:
        assert_once(stdout, "xhs:worker-partial-out")
        assert_once(stdout, "xhs:worker-cleanup-out")
        assert_once(stdout, "Received interrupt signal 15")
        assert "again" not in stdout
        assert_once(stderr, "xhs:worker-partial-err")
        assert_once(stderr, "xhs:worker-cleanup-err")
    _assert_interrupt_terminal_and_cleanup(observed)


@pytest.mark.macos_process
def test_xhs_slow_worker_gets_one_gentle_signal_then_sigkill_from_middle(tmp_path: Path) -> None:
    """worker 关闭耗时超过中间层等待上限：中间层 SIGKILL 兜底后照常落盘日志并以 128+SIGTERM 退出。"""

    observed = _run_interrupted(tmp_path, "slow", exporter_grace_seconds=1.0)
    work = observed["work"]

    assert (work / "cleanup-started").is_file()
    assert not (work / "cleanup-done").exists()
    _assert_single_gentle_signal(observed)
    assert json.loads((work / "middle-exit.json").read_text(encoding="utf-8")) == {
        "code": 128 + signal.SIGTERM
    }
    stdout = (work / "worker-logs" / "stdout.log").read_text(encoding="utf-8")
    assert_once(stdout, "xhs:worker-partial-out")
    assert_once(stdout, "Received interrupt signal 15")
    assert "again" not in stdout
    assert "xhs:worker-cleanup-out" not in stdout
    _assert_interrupt_terminal_and_cleanup(observed)


@pytest.mark.macos_process
def test_xhs_middle_killed_after_forwarding_leaves_exporter_only_sigkill(tmp_path: Path) -> None:
    """中间层超出 LeaseGuard 中断宽限被 SIGKILL：仍存活的 exporter 只收 SIGKILL 兜底，不收第二次 SIGTERM。"""

    observed = _run_interrupted(
        tmp_path,
        "slow",
        child_shutdown_seconds=4,
        exporter_grace_seconds=120.0,
    )
    work = observed["work"]

    assert (work / "cleanup-started").is_file()
    assert not (work / "cleanup-done").exists()
    # 中间层在收尾前被 LeaseGuard 兜底强杀，没有走到入口包装的退出。
    assert not (work / "middle-exit.json").exists()
    _assert_single_gentle_signal(observed)
    _assert_interrupt_terminal_and_cleanup(observed)
