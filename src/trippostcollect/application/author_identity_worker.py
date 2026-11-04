"""作者身份 child：套用现有策略门禁和看门狗后，启动平台 detail 入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from trippostcollect.application.collection import behavior_environment
from trippostcollect.application.contracts import PLATFORMS
from trippostcollect.application.policy import site_request_guard
from trippostcollect.core.paths import PROJECT_ROOT
from trippostcollect.platforms.registry import get_site
from trippostcollect.runtime.process import NO_PROGRESS_WATCHDOG_SECONDS, run_command


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one author-identity platform batch.")
    parser.add_argument("--platform", required=True, choices=("douyin", "weibo", "zhihu"))
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--save-data-path", required=True)
    parser.add_argument("--headless", choices=("true", "false"), default="true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    save_path = Path(args.save_data_path)
    log_dir = save_path.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = log_dir / "behavior_evidence.json"
    targets_source = save_path / "author_identity_targets.json"
    if targets_source.is_file():
        (log_dir / "author_identity_targets.json").write_bytes(targets_source.read_bytes())
    extra_env = {
        "TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS": "1",
        "TRIPPOSTCOLLECT_POST_REPAIR": "1",
        "TRIPPOSTCOLLECT_DB_PATH": str(Path(args.db).expanduser().resolve()),
        **behavior_environment(evidence_path, "social_high_risk"),
    }
    if args.platform == "douyin":
        extra_env["TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_FALLBACK"] = "1"
        extra_env["TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_TIMEOUT_MS"] = "30000"
    if args.platform == "zhihu":
        extra_env["TRIPPOSTCOLLECT_SHARE_CDP_PROFILE"] = "1"
        extra_env["TRIPPOSTCOLLECT_CLEAN_BROWSER_TABS"] = "1"
        extra_env["TRIPPOSTCOLLECT_ZHIHU_INITIAL_SETTLE_SECONDS"] = "8"
    command = [
        sys.executable,
        "-P",
        "-m",
        "trippostcollect.platforms.entry",
        "--platform",
        PLATFORMS[args.platform]["mediacrawler"],
        "--lt",
        "cookie",
        "--type",
        "detail",
        "--keywords",
        args.keyword,
        "--get_comment",
        "false",
        "--get_sub_comment",
        "false",
        "--get_media",
        "false",
        "--headless",
        args.headless,
        "--save_data_option",
        "jsonl",
        "--save_data_path",
        str(save_path),
        "--start",
        "1",
        "--max_concurrency_num",
        "1",
        "--enable_ip_proxy",
        "false",
    ]
    if args.platform == "zhihu":
        command.extend(["--enable_cdp_mode", "true"])
    with site_request_guard(get_site(args.platform), label="mediacrawler:formal_platform_session"):
        result = run_command(
            command,
            PROJECT_ROOT,
            NO_PROGRESS_WATCHDOG_SECONDS,
            log_dir,
            extra_env=extra_env,
            progress_paths=[save_path, evidence_path],
        )
    return int(result.get("returncode") or 0)


if __name__ == "__main__":
    raise SystemExit(main())
