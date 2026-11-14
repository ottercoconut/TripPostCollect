#!/usr/bin/env python3
"""Rescan explicit existing Douyin, Weibo, or Zhihu rows through detail mode."""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from trippostcollect.core.execution_state import FrozenExecutionState
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
from trippostcollect.platforms.zhihu.parser import collapse_zhihu_v2_image_urls
from trippostcollect.runtime.process import (
    PROCESS_CLEANUP_GRACE_SECONDS,
    PROCESS_FINAL_REAP_SECONDS,
    kill_recorded_child_process_groups,
    operator_interrupt_error,
    terminate_managed_process,
    write_command_logs,
)
from trippostcollect.xhs.leases import DeferredTerminationSignals
from trippostcollect.application.repair_runtime import (
    _complete_no_op,
    _fatal_child_failure,
    _state_fail_open,
    _strict_batch_blocker,
    child_candidate_failures,
    create_sqlite_backup,
    interrupt_details,
    interrupted_batch_result,
    print_repair_stream,
    repair_persistence_failure_reason,
    run_repair_child as _run_repair_child_impl,
    tail,
    terminalize_repair_interrupt,
    utc_iso,
    utc_stamp,
    write_json,
)


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
    # #194：详情路径只写 answer_detail / article_detail，当前代码不再产生 search_content。
    "zhihu": {"answer_detail", "article_detail"},
}
RAW_SAMPLE_UNCHANGED_NOTE = "raw_sample_json 不改，原始证据与 #184 迁移一致"


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
    """把可 patch 的模块全局传给共享实现，测试替换这些名字后仍然生效。"""

    resolved_grace = (
        REPAIR_CHILD_INTERRUPT_GRACE_SECONDS if grace_seconds is None else grace_seconds
    )
    return _run_repair_child_impl(
        command,
        cwd=cwd,
        log_dir=log_dir,
        state_path=state_path,
        interrupt_signal=interrupt_signal,
        grace_seconds=resolved_grace,
        poll_seconds=poll_seconds,
        popen=subprocess.Popen,
        terminate_process=terminate_managed_process,
        kill_groups=kill_recorded_child_process_groups,
        write_logs=write_command_logs,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Repair existing Douyin, Weibo, or Zhihu rows in bounded detail-mode "
            "batches without touching discovery state. Zhihu also accepts "
            "--dedupe-images and --refresh-all."
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
    parser.add_argument(
        "--dedupe-images",
        action="store_true",
        help="Zhihu-only offline content-image dedupe. Mutually exclusive with --refresh-all.",
    )
    parser.add_argument(
        "--refresh-all",
        action="store_true",
        help=(
            "Zhihu-only refresh of rows whose raw_sample_json has no author_page_badges key, "
            "regardless of content_detail_status."
        ),
    )
    return parser.parse_args()


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


def _selection_predicate(refresh_all: bool, *, column: str = "raw_sample_json") -> str:
    """默认只选未观察详情；知乎全量刷新改选没有 author_page_badges 键的行。"""

    if refresh_all:
        return f"json_type({column}, '$.author_page_badges') IS NULL"
    return f"""COALESCE(
            CASE WHEN json_valid({column})
                 THEN json_extract({column}, '$.content_detail_status') END,
            ''
          ) <> 'detail_observed'"""


def _repair_fallback_for_row(
    platform: str,
    row: sqlite3.Row,
    raw_sample: dict[str, Any],
) -> dict[str, Any]:
    if platform == "zhihu":
        # #194：只回落发帖时间。作者字段必须来自本次作者页。
        fields = {
            "published_at": row["published_at"],
            "created_time": raw_sample.get("created_time"),
        }
    else:
        fields = {
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
        }
    return {key: value for key, value in fields.items() if value not in (None, "")}


def select_targets(
    conn: sqlite3.Connection,
    *,
    platform: str,
    post_ids: list[str],
    max_items: int,
    refresh_all: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    rows = conn.execute(
        f"""
        SELECT id, platform_post_id, canonical_url, keyword, artifact_dir,
               published_at, author_followers_count, author_display_name,
               author_platform_id, author_profile_url, author_description,
               raw_sample_json
        FROM web_posts
        WHERE platform_key=?
          AND {_selection_predicate(refresh_all)}
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
        repair_fallback = _repair_fallback_for_row(platform, row, raw_sample)
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


def repaired_rows(
    conn: sqlite3.Connection,
    platform: str,
    target_ids: list[str],
    *,
    refresh_all: bool = False,
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
                   json_extract(p.raw_sample_json, '$.author_followers_source')
                        AS followers_source,
                   json_type(p.raw_sample_json, '$.author_page_badges') AS badges_type,
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
            # #191：知乎纯文字回答/文章没有正文图也算恢复。有图时仍要求全部落盘。
            images_ok = content_images > 0 and persisted_images == content_images
            if platform == "zhihu" and content_images == 0:
                images_ok = True
            recovered = bool(
                row["detail_status"] == "detail_observed"
                and source in trusted
                and str(row["content_text"] or "").strip()
                and images_ok
            )
            badges_present = row["badges_type"] is not None
            followers_source = str(row["followers_source"] or "")
            if refresh_all:
                recovered = recovered and followers_source == "author_page" and badges_present
            result[str(row["platform_post_id"])] = {
                "detail_status": str(row["detail_status"] or ""),
                "detail_source": source,
                "content_images": content_images,
                "persisted_images": persisted_images,
                "author_followers_source": followers_source,
                "author_page_badges_present": badges_present,
                "recovered": recovered,
            }
    return result


def pending_count(
    conn: sqlite3.Connection,
    platform: str,
    *,
    refresh_all: bool = False,
) -> int:
    row = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM web_posts
        WHERE platform_key=?
          AND {_selection_predicate(refresh_all)}
        """,
        (platform,),
    ).fetchone()
    return int(row[0] or 0)


def waived_pending_count(
    conn: sqlite3.Connection,
    platform: str,
    *,
    refresh_all: bool = False,
) -> int:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='post_detail_repair_waivers'"
    ).fetchone():
        return 0
    row = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM post_detail_repair_waivers AS w
        JOIN web_posts AS p ON p.id=w.web_post_id
        WHERE p.platform_key=?
          AND {_selection_predicate(refresh_all, column="p.raw_sample_json")}
        """,
        (platform,),
    ).fetchone()
    return int(row[0] or 0)


def build_target_results(
    *,
    targets: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    statuses: dict[str, dict[str, Any]],
    candidate_failures: list[dict[str, Any]],
    executed: bool,
) -> list[dict[str, Any]]:
    """刷新摘要逐行结果：已刷新、候选失败、被拒绝。dry-run 的选中行记为 planned。"""

    failure_by_id: dict[str, dict[str, Any]] = {}
    for item in candidate_failures:
        post_id = str(item.get("platform_post_id") or "")
        if post_id and post_id not in failure_by_id:
            failure_by_id[post_id] = item
    results: list[dict[str, Any]] = []
    for item in rejected:
        results.append(
            {
                "platform_post_id": str(item.get("platform_post_id") or ""),
                "web_post_id": item.get("web_post_id"),
                "outcome": "rejected",
                "reason": str(item.get("reason") or ""),
                "detail": "",
            }
        )
    for item in targets:
        post_id = str(item.get("platform_post_id") or "")
        if not executed:
            results.append(
                {
                    "platform_post_id": post_id,
                    "web_post_id": item.get("web_post_id"),
                    "outcome": "planned",
                    "reason": "",
                    "detail": "",
                }
            )
            continue
        status = statuses.get(post_id) or {}
        if status.get("recovered"):
            results.append(
                {
                    "platform_post_id": post_id,
                    "web_post_id": item.get("web_post_id"),
                    "outcome": "refreshed",
                    "reason": "",
                    "detail": "",
                }
            )
            continue
        failure = failure_by_id.get(post_id) or {}
        results.append(
            {
                "platform_post_id": post_id,
                "web_post_id": item.get("web_post_id"),
                "outcome": "candidate_failed",
                "reason": str(failure.get("error_code") or "not_recovered"),
                "detail": str(failure.get("detail") or ""),
            }
        )
    return results


def kept_content_images(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按解析器的 v2 哈希合并保留正文图行，胜者占该组首次出现的位置。"""

    urls = [str(row.get("image_url") or "") for row in rows]
    kept_urls = collapse_zhihu_v2_image_urls(urls)
    used: set[int] = set()
    kept: list[dict[str, Any]] = []
    for url in kept_urls:
        for index, row in enumerate(rows):
            if index in used:
                continue
            if str(row.get("image_url") or "") == url:
                used.add(index)
                kept.append(row)
                break
    return kept


def _relative_media_path(local_path: str) -> Path:
    raw = Path(local_path)
    if raw.is_absolute():
        relative = raw.resolve().relative_to(PROJECT_ROOT.resolve())
    else:
        relative = raw
    if relative.is_absolute() or any(part in {"", ".."} for part in relative.parts):
        raise ValueError(f"unsafe media path: {local_path}")
    return relative


def plan_zhihu_content_image_dedupe(conn: sqlite3.Connection) -> dict[str, Any]:
    """只读计划：不写库、不移动文件。无法解析 v2 哈希的 URL 原样保留。"""

    rows = conn.execute(
        """
        SELECT p.id AS web_post_id,
               p.platform_post_id,
               p.post_images_count,
               i.id AS image_id,
               i.image_index,
               i.image_url,
               i.image_role,
               i.local_path
        FROM web_posts AS p
        JOIN web_post_images AS i ON i.web_post_id=p.id
        WHERE p.platform_key='zhihu' AND i.image_role='content'
        ORDER BY p.id, i.image_index, i.id
        """
    ).fetchall()
    ref_counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            """
            SELECT local_path, COUNT(*)
            FROM web_post_images
            WHERE COALESCE(local_path, '') <> ''
            GROUP BY local_path
            """
        )
    }
    grouped: dict[int, list[dict[str, Any]]] = {}
    identity: dict[int, dict[str, Any]] = {}
    for row in rows:
        web_post_id = int(row["web_post_id"])
        identity[web_post_id] = {
            "web_post_id": web_post_id,
            "platform_post_id": str(row["platform_post_id"] or ""),
            "post_images_count": int(row["post_images_count"] or 0),
        }
        grouped.setdefault(web_post_id, []).append(
            {
                "id": int(row["image_id"]),
                "image_index": int(row["image_index"]),
                "image_url": str(row["image_url"] or ""),
                "local_path": str(row["local_path"] or ""),
            }
        )
    posts: list[dict[str, Any]] = []
    deleted_paths: Counter[str] = Counter()
    for web_post_id, content_rows in grouped.items():
        kept = kept_content_images(content_rows)
        kept_ids = {int(item["id"]) for item in kept}
        deleted = [item for item in content_rows if int(item["id"]) not in kept_ids]
        start = min(int(item["image_index"]) for item in content_rows)
        kept_plan = [
            {**item, "new_index": start + offset} for offset, item in enumerate(kept)
        ]
        for item in deleted:
            if item["local_path"]:
                deleted_paths[item["local_path"]] += 1
        posts.append(
            {
                **identity[web_post_id],
                "start_index": start,
                "kept": kept_plan,
                "deleted": deleted,
                "deleted_count": len(deleted),
                "count_after": len(kept_plan),
            }
        )
    moves: list[dict[str, str]] = []
    shared_skipped = 0
    for local_path, deleted_count in sorted(deleted_paths.items()):
        if ref_counts.get(local_path, 0) != deleted_count:
            shared_skipped += 1
            continue
        moves.append({"local_path": local_path, "status": "planned"})
    return {
        "posts": posts,
        "moves": moves,
        "shared_path_skipped_count": shared_skipped,
        "content_image_rows_before": sum(len(post["kept"]) + post["deleted_count"] for post in posts),
    }


