#!/usr/bin/env python3
"""Validate and refresh every persisted browser login through one entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from ctf_login_warmup import LOGIN_CHECKERS, warmup_one as warmup_page_site
from mediacrawler_login_warmup import (
    ALIASES as MEDIACRAWLER_ALIASES,
    PLATFORMS as MEDIACRAWLER_PLATFORMS,
    warmup_one as warmup_mediacrawler,
)
from trippostcollect.core.paths import LOGIN_WARMUP_OUTPUT, MEDIACRAWLER_DIR, ensure_dir


TARGETS: dict[str, dict[str, str]] = {
    **{
        key: {"kind": "mediacrawler", "label": str(config["label"])}
        for key, config in MEDIACRAWLER_PLATFORMS.items()
    },
    **{
        key: {"kind": "page", "label": str(config["label"])}
        for key, config in LOGIN_CHECKERS.items()
    },
}

ALIASES = {
    **MEDIACRAWLER_ALIASES,
    "douban": "douban_group",
    "douban_group": "douban_group",
    "豆瓣": "douban_group",
    "豆瓣小组": "douban_group",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate persisted login state for all crawl platforms and wait for manual repair when expired.",
    )
    parser.add_argument(
        "--targets",
        nargs="+",
        default=["all"],
        help="Login targets or aliases. Defaults to all supported targets.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=600, help="Maximum manual-login wait per target.")
    parser.add_argument("--output-dir", default=str(LOGIN_WARMUP_OUTPUT), help="Unified login report root.")
    parser.add_argument("--browser-path", help="Explicit Chrome/Chromium executable for every target.")
    parser.add_argument("--list-targets", action="store_true", help="Print supported targets and exit.")
    return parser.parse_args()


def selected_targets(values: list[str]) -> list[str]:
    if "all" in values:
        return list(TARGETS)
    selected: list[str] = []
    for value in values:
        key = ALIASES.get(value, value)
        if key not in TARGETS:
            raise SystemExit(f"Unknown login target: {value}. Choices: {', '.join(TARGETS)}")
        if key not in selected:
            selected.append(key)
    return selected


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%z")


def implementation_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        timeout_seconds=args.timeout_seconds,
        output_dir=args.output_dir,
        browser_path=args.browser_path,
        no_close_on_success=False,
        skip_reopen_verify=False,
    )


def error_record(target: str, exc: Exception) -> dict[str, Any]:
    config = TARGETS[target]
    return {
        "target": target,
        "target_kind": config["kind"],
        "label": config["label"],
        "ok": False,
        "session_ok": False,
        "persisted_ok": False,
        "initial_ok": False,
        "login_refreshed": False,
        "profile_dir": "",
        "verified_at": utc_iso(),
        "error": f"{type(exc).__name__}: {exc}",
    }


async def run_target(
    playwright: Any,
    target: str,
    batch_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    config = TARGETS[target]
    child_args = implementation_args(args)
    try:
        if config["kind"] == "mediacrawler":
            result = await warmup_mediacrawler(playwright, target, batch_dir, child_args)
        else:
            result = await warmup_page_site(playwright, target, batch_dir, child_args)
        normalized = dict(result)
        normalized["target"] = target
        normalized["target_kind"] = config["kind"]
        return normalized
    except Exception as exc:
        result = error_record(target, exc)
        out_path = batch_dir / f"{target}.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[login] {config['label']} failed: {result['error']} -> {out_path}", flush=True)
        return result


def markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def markdown_summary(summary: dict[str, Any]) -> str:
    lines = [
        "# 统一登录检查报告",
        "",
        f"- 状态：`{summary['status']}`",
        f"- 开始：`{summary['started_at']}`",
        f"- 完成：`{summary['finished_at']}`",
        f"- 通过：`{summary['ok_count']}`",
        f"- 失败：`{summary['failed_count']}`",
        "",
        "| target | 类型 | 状态 | 原状态有效 | 已刷新 | 重开有效 | profile | 错误 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in summary["records"]:
        lines.append(
            "| {target} | {kind} | {status} | {initial} | {refreshed} | {persisted} | {profile} | {error} |".format(
                target=markdown_cell(item.get("target")),
                kind=markdown_cell(item.get("target_kind")),
                status="ok" if item.get("ok") else "failed",
                initial=markdown_cell(item.get("initial_ok")),
                refreshed=markdown_cell(item.get("login_refreshed")),
                persisted=markdown_cell(item.get("persisted_ok")),
                profile=markdown_cell(item.get("profile_dir")),
                error=markdown_cell(item.get("error", "")),
            )
        )
    lines.append("")
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be greater than zero")
    targets = selected_targets(args.targets)
    if any(TARGETS[target]["kind"] == "mediacrawler" for target in targets) and not MEDIACRAWLER_DIR.is_dir():
        raise SystemExit(f"MediaCrawler is missing: {MEDIACRAWLER_DIR}")

    started_at = utc_iso()
    batch_dir = ensure_dir(Path(args.output_dir).expanduser() / utc_stamp())
    records: list[dict[str, Any]] = []
    async with async_playwright() as playwright:
        for target in targets:
            records.append(await run_target(playwright, target, batch_dir, args))

    failed_count = sum(1 for item in records if not item.get("ok"))
    summary = {
        "status": "completed" if failed_count == 0 else "failed",
        "started_at": started_at,
        "finished_at": utc_iso(),
        "batch_dir": str(batch_dir),
        "target_count": len(records),
        "ok_count": len(records) - failed_count,
        "failed_count": failed_count,
        "records": records,
    }
    summary_path = batch_dir / "summary.json"
    report_path = batch_dir / "summary.md"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(markdown_summary(summary), encoding="utf-8")
    print(
        json.dumps(
            {
                "summary": str(summary_path),
                "report": str(report_path),
                "status": summary["status"],
                "ok_count": summary["ok_count"],
                "failed_count": summary["failed_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if failed_count == 0 else 1


def main() -> int:
    args = parse_args()
    if args.list_targets:
        for key, config in TARGETS.items():
            print(f"{key}\t{config['kind']}\t{config['label']}")
        return 0
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
