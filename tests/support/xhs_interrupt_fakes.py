"""小红书租约路径中断用例的真实进程替身：xhs_runner 驱动、中间层与独立 worker 进程组。

三层都以真实进程运行且不访问网络、不启动浏览器：
- ``runner``：真实 ``xhs_runner._run_main``（LeaseGuard、终态写入、租约与 session 清理均为生产实现），
  只把 child 命令换成本文件的 ``middle``，并把运行根、锁根与 session 根指向临时目录；
- ``middle``：真实入口包装 ``run_main_with_operator_interrupt`` 与带租约环境的 ``run_command``
  （exporter 经 gated 子进程启动并登记到租约）；
- ``worker``：真实 ``runtime.worker.run`` 生命周期，在本进程组内保留一个孙进程；不写批次事件，
  因而不会触发小红书批次确认，用于证明中断不推进 discovery。``slow`` 模式的清理永不结束，只能被
  SIGKILL 兜底；第二次温和信号会留下 ``second-signal``（及 ``forced-exit``）标记。
计划可选 ``child_shutdown_seconds``（覆盖 LeaseGuard 的 child 关闭预算）与
``exporter_grace_seconds``（覆盖中间层等待 exporter 的中断宽限），只用于缩短兜底用例耗时。
握手与观测都写入 ``work`` 目录下的文件，测试据此同步。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
from pathlib import Path
import signal
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
THIS = Path(__file__).resolve()
AVATAR_LINE = "author_avatar=https://img.example.invalid/a.jpg"


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temporary, path)


def run_runner(work: Path) -> int:
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.path.insert(0, str(ROOT / "scripts"))
    import xhs_runner
    from trippostcollect.xhs import accounts, runtime

    plan = json.loads((work / "plan.json").read_text(encoding="utf-8"))
    xhs_runner.XHS_RUNTIME_ROOT = Path(plan["runtime_root"])
    xhs_runner.XHS_EXECUTION_STATE_ROOT = Path(plan["execution_root"])
    xhs_runner.XHS_RUNS_OUTPUT = Path(plan["output_root"])
    xhs_runner.utc_stamp = lambda: plan["run_id"]
    runtime.XHS_SESSION_ROOT = Path(plan["session_root"])
    accounts.XHS_LOCK_ROOT = Path(plan["lock_root"])

    def fake_command(**_kwargs: object) -> list[str]:
        return [sys.executable, str(THIS), "middle", str(work), plan["worker_mode"]]

    xhs_runner.build_child_command = fake_command
    if plan.get("child_shutdown_seconds") is not None:
        production_budget = xhs_runner.crawl_lease_budget

        def short_budget(**kwargs: object) -> object:
            return dataclasses.replace(
                production_budget(**kwargs),
                child_shutdown_seconds=int(plan["child_shutdown_seconds"]),
            )

        xhs_runner.crawl_lease_budget = short_budget
    _write_json(work / "runner.json", {"pid": os.getpid(), "pgid": os.getpgid(0)})
    args = argparse.Namespace(
        target_key="test",
        account_id="xhs-a01",
        post_interaction="none",
        db=plan["db"],
        target_config=plan["target_config"],
        pool_config=plan["pool_config"],
        dry_run=False,
        no_import=False,
        retry_on_300011=False,
    )
    return xhs_runner._run_main(args)


def run_middle(work: Path, worker_mode: str) -> int:
    from trippostcollect.runtime import process

    # 不创建浏览器运行目录；只替换环境来源，租约登记、监督与收束仍是真实实现。
    process.browser_launch_environment = lambda: dict(os.environ)
    plan = json.loads((work / "plan.json").read_text(encoding="utf-8"))
    if plan.get("exporter_grace_seconds") is not None:
        process.XHS_LEASE_INTERRUPT_EXPORTER_GRACE_SECONDS = float(plan["exporter_grace_seconds"])

    def main() -> int:
        print("xhs:middle-partial-out", flush=True)
        print("xhs:middle-partial-err", file=sys.stderr, flush=True)
        _write_json(work / "middle.json", {"pid": os.getpid(), "pgid": os.getpgid(0)})
        process.run_command(
            [sys.executable, str(THIS), "worker", str(work), worker_mode],
            ROOT,
            3600,
            work / "worker-logs",
        )
        return 0

    code = process.run_main_with_operator_interrupt(main)
    _write_json(work / "middle-exit.json", {"code": code})
    return code


def run_worker(work: Path, mode: str) -> None:
    from trippostcollect.runtime import worker

    grandchild: dict[str, subprocess.Popen[bytes]] = {}
    real_exit = os._exit

    def observed_exit(code: int) -> None:
        # worker 只在收到第二次温和信号时经 os._exit 强退；留下标记供测试断言。
        (work / "forced-exit").write_text(str(code), encoding="utf-8")
        real_exit(code)

    os._exit = observed_exit

    def observed_print(*args: object, **kwargs: object) -> None:
        # 第二次温和信号先打印 "again" 再强退；上层管道已断时打印本身会失败，先落标记。
        if any("again" in str(value) for value in args):
            (work / "second-signal").write_text("again", encoding="utf-8")
        print(*args, **kwargs)

    worker.print = observed_print

    async def app_main() -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(3600)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        grandchild["process"] = process
        print("xhs:worker-partial-out", flush=True)
        print("xhs:worker-partial-err", file=sys.stderr, flush=True)
        if mode == "avatar":
            print(AVATAR_LINE, file=sys.stderr, flush=True)
        _write_json(
            work / "worker.json",
            {"pid": os.getpid(), "pgid": os.getpgid(0), "grandchild_pid": process.pid},
        )
        (work / "ready").write_text("ready", encoding="utf-8")
        await asyncio.sleep(3600)

    async def app_cleanup() -> None:
        if mode == "slow":
            # 关闭耗时超过中间层等待上限：只能由 SIGKILL 兜底。
            (work / "cleanup-started").write_text("started", encoding="utf-8")
            await asyncio.sleep(3600)
        process = grandchild.get("process")
        if process is not None:
            process.terminate()
            process.wait(timeout=10)
        # 给潜在的第二次信号留出窗口；收到第二次会经 os._exit(130) 跳过下面的标记。
        await asyncio.sleep(0.3)
        print("xhs:worker-cleanup-out", flush=True)
        print("xhs:worker-cleanup-err", file=sys.stderr, flush=True)
        (work / "cleanup-done").write_text("done", encoding="utf-8")

    def first_interrupt() -> None:
        with (work / "worker-signals").open("a", encoding="utf-8") as handle:
            handle.write("signal\n")

    worker.run(app_main, app_cleanup, on_first_interrupt=first_interrupt)


def main() -> int:
    role = sys.argv[1]
    if role == "runner":
        return run_runner(Path(sys.argv[2]))
    if role == "middle":
        return run_middle(Path(sys.argv[2]), sys.argv[3])
    if role == "worker":
        run_worker(Path(sys.argv[2]), sys.argv[3])
        return 0
    raise SystemExit(f"unknown role: {role}")


if __name__ == "__main__":
    raise SystemExit(main())