def _post_dedupe_changes(post: dict[str, Any]) -> bool:
    if post["deleted_count"]:
        return True
    if int(post["post_images_count"]) != int(post["count_after"]):
        return True
    return any(int(item["image_index"]) != int(item["new_index"]) for item in post["kept"])


def apply_zhihu_content_image_dedupe(
    conn: sqlite3.Connection,
    plan: dict[str, Any],
) -> int:
    """在调用方事务里删除被淘汰行、重编号并重算 post_images_count。不改 raw_sample_json。"""

    changed_posts = 0
    for post in plan["posts"]:
        if not _post_dedupe_changes(post):
            continue
        changed_posts += 1
        for offset, item in enumerate(post["kept"]):
            conn.execute(
                "UPDATE web_post_images SET image_index=? WHERE id=?",
                (-1_000_000 - offset, int(item["id"])),
            )
        delete_ids = [int(item["id"]) for item in post["deleted"]]
        if delete_ids:
            conn.executemany(
                "DELETE FROM web_post_images WHERE id=?",
                [(image_id,) for image_id in delete_ids],
            )
        for item in post["kept"]:
            conn.execute(
                "UPDATE web_post_images SET image_index=? WHERE id=?",
                (int(item["new_index"]), int(item["id"])),
            )
        conn.execute(
            "UPDATE web_posts SET post_images_count=? WHERE id=?",
            (int(post["count_after"]), int(post["web_post_id"])),
        )
    return changed_posts


