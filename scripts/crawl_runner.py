#!/usr/bin/env python3
"""Deterministic long-running crawl runner controlled by config and SQLite state."""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from db_bootstrap import bootstrap_connection
from failure_classifier import classify_attempt, extract_stdout_json
from project_paths import (
    CRAWL_RUNNER_RUNTIME,
    DEFAULT_CONFIG,
    DEFAULT_DB,
    PROJECT_ROOT,
    ensure_dir,
    ensure_parent,
)


ROOT = PROJECT_ROOT
DEFAULT_RUN_ROOT = CRAWL_RUNNER_RUNTIME
RANDOM = random.SystemRandom()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run configured crawl jobs with deterministic scheduling.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Crawl target config JSON.")
    parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT), help="Directory for run summaries.")
    parser.add_argument("--max-jobs", type=int, default=3, help="Maximum due jobs to run in this invocation.")
    parser.add_argument("--site", help="Optional site_key filter.")
    parser.add_argument("--kind", help="Optional job_kind filter.")
    parser.add_argument("--job-key", help="Run one specific job key.")
    parser.add_argument("--sync-only", action="store_true", help="Only sync config into crawl_jobs.")
    parser.add_argument("--no-sync-config", action="store_true", help="Do not sync config before selecting jobs.")
    parser.add_argument("--dry-run", action="store_true", help="Plan jobs and commands without executing them.")
    parser.add_argument("--headless", action="store_true", help="Pass headless mode to browser jobs.")
    parser.add_argument("--headful", action="store_true", help="Pass headed mode to browser jobs when supported.")
    parser.add_argument("--no-throttle", action="store_true", help="Forward --no-throttle to child scripts.")
    parser.add_argument("--no-import", action="store_true", help="Do not import capture results after successful jobs.")
    return parser.parse_args()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def tail(text: str, limit: int = 6000) -> str:
    return text[-limit:] if len(text) > limit else text


def select_due_jobs(conn: sqlite3.Connection, args: argparse.Namespace) -> list[sqlite3.Row]:
    clauses = ["enabled = 1"]
    params: list[Any] = []
    if args.job_key:
        clauses.append("job_key = ?")
        params.append(args.job_key)
    else:
        clauses.append("status IN ('pending', 'completed', 'retry_wait')")
        clauses.append("next_run_at <= ?")
        params.append(iso())
        if args.site:
            clauses.append("site_key = ?")
            params.append(args.site)
        if args.kind:
            clauses.append("job_kind = ?")
            params.append(args.kind)
    query = f"""
        SELECT * FROM crawl_jobs
        WHERE {" AND ".join(clauses)}
        ORDER BY priority ASC, next_run_at ASC, id ASC
        LIMIT ?
    """
    params.append(max(1, args.max_jobs))
    return list(conn.execute(query, params).fetchall())


def behavior_profile_name(row: sqlite3.Row) -> str:
    data = json.loads(row["behavior_profile_json"] or "{}")
    return str(data.get("name") or row["site_key"] or "conservative")


def params_for(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["params_json"] or "{}")


def add_flag(command: list[str], flag: str, value: Any | None = None) -> None:
    command.append(flag)
    if value is not None:
        command.append(str(value))


def build_command(row: sqlite3.Row, args: argparse.Namespace) -> list[str]:
    params = params_for(row)
    site = row["site_key"]
    url = row["target_url"]
    kind = row["job_kind"]
    profile = behavior_profile_name(row)

    if kind == "mediacrawler_search":
        platform = str(params.get("platform") or site)
        if params.get("get_media"):
            raise ValueError(f"Generic media/video downloads are disabled for job {row['job_key']}")
        download_images = bool(params.get("download_images"))
        if download_images and platform != "xhs":
            raise ValueError(f"download_images is only allowed for xhs job {row['job_key']}")
        command = [sys.executable, str(ROOT / "scripts" / "mediacrawler_crawl.py"), "--platforms", platform]
        add_flag(command, "--keyword", params.get("keyword", "济南旅游"))
        add_flag(command, "--timeout-per-platform", params.get("timeout_per_platform", 180))
        add_flag(command, "--mediacrawler-max-notes", params.get("max_notes", 1))
        add_flag(command, "--login-type", params.get("login_type", "cookie"))
        add_flag(command, "--db", args.db)
        if download_images:
            command.append("--download-images")
        if args.headful or (params.get("headless") is False and not args.headless):
            command.append("--headed")
        if args.no_import:
            command.append("--no-import")
    elif kind == "ctf_resource_crawl":
        command = [sys.executable, str(ROOT / "scripts" / "ctf_resource_crawl.py"), "--sites", site]
        add_flag(command, "--scrapling-preflight", params.get("scrapling_preflight", "auto"))
        add_flag(command, "--max-image-save", params.get("max_image_save", 20))
        add_flag(command, "--max-scrolls", params.get("max_scrolls", 4))
        add_flag(command, "--behavior-profile", profile)
        if args.headless or params.get("headless", False):
            command.append("--headless")
    else:
        raise ValueError(f"Unsupported or retired job_kind: {kind}")

    if args.no_throttle:
        command.append("--no-throttle")
    return command


