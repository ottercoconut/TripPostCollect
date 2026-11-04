#!/usr/bin/env python3
"""Rescan explicit existing Douyin, Weibo, or Zhihu rows through detail mode."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from trippostcollect.core.execution_state import FORMAL_STEPS, FrozenExecutionState
from trippostcollect.application.failures import extract_stdout_json
from trippostcollect.artifacts.image_completion import (
    verify_image_artifacts,
    verify_image_persistence,
)
from trippostcollect.core.paths import (
    DEFAULT_DB,
    FORMAL_CRAWL_CONTRACT,
    LOCAL_MEDIA_ROOT,
    POST_DETAIL_REPAIR_BACKUP_ROOT,
    POST_DETAIL_REPAIR_OUTPUT,
    POST_DETAIL_REPAIR_RUNTIME_ROOT,
    PROJECT_ROOT,
    ensure_dir,
)
from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.records.sanitization import redact_author_avatar_text
from trippostcollect.runtime.process import (
    EXECUTION_STATE_PATH_ENV,
    PROCESS_CLEANUP_GRACE_SECONDS,
    PROCESS_FINAL_REAP_SECONDS,
    decode_text,
    kill_recorded_child_process_groups,
    operator_interrupt_error,
    terminate_managed_process,
    write_command_logs,
)
from trippostcollect.xhs.leases import DeferredTerminationSignals


PLATFORMS = ("douyin", "weibo", "zhihu")
# 与通用 runner 相同：首个 SIGINT/SIGTERM/SIGHUP 锁存，独立会话里的 child 收不到终端信号。
REPAIR_INTERRUPT_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
REPAIR_CHILD_POLL_SECONDS = 0.2
# 中间层自己用 PROCESS_CLEANUP_GRACE_SECONDS 等 worker，再用 PROCESS_FINAL_REAP_SECONDS 回收。
# 总控的倒计时更早开始，所以必须留出这两段再加余量，否则总会先 SIGKILL 中间层，worker 变成孤儿。
REPAIR_CHILD_INTERRUPT_GRACE_SECONDS = (
    PROCESS_CLEANUP_GRACE_SECONDS + PROCESS_FINAL_REAP_SECONDS + 15.0
)
COMPLETED_REPAIR_OUTCOMES = {"completed", "completed_with_remaining"}
TRUSTED_DETAIL_SOURCES = {
    "douyin": {"aweme_detail"},
    "weibo": {"search_mblog_complete", "mobile_detail"},
    "zhihu": {"search_content", "answer_detail", "article_detail"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Repair existing non-detail_observed Douyin, Weibo, or Zhihu rows in "
            "bounded detail-mode batches without touching discovery state."
        )
    )
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--keyword", default="青岛旅游", help="Visible behavior-search keyword.")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument(
        "--max-items",
        type=int,
        default=0,
        help="Maximum pending rows in this pass; 0 scans every currently actionable row.",
    )
    parser.add_argument("--post-id", action="append", dest="post_ids", default=[])
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--confirm-default-db-repair",
        action="store_true",
        help="Required for a non-dry-run repair of data/trippostcollect.sqlite.",
    )
    return parser.parse_args()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f%z")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tail(value: str, limit: int = 6000) -> str:
    cleaned, _ = redact_author_avatar_text(value or "")
    return cleaned[-limit:] if len(cleaned) > limit else cleaned


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_sqlite_backup(source_path: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError(f"backup destination already exists: {destination}")
    source_uri = f"file:{source_path}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    with sqlite3.connect(destination) as check:
        quick_check = [str(row[0]) for row in check.execute("PRAGMA quick_check")]
        foreign_keys = [tuple(row) for row in check.execute("PRAGMA foreign_key_check")]
    if quick_check != ["ok"] or foreign_keys:
        raise RuntimeError(
            f"SQLite backup validation failed: quick_check={quick_check}, "
            f"foreign_key_errors={len(foreign_keys)}"
        )
    return {
        "path": str(destination),
        "sha256": sha256_file(destination),
        "size_bytes": destination.stat().st_size,
        "quick_check": quick_check,
        "foreign_key_error_count": len(foreign_keys),
    }


def _detail_target(platform: str, post_id: str, canonical_url: str) -> tuple[str | None, str]:
    parsed = urlparse(canonical_url.strip().split("#", 1)[0].split("?", 1)[0])
    if platform == "douyin":
        expected_paths = {f"/video/{post_id}", f"/note/{post_id}"}
        if (
            parsed.scheme == "https"
            and parsed.hostname in {"douyin.com", "www.douyin.com"}
            and parsed.path.rstrip("/") in expected_paths
        ):
            return parsed._replace(query="", fragment="").geturl(), ""
        return None, "invalid_douyin_canonical_url"
    if platform == "weibo":
        if (
            parsed.scheme == "https"
            and parsed.hostname == "m.weibo.cn"
            and parsed.path.rstrip("/") == f"/detail/{post_id}"
        ):
            return post_id, ""
        return None, "invalid_weibo_canonical_url"
    answer = bool(
        parsed.scheme == "https"
        and parsed.hostname in {"zhihu.com", "www.zhihu.com"}
        and parsed.path.rstrip("/").endswith(f"/answer/{post_id}")
    )
    article = bool(
        parsed.scheme == "https"
        and parsed.hostname == "zhuanlan.zhihu.com"
        and parsed.path.rstrip("/") == f"/p/{post_id}"
    )
    if answer or article:
        return parsed._replace(query="", fragment="").geturl(), ""
    return None, "invalid_zhihu_canonical_url"


def load_post_detail_repair_waivers(
    conn: sqlite3.Connection,
    platform: str,
) -> dict[str, dict[str, Any]]:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='post_detail_repair_waivers'"
    ).fetchone():
        return {}
    rows = conn.execute(
        """
        SELECT p.platform_post_id, w.reason, w.authorized_by,
               w.authorized_at, w.evidence_json
        FROM post_detail_repair_waivers AS w
        JOIN web_posts AS p ON p.id=w.web_post_id
        WHERE p.platform_key=?
        """,
        (platform,),
    ).fetchall()
    waivers: dict[str, dict[str, Any]] = {}
    for row in rows:
        post_id = str(row["platform_post_id"] or "")
        try:
            evidence = json.loads(str(row["evidence_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        waivers[post_id] = {
            "reason": str(row["reason"] or ""),
            "authorized_by": str(row["authorized_by"] or ""),
            "authorized_at": str(row["authorized_at"] or ""),
            "evidence": evidence if isinstance(evidence, dict) else {},
        }
    return waivers


def waive_post_detail_repairs(
    conn: sqlite3.Connection,
    *,
    platform: str,
    post_ids: list[str],
    reason: str,
    authorized_by: str,
    evidence_by_post_id: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    normalized_ids = sorted({str(value).strip() for value in post_ids if str(value).strip()})
    if not normalized_ids:
        raise ValueError("post detail repair waiver requires at least one platform post ID")
    if not reason.strip() or not authorized_by.strip():
        raise ValueError("post detail repair waiver requires reason and authorized_by")
    placeholders = ",".join("?" for _ in normalized_ids)
    rows = conn.execute(
        f"""
        SELECT id, platform_post_id,
               COALESCE(
                   CASE WHEN json_valid(raw_sample_json)
                        THEN json_extract(raw_sample_json, '$.content_detail_status') END,
                   ''
               ) AS detail_status
        FROM web_posts
        WHERE platform_key=? AND platform_post_id IN ({placeholders})
        """,
        (platform, *normalized_ids),
    ).fetchall()
    by_id = {str(row["platform_post_id"]): row for row in rows}
    missing = sorted(set(normalized_ids) - set(by_id))
    if missing:
        raise ValueError(f"post detail repair waiver targets not found: {missing}")
    already_observed = sorted(
        post_id
        for post_id, row in by_id.items()
        if str(row["detail_status"] or "") == "detail_observed"
    )
    if already_observed:
        raise ValueError(
            f"post detail repair waiver targets are already detail_observed: {already_observed}"
        )
    authorized_at = utc_iso()
    for post_id in normalized_ids:
        evidence = (evidence_by_post_id or {}).get(post_id) or {}
        conn.execute(
            """
            INSERT INTO post_detail_repair_waivers(
                web_post_id, reason, authorized_by, authorized_at, evidence_json
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(web_post_id) DO UPDATE SET
                reason=excluded.reason,
                authorized_by=excluded.authorized_by,
                authorized_at=excluded.authorized_at,
                evidence_json=excluded.evidence_json,
                updated_at=datetime('now')
            """,
            (
                int(by_id[post_id]["id"]),
                reason.strip(),
                authorized_by.strip(),
                authorized_at,
                json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
            ),
        )
    return normalized_ids


def select_targets(
    conn: sqlite3.Connection,
    *,
    platform: str,
    post_ids: list[str],
    max_items: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    rows = conn.execute(
        """
        SELECT id, platform_post_id, canonical_url, keyword, artifact_dir,
               published_at, author_followers_count, author_display_name,
               author_platform_id, author_profile_url, author_description,
               raw_sample_json
        FROM web_posts
        WHERE platform_key=?
          AND COALESCE(
                CASE WHEN json_valid(raw_sample_json)
                     THEN json_extract(raw_sample_json, '$.content_detail_status') END,
                ''
              ) <> 'detail_observed'
        ORDER BY id
        """,
        (platform,),
    ).fetchall()
    pending_count = len(rows)
    requested = {str(value).strip() for value in post_ids if str(value).strip()}
    waivers = load_post_detail_repair_waivers(conn, platform)
    targets: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        post_id = str(row["platform_post_id"] or "").strip()
        if requested and post_id not in requested:
            continue
        detail_target, reason = _detail_target(
            platform,
            post_id,
            str(row["canonical_url"] or ""),
        )
        raw_sample: dict[str, Any] = {}
        try:
            parsed_raw = json.loads(str(row["raw_sample_json"] or ""))
            if isinstance(parsed_raw, dict):
                raw_sample = parsed_raw
        except (TypeError, json.JSONDecodeError):
            raw_sample = {}
        repair_fallback = {
            key: value
            for key, value in {
                "published_at": row["published_at"],
                "author_followers_count": row["author_followers_count"],
                "author_display_name": row["author_display_name"],
                "author_platform_id": row["author_platform_id"],
                "author_profile_url": row["author_profile_url"],
                "author_description": row["author_description"],
                "created_time": raw_sample.get("created_time"),
                "updated_time": raw_sample.get("updated_time"),
                "creator_hash": raw_sample.get("creator_hash"),
                "creator_url_token": raw_sample.get("creator_url_token"),
                "user_nickname": raw_sample.get("user_nickname"),
                "author_followers_source": raw_sample.get("author_followers_source"),
                "followers_observed": raw_sample.get("followers_observed"),
                "followers_count": raw_sample.get("followers_count"),
            }.items()
            if value not in (None, "")
        }
        item = {
            "web_post_id": int(row["id"]),
            "platform_post_id": post_id,
            "detail_target": detail_target,
            "keyword": str(row["keyword"] or ""),
            "artifact_dir": str(row["artifact_dir"] or ""),
            "repair_fallback": repair_fallback,
            "reason": reason,
        }
        if post_id in waivers:
            item["reason"] = "post_detail_repair_waived"
            item["repair_waiver"] = waivers[post_id]
            rejected.append(item)
        elif not post_id:
            item["reason"] = "missing_platform_post_id"
            rejected.append(item)
        elif not item["keyword"]:
            item["reason"] = "missing_original_keyword"
            rejected.append(item)
        elif detail_target:
            targets.append(item)
        else:
            rejected.append(item)
    if max_items > 0:
        targets = targets[:max_items]
    return targets, rejected, pending_count


def chunked(values: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def all_rejected_targets_waived(rejected: list[dict[str, Any]]) -> bool:
    return bool(rejected) and all(
        item.get("reason") == "post_detail_repair_waived" for item in rejected
    )


def build_child_command(
    *,
    platform: str,
    keyword: str,
    db_path: Path,
    output_root: Path,
    targets_path: Path,
    headless: bool,
) -> list[str]:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "mediacrawler_crawl.py"),
        "--platforms",
        platform,
        "--keyword",
        keyword,
        "--output-dir",
        str(output_root),
        "--required-fields-profile",
        "image_post_with_followers_v1",
        "--behavior-profile",
        "social_high_risk",
        "--login-type",
        "cookie",
        "--db",
        str(db_path),
        "--post-repair",
        "--repair-targets-file",
        str(targets_path),
        "--download-images",
        "--media-root",
        str(LOCAL_MEDIA_ROOT.resolve()),
        "--no-checkpoint-write",
    ]
    if not headless:
        command.append("--headed")
    return command


def load_child_summary(path_value: str) -> dict[str, Any]:
    if not path_value:
        return {}
    path = Path(path_value).expanduser()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def discard_hung_up_stream(stream: Any) -> None:
    """输出端已失效时改接 /dev/null，避免解释器退出时 flush 失败把退出码改成 120。"""

    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, stream.fileno())
        finally:
            os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass


def print_repair_stream(line: str, stream: Any) -> None:
    try:
        print(line, file=stream, flush=True)
    except OSError:
        discard_hung_up_stream(stream)


def _child_text(value: bytes | None) -> str:
    return decode_text(value).replace("\r\n", "\n").replace("\r", "\n")


def interrupt_details(
    signum: int,
    *,
    forced_termination: bool,
    exception: str = "",
) -> dict[str, Any]:
    details = {
        "reason": "operator_interrupt",
        "signum": int(signum),
        "signal": signal.Signals(int(signum)).name,
        "exit_code": 128 + int(signum),
        "error": operator_interrupt_error(signum),
        "forced_termination": bool(forced_termination),
    }
    if exception:
        details["exception"] = exception
    return details


def interrupted_batch_result(batch_plan: dict[str, Any], child_run: dict[str, Any]) -> dict[str, Any]:
    signum = int(child_run["interrupt_signum"])
    stdout = str(child_run.get("stdout") or "")
    stderr = str(child_run.get("stderr") or "")
    logs = child_run.get("logs") or {}
    return {
        "batch": batch_plan["batch"],
        "target_count": batch_plan["target_count"],
        "launched": bool(child_run.get("launched", True)),
        "exit_code": child_run.get("returncode"),
        "error": operator_interrupt_error(signum),
        "interrupt": interrupt_details(
            signum,
            forced_termination=bool(child_run.get("forced_termination")),
        ),
        "stdout_tail": stdout[-6000:],
        "stderr_tail": stderr[-6000:],
        "killed_child_process_groups": list(child_run.get("killed_groups") or []),
        "logs": {
            key: logs.get(key)
            for key in ("stdout_log", "stderr_log", "command_log")
        },
    }


def terminalize_repair_interrupt(
    state: FrozenExecutionState,
    *,
    error: str,
    evidence: dict[str, Any],
) -> str | None:
    """未完成的执行状态收成中断失败。已经 completed 的状态不改写。"""

    try:
        payload = state.load()
        if payload.get("status") in COMPLETED_REPAIR_OUTCOMES:
            return None
        steps = payload.get("steps") or {}
        open_step: str | None = None
        for name in FORMAL_STEPS:
            if name == "plan_frozen":
                continue
            step_status = (steps.get(name) or {}).get("status")
            if step_status == "failed":
                open_step = None
                break
            if step_status in {"pending", "in_progress"}:
                open_step = name
                break
        if open_step is not None and open_step != "task_finalized":
            state.fail(open_step, error=error, evidence=evidence)
        state.finalize_failure(error=error, evidence=evidence)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _interrupted_child_result(
    command: list[str],
    *,
    log_dir: Path,
    signum: int,
    returncode: int | None,
    stdout: str,
    stderr: str,
    forced_termination: bool,
    launched: bool,
    killed_groups: list[dict[str, Any]],
) -> dict[str, Any]:
    logs = write_command_logs(command, log_dir, stdout, stderr)
    return {
        "returncode": returncode,
        "stdout": logs["stdout"],
        "stderr": logs["stderr"],
        "interrupt_signum": int(signum),
        "forced_termination": forced_termination,
        "launched": launched,
        "killed_groups": killed_groups,
        "logs": logs,
    }


def run_repair_child(
    command: list[str],
    *,
    cwd: Path,
    log_dir: Path,
    state_path: Path,
    interrupt_signal: Callable[[], int | None],
    grace_seconds: float | None = None,
    poll_seconds: float = REPAIR_CHILD_POLL_SECONDS,
) -> dict[str, Any]:
    """运行一批修复 child。没有整轮墙钟；首个信号只向中间层发一次 SIGTERM。

    ``TRIPPOSTCOLLECT_EXECUTION_STATE_PATH`` 指向本轮修复状态，供中间层登记 worker 进程组。
    worker 的执行事件也会写进同一份文件，无进展看门狗把这份文件的变化当作进度。
    不因此写 discovery checkpoint。未传入宽限时用 ``REPAIR_CHILD_INTERRUPT_GRACE_SECONDS``，
    盖过中间层自己的 20 秒等待和 5 秒回收。仍不得不强杀中间层时，按登记兜底杀掉 worker。
    """

    if grace_seconds is None:
        grace_seconds = REPAIR_CHILD_INTERRUPT_GRACE_SECONDS
    signum = interrupt_signal()
    if signum is not None:
        return _interrupted_child_result(
            command,
            log_dir=log_dir,
            signum=int(signum),
            returncode=None,
            stdout="",
            stderr="",
            forced_termination=False,
            launched=False,
            killed_groups=[],
        )
    env = os.environ.copy()
    env[EXECUTION_STATE_PATH_ENV] = str(state_path)
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    forced = False
    killed_groups: list[dict[str, Any]] = []
    while True:
        signum = interrupt_signal()
        if signum is not None:
            stdout_bytes, stderr_bytes, forced = terminate_managed_process(
                process,
                grace_seconds=grace_seconds,
            )
            if forced:
                killed_groups = kill_recorded_child_process_groups(state_path)
            break
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=poll_seconds)
        except subprocess.TimeoutExpired:
            continue
        signum = interrupt_signal()
        if signum is None:
            return {
                "returncode": int(process.returncode or 0),
                "stdout": _child_text(stdout_bytes),
                "stderr": _child_text(stderr_bytes),
            }
        forced = False
        break
    returncode = process.returncode
    return _interrupted_child_result(
        command,
        log_dir=log_dir,
        signum=int(signum),
        returncode=None if returncode is None else int(returncode),
        stdout=_child_text(stdout_bytes),
        stderr=_child_text(stderr_bytes),
        forced_termination=forced,
        launched=True,
        killed_groups=killed_groups,
    )


def repaired_rows(
    conn: sqlite3.Connection,
    platform: str,
    target_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not target_ids:
        return {}
    trusted = TRUSTED_DETAIL_SOURCES[platform]
    result: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(target_ids), 500):
        target_batch = target_ids[offset : offset + 500]
        placeholders = ",".join("?" for _ in target_batch)
        rows = conn.execute(
            f"""
            SELECT p.platform_post_id,
                   p.content_text,
                   CASE WHEN json_valid(p.raw_sample_json)
                        THEN json_extract(p.raw_sample_json, '$.content_detail_status') END
                        AS detail_status,
                   CASE WHEN json_valid(p.raw_sample_json)
                        THEN json_extract(p.raw_sample_json, '$.content_detail_source') END
                        AS detail_source,
                   SUM(CASE WHEN i.image_role='content' THEN 1 ELSE 0 END) AS content_images,
                   SUM(CASE WHEN i.image_role='content'
                                 AND COALESCE(i.local_path, '') <> ''
                                 AND COALESCE(i.sha256, '') <> ''
                            THEN 1 ELSE 0 END) AS persisted_images
            FROM web_posts p
            LEFT JOIN web_post_images i ON i.web_post_id=p.id
            WHERE p.platform_key=? AND p.platform_post_id IN ({placeholders})
            GROUP BY p.id
            """,
            [platform, *target_batch],
        ).fetchall()
        for row in rows:
            source = str(row["detail_source"] or "")
            content_images = int(row["content_images"] or 0)
            persisted_images = int(row["persisted_images"] or 0)
            result[str(row["platform_post_id"])] = {
                "detail_status": str(row["detail_status"] or ""),
                "detail_source": source,
                "content_images": content_images,
                "persisted_images": persisted_images,
                "recovered": bool(
                    row["detail_status"] == "detail_observed"
                    and source in trusted
                    and str(row["content_text"] or "").strip()
                    and content_images > 0
                    and persisted_images == content_images
                ),
            }
    return result


def pending_count(conn: sqlite3.Connection, platform: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM web_posts
        WHERE platform_key=?
          AND COALESCE(
                CASE WHEN json_valid(raw_sample_json)
                     THEN json_extract(raw_sample_json, '$.content_detail_status') END,
                ''
              ) <> 'detail_observed'
        """,
        (platform,),
    ).fetchone()
    return int(row[0] or 0)


