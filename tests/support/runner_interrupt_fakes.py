"""通用 runner 中断用例的真实进程替身：runner 驱动、中间层与独立 worker 进程组。

三层都以真实进程运行且不访问网络：
- ``runner``：真实 ``crawl_runner.run_cli``，只把 child 命令的脚本换成本文件的 ``middle``，
  其余参数仍是生产 ``build_command`` 生成的参数；
- ``middle``：真实入口包装 ``run_main_with_operator_interrupt``、``collection.main``、生产
  ``parse_args`` 与 ``run_command``；worker 结束后用生产 ``load_pagination_evidence`` 与
  ``persist_discovery_checkpoint`` 提交 checkpoint/seen；``import-hang`` 模式先进入生产
  ``import_valid_records_with_media_rollback`` 的导入事务并在提交点阻塞，等待中断，导入失败时
  与生产一样跳过 checkpoint；
- ``worker``：真实 ``runtime.worker.run`` 生命周期，用生产 ``AdaptiveAccumulator`` 与 worker 事件出口
  写出一批已确认批次和一个未确认尾批，并在本进程组内保留一个模拟浏览器的孙进程。
握手与观测都写入 ``work`` 目录下的文件，测试据此同步，不靠固定 sleep 猜时机。
"""

from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
THIS = Path(__file__).resolve()
CONFIRMED_IDENTITIES = ("confirmed-1", "confirmed-2")
TAIL_IDENTITY = "tail-1"


def _job_dir(work: Path, job_key: str) -> Path:
    path = work / job_key
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temporary, path)


def _block_forever() -> None:
    while True:
        time.sleep(60)