def latest_attempt_no(conn: sqlite3.Connection, job_id: int) -> int:
    row = conn.execute("SELECT max(attempt_no) FROM crawl_attempts WHERE job_id = ?", (job_id,)).fetchone()
    return int(row[0] or 0) + 1


def insert_attempt(conn: sqlite3.Connection, row: sqlite3.Row, run_id: str, command: list[str]) -> int:
    attempt_no = latest_attempt_no(conn, int(row["id"]))
    started_at = iso()
    cur = conn.execute(
        """
        INSERT INTO crawl_attempts (
            job_id, run_id, attempt_no, status, command_json, started_at
        )
        VALUES (?, ?, ?, 'running', ?, ?)
        """,
        (row["id"], run_id, attempt_no, json_dump(command), started_at),
    )
    conn.execute("UPDATE crawl_jobs SET status='leased', updated_at=datetime('now') WHERE id=?", (row["id"],))
    conn.commit()
    return int(cur.lastrowid)


def find_artifact_paths(stdout: str, row: sqlite3.Row) -> tuple[str, list[str], str | None]:
    stdout_json = extract_stdout_json(stdout)
    artifact_dirs: list[str] = []
    summary_path: str | None = None
    for key in ("artifact_dir", "batch_dir"):
        value = stdout_json.get(key)
        if value:
            artifact_dirs.append(str(value))
    for record in stdout_json.get("records") or []:
        if isinstance(record, dict):
            for key in ("artifact_dir", "capture_dir"):
                if record.get(key):
                    artifact_dirs.append(str(record[key]))
    match = re.search(r"Summary:\s*(.+)", stdout)
    if match:
        summary_path = match.group(1).strip()
        try:
            summary = load_json(Path(summary_path))
            if summary.get("batch_dir"):
                artifact_dirs.append(str(summary["batch_dir"]))
            for record in summary.get("records") or []:
                if isinstance(record, dict) and record.get("artifact_dir"):
                    artifact_dirs.append(str(record["artifact_dir"]))
        except Exception:
            pass

    for match in re.finditer(r"Saved artifacts to:\s*(.+)", stdout):
        artifact_dirs.append(match.group(1).strip())

    capture_meta_paths: list[str] = []
    seen: set[str] = set()
    for artifact in artifact_dirs:
        path = Path(artifact)
        candidates = [path / "capture_meta.json"] if path.is_dir() else []
        if path.is_dir():
            candidates.extend(path.glob("*/capture_meta.json"))
        for candidate in candidates:
            resolved = str(candidate)
            if candidate.exists() and resolved not in seen:
                seen.add(resolved)
                capture_meta_paths.append(resolved)

    artifact_dir = artifact_dirs[0] if artifact_dirs else ""
    return artifact_dir, capture_meta_paths, summary_path


def load_first_meta(paths: list[str]) -> dict[str, Any]:
    if not paths:
        return {}
    try:
        return load_json(Path(paths[0]))
    except Exception:
        return {}


def import_capture_results(paths: list[str], db_path: Path) -> dict[str, Any]:
    if not paths:
        return {"skipped": True, "reason": "no_capture_meta_paths"}
    command = [
        sys.executable,
        str(ROOT / "scripts" / "import_ctf_captures.py"),
        "--db",
        str(db_path),
        "--capture-meta",
        *paths,
    ]
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    return {
        "command": command,
        "exit_code": completed.returncode,
        "stdout_tail": tail(completed.stdout),
        "stderr_tail": tail(completed.stderr),
        "ok": completed.returncode == 0,
    }


