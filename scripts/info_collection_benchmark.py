#!/usr/bin/env python3
"""Benchmark configured crawl targets importing into web_posts."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trippostcollect.core.paths import (
    DEFAULT_CONFIG,
    DEFAULT_DB,
    LOCAL_MEDIA_ROOT,
    PROJECT_ROOT,
    ensure_dir,
    ensure_parent,
    runtime_dir,
)
from trippostcollect.core.scope import require_qingdao_topic_keyword
from trippostcollect.db.bootstrap import bootstrap_database
from mediacrawler_behavior import HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS


ROOT = PROJECT_ROOT
DEFAULT_OUTPUT = runtime_dir("info_collection_benchmarks")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run crawl/import benchmark for configured targets.")
    parser.add_argument("--keyword", default="青岛旅游", help="Qingdao keyword for structured-search targets.")
    parser.add_argument(
        "--per-target",
        type=int,
        default=0,
        help="Override the formal new-post target. 0 reads target_new_posts from crawl_targets.json.",
    )
    parser.add_argument(
        "--fetch-multiplier",
        type=int,
        default=0,
        help="Override candidate hard limit. 0 reads candidate_hard_limit from crawl_targets.json.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Crawl target config JSON.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT), help="Benchmark output root.")
    parser.add_argument("--sites", nargs="+", help="Optional site_key filter.")
    parser.add_argument("--timeout-per-target", type=int, default=300, help="Minimum MediaCrawler timeout per target.")
    parser.add_argument("--headless", action="store_true", help="Force MediaCrawler headless mode.")
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f%z")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(value: Any, path: Path) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def selected_jobs(config: dict[str, Any], sites: list[str] | None) -> list[dict[str, Any]]:
    jobs = [job for job in config.get("jobs") or [] if job.get("enabled", True)]
    if sites:
        site_filter = set(sites)
        jobs = [job for job in jobs if job.get("site_key") in site_filter or job.get("job_key") in site_filter]
    return jobs


def db_keyword_count(db_path: Path, platform_key: str, keyword: str) -> int:
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM web_posts WHERE platform_key=? AND keyword=?",
            (platform_key, keyword),
        ).fetchone()
    return int(row[0] or 0)


def db_source_count(db_path: Path, platform_key: str, source_type: str) -> int:
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM web_posts WHERE platform_key=? AND source_type=?",
            (platform_key, source_type),
        ).fetchone()
    return int(row[0] or 0)


def latest_summary(output_root: Path) -> Path | None:
    summaries = sorted(output_root.glob("*/summary.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    return summaries[0] if summaries else None


def platform_summary(summary: dict[str, Any], platform_key: str) -> dict[str, Any]:
    for record in summary.get("records") or []:
        if record.get("platform") == platform_key:
            return record
    return {}


def behavior_profile_name(job: dict[str, Any]) -> str:
    profile = job.get("behavior_profile") or {}
    return str(profile.get("name") or "")


def ctf_capture_meta_paths(summary: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for record in summary.get("records") or []:
        if not isinstance(record, dict):
            continue
        artifact_dir = record.get("artifact_dir")
        if not artifact_dir:
            continue
        candidate = Path(str(artifact_dir)) / "capture_meta.json"
        if candidate.exists():
            paths.append(str(candidate))
    return paths


def load_subprocess_json(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8").strip()
        return json.loads(text) if text else {}
    except (OSError, json.JSONDecodeError):
        return {}


def run_mediacrawler_job(job: dict[str, Any], args: argparse.Namespace, batch_dir: Path, db_path: Path) -> dict[str, Any]:
    params = job.get("params") or {}
    platform = str(params.get("platform") or job["site_key"])
    if platform == "xhs":
        raise ValueError("XHS is independently orchestrated; use scripts/xhs_runner.py")
    target_count = int(args.per_target or params.get("target_new_posts") or 0)
    if target_count <= 0:
        raise ValueError(f"Missing formal target_new_posts for {job['job_key']}")
    candidate_hard_limit = int(params.get("candidate_hard_limit") or target_count)
    if args.fetch_multiplier > 0:
        candidate_hard_limit = max(target_count, target_count * args.fetch_multiplier)
    timeout = max(int(params.get("timeout_per_platform") or 180), int(args.timeout_per_target))
    target_dir = ensure_dir(batch_dir / job["job_key"])
    logs_dir = ensure_dir(target_dir / "logs")
    mc_output = ensure_dir(target_dir / "mediacrawler")
    stdout_path = logs_dir / "stdout.log"
    stderr_path = logs_dir / "stderr.log"
    before_count = db_keyword_count(db_path, platform, args.keyword)
    command = [
        sys.executable,
        str(ROOT / "scripts" / "mediacrawler_crawl.py"),
        "--platforms",
        platform,
        "--keyword",
        args.keyword,
        "--candidate-hard-limit",
        str(candidate_hard_limit),
        "--target-new-posts",
        str(target_count),
        "--timeout-per-platform",
        str(timeout),
        "--login-type",
        str(params.get("login_type") or "cookie"),
        "--db",
        str(db_path),
        "--output-dir",
        str(mc_output),
        "--download-images",
        "--media-root",
        str(LOCAL_MEDIA_ROOT.resolve()),
    ]
    if not args.headless and params.get("headless") is False:
        command.append("--headed")

    started = time.monotonic()
    timed_out = False
    returncode = 0
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        try:
            completed = subprocess.run(
                command,
                cwd=str(ROOT),
                stdout=stdout,
                stderr=stderr,
                text=True,
                timeout=timeout + HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS + 120,
                check=False,
            )
            returncode = int(completed.returncode)
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = 124
            stderr.write(
                f"\n[benchmark] outer timeout after "
                f"{timeout + HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS + 120}s\n"
            )
    elapsed = round(time.monotonic() - started, 2)
    after_count = db_keyword_count(db_path, platform, args.keyword)
    summary_path = latest_summary(mc_output)
    summary = load_json(summary_path) if summary_path else {}
    record = platform_summary(summary, platform)
    output = record.get("output") or {}
    import_result = summary.get("import_result") or {}
    formal_validation = summary.get("formal_validation") or {}
    imported = int(import_result.get("processed_rows") or 0)
    db_written = max(0, after_count - before_count)
    average = round(elapsed / imported, 3) if imported else None
    db_average = round(elapsed / db_written, 3) if db_written else None
    valid_new = int(formal_validation.get("valid_new_count") or 0)
    new_target_met = bool(formal_validation.get("new_target_met")) and valid_new >= target_count and not timed_out
    return {
        "job_key": job["job_key"],
        "site_key": job["site_key"],
        "platform": platform,
        "job_kind": job["job_kind"],
        "status": "completed" if new_target_met else ("timed_out" if timed_out else "new_target_not_met"),
        "ok": bool(record.get("ok")) and new_target_met,
        "requested_new_records": target_count,
        "record_mode": "keyword_search_post",
        "candidate_hard_limit": candidate_hard_limit,
        "processed_import_rows": imported,
        "valid_new_records": valid_new,
        "formal_stop_reason": str(formal_validation.get("stop_reason") or ""),
        "imported_records": db_written,
        "db_keyword_rows_before": before_count,
        "db_keyword_rows_after": after_count,
        "db_keyword_rows_delta": db_written,
        "content_records": int(output.get("content_records") or 0),
        "non_video_content_records": int(output.get("non_video_content_records") or 0),
        "skipped_video_records": int(import_result.get("skipped_video") or 0),
        "parse_errors": int(import_result.get("parse_errors") or 0),
        "elapsed_seconds": elapsed,
        "avg_seconds_per_imported_record": average,
        "avg_seconds_per_db_record": db_average,
        "returncode": returncode,
        "timed_out": timed_out,
        "summary_path": str(summary_path) if summary_path else "",
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }


def run_ctf_resource_job(job: dict[str, Any], args: argparse.Namespace, batch_dir: Path, db_path: Path) -> dict[str, Any]:
    params = job.get("params") or {}
    site = str(job["site_key"])
    timeout = max(60, int(params.get("timeout_seconds") or args.timeout_per_target))
    target_dir = ensure_dir(batch_dir / job["job_key"])
    logs_dir = ensure_dir(target_dir / "logs")
    ctf_output = ensure_dir(target_dir / "ctf_resource")
    stdout_path = logs_dir / "stdout.log"
    stderr_path = logs_dir / "stderr.log"
    import_stdout_path = logs_dir / "import_stdout.log"
    import_stderr_path = logs_dir / "import_stderr.log"
    before_count = db_source_count(db_path, site, "ctf_capture_page")
    command = [
        sys.executable,
        str(ROOT / "scripts" / "ctf_resource_crawl.py"),
        "--sites",
        site,
        "--output-dir",
        str(ctf_output),
        "--scrapling-preflight",
        str(params.get("scrapling_preflight") or "auto"),
        "--max-image-save",
        str(int(params.get("max_image_save") or 3)),
        "--max-scrolls",
        str(int(params.get("max_scrolls") or 2)),
        "--timeout",
        str(timeout * 1000),
    ]
    profile = behavior_profile_name(job)
    if profile:
        command.extend(["--behavior-profile", profile])
    if args.headless or params.get("headless", False):
        command.append("--headless")

    started = time.monotonic()
    timed_out = False
    returncode = 0
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        try:
            completed = subprocess.run(
                command,
                cwd=str(ROOT),
                stdout=stdout,
                stderr=stderr,
                text=True,
                timeout=timeout + 120,
                check=False,
            )
            returncode = int(completed.returncode)
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = 124
            stderr.write(f"\n[benchmark] outer timeout after {timeout + 120}s\n")

    summary_path = latest_summary(ctf_output)
    summary = load_json(summary_path) if summary_path else {}
    capture_paths = ctf_capture_meta_paths(summary)
    import_returncode: int | None = None
    if capture_paths and not timed_out:
        import_command = [
            sys.executable,
            str(ROOT / "scripts" / "import_ctf_captures.py"),
            "--db",
            str(db_path),
            "--capture-meta",
            *capture_paths,
        ]
        with import_stdout_path.open("w", encoding="utf-8") as stdout, import_stderr_path.open("w", encoding="utf-8") as stderr:
            completed = subprocess.run(
                import_command,
                cwd=str(ROOT),
                stdout=stdout,
                stderr=stderr,
                text=True,
                check=False,
            )
            import_returncode = int(completed.returncode)

    elapsed = round(time.monotonic() - started, 2)
    after_count = db_source_count(db_path, site, "ctf_capture_page")
    import_result = load_subprocess_json(import_stdout_path)
    processed = int(import_result.get("post_rows") or 0)
    db_written = max(0, after_count - before_count)
    average = round(elapsed / processed, 3) if processed else None
    db_average = round(elapsed / db_written, 3) if db_written else None
    return {
        "job_key": job["job_key"],
        "site_key": site,
        "platform": site,
        "job_kind": job["job_kind"],
        "status": "timed_out" if timed_out else ("completed" if processed else "no_web_posts_imported"),
        "ok": bool(processed) and not timed_out,
        "requested_records": 1,
        "record_mode": "page_capture_to_post",
        "processed_import_rows": processed,
        "imported_records": db_written,
        "capture_rows": int(import_result.get("imported") or 0),
        "post_image_rows": int(import_result.get("post_image_rows") or 0),
        "db_source_rows_before": before_count,
        "db_source_rows_after": after_count,
        "db_source_rows_delta": db_written,
        "content_records": int(summary.get("ok_count") or 0),
        "non_video_content_records": int(summary.get("ok_count") or 0),
        "skipped_video_records": int((summary.get("media_policy") or {}).get("skipped_video_targets") or 0),
        "parse_errors": int(import_result.get("failed") or 0),
        "elapsed_seconds": elapsed,
        "avg_seconds_per_imported_record": average,
        "avg_seconds_per_db_record": db_average,
        "returncode": returncode,
        "import_returncode": import_returncode,
        "timed_out": timed_out,
        "summary_path": str(summary_path) if summary_path else "",
        "capture_meta_paths": capture_paths,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "import_stdout_log": str(import_stdout_path),
        "import_stderr_log": str(import_stderr_path),
    }


def write_markdown(summary: dict[str, Any], path: Path) -> None:
    lines = [
        "# 信息收集入库效率基准测试",
        "",
        f"- 结构化搜索关键词：`{summary['keyword']}`",
        f"- 结构化搜索每目标覆盖值：`{summary['per_target_override'] or '读取正式配置'}`",
        f"- 数据库：`{summary['db']}`",
        "",
        "| 目标 | 类型 | 模式 | 状态 | DB新增 | 处理行 | 非视频内容 | 跳过视频 | 用时(s) | 平均(s/处理条) |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for record in summary["records"]:
        avg = record.get("avg_seconds_per_imported_record")
        lines.append(
            "| {site} | {kind} | {mode} | {status} | {imported} | {processed} | {non_video} | {skipped_video} | {elapsed} | {avg} |".format(
                site=record["site_key"],
                kind=record["job_kind"],
                mode=record.get("record_mode", ""),
                status=record["status"],
                imported=record.get("imported_records", 0),
                processed=record.get("processed_import_rows", 0),
                non_video=record.get("non_video_content_records", 0),
                skipped_video=record.get("skipped_video_records", 0),
                elapsed=record.get("elapsed_seconds", 0),
                avg="" if avg is None else avg,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    try:
        args.keyword = require_qingdao_topic_keyword(args.keyword)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.per_target < 0 or args.fetch_multiplier < 0:
        raise SystemExit("--per-target and --fetch-multiplier must be zero or positive")
    db_path = ensure_parent(Path(args.db).expanduser())
    bootstrap_database(db_path)
    config = load_json(Path(args.config).expanduser())
    batch_dir = ensure_dir(Path(args.output_dir).expanduser() / run_id())
    records: list[dict[str, Any]] = []
    for job in selected_jobs(config, args.sites):
        if job.get("job_kind") == "mediacrawler_search":
            records.append(run_mediacrawler_job(job, args, batch_dir, db_path))
        elif job.get("job_kind") == "ctf_resource_crawl":
            records.append(run_ctf_resource_job(job, args, batch_dir, db_path))
        else:
            raise ValueError(f"Unsupported job_kind in current crawl structure: {job.get('job_kind')}")
    total_imported = sum(int(record.get("imported_records") or 0) for record in records)
    total_processed = sum(int(record.get("processed_import_rows") or 0) for record in records)
    total_elapsed = sum(float(record.get("elapsed_seconds") or 0) for record in records if record.get("processed_import_rows"))
    summary = {
        "started_at": batch_dir.name,
        "finished_at": utc_now(),
        "keyword": args.keyword,
        "per_target_override": args.per_target or None,
        "db": str(db_path),
        "batch_dir": str(batch_dir),
        "target_count": len(records),
        "structured_search_count": sum(1 for record in records if record.get("record_mode") == "keyword_search_post"),
        "page_capture_count": sum(1 for record in records if record.get("record_mode") == "page_capture_to_post"),
        "total_imported_records": total_imported,
        "total_processed_records": total_processed,
        "total_elapsed_seconds_for_importing_targets": round(total_elapsed, 2),
        "overall_avg_seconds_per_imported_record": round(total_elapsed / total_processed, 3) if total_processed else None,
        "records": records,
    }
    summary_path = batch_dir / "summary.json"
    report_path = batch_dir / "summary.md"
    dump_json(summary, summary_path)
    write_markdown(summary, report_path)
    compact = {
        "summary": str(summary_path),
        "report": str(report_path),
        "db": str(db_path),
        "keyword": args.keyword,
        "per_target_override": args.per_target or None,
        "total_imported_records": total_imported,
        "total_processed_records": total_processed,
        "overall_avg_seconds_per_imported_record": summary["overall_avg_seconds_per_imported_record"],
        "records": [
            {
                "site": item["site_key"],
                "mode": item.get("record_mode", ""),
                "status": item["status"],
                "imported": item.get("imported_records", 0),
                "processed_import_rows": item.get("processed_import_rows", 0),
                "avg_seconds_per_record": item.get("avg_seconds_per_imported_record"),
            }
            for item in records
        ],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))
    return 0 if records and all(record.get("ok") for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