def run_runner(plan_path: Path, runner_args: list[str]) -> int:
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.path.insert(0, str(ROOT / "scripts"))
    import crawl_runner

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    work = Path(plan["work"])
    if plan.get("grace_seconds") is not None:
        crawl_runner.RUNNER_CHILD_INTERRUPT_GRACE_SECONDS = float(plan["grace_seconds"])
    production_command = crawl_runner.build_command

    def fake_command(row: dict, args: object) -> list[str]:
        command = production_command(row, args)
        mode = plan["modes"][str(row["job_key"])]
        return [
            sys.executable, str(THIS), "middle", str(work), str(row["job_key"]), mode,
            "--", *command[2:],
        ]

    crawl_runner.build_command = fake_command
    if plan.get("self_signal_while_planning") is not None:
        # dry-run 计划阶段向自身发信号：确定性地在锁存后进入收尾。
        original_planned = crawl_runner.planned_record

        def planned_with_signal(job: object) -> dict:
            os.kill(os.getpid(), int(plan["self_signal_while_planning"]))
            return original_planned(job)

        crawl_runner.planned_record = planned_with_signal
    if plan.get("fail_finish_run_report"):
        def broken_finish(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected finish failure")

        crawl_runner.finish_run_report = broken_finish
    _write_json(work / "runner.json", {"pid": os.getpid(), "pgid": os.getpgid(0)})
    sys.argv = ["crawl_runner.py", *runner_args]
    return crawl_runner.run_cli()


def _success_summary(job_dir: Path) -> Path:
    summary = {
        "records": [],
        "formal_validation": {"candidate_count": 0},
        "import_completion_met": False,
        "import_result": {"skipped": True, "reason": "no_import"},
        "image_materialization": {
            "required": True,
            "complete": True,
            "promotion_required": False,
            "candidate_posts": 0,
            "complete_posts": 0,
            "expected_images": 0,
            "downloaded_images": 0,
            "validated_images": 0,
            "unique_images": 0,
            "sha256_duplicate_images": 0,
            "sha256_duplicates": [],
            "manifest_evidence": [],
            "manifest_paths": [],
            "manifest_sha256": sha256(json.dumps([], sort_keys=True).encode("utf-8")).hexdigest(),
        },
    }
    path = job_dir / "summary.json"
    _write_json(path, summary)
    return path


def _import_until_interrupted(args: object, job_dir: Path) -> dict:
    """进入生产导入事务：事务内写入探针后在提交点阻塞；提交前中断按契约回滚并转成
    ``sqlite_import_failed`` 结果。"""

    from trippostcollect.application import collection
    from trippostcollect.db import content

    def blocking_commit(conn: object) -> None:
        conn.execute("CREATE TABLE interrupt_import_probe (value TEXT)")
        conn.execute("INSERT INTO interrupt_import_probe VALUES ('uncommitted')")
        (job_dir / "importing").write_text("importing", encoding="utf-8")
        _block_forever()

    content.commit_formal_import = blocking_commit
    media_root = job_dir / "media"
    media_root.mkdir(exist_ok=True)
    return collection.import_valid_records_with_media_rollback(
        {"captured_at": "2026-10-09T00:00:00+00:00", "keyword": "青岛", "batch_dir": str(job_dir)},
        [],
        Path(args.db),
        materialized_images_by_identity={},
        image_materialization={},
        project_root=job_dir,
        media_root=media_root,
    )


def run_middle(work: Path, job_key: str, mode: str, production_args: list[str]) -> int:
    from trippostcollect.application import collection
    from trippostcollect.runtime import process

    # 不创建浏览器运行目录；只替换环境来源，子进程监督、登记与收束仍是真实实现。
    process.browser_launch_environment = lambda: dict(os.environ)
    job_dir = _job_dir(work, job_key)

    def parse_args() -> object:
        sys.path.insert(0, str(ROOT / "scripts"))
        import mediacrawler_crawl

        sys.argv = ["mediacrawler_crawl.py", *production_args]
        return mediacrawler_crawl.parse_args()

    def fake_run_main(args: object, _reporter: object) -> int:
        print(f"{job_key}:middle-partial-out", flush=True)
        print(f"{job_key}:middle-partial-err", file=sys.stderr, flush=True)
        _write_json(job_dir / "middle.json", {"pid": os.getpid(), "pgid": os.getpgid(0)})
        if mode == "success":
            print(json.dumps({"summary": str(_success_summary(job_dir))}), flush=True)
            return 0
        if mode == "stubborn":
            # 启动 worker 后中间层失去转发能力：只能由 runner 超时兜底强杀两组。
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        worker_mode = "exit" if mode in {"complete", "persist-hang", "import-hang"} else "hang"
        process.run_command(
            [sys.executable, str(THIS), "worker", str(work), job_key, worker_mode],
            ROOT,
            3600,
            job_dir / "worker-logs",
        )
        if mode == "import-hang":
            # 与生产一致：导入失败（sqlite_import_failed）时不提交 checkpoint，直接以 2 结束。
            _write_json(job_dir / "import-result.json", _import_until_interrupted(args, job_dir))
            return 2
        # 只有 worker 正常结束才会到达：生产 checkpoint/seen 提交路径。
        state_path = os.environ["TRIPPOSTCOLLECT_EXECUTION_STATE_PATH"]
        evidence = collection.load_pagination_evidence(state_path)
        platform_key = collection.selected_platforms(args.platforms)[0]
        result = collection.persist_discovery_checkpoint(args, platform_key, evidence)
        _write_json(job_dir / "persisted.json", result)
        if mode == "persist-hang":
            _block_forever()
        summary_path = job_dir / "summary.json"
        _write_json(
            summary_path,
            {
                "records": [],
                "formal_validation": {"candidate_count": len(CONFIRMED_IDENTITIES)},
                "import_completion_met": False,
                "import_result": {},
            },
        )
        print(json.dumps({"summary": str(summary_path)}), flush=True)
        return 2

    return process.run_main_with_operator_interrupt(
        lambda: collection.main(
            parse_args=parse_args,
            xhs_supervisor_runtime_reporter_from_context=lambda _args: None,
            _run_main=fake_run_main,
        )
    )


def run_worker(work: Path, job_key: str, mode: str) -> None:
    from trippostcollect.application.candidates import AdaptiveAccumulator
    from trippostcollect.application.events import append_worker_execution_event
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
        accumulator = AdaptiveAccumulator.for_platform("weibo", existing_identities=set())
        accumulator.event_sink = append_worker_execution_event
        accumulator.begin_batch()
        for identity in CONFIRMED_IDENTITIES:
            accumulator.consider(identity, valid=True)
        accumulator.finish_batch(
            source_page=3,
            resume_page=4,
            source_has_more=True,
            batch_complete=True,
        )
        # 未确认尾批：已看到候选但批次未结束，不得写入 checkpoint/seen。
        accumulator.begin_batch()
        accumulator.consider(TAIL_IDENTITY, valid=True)
        print(f"{job_key}:worker-partial-out", flush=True)
        print(f"{job_key}:worker-partial-err", file=sys.stderr, flush=True)
        _write_json(
            job_dir / "worker.json",
            {"pid": os.getpid(), "pgid": os.getpgid(0), "grandchild_pid": process.pid},
        )
        (job_dir / "ready").write_text("ready", encoding="utf-8")
        if mode == "hang":
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
        separator = sys.argv.index("--")
        return run_middle(Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[separator + 1:])
    if role == "worker":
        run_worker(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
        return 0
    raise SystemExit(f"unknown role: {role}")


if __name__ == "__main__":
    raise SystemExit(main())
