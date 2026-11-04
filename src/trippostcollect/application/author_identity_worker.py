"""作者身份 child：套用现有策略门禁和看门狗后，启动平台 detail 入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from trippostcollect.application.collection import behavior_environment
from trippostcollect.application.contracts import PLATFORMS
from trippostcollect.application.policy import CrawlPolicyBlocked, site_request_guard
from trippostcollect.application.reporting import (
    collect_behavior_validation,
    terminal_summary_envelope,
)
from trippostcollect.core.paths import PROJECT_ROOT
from trippostcollect.platforms.registry import get_site
from trippostcollect.records.author_observe import OBSERVATIONS_NAME
from trippostcollect.runtime.behavior import load_behavior_evidence
from trippostcollect.runtime.process import (
    NO_PROGRESS_WATCHDOG_SECONDS,
    OperatorInterrupt,
    run_command,
    run_main_with_operator_interrupt,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one author-identity platform batch.")
    parser.add_argument("--platform", required=True, choices=("douyin", "weibo", "zhihu"))
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--save-data-path", required=True)
    parser.add_argument("--headless", choices=("true", "false"), default="true")
    return parser.parse_args(argv)


def platform_entry_command(args: argparse.Namespace) -> list[str]:
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
        str(Path(args.save_data_path)),
        "--start",
        "1",
        "--max_concurrency_num",
        "1",
        "--enable_ip_proxy",
        "false",
    ]
    if args.platform == "zhihu":
        command.extend(["--enable_cdp_mode", "true"])
    return command


def batch_progress_paths(save_path: Path, evidence_path: Path) -> list[Path]:
    """观察文件本身计入无进展看门狗；目录只统计 jsonl，不能代替这个文件。"""

    return [save_path, evidence_path, evidence_path.with_name(OBSERVATIONS_NAME)]


def _write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            terminal_summary_envelope(
                summary_path=path,
                report_path=path.with_suffix(".md"),
                batch_dir=path.parent,
                summary=summary,
            ),
            ensure_ascii=False,
        ),
        flush=True,
    )


def _batch_summary(
    *,
    args: argparse.Namespace,
    evidence_path: Path,
    policy_events: list[dict[str, Any]],
    run: dict[str, Any] | None,
    stop_reason: str,
    wait_seconds: int = 0,
) -> dict[str, Any]:
    record = {
        "platform": args.platform,
        "behavior_evidence": load_behavior_evidence(evidence_path),
        "policy_events": policy_events,
    }
    behavior_validation = collect_behavior_validation(
        [record],
        [args.platform],
        args.keyword,
    )
    platform_returncode = None if run is None else run.get("returncode")
    timeout_reason = str((run or {}).get("timeout_reason") or "")
    if not stop_reason and timeout_reason:
        stop_reason = timeout_reason
    classification: dict[str, Any] = {}
    if stop_reason == "policy_blocked":
        classification = {
            "status": "retry_wait",
            "failure_type": "policy_blocked",
            "retryable": True,
            "wait_seconds": int(wait_seconds),
            "reason": "policy_blocked",
        }
    observations = evidence_path.with_name(OBSERVATIONS_NAME)
    ok = (
        stop_reason == ""
        and platform_returncode in (None, 0)
        and bool(behavior_validation.get("behavior_ok"))
        and bool(behavior_validation.get("policy_ok"))
    )
    return {
        "platform": args.platform,
        "keyword": args.keyword,
        "behavior_validation": behavior_validation,
        "policy_events": policy_events,
        "formal_validation": {
            "stop_reason": stop_reason,
            "skipped_candidate_failures": [],
        },
        "failure_classification": classification,
        "platform_returncode": platform_returncode,
        "import_completion_met": ok,
        "import_result": {"skipped": True, "reason": "author_identity"},
        "author_identity_observations": str(observations) if observations.is_file() else "",
    }


def run_supervised_batch(
    args: argparse.Namespace,
    command: list[str],
    *,
    cleanup_grace_seconds: float | None = None,
    inactivity_timeout_seconds: float = NO_PROGRESS_WATCHDOG_SECONDS,
) -> int:
    """跑一批身份取数。SIGINT/SIGTERM/SIGHUP 由外层转成一次 SIGTERM 收束 entry。"""

    save_path = Path(args.save_data_path)
    log_dir = save_path.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = log_dir / "behavior_evidence.json"
    summary_path = log_dir / "summary.json"
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
    policy_events: list[dict[str, Any]] = []
    run: dict[str, Any] | None = None
    stop_reason = ""
    wait_seconds = 0
    run_kwargs: dict[str, Any] = {}
    if cleanup_grace_seconds is not None:
        run_kwargs["cleanup_grace_seconds"] = cleanup_grace_seconds
    try:
        with site_request_guard(
            get_site(args.platform),
            label="mediacrawler:formal_platform_session",
        ) as event:
            policy_events.append(event)
            run = run_command(
                command,
                PROJECT_ROOT,
                inactivity_timeout_seconds,
                log_dir,
                extra_env=extra_env,
                progress_paths=batch_progress_paths(save_path, evidence_path),
                **run_kwargs,
            )
    except CrawlPolicyBlocked as exc:
        policy_events.append(exc.event)
        stop_reason = "policy_blocked"
        wait_seconds = int(exc.event.get("wait_seconds") or 0)
    except OperatorInterrupt:
        _write_summary(
            summary_path,
            _batch_summary(
                args=args,
                evidence_path=evidence_path,
                policy_events=policy_events,
                run=run,
                stop_reason="operator_interrupt",
            ),
        )
        raise
    summary = _batch_summary(
        args=args,
        evidence_path=evidence_path,
        policy_events=policy_events,
        run=run,
        stop_reason=stop_reason,
        wait_seconds=wait_seconds,
    )
    _write_summary(summary_path, summary)
    if stop_reason == "policy_blocked":
        return 2
    return int((run or {}).get("returncode") or 0)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_main_with_operator_interrupt(
        lambda: run_supervised_batch(args, platform_entry_command(args))
    )


if __name__ == "__main__":
    raise SystemExit(main())
