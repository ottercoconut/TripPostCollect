"""抖音作者资料回填的冻结清单、观测和 child。

不写内容记录、不下载正文图、不写 checkpoint。作品只接受与既有 aweme_id 完全相同的数字 ID。
观测值是回填当时的作者资料，不是原始抓取时刻的。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from trippostcollect.application.author_profile_backfill import public_payload
from trippostcollect.platforms.douyin import parser
from trippostcollect.records.identity import platform_user_id


AWEME_ID_RE = re.compile(r"[0-9]+")
FILLABLE_COLUMNS = (
    "author_profile_url",
    "author_description",
    "author_verified",
    "author_verified_text",
)
# 这些名字已在正式抓取中使用；回填只在 child 进程里打开同一开关，不新增环境变量名。
BACKFILL_ENV = {
    "TRIPPOSTCOLLECT_DOUYIN_ENRICH_CREATORS": "1",
    "TRIPPOSTCOLLECT_DOUYIN_ENRICH_ONLY_IMAGES": "0",
    "TRIPPOSTCOLLECT_DOUYIN_MAX_CREATOR_ENRICH": "-1",
}


def author_profile_fields(author: dict[str, Any], creator_profile: dict[str, Any]) -> dict[str, Any]:
    """用入库同一组解析函数得到简介、认证两列和主页链接。"""

    extras = parser._profile_author_extras(creator_profile if isinstance(creator_profile, dict) else {})
    profile_url = parser._author_profile_url(
        author if isinstance(author, dict) else {},
        creator_profile if isinstance(creator_profile, dict) else {},
    )
    fields: dict[str, Any] = {}
    if extras.get("author_desc"):
        fields["author_description"] = extras["author_desc"]
    if "author_verified" in extras:
        fields["author_verified"] = extras["author_verified"]
    if extras.get("author_verified_text"):
        fields["author_verified_text"] = extras["author_verified_text"]
    if profile_url:
        fields["author_profile_url"] = profile_url
    evidence = extras.get("author_verify_evidence")
    if isinstance(evidence, dict) and evidence:
        fields["author_verify_evidence"] = evidence
    cleaned = public_payload(fields)
    return cleaned if isinstance(cleaned, dict) else {}


def detail_failure_reason(detail: Any, aweme_id: str) -> str:
    if not isinstance(detail, dict) or not detail:
        return "empty_detail"
    if str(detail.get("aweme_id") or "") != str(aweme_id):
        return "empty_detail"
    status = detail.get("status")
    deleted = detail.get("is_delete") in (1, True, "1")
    if isinstance(status, dict) and status.get("is_delete") in (1, True, "1"):
        deleted = True
    if deleted:
        return "aweme_deleted"
    author = detail.get("author")
    if not isinstance(author, dict) or not author:
        return "no_author"
    return ""


def has_fillable_fields(fields: dict[str, Any]) -> bool:
    return any(fields.get(column) is not None for column in FILLABLE_COLUMNS)


def validate_author_works(authors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只接受 aweme_id 与 platform_post_id 完全相同的数字作品 ID。"""

    validated: list[dict[str, Any]] = []
    for index, author in enumerate(authors):
        if not isinstance(author, dict):
            raise ValueError(f"author profile target {index} is not an object")
        author_id = str(author.get("author_platform_id") or "").strip()
        works_in = author.get("works")
        if not author_id or not isinstance(works_in, list) or not works_in:
            raise ValueError(f"author profile target {index} requires author_platform_id and works")
        works: list[dict[str, str]] = []
        for work in works_in:
            if not isinstance(work, dict):
                raise ValueError(f"author profile work for {author_id} is not an object")
            aweme_id = str(work.get("aweme_id") or "")
            post_id = str(work.get("platform_post_id") or "")
            if aweme_id != post_id or AWEME_ID_RE.fullmatch(aweme_id) is None:
                raise ValueError(f"author profile work does not match aweme_id: {aweme_id!r}")
            works.append({"aweme_id": aweme_id, "platform_post_id": post_id})
        validated.append({"author_platform_id": author_id, "works": works})
    return validated


