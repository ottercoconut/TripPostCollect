#!/usr/bin/env python3
"""抖音作者资料回填的中间层和 worker。

父入口只把冻结清单交给本脚本。本脚本再启动同一个脚本的 worker：先在关键词搜索页完成人类行为，
再按清单取作者资料。不写 checkpoint、不下载正文图、不写内容记录。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


# 这些名字已在正式抓取中使用；回填只在 child 进程里打开同一开关，不新增环境变量名。
BACKFILL_ENV = {
    "TRIPPOSTCOLLECT_DOUYIN_ENRICH_CREATORS": "1",
    "TRIPPOSTCOLLECT_DOUYIN_ENRICH_ONLY_IMAGES": "0",
    "TRIPPOSTCOLLECT_DOUYIN_MAX_CREATOR_ENRICH": "-1",
}


def backfill_worker_config(keyword: str, *, headless: bool, save_path: Path):
    """详情模式的配置壳：不挂指定作品、不下载正文图，避免走内容写出。"""

    from trippostcollect.application.worker_inputs import worker_config

    config = worker_config()
    config.PLATFORM = "dy"
    config.LOGIN_TYPE = "cookie"
    config.CRAWLER_TYPE = "detail"
    config.KEYWORDS = keyword
    config.ENABLE_GET_MEIDAS = False
    config.ENABLE_GET_COMMENTS = False
    config.ENABLE_GET_SUB_COMMENTS = False
    config.HEADLESS = headless
    config.CDP_HEADLESS = headless
    config.SAVE_LOGIN_STATE = True
    config.ENABLE_CDP_MODE = False
    config.SAVE_DATA_OPTION = "jsonl"
    config.SAVE_DATA_PATH = str(save_path)
    config.DY_SPECIFIED_ID_LIST = []
    config.MAX_CONCURRENCY_NUM = 1
    return config


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Observe frozen Douyin author profiles.")
    parser.add_argument("--targets", required=True)
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--worker", action="store_true")
    return parser.parse_args(argv)


def _write_result(path: Path, payload: dict[str, Any]) -> None:
    from trippostcollect.records.sanitization import public_payload

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(public_payload(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


async def run_worker(args: argparse.Namespace) -> int:
    from trippostcollect.application.collection import behavior_environment
    from trippostcollect.platforms.douyin.core import DouYinCrawler
    from trippostcollect.platforms.douyin.profile_backfill import _stopped, load_manifest
    from trippostcollect.platforms.entry import douyin_dependencies

    output_path = Path(args.output).expanduser()
    authors = load_manifest(Path(args.targets).expanduser())
    evidence_path = output_path.with_name("behavior_evidence.json")
    progress_path = output_path.with_name("progress.txt")
    os.environ.update(behavior_environment(evidence_path, "social_high_risk"))
    os.environ.update(BACKFILL_ENV)
    config = backfill_worker_config(
        str(args.keyword),
        headless=bool(args.headless),
        save_path=output_path.parent / "unused-content",
    )
    crawler = DouYinCrawler.bind(lambda: douyin_dependencies(config))()
    crawler.author_profile_targets = authors

    def touch() -> None:
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        current = progress_path.stat().st_size + 1 if progress_path.exists() else 1
        progress_path.write_text(str(current), encoding="utf-8")

    crawler.author_profile_progress = touch
    touch()
    try:
        await crawler.start()
        result = crawler.author_profile_result or _stopped([], [], "browser_launch_failed")
    except Exception as exc:
        code = str(getattr(exc, "code", "") or "")
        message = str(exc)
        if not code and "human_behavior" in message:
            code = "behavior_evidence_failed"
        result = _stopped([], [], code or "browser_launch_failed")
    if not isinstance(result, dict):
        result = _stopped([], [], "browser_launch_failed")
    _write_result(output_path, result)
    print(json.dumps({"child_summary": str(output_path.resolve())}, ensure_ascii=False), flush=True)
    return 0


def run_middle(args: argparse.Namespace) -> int:
    from trippostcollect.core.paths import PROJECT_ROOT
    from trippostcollect.platforms.douyin.profile_backfill import load_manifest
    from trippostcollect.runtime.behavior import HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS
    from trippostcollect.runtime.process import NO_PROGRESS_WATCHDOG_SECONDS, run_command

    output_path = Path(args.output).expanduser()
    load_manifest(Path(args.targets).expanduser())
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--targets",
        str(Path(args.targets).expanduser()),
        "--keyword",
        str(args.keyword),
        "--output",
        str(output_path),
    ]
    if args.headless:
        command.append("--headless")
    result = run_command(
        command,
        PROJECT_ROOT,
        NO_PROGRESS_WATCHDOG_SECONDS,
        output_path.parent / "logs",
        progress_paths=[
            output_path,
            output_path.with_name("behavior_evidence.json"),
            output_path.with_name("progress.txt"),
        ],
        startup_grace_seconds=HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS,
    )
    if output_path.is_file():
        print(json.dumps({"child_summary": str(output_path.resolve())}, ensure_ascii=False), flush=True)
        return 0 if int(result.get("returncode") or 0) == 0 else int(result["returncode"])
    blocker = "browser_launch_failed"
    if result.get("timed_out"):
        blocker = str(result.get("timeout_reason") or "browser_target_closed")
    print(json.dumps({"child_summary": "", "runtime_blocker": blocker}, ensure_ascii=False), flush=True)
    return int(result.get("returncode") or 1)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not str(args.keyword or "").strip():
        raise SystemExit("抖音回填需要非空的 --keyword")
    if args.worker:
        return asyncio.run(run_worker(args))
    return run_middle(args)


if __name__ == "__main__":
    raise SystemExit(main())