def next_run_time(row: sqlite3.Row, classification: dict[str, Any], config: dict[str, Any]) -> str | None:
    status = classification["status"]
    if status in {"blocked", "login_required", "captcha_detected", "failed_final"}:
        return None
    wait_seconds = int(classification.get("wait_seconds") or 0)
    if status == "retry_wait":
        return iso(utc_now() + timedelta(seconds=max(60, wait_seconds)))
    schedule_seconds = int(row["schedule_seconds"] or 86400)
    jitter_ratio = float((config.get("defaults") or {}).get("schedule_jitter_ratio") or 0.0)
    jitter = int(schedule_seconds * jitter_ratio)
    offset = schedule_seconds + (RANDOM.randint(-jitter, jitter) if jitter > 0 else 0)
    return iso(utc_now() + timedelta(seconds=max(60, offset)))


def finalize_attempt(
    conn: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    attempt_id: int,
    completed: subprocess.CompletedProcess[str] | None,
    classification: dict[str, Any],
    artifact_dir: str,
    capture_meta_paths: list[str],
    import_result: dict[str, Any],
    config: dict[str, Any],
) -> None:
    exit_code = completed.returncode if completed is not None else None
    stdout_tail = tail(completed.stdout if completed is not None else "")
    stderr_tail = tail(completed.stderr if completed is not None else "")
    conn.execute(
        """
        UPDATE crawl_attempts
        SET status=?, failure_type=?, retryable=?, wait_seconds=?, finished_at=?, exit_code=?,
            artifact_dir=?, capture_meta_paths_json=?, import_result_json=?,
            stdout_tail=?, stderr_tail=?, classification_json=?
        WHERE id=?
        """,
        (
            classification["status"],
            classification.get("failure_type"),
            1 if classification.get("retryable") else 0,
            int(classification.get("wait_seconds") or 0),
            iso(),
            exit_code,
            artifact_dir,
            json_dump(capture_meta_paths),
            json_dump(import_result),
            stdout_tail,
            stderr_tail,
            json_dump(classification),
            attempt_id,
        ),
    )
    failures = 0 if classification["status"] == "completed" else int(row["consecutive_failures"] or 0) + 1
    status = classification["status"]
    if status == "retry_wait" and failures >= int(row["max_attempts"]):
        status = "failed_final"
        classification["status"] = status
    next_at = next_run_time(row, classification, config)
    conn.execute(
        """
        UPDATE crawl_jobs
        SET status=?, consecutive_failures=?, last_attempt_id=?, last_status=?, last_failure_type=?,
            next_run_at=COALESCE(?, next_run_at), updated_at=datetime('now')
        WHERE id=?
        """,
        (
            status,
            failures,
            attempt_id,
            status,
            classification.get("failure_type"),
            next_at,
            row["id"],
        ),
    )
    conn.commit()


def markdown_report(summary: dict[str, Any]) -> str:
    lines = [
        "# 抓取运行摘要",
        "",
        f"- 运行 ID：`{summary['run_id']}`",
        f"- 开始时间：`{summary['started_at']}`",
        f"- 结束时间：`{summary['finished_at']}`",
        f"- 选中任务：`{summary['jobs_selected']}`",
        f"- 完成：`{summary['completed_count']}`",
        f"- 失败：`{summary['failed_count']}`",
        f"- 阻断/需人工：`{summary['blocked_count']}`",
        "",
        "## 任务结果",
        "",
        "| job_key | site | kind | status | failure_type | artifact |",
        "|---|---|---|---|---|---|",
    ]
    for item in summary["records"]:
        lines.append(
            "| {job_key} | {site_key} | {job_kind} | {status} | {failure_type} | {artifact_dir} |".format(
                **{key: str(item.get(key, "")) for key in ("job_key", "site_key", "job_kind", "status", "failure_type", "artifact_dir")}
            )
        )
    lines.append("")
    return "\n".join(lines)


def create_run_report(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO crawl_run_reports(run_id, started_at, status) VALUES (?, ?, 'running')",
        (run_id, iso()),
    )
    conn.commit()