def _resolve_media_file(local_path: str) -> Path:
    raw = Path(local_path).expanduser()
    return raw if raw.is_absolute() else PROJECT_ROOT / raw


def move_deduped_media(
    moves: list[dict[str, str]],
    *,
    backup_media_root: Path,
) -> list[dict[str, str]]:
    """只移动已经没有任何 web_post_images 行引用的文件。缺失文件留在清单里，不删除。"""

    results: list[dict[str, str]] = []
    for item in moves:
        local_path = item["local_path"]
        record = {"local_path": local_path, "destination": "", "status": "error", "error": ""}
        try:
            relative = _relative_media_path(local_path)
            destination = backup_media_root / relative
            source = _resolve_media_file(local_path)
            record["destination"] = str(destination)
            if not source.is_file():
                record["status"] = "missing"
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise FileExistsError(f"backup media already exists: {destination}")
                shutil.move(str(source), str(destination))
                record["status"] = "moved"
        except (OSError, ValueError) as exc:
            record["status"] = "error"
            record["error"] = f"{type(exc).__name__}: {exc}"
        results.append(record)
    return results


def _dedupe_summary(
    *,
    run_id: str,
    db_path: Path,
    summary_path: Path,
    plan: dict[str, Any],
    dry_run: bool,
    backup: dict[str, Any],
    move_results: list[dict[str, str]],
    manifest_path: Path,
) -> dict[str, Any]:
    posts = plan["posts"]
    deleted_row_count = sum(int(post["deleted_count"]) for post in posts)
    histogram = Counter(str(int(post["deleted_count"])) for post in posts)
    changed = [post for post in posts if _post_dedupe_changes(post)]
    sample = [
        {
            "platform_post_id": post["platform_post_id"],
            "web_post_id": post["web_post_id"],
            "deleted_count": post["deleted_count"],
            "count_before": post["post_images_count"],
            "count_after": post["count_after"],
            "kept_urls": [item["image_url"] for item in post["kept"]],
            "dropped_urls": [item["image_url"] for item in post["deleted"]],
        }
        for post in changed[:10]
    ]
    moved = [item for item in move_results if item.get("status") == "moved"]
    return {
        "status": "planned" if dry_run else "completed",
        "mode": "dedupe_images",
        "run_id": run_id,
        "job_kind": "post_detail_repair",
        "platform": "zhihu",
        "dry_run": dry_run,
        "db": str(db_path),
        "posts_examined": len(posts),
        "posts_changed": len(changed),
        "content_image_rows_before": int(plan["content_image_rows_before"]),
        "content_image_rows_after": int(plan["content_image_rows_before"]) - deleted_row_count,
        "deleted_row_count": deleted_row_count,
        "deletion_histogram": dict(sorted(histogram.items(), key=lambda item: int(item[0]))),
        "moved_file_count": len(plan["moves"]) if dry_run else len(moved),
        "shared_path_skipped_count": int(plan["shared_path_skipped_count"]),
        "sample": sample,
        "moved_files_path": str(manifest_path),
        "moved_files_sample": (plan["moves"] if dry_run else move_results)[:10],
        "raw_sample_json_unchanged": True,
        "raw_sample_json_note": RAW_SAMPLE_UNCHANGED_NOTE,
        "backup": backup,
        "summary": str(summary_path),
        "finished_at": utc_iso(),
    }