def waived_pending_count(conn: sqlite3.Connection, platform: str) -> int:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='post_detail_repair_waivers'"
    ).fetchone():
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM post_detail_repair_waivers AS w
        JOIN web_posts AS p ON p.id=w.web_post_id
        WHERE p.platform_key=?
          AND COALESCE(
                CASE WHEN json_valid(p.raw_sample_json)
                     THEN json_extract(p.raw_sample_json, '$.content_detail_status') END,
                ''
              ) <> 'detail_observed'
        """,
        (platform,),
    ).fetchone()
    return int(row[0] or 0)


def _fatal_child_failure(summary: dict[str, Any]) -> str:
    validation = summary.get("formal_validation") or {}
    stop_reason = str(validation.get("stop_reason") or "")
    if stop_reason in {
        "runtime_failed",
        "login_required",
        "captcha_detected",
        "rate_limited",
        "platform_security_limit",
        "policy_blocked",
        "blocked_or_forbidden",
        "runtime_permission_error",
        "browser_launch_failed",
        "browser_target_closed",
    }:
        return stop_reason
    behavior = summary.get("behavior_validation") or {}
    if behavior and not behavior.get("behavior_ok"):
        return "behavior_evidence_failed"
    if behavior and not behavior.get("policy_ok"):
        return "crawl_policy_evidence_failed"
    import_result = summary.get("import_result") or {}
    if import_result.get("reason") == "sqlite_import_failed":
        return "sqlite_import_failed"
    return ""


def child_candidate_failures(summary: dict[str, Any]) -> list[dict[str, Any]]:
    validation = summary.get("formal_validation") or {}
    failures = validation.get("skipped_candidate_failures") or []
    return [dict(item) for item in failures if isinstance(item, dict)]


def repair_persistence_failure_reason(
    *,
    recovered_ids: list[str],
    successful_child_summaries: list[dict[str, Any]],
    persistence_checks: list[dict[str, Any]],
) -> str:
    if not recovered_ids and not successful_child_summaries:
        return "post_detail_repair_no_progress"
    if not (
        recovered_ids
        and successful_child_summaries
        and all(check.get("ok") for check in persistence_checks)
    ):
        return "post_detail_repair_persistence_not_verified"
    return ""


def _strict_batch_blocker(error: str) -> bool:
    if not error:
        return False
    if error.startswith("missing_child_summary_exit_"):
        return False
    return error in {
        "login_required",
        "captcha_detected",
        "rate_limited",
        "platform_security_limit",
        "policy_blocked",
        "blocked_or_forbidden",
        "runtime_permission_error",
        "browser_launch_failed",
        "browser_target_closed",
        "behavior_evidence_failed",
        "crawl_policy_evidence_failed",
        "sqlite_import_failed",
        "post_detail_repair_image_artifacts_incomplete",
        "post_detail_repair_persistence_not_verified",
    }


def _state_fail_open(state: FrozenExecutionState | None, error: str) -> None:
    if state is None:
        return
    try:
        payload = state.load()
        for step in FORMAL_STEPS[1:]:
            status = (payload.get("steps") or {}).get(step, {}).get("status")
            if status in {"pending", "in_progress"}:
                state.fail(step, error=error)
                return
    except Exception:
        return


def _complete_no_op(
    state: FrozenExecutionState,
    *,
    reason: str,
) -> None:
    state.begin("command_executed")
    state.complete("command_executed", evidence={"skipped": True, "reason": reason})
    state.begin("artifacts_verified")
    state.complete("artifacts_verified", evidence={"skipped": True, "reason": reason})
    state.begin("persistence_verified")
    state.complete(
        "persistence_verified",
        evidence={"skipped": True, "reason": reason},
        skipped=True,
    )
    state.finalize(outcome="completed", evidence={"skipped": True, "reason": reason})


def main() -> int:
    args = parse_args()
    if args.batch_size <= 0 or args.max_items < 0:
        raise SystemExit("--batch-size must be positive; --max-items cannot be negative")
    if not str(args.keyword or "").strip():
        raise SystemExit("--keyword cannot be empty")

    db_path = Path(args.db).expanduser().resolve()
    if (
        not args.dry_run
        and db_path == DEFAULT_DB.resolve()
        and not args.confirm_default_db_repair
    ):
        raise SystemExit(
            "default database repair requires --confirm-default-db-repair"
        )
    bootstrap_database(db_path, sync_jobs=False)
    run_id = f"{args.platform}-{utc_stamp()}"
    run_dir = ensure_dir(POST_DETAIL_REPAIR_OUTPUT / run_id)
    runtime_dir = ensure_dir(POST_DETAIL_REPAIR_RUNTIME_ROOT / run_id)
    state_path = runtime_dir / "execution_state.json"
    summary_path = run_dir / "run_summary.json"
    backup_path = POST_DETAIL_REPAIR_BACKUP_ROOT / run_id / db_path.name

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        targets, rejected, initial_pending = select_targets(
            conn,
            platform=args.platform,
            post_ids=args.post_ids,
            max_items=args.max_items,
        )
        initial_waived_pending = waived_pending_count(conn, args.platform)
    selected_waived = [
        item for item in rejected if item.get("reason") == "post_detail_repair_waived"
    ]

    manifest_path = write_json(
        run_dir / "targets.json",
        {
            "platform": args.platform,
            "behavior_keyword": args.keyword,
            "targets": targets,
            "rejected": rejected,
        },
    )
    batch_plans: list[dict[str, Any]] = []
    frozen_inputs = [FORMAL_CRAWL_CONTRACT, manifest_path]
    for index, batch in enumerate(chunked(targets, args.batch_size), start=1):
        child_targets = [
            {
                "platform_post_id": item["platform_post_id"],
                "detail_target": item["detail_target"],
                "keyword": item["keyword"],
                "repair_fallback": item.get("repair_fallback") or {},
            }
            for item in batch
        ]
        targets_path = write_json(run_dir / "batches" / f"batch-{index:04d}-targets.json", child_targets)
        command = build_child_command(
            platform=args.platform,
            keyword=str(args.keyword),
            db_path=db_path,
            output_root=run_dir / "children" / f"batch-{index:04d}",
            targets_path=targets_path,
            headless=args.headless,
        )
        batch_plans.append(
            {
                "batch": index,
                "target_count": len(batch),
                "target_ids": [item["platform_post_id"] for item in batch],
                "targets_path": str(targets_path),
                "command": command,
            }
        )
        frozen_inputs.append(targets_path)
    commands_path = write_json(run_dir / "commands.json", batch_plans)
    frozen_inputs.append(commands_path)

    plan = {
        "run_id": run_id,
        "job_kind": "post_detail_repair",
        "platform": args.platform,
        "behavior_keyword": args.keyword,
        "db": str(db_path),
        "target_count": len(targets),
        "rejected_count": len(rejected),
        "initial_pending_count": initial_pending,
        "initial_waived_pending_count": initial_waived_pending,
        "initial_unwaived_pending_count": max(0, initial_pending - initial_waived_pending),
        "selected_waived_count": len(selected_waived),
        "batch_size": args.batch_size,
        "batch_count": len(batch_plans),
        "headed": not args.headless,
        "discovery_writes": False,
        "local_image_storage_required": True,
        "media_root": str(LOCAL_MEDIA_ROOT.resolve()),
        "backup_path": str(backup_path),
        "manifest": str(manifest_path),
        "commands": str(commands_path),
    }
    state = FrozenExecutionState.create(
        state_path,
        run_id=run_id,
        job_key=f"post_detail_repair:{args.platform}",
        site_key=args.platform,
        job_kind="post_detail_repair",
        plan=plan,
        frozen_inputs=frozen_inputs,
        dry_run=args.dry_run,
    )
    base_summary = {
        "status": "planned" if args.dry_run else "running",
        "run_id": run_id,
        "job_kind": "post_detail_repair",
        "platform": args.platform,
        "execution_state": str(state_path),
        "plan": plan,
        "target_sample": targets[:10],
        "rejected_sample": rejected[:10],
        "started_at": utc_iso(),
    }
    if args.dry_run:
        write_json(summary_path, base_summary)
        print(json.dumps({**base_summary, "summary": str(summary_path)}, ensure_ascii=False, indent=2))
        return 0

    if not targets:
        if all_rejected_targets_waived(rejected):
            reason = "no_actionable_repair_targets_all_waived"
            _complete_no_op(state, reason=reason)
            summary = {
                **base_summary,
                "status": "completed",
                "no_op": True,
                "no_op_reason": reason,
                "remaining_pending_count": initial_pending,
                "remaining_waived_pending_count": initial_waived_pending,
                "remaining_unwaived_pending_count": max(
                    0, initial_pending - initial_waived_pending
                ),
                "finished_at": utc_iso(),
            }
            write_json(summary_path, summary)
            print(
                json.dumps(
                    {**summary, "summary": str(summary_path)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if rejected:
            error = "no_actionable_repair_targets"
            state.begin("command_executed")
            state.fail(
                "command_executed",
                error=error,
                evidence={"rejected_count": len(rejected), "rejected_sample": rejected[:10]},
            )
            summary = {**base_summary, "status": "failed", "error": error, "finished_at": utc_iso()}
            write_json(summary_path, summary)
            print(json.dumps({**summary, "summary": str(summary_path)}, ensure_ascii=False, indent=2))
            return 2
        _complete_no_op(state, reason="no_pending_repair_targets")
        summary = {
            **base_summary,
            "status": "completed",
            "no_op": True,
            "remaining_pending_count": initial_pending,
            "remaining_waived_pending_count": initial_waived_pending,
            "remaining_unwaived_pending_count": max(
                0, initial_pending - initial_waived_pending
            ),
            "finished_at": utc_iso(),
        }
        write_json(summary_path, summary)
        print(json.dumps({**summary, "summary": str(summary_path)}, ensure_ascii=False, indent=2))
        return 0

    backup: dict[str, Any] = {}
    batch_results: list[dict[str, Any]] = []
    child_summaries: list[dict[str, Any]] = []
    candidate_failures: list[dict[str, Any]] = []
    fatal_error = ""
    outcome = "failed"
    interrupt_signum: int | None = None
    forced_termination = False
    interrupt_exception = ""
    state_update_error: str | None = None
    latch = DeferredTerminationSignals(REPAIR_INTERRUPT_SIGNALS)
    latch.install()
    try:
        backup = create_sqlite_backup(db_path, backup_path)
        if latch.signal_received is None:
            state.begin("command_executed")
        for batch_plan in batch_plans:
            if latch.signal_received is not None:
                interrupt_signum = int(latch.signal_received)
                break
            child_run = run_repair_child(
                batch_plan["command"],
                cwd=PROJECT_ROOT,
                log_dir=run_dir / "logs" / f"batch-{int(batch_plan['batch']):04d}",
                state_path=state_path,
                interrupt_signal=lambda: latch.signal_received,
            )
            if child_run.get("interrupt_signum") is not None:
                interrupt_signum = int(child_run["interrupt_signum"])
                forced_termination = bool(child_run.get("forced_termination"))
                batch_results.append(interrupted_batch_result(batch_plan, child_run))
                break
            stdout = child_run["stdout"]
            stderr = child_run["stderr"]
            stdout_json = extract_stdout_json(stdout)
            child_summary_path = str(stdout_json.get("summary") or "")
            child_summary = load_child_summary(child_summary_path)
            child_error = ""
            if not child_summary:
                child_error = f"missing_child_summary_exit_{child_run['returncode']}"
            else:
                child_error = _fatal_child_failure(child_summary)
            batch_candidate_failures = child_candidate_failures(child_summary)
            candidate_failures.extend(batch_candidate_failures)
            batch_result = {
                "batch": batch_plan["batch"],
                "target_count": batch_plan["target_count"],
                "exit_code": int(child_run["returncode"]),
                "summary": child_summary_path,
                "import_completion_met": bool(child_summary.get("import_completion_met")),
                "valid_total_count": int(
                    (child_summary.get("formal_validation") or {}).get("valid_total_count") or 0
                ),
                "candidate_failure_count": len(batch_candidate_failures),
                "candidate_failures_sample": batch_candidate_failures[:20],
                "import_result": child_summary.get("import_result") or {},
                "error": child_error,
                "stdout_tail": tail(stdout),
                "stderr_tail": tail(stderr),
            }
            batch_results.append(batch_result)
            if child_summary:
                child_summaries.append(child_summary)
            state.append_event("post_detail_repair_batch_finished", batch_result)
            if child_error and _strict_batch_blocker(child_error):
                fatal_error = f"batch_{batch_plan['batch']}:{child_error}"
                break

        if interrupt_signum is None and latch.signal_received is not None:
            interrupt_signum = int(latch.signal_received)
        if interrupt_signum is not None:
            pass
        elif fatal_error:
            state.fail(
                "command_executed",
                error=fatal_error,
                evidence={"backup": backup, "batches": batch_results},
            )
        else:
            state.complete(
                "command_executed",
                evidence={"backup": backup, "batches": batch_results},
            )
            successful = [
                summary for summary in child_summaries if summary.get("import_completion_met")
            ]
            state.begin("artifacts_verified")
            artifact_checks = [
                verify_image_artifacts(
                    summary,
                    project_root=PROJECT_ROOT,
                    expect_promotion=True,
                )
                for summary in successful
            ]
            artifacts_ok = bool(successful) and all(check["ok"] for check in artifact_checks)
            if not successful:
                state.complete(
                    "artifacts_verified",
                    evidence={"skipped": True, "reason": "no_successful_child_imports"},
                    skipped=True,
                )
            elif not artifacts_ok:
                fatal_error = "post_detail_repair_image_artifacts_incomplete"
                state.fail(
                    "artifacts_verified",
                    error=fatal_error,
                    evidence={"checks": artifact_checks},
                )
            else:
                state.complete("artifacts_verified", evidence={"checks": artifact_checks})

            if not fatal_error:
                state.begin("persistence_verified")
                persistence_checks = [
                    verify_image_persistence(
                        summary,
                        db_path,
                        project_root=PROJECT_ROOT,
                        media_root=LOCAL_MEDIA_ROOT,
                    )
                    for summary in successful
                ]
                target_ids = [item["platform_post_id"] for item in targets]
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    statuses = repaired_rows(conn, args.platform, target_ids)
                    remaining_pending = pending_count(conn, args.platform)
                    remaining_waived_pending = waived_pending_count(
                        conn, args.platform
                    )
                recovered_ids = sorted(
                    post_id for post_id, value in statuses.items() if value["recovered"]
                )
                remaining_target_ids = sorted(set(target_ids) - set(recovered_ids))
                persistence_evidence = {
                    "checks": persistence_checks,
                    "target_statuses_sample": dict(list(statuses.items())[:20]),
                    "target_count": len(target_ids),
                    "recovered_count": len(recovered_ids),
                    "recovered_ids_sample": recovered_ids[:20],
                    "remaining_target_count": len(remaining_target_ids),
                    "remaining_target_ids_sample": remaining_target_ids[:20],
                    "remaining_pending_count": remaining_pending,
                    "remaining_waived_pending_count": remaining_waived_pending,
                    "remaining_unwaived_pending_count": max(
                        0, remaining_pending - remaining_waived_pending
                    ),
                    "candidate_failure_count": len(candidate_failures),
                    "candidate_failures_sample": candidate_failures[:20],
                }
                persistence_failure = repair_persistence_failure_reason(
                    recovered_ids=recovered_ids,
                    successful_child_summaries=successful,
                    persistence_checks=persistence_checks,
                )
                if persistence_failure:
                    fatal_error = persistence_failure
                    state.fail(
                        "persistence_verified",
                        error=fatal_error,
                        evidence=persistence_evidence,
                    )
                else:
                    state.complete("persistence_verified", evidence=persistence_evidence)
                    outcome = (
                        "completed"
                        if not remaining_target_ids
                        else "completed_with_remaining"
                    )
                    state.finalize(outcome=outcome, evidence=persistence_evidence)
    except (OSError, sqlite3.Error, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        if latch.signal_received is not None:
            interrupt_signum = int(latch.signal_received)
            interrupt_exception = f"{type(exc).__name__}: {exc}"
        else:
            fatal_error = f"post_detail_repair_exception:{type(exc).__name__}:{exc}"
            _state_fail_open(state, fatal_error)
    if interrupt_signum is None and latch.signal_received is not None:
        interrupt_signum = int(latch.signal_received)
    interrupt_record: dict[str, Any] | None = None
    if interrupt_signum is not None:
        interrupt_record = interrupt_details(
            interrupt_signum,
            forced_termination=forced_termination,
            exception=interrupt_exception,
        )
        try:
            current_status = str(state.load().get("status") or "")
        except Exception as exc:
            current_status = ""
            state_update_error = f"{type(exc).__name__}: {exc}"
        if current_status in COMPLETED_REPAIR_OUTCOMES:
            outcome = current_status
        else:
            outcome = "failed"
            if not fatal_error:
                fatal_error = operator_interrupt_error(interrupt_signum)
            if state_update_error is None:
                state_update_error = terminalize_repair_interrupt(
                    state,
                    error=fatal_error,
                    evidence={
                        "interrupt": interrupt_record,
                        "backup": backup,
                        "batches": batch_results,
                    },
                )

    try:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                final_statuses = repaired_rows(
                    conn,
                    args.platform,
                    [item["platform_post_id"] for item in targets],
                )
                final_pending = pending_count(conn, args.platform)
                final_waived_pending = waived_pending_count(conn, args.platform)
            recovered_ids = sorted(
                post_id for post_id, value in final_statuses.items() if value["recovered"]
            )
            remaining_ids = sorted(
                {item["platform_post_id"] for item in targets} - set(recovered_ids)
            )
            summary = {
                **base_summary,
                "status": outcome,
                "error": fatal_error,
                "backup": backup,
                "batch_results": batch_results,
                "recovered_count": len(recovered_ids),
                "recovered_ids_sample": recovered_ids[:20],
                "remaining_target_count": len(remaining_ids),
                "remaining_target_ids_sample": remaining_ids[:20],
                "remaining_pending_count": final_pending,
                "remaining_waived_pending_count": final_waived_pending,
                "remaining_unwaived_pending_count": max(
                    0, final_pending - final_waived_pending
                ),
                "all_selected_targets_recovered": not remaining_ids,
                "candidate_failure_count": len(candidate_failures),
                "candidate_failures_sample": candidate_failures[:20],
                "finished_at": utc_iso(),
            }
            if interrupt_record is not None:
                summary["interrupt"] = interrupt_record
                summary["forced_termination"] = forced_termination
                if state_update_error:
                    summary["state_update_error"] = state_update_error
            write_json(summary_path, summary)
            if interrupt_signum is not None and state_update_error:
                print_repair_stream(
                    "runtime_failed:operator_interrupt_state_update_failed:"
                    f"post_detail_repair:{args.platform}: {state_update_error}",
                    sys.stderr,
                )
            if interrupt_signum is not None:
                print_repair_stream(operator_interrupt_error(interrupt_signum), sys.stderr)
            print_repair_stream(
                json.dumps(
                    {**summary, "summary": str(summary_path)},
                    ensure_ascii=False,
                    indent=2,
                ),
                sys.stdout,
            )
        except Exception as exc:
            if interrupt_signum is None:
                raise
            fallback = {
                **base_summary,
                "status": outcome if outcome in {"failed", *COMPLETED_REPAIR_OUTCOMES} else "failed",
                "error": fatal_error or operator_interrupt_error(interrupt_signum),
                "backup": backup,
                "batch_results": batch_results,
                "finished_at": utc_iso(),
                "tail_error": f"{type(exc).__name__}: {exc}",
            }
            if interrupt_record is not None:
                fallback["interrupt"] = interrupt_record
                fallback["forced_termination"] = forced_termination
            try:
                write_json(summary_path, fallback)
            except Exception as write_exc:
                print_repair_stream(
                    "runtime_failed:operator_interrupt_summary_write_failed:"
                    f"post_detail_repair:{args.platform}: "
                    f"{type(write_exc).__name__}: {write_exc}",
                    sys.stderr,
                )
            print_repair_stream(operator_interrupt_error(interrupt_signum), sys.stderr)
        if interrupt_signum is not None:
            return 128 + interrupt_signum
        return 0 if outcome in COMPLETED_REPAIR_OUTCOMES else 2
    finally:
        latch.restore()


if __name__ == "__main__":
    raise SystemExit(main())