def finish_run_report(conn: sqlite3.Connection, run_id: str, summary: dict[str, Any], report_path: Path) -> None:
    conn.execute(
        """
        UPDATE crawl_run_reports
        SET finished_at=?, status=?, jobs_selected=?, completed_count=?, failed_count=?, blocked_count=?,
            report_json=?, report_path=?, updated_at=datetime('now')
        WHERE run_id=?
        """,
        (
            summary["finished_at"],
            "completed",
            summary["jobs_selected"],
            summary["completed_count"],
            summary["failed_count"],
            summary["blocked_count"],
            json_dump(summary),
            str(report_path),
            run_id,
        ),
    )
    conn.commit()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).expanduser()
    config_path = Path(args.config).expanduser()
    config = load_json(config_path)
    run_id = utc_now().strftime("%Y%m%dT%H%M%S%z")
    run_dir = ensure_dir(Path(args.run_root).expanduser() / run_id)

    ensure_parent(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        bootstrap = bootstrap_connection(conn, config=config, sync_jobs=not args.no_sync_config)
        synced = int(bootstrap["synced_jobs"])
        if args.sync_only:
            print(json.dumps({"db": str(db_path), **bootstrap}, ensure_ascii=False, indent=2))
            return 0

        create_run_report(conn, run_id)
        jobs = select_due_jobs(conn, args)
        records: list[dict[str, Any]] = []
        for row in jobs:
            command = build_command(row, args)
            if args.dry_run:
                records.append(
                    {
                        "job_key": row["job_key"],
                        "site_key": row["site_key"],
                        "job_kind": row["job_kind"],
                        "status": "planned",
                        "failure_type": "",
                        "retryable": False,
                        "artifact_dir": "",
                        "capture_meta_paths": [],
                        "command": command,
                        "import_result": {"skipped": True, "reason": "dry_run"},
                    }
                )
                continue
            attempt_id = insert_attempt(conn, row, run_id, command)
            completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
            artifact_dir, capture_meta_paths, _summary_path = find_artifact_paths(completed.stdout, row)
            meta = load_first_meta(capture_meta_paths)
            classification = classify_attempt(
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                meta=meta,
            )
            if (
                not args.no_import
                and classification.get("status") == "completed"
                and row["job_kind"] == "ctf_resource_crawl"
                and not meta.get("skipped")
            ):
                import_result = import_capture_results(capture_meta_paths, db_path)
            else:
                reason = "skipped_capture" if meta.get("skipped") else "not_importable"
                import_result = {"skipped": True, "reason": reason}
            finalize_attempt(
                conn,
                row=row,
                attempt_id=attempt_id,
                completed=completed,
                classification=classification,
                artifact_dir=artifact_dir,
                capture_meta_paths=capture_meta_paths,
                import_result=import_result,
                config=config,
            )
            records.append(
                {
                    "job_key": row["job_key"],
                    "site_key": row["site_key"],
                    "job_kind": row["job_kind"],
                    "status": classification["status"],
                    "failure_type": classification.get("failure_type", ""),
                    "retryable": bool(classification.get("retryable")),
                    "artifact_dir": artifact_dir,
                    "capture_meta_paths": capture_meta_paths,
                    "command": command,
                    "import_result": import_result,
                }
            )

        blocked_statuses = {"blocked", "login_required", "captcha_detected"}
        non_failed_statuses = {"completed", "planned"} | blocked_statuses
        summary = {
            "run_id": run_id,
            "started_at": run_id,
            "finished_at": iso(),
            "db": str(db_path),
            "config": str(config_path),
            "synced_jobs": synced,
            "jobs_selected": len(jobs),
            "completed_count": sum(1 for item in records if item["status"] == "completed"),
            "failed_count": sum(1 for item in records if item["status"] not in non_failed_statuses),
            "blocked_count": sum(1 for item in records if item["status"] in blocked_statuses),
            "records": records,
        }
        summary_path = run_dir / "run_summary.json"
        report_path = run_dir / "run_summary.md"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        report_path.write_text(markdown_report(summary), encoding="utf-8")
        finish_run_report(conn, run_id, summary, report_path)

    print(json.dumps({"summary": str(summary_path), "report": str(report_path), **summary}, ensure_ascii=False, indent=2))
    return 0 if summary["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