def _validate_repair_modes(args: argparse.Namespace) -> None:
    if args.dedupe_images and args.refresh_all:
        raise SystemExit("--dedupe-images and --refresh-all are mutually exclusive")
    if args.dedupe_images and args.platform != "zhihu":
        raise SystemExit("--dedupe-images requires --platform zhihu")
    if args.refresh_all and args.platform != "zhihu":
        raise SystemExit("--refresh-all requires --platform zhihu")
    if args.dedupe_images and (args.post_ids or args.max_items):
        raise SystemExit("--dedupe-images does not accept --post-id or --max-items")


def _require_default_db_confirmation(args: argparse.Namespace, db_path: Path) -> None:
    if (
        not args.dry_run
        and db_path == DEFAULT_DB.resolve()
        and not args.confirm_default_db_repair
    ):
        raise SystemExit("default database repair requires --confirm-default-db-repair")


def run_zhihu_image_dedupe(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser().resolve()
    _require_default_db_confirmation(args, db_path)
    run_id = f"zhihu-{utc_stamp()}"
    run_dir = ensure_dir(POST_DETAIL_REPAIR_OUTPUT / run_id)
    summary_path = run_dir / "run_summary.json"
    manifest_path = run_dir / "media_moves.json"
    backup_path = POST_DETAIL_REPAIR_BACKUP_ROOT / run_id / db_path.name
    if not db_path.is_file():
        raise SystemExit(f"database does not exist: {db_path}")

    def read_plan() -> dict[str, Any]:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            return plan_zhihu_content_image_dedupe(conn)

    if args.dry_run:
        plan = read_plan()
        write_json(
            manifest_path,
            {"dry_run": True, "moves": plan["moves"]},
        )
        summary = _dedupe_summary(
            run_id=run_id,
            db_path=db_path,
            summary_path=summary_path,
            plan=plan,
            dry_run=True,
            backup={},
            move_results=[],
            manifest_path=manifest_path,
        )
        write_json(summary_path, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    bootstrap_database(db_path, sync_jobs=False)
    plan = read_plan()
    backup = create_sqlite_backup(db_path, backup_path)
    if any(_post_dedupe_changes(post) for post in plan["posts"]):
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                apply_zhihu_content_image_dedupe(conn, plan)
                conn.commit()
        except sqlite3.Error as exc:
            summary = _dedupe_summary(
                run_id=run_id,
                db_path=db_path,
                summary_path=summary_path,
                plan=plan,
                dry_run=False,
                backup=backup,
                move_results=[],
                manifest_path=manifest_path,
            )
            summary["status"] = "failed"
            summary["error"] = f"post_detail_repair_exception:{type(exc).__name__}:{exc}"
            write_json(manifest_path, {"dry_run": False, "moves": []})
            write_json(summary_path, summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 2
    move_results = move_deduped_media(
        plan["moves"],
        backup_media_root=POST_DETAIL_REPAIR_BACKUP_ROOT / run_id / "media",
    )
    write_json(manifest_path, {"dry_run": False, "moves": move_results})
    summary = _dedupe_summary(
        run_id=run_id,
        db_path=db_path,
        summary_path=summary_path,
        plan=plan,
        dry_run=False,
        backup=backup,
        move_results=move_results,
        manifest_path=manifest_path,
    )
    if any(item.get("status") == "error" for item in move_results):
        summary["status"] = "failed"
        summary["error"] = "post_detail_repair_media_move_failed"
        write_json(summary_path, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 2
    write_json(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    args = parse_args()
    _validate_repair_modes(args)
    if args.dedupe_images:
        return run_zhihu_image_dedupe(args)
    if args.batch_size <= 0 or args.max_items < 0:
        raise SystemExit("--batch-size must be positive; --max-items cannot be negative")
    if not str(args.keyword or "").strip():
        raise SystemExit("--keyword cannot be empty")

    db_path = Path(args.db).expanduser().resolve()
    _require_default_db_confirmation(args, db_path)
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
            refresh_all=args.refresh_all,
        )
        initial_waived_pending = waived_pending_count(
            conn,
            args.platform,
            refresh_all=args.refresh_all,
        )
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
        "selection": "refresh_all" if args.refresh_all else "pending_detail",
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
    if args.refresh_all:
        base_summary["selection"] = "refresh_all"
        base_summary["target_results"] = build_target_results(
            targets=targets,
            rejected=rejected,
            statuses={},
            candidate_failures=[],
            executed=False,
        )
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
                    statuses = (
                        repaired_rows(conn, args.platform, target_ids, refresh_all=True)
                        if args.refresh_all
                        else repaired_rows(conn, args.platform, target_ids)
                    )
                    remaining_pending = (
                        pending_count(conn, args.platform, refresh_all=True)
                        if args.refresh_all
                        else pending_count(conn, args.platform)
                    )
                    remaining_waived_pending = (
                        waived_pending_count(conn, args.platform, refresh_all=True)
                        if args.refresh_all
                        else waived_pending_count(conn, args.platform)
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
    except Exception as exc:
        if latch.signal_received is not None:
            interrupt_signum = int(latch.signal_received)
            interrupt_exception = f"{type(exc).__name__}: {exc}"
        elif isinstance(
            exc,
            (OSError, sqlite3.Error, RuntimeError, subprocess.SubprocessError, ValueError),
        ):
            fatal_error = f"post_detail_repair_exception:{type(exc).__name__}:{exc}"
            _state_fail_open(state, fatal_error)
        else:
            raise
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
                final_target_ids = [item["platform_post_id"] for item in targets]
                final_statuses = (
                    repaired_rows(conn, args.platform, final_target_ids, refresh_all=True)
                    if args.refresh_all
                    else repaired_rows(conn, args.platform, final_target_ids)
                )
                final_pending = (
                    pending_count(conn, args.platform, refresh_all=True)
                    if args.refresh_all
                    else pending_count(conn, args.platform)
                )
                final_waived_pending = (
                    waived_pending_count(conn, args.platform, refresh_all=True)
                    if args.refresh_all
                    else waived_pending_count(conn, args.platform)
                )
            recovered_ids = sorted(
                post_id for post_id, value in final_statuses.items() if value["recovered"]
            )
            remaining_ids = sorted(
                {item["platform_post_id"] for item in targets} - set(recovered_ids)
            )
            if latch.signal_received is not None and interrupt_signum is None:
                interrupt_signum = int(latch.signal_received)
                interrupt_record = interrupt_details(
                    interrupt_signum,
                    forced_termination=forced_termination,
                    exception=interrupt_exception,
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
            if args.refresh_all:
                summary["target_results"] = build_target_results(
                    targets=targets,
                    rejected=rejected,
                    statuses=final_statuses,
                    candidate_failures=candidate_failures,
                    executed=True,
                )
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
            if interrupt_signum is None and latch.signal_received is not None:
                interrupt_signum = int(latch.signal_received)
                interrupt_record = interrupt_details(
                    interrupt_signum,
                    forced_termination=forced_termination,
                    exception=interrupt_exception,
                )
            if interrupt_signum is None:
                raise
            fallback_status = (
                outcome if outcome in {"failed", *COMPLETED_REPAIR_OUTCOMES} else "failed"
            )
            fallback = {
                **base_summary,
                "status": fallback_status,
                "error": (
                    ""
                    if fallback_status in COMPLETED_REPAIR_OUTCOMES
                    else fatal_error or operator_interrupt_error(interrupt_signum)
                ),
                "backup": backup,
                "batch_results": batch_results,
                "finished_at": utc_iso(),
                "tail_error": f"{type(exc).__name__}: {exc}",
            }
            if args.refresh_all:
                fallback["target_results"] = build_target_results(
                    targets=targets,
                    rejected=rejected,
                    statuses={},
                    candidate_failures=candidate_failures,
                    executed=True,
                )
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
        exit_signum = latch.signal_received
        if exit_signum is not None:
            return 128 + int(exit_signum)
        return 0 if outcome in COMPLETED_REPAIR_OUTCOMES else 2
    finally:
        latch.restore()


if __name__ == "__main__":
    raise SystemExit(main())
