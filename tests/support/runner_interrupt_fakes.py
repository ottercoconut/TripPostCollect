"""通用 runner 中断用例的真实进程替身：runner 驱动、中间层与独立 worker 进程组。

三层都以真实进程运行且不访问网络：
- ``runner``：真实 ``crawl_runner.main``，只把 child 命令替换为本文件的 ``middle``；
- ``middle``：真实 ``collection.main`` 与 ``run_command``，业务体换成假批次；
- ``worker``：真实 ``runtime.worker.run`` 生命周期，并在本进程组内保留一个孙进程。
握手与观测都写入 ``work`` 目录下的文件，测试据此同步，不靠固定 sleep 猜时机。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
THIS = Path(__file__).resolve()


def _job_dir(work: Path, job_key: str) -> Path:
    path = work / job_key
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temporary, path)


def run_runner(plan_path: Path, runner_args: list[str]) -> int:
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    sys.path.insert(0, str(ROOT / "scripts"))
    import crawl_runner

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    work = Path(plan["work"])
    if plan.get("grace_seconds") is not None:
        crawl_runner.RUNNER_CHILD_INTERRUPT_GRACE_SECONDS = float(plan["grace_seconds"])

    def fake_command(row: dict, _args: object) -> list[str]:
        mode = plan["modes"][str(row["job_key"])]
        return [sys.executable, str(THIS), "middle", str(work), str(row["job_key"]), mode]

    crawl_runner.build_command = fake_command
    _write_json(work / "runner.json", {"pid": os.getpid(), "pgid": os.getpgid(0)})
    sys.argv = ["crawl_runner.py", *runner_args]
    return crawl_runner.main()


def run_middle(work: Path, job_key: str, mode: str) -> int:
    from trippostcollect.application import collection
    from trippostcollect.core.execution_state import FrozenExecutionState
    from trippostcollect.db.connection import connect_db
    from trippostcollect.runtime import process
    from trippostcollect.scheduler.discovery import save_checkpoint

    # 不创建浏览器运行目录；只替换环境来源，子进程监督与收束仍是真实实现。
    process.browser_launch_environment = lambda: dict(os.environ)
    job_dir = _job_dir(work, job_key)
    identity = {"pid": os.getpid(), "pgid": os.getpgid(0)}

    def fake_run_main(_args: object, _reporter: object) -> int:
        print(f"{job_key}:middle-partial-out", flush=True)
        print(f"{job_key}:middle-partial-err", file=sys.stderr, flush=True)
        if mode == "quick":
            _write_json(job_dir / "middle.json", identity)
            print(json.dumps({"job_key": job_key, "quick": True}), flush=True)
            return 0
        if mode == "stubborn":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            _write_json(job_dir / "middle.json", identity)
            (job_dir / "ready").write_text("ready", encoding="utf-8")
            while True:
                time.sleep(60)
        # 已确认批次：中断前写入的批次事件必须保留在 execution state。
        state = FrozenExecutionState(os.environ["TRIPPOSTCOLLECT_EXECUTION_STATE_PATH"])
        state.append_event(
            "adaptive_batch_completed",
            {"batch_no": 1, "batch_complete": True, "resume_page": 2},
        )
        _write_json(job_dir / "middle.json", identity)
        process.run_command(
            [sys.executable, str(THIS), "worker", str(work), job_key],
            ROOT,
            3600,
            job_dir / "worker-logs",
        )
        # 只有 worker 正常结束才会到达：模拟 collection 推进 checkpoint 的尾批路径。
        plan = json.loads((work / "checkpoint.json").read_text(encoding="utf-8"))
        with connect_db(Path(plan["db"])) as conn:
            save_checkpoint(
                conn,
                job_id=int(plan["job_ids"][job_key]),
                platform_key=str(plan["platforms"][job_key]),
                keyword="tail-batch",
                query_fingerprint_value=str(plan["fingerprints"][job_key]),
                resume_page=99,
                resume_offset=None,
                resume_cursor=None,
                source_has_more=False,
                last_batch_complete=False,
                last_stop_reason="source_exhausted",
                last_run_id="tail-batch",
            )
            conn.commit()
        return 0

    return collection.main(
        parse_args=lambda: None,
        xhs_supervisor_runtime_reporter_from_context=lambda _args: None,
        _run_main=fake_run_main,
    )


def run_worker(work: Path, job_key: str) -> None:
    from trippostcollect.runtime import worker

    job_dir = _job_dir(work, job_key)
    grandchild: dict[str, subprocess.Popen[bytes]] = {}

    async def app_main() -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(3600)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        grandchild["process"] = process
        print(f"{job_key}:worker-partial-out", flush=True)
        print(f"{job_key}:worker-partial-err", file=sys.stderr, flush=True)
        _write_json(
            job_dir / "worker.json",
            {"pid": os.getpid(), "pgid": os.getpgid(0), "grandchild_pid": process.pid},
        )
        (job_dir / "ready").write_text("ready", encoding="utf-8")
        await asyncio.sleep(3600)

    async def app_cleanup() -> None:
        process = grandchild.get("process")
        if process is not None:
            process.terminate()
            process.wait(timeout=10)
        # 给潜在的第二次信号留出窗口；收到第二次会经 os._exit(130) 跳过下面的标记。
        await asyncio.sleep(0.3)
        print(f"{job_key}:worker-cleanup-out", flush=True)
        print(f"{job_key}:worker-cleanup-err", file=sys.stderr, flush=True)
        (job_dir / "cleanup-done").write_text("done", encoding="utf-8")

    def first_interrupt() -> None:
        with (job_dir / "worker-signals").open("a", encoding="utf-8") as handle:
            handle.write("signal\n")

    worker.run(app_main, app_cleanup, on_first_interrupt=first_interrupt)


def main() -> int:
    role = sys.argv[1]
    if role == "runner":
        separator = sys.argv.index("--")
        return run_runner(Path(sys.argv[2]), sys.argv[separator + 1:])
    if role == "middle":
        return run_middle(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
    if role == "worker":
        run_worker(Path(sys.argv[2]), sys.argv[3])
        return 0
    raise SystemExit(f"unknown role: {role}")


if __name__ == "__main__":
    raise SystemExit(main())