def load_manifest(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    authors = payload.get("authors") if isinstance(payload, dict) else None
    if not isinstance(authors, list):
        raise ValueError("author profile manifest requires an authors list")
    return validate_author_works(authors)


def _profile_reason(aweme: dict[str, Any]) -> str:
    error = str(aweme.get("creator_profile_error") or "")
    if error == "creator_profile_empty" or not error:
        return "profile_empty"
    return "profile_unavailable"


async def observe_work(crawler: Any, author_id: str, aweme_id: str, semaphore: asyncio.Semaphore) -> dict[str, Any]:
    detail = await crawler.get_aweme_detail(aweme_id, semaphore)
    reason = detail_failure_reason(detail, aweme_id)
    if reason:
        return {"reason": reason}
    author = detail.get("author") or {}
    if platform_user_id(author.get("uid")) != platform_user_id(author_id):
        return {"reason": "author_mismatch"}
    enriched = await crawler.enrich_aweme_creator(detail)
    blocker = str(enriched.get("creator_profile_runtime_error") or "")
    if blocker:
        return {"runtime_blocker": blocker}
    profile = enriched.get("creator_profile")
    if not isinstance(profile, dict) or not profile:
        return {"reason": _profile_reason(enriched if isinstance(enriched, dict) else {})}
    fields = author_profile_fields(author, profile)
    if not has_fillable_fields(fields):
        return {"reason": "profile_insufficient"}
    return {
        "observation": {
            "author_platform_id": platform_user_id(author_id),
            "source_aweme_id": aweme_id,
            "fields": {key: fields[key] for key in FILLABLE_COLUMNS if key in fields},
            "author_verify_evidence": fields.get("author_verify_evidence") or {},
        }
    }


async def collect_author_profiles(
    crawler: Any,
    authors: list[dict[str, Any]],
    *,
    progress: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """按冻结顺序取作品。运行级错误立刻返回已完成的观测，不继续后面的作者。"""

    observations: list[dict[str, Any]] = []
    unrecovered: list[dict[str, str]] = []
    semaphore = asyncio.Semaphore(1)
    for author in validate_author_works(authors):
        if progress is not None:
            progress()
        author_id = author["author_platform_id"]
        failure = "empty_detail"
        chosen: dict[str, Any] | None = None
        for work in author["works"]:
            try:
                outcome = await observe_work(crawler, author_id, work["aweme_id"], semaphore)
            except Exception as exc:
                code = str(getattr(exc, "code", "") or "")
                if code:
                    return _stopped(observations, unrecovered, code)
                return _stopped(observations, unrecovered, "browser_launch_failed")
            blocker = str(outcome.get("runtime_blocker") or "")
            if blocker:
                return _stopped(observations, unrecovered, blocker)
            observation = outcome.get("observation")
            if isinstance(observation, dict):
                chosen = observation
                break
            failure = str(outcome.get("reason") or failure)
        if chosen is None:
            unrecovered.append({"author_platform_id": author_id, "reason": failure})
        else:
            observations.append(chosen)
    return {"observations": observations, "unrecovered": unrecovered, "runtime_blocker": ""}


def _stopped(
    observations: list[dict[str, Any]],
    unrecovered: list[dict[str, str]],
    blocker: str,
) -> dict[str, Any]:
    return {
        "observations": observations,
        "unrecovered": unrecovered,
        "runtime_blocker": blocker,
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
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = public_payload(payload)
    path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def run_worker(args: argparse.Namespace) -> int:
    from trippostcollect.application.collection import behavior_environment
    from trippostcollect.platforms.douyin.core import DouYinCrawler
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
        progress_path.write_text(str(progress_path.stat().st_size + 1 if progress_path.exists() else 1), encoding="utf-8")

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
    _write_result(output_path, result if isinstance(result, dict) else _stopped([], [], "browser_launch_failed"))
    print(json.dumps({"child_summary": str(output_path.resolve())}, ensure_ascii=False), flush=True)
    return 0


def run_middle(args: argparse.Namespace) -> int:
    from trippostcollect.core.paths import PROJECT_ROOT
    from trippostcollect.runtime.behavior import HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS
    from trippostcollect.runtime.process import NO_PROGRESS_WATCHDOG_SECONDS, run_command

    output_path = Path(args.output).expanduser()
    load_manifest(Path(args.targets).expanduser())
    command = [
        sys.executable,
        "-m",
        "trippostcollect.platforms.douyin.profile_backfill",
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
    log_dir = output_path.parent / "logs"
    result = run_command(
        command,
        PROJECT_ROOT,
        NO_PROGRESS_WATCHDOG_SECONDS,
        log_dir,
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
