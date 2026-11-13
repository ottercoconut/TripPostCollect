"""作者资料回填。

小红书离线补认证两列，抖音联网补主页、简介和认证：只填当前为 NULL 的列，不覆盖已有值。
知乎离线把三处同为 0 的关注数改为 NULL，不访问平台，也不改其他列或其他 JSON 键。
取不回或不一致的行保留原值，不删除。抖音写入的是回填时刻的作者资料，不是原始抓取时刻的。
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

from trippostcollect.application.failures import extract_stdout_json
from trippostcollect.application.repair_runtime import (
    _complete_no_op,
    _state_fail_open,
    create_sqlite_backup,
    interrupt_details,
    interrupted_batch_result,
    print_repair_stream,
    run_repair_child,
    terminalize_repair_interrupt,
    utc_iso,
    utc_stamp,
    write_json,
)
from trippostcollect.core.execution_state import FrozenExecutionState
from trippostcollect.core.paths import (
    AUTHOR_PROFILE_BACKFILL_BACKUP_ROOT,
    AUTHOR_PROFILE_BACKFILL_OUTPUT,
    AUTHOR_PROFILE_BACKFILL_RUNTIME_ROOT,
    DEFAULT_DB,
    FORMAL_CRAWL_CONTRACT,
    PROJECT_ROOT,
    ensure_dir,
)
from trippostcollect.records.sanitization import public_payload
from trippostcollect.runtime.process import operator_interrupt_error
from trippostcollect.xhs.leases import DeferredTerminationSignals


PLATFORMS = ("xhs", "douyin", "zhihu")
XHS_COLUMNS = ("author_verified", "author_verified_text")
DOUYIN_COLUMNS = (
    "author_profile_url",
    "author_description",
    "author_verified",
    "author_verified_text",
)
ZHIHU_COLUMNS = ("author_following_count",)
COLUMNS = {"xhs": XHS_COLUMNS, "douyin": DOUYIN_COLUMNS, "zhihu": ZHIHU_COLUMNS}
AWEME_ID_RE_TEXT = r"^[0-9]+$"
CHILD_INTERRUPT_GRACE_SECONDS = None
INTERRUPT_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="回填小红书认证、抖音作者资料，或把知乎三处同为 0 的关注数改为 NULL。",
    )
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--confirm-default-db-backfill",
        action="store_true",
        help="写默认库 data/trippostcollect.sqlite 时必须显式确认。",
    )
    parser.add_argument("--max-authors", type=int, default=0, help="本轮最多处理的作者数；0 表示全部。")
    parser.add_argument("--author-id", action="append", dest="author_ids", default=[])
    parser.add_argument("--keyword", default="青岛旅游", help="抖音可见行为搜索词。")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--headless", action="store_true")
    return parser.parse_args(argv)


def connect_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def column_counts(connection: sqlite3.Connection, platform: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for column in COLUMNS[platform]:
        row = connection.execute(
            f"SELECT COUNT({column}) FROM web_posts WHERE platform_key=?",
            (platform,),
        ).fetchone()
        counts[column] = int(row[0] or 0)
    return counts


def author_key(row: sqlite3.Row) -> str:
    author_id = str(row["author_platform_id"] or "").strip()
    if author_id:
        return author_id
    return f"post:{row['platform_post_id']}"


def _author_id_clause(author_ids: list[str]) -> tuple[str, list[str]]:
    cleaned = [str(item).strip() for item in author_ids if str(item).strip()]
    if not cleaned:
        return "", []
    placeholders = ",".join("?" for _ in cleaned)
    return f" AND author_platform_id IN ({placeholders})", cleaned


def load_rows(connection: sqlite3.Connection, platform: str, author_ids: list[str]) -> list[sqlite3.Row]:
    clause, params = _author_id_clause(author_ids)
    if platform == "xhs":
        where = "platform_key='xhs' AND author_verified IS NULL"
    else:
        where = (
            "platform_key='douyin' AND ("
            "author_profile_url IS NULL OR author_description IS NULL OR author_verified IS NULL)"
            # 已回填过的行不再重复联网：作者没有简介等平台本就不提供的列会一直为空。
            " AND NOT (json_valid(author_json) AND json_extract(author_json, '$.profile_backfill') IS NOT NULL)"
        )
    return list(
        connection.execute(
            f"""
            SELECT id, platform_post_id, author_platform_id, author_profile_url,
                   author_description, author_verified, author_verified_text,
                   author_json, raw_sample_json, content_text, author_display_name,
                   author_followers_count, author_following_count, published_at, created_at
            FROM web_posts
            WHERE {where}{clause}
            ORDER BY created_at DESC, id DESC
            """,
            params,
        ).fetchall()
    )


def group_authors(rows: list[sqlite3.Row], *, max_authors: int) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = author_key(row)
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {"author_platform_id": key, "rows": []}
            grouped[key] = bucket
        bucket["rows"].append(row)
    authors = list(grouped.values())
    if max_authors > 0:
        authors = authors[:max_authors]
    return authors


def projection_from_row(raw_sample_json: str) -> tuple[dict[str, Any] | None, str]:
    try:
        payload = json.loads(raw_sample_json or "")
    except json.JSONDecodeError:
        return None, "parse_failed"
    if not isinstance(payload, dict):
        return None, "parse_failed"
    encoded = payload.get("creator_profile_json")
    if encoded in (None, "", {}):
        return None, "empty_projection"
    if isinstance(encoded, str):
        if not encoded.strip():
            return None, "empty_projection"
        try:
            profile = json.loads(encoded)
        except json.JSONDecodeError:
            return None, "parse_failed"
    elif isinstance(encoded, dict):
        profile = encoded
    else:
        return None, "parse_failed"
    if not isinstance(profile, dict) or not profile:
        return None, "empty_projection"
    return profile, ""


def xhs_fields_for_row(
    row: sqlite3.Row,
    verified_fields: Any,
) -> tuple[dict[str, Any], str]:
    profile, reason = projection_from_row(str(row["raw_sample_json"] or ""))
    if profile is None:
        return {}, reason
    fields = verified_fields(profile)
    if not fields:
        return {}, "verification_unknown"
    return fields, ""


def null_assignments(row: sqlite3.Row, fields: dict[str, Any], columns: tuple[str, ...]) -> dict[str, Any]:
    assignments: dict[str, Any] = {}
    for column in columns:
        if row[column] is None and fields.get(column) is not None:
            assignments[column] = fields[column]
    return assignments


def merge_profile_backfill(existing: str, backfill: dict[str, Any]) -> str:
    try:
        payload = json.loads(existing or "")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["profile_backfill"] = public_payload(backfill)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def update_null_columns(
    connection: sqlite3.Connection,
    *,
    platform: str,
    row_id: int,
    assignments: dict[str, Any],
    author_json: str | None = None,
) -> None:
    sets = [f"{column}=?" for column in assignments]
    values: list[Any] = list(assignments.values())
    if author_json is not None:
        sets.append("author_json=?")
        values.append(author_json)
    sets.append("updated_at=datetime('now')")
    values.extend([row_id, platform])
    connection.execute(
        f"UPDATE web_posts SET {', '.join(sets)} WHERE id=? AND platform_key=?",
        values,
    )


def apply_xhs_authors(
    connection: sqlite3.Connection,
    authors: list[dict[str, Any]],
    verified_fields: Any,
) -> dict[str, Any]:
    changed = 0
    success = 0
    partial = 0
    unrecovered: list[dict[str, str]] = []
    for author in authors:
        row_states: list[str] = []
        row_failures: list[dict[str, str]] = []
        for row in author["rows"]:
            fields, reason = xhs_fields_for_row(row, verified_fields)
            if reason:
                row_states.append("unrecovered")
                row_failures.append({"author_platform_id": author["author_platform_id"], "reason": reason})
                continue
            current = connection.execute(
                "SELECT author_verified, author_verified_text FROM web_posts WHERE id=?",
                (row["id"],),
            ).fetchone()
            assignments = null_assignments(current, fields, XHS_COLUMNS)
            if not assignments:
                row_states.append("unrecovered")
                row_failures.append(
                    {"author_platform_id": author["author_platform_id"], "reason": "verification_unknown"}
                )
                continue
            update_null_columns(
                connection,
                platform="xhs",
                row_id=int(row["id"]),
                assignments=assignments,
            )
            changed += 1
            after = connection.execute(
                "SELECT author_verified, author_verified_text FROM web_posts WHERE id=?",
                (row["id"],),
            ).fetchone()
            if after["author_verified"] is None:
                row_states.append("unrecovered")
            elif after["author_verified"] == 1 and after["author_verified_text"] is None:
                row_states.append("partial")
            else:
                row_states.append("success")
        if row_states and all(state == "success" for state in row_states):
            success += 1
        elif any(state in {"success", "partial"} for state in row_states):
            partial += 1
        else:
            unrecovered.extend(row_failures)
    return {
        "changed_row_count": changed,
        "success_count": success,
        "partial_count": partial,
        "unrecovered": unrecovered,
    }


def douyin_works(rows: list[sqlite3.Row]) -> tuple[list[dict[str, str]], str]:
    works: list[dict[str, str]] = []
    for row in rows:
        post_id = str(row["platform_post_id"] or "")
        if re.fullmatch(AWEME_ID_RE_TEXT, post_id) is None:
            continue
        works.append({"aweme_id": post_id, "platform_post_id": post_id})
        if len(works) == 3:
            break
    if not works:
        return [], "invalid_aweme_id"
    return works, ""


def prepare_douyin_authors(authors: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    ready: list[dict[str, Any]] = []
    unrecovered: list[dict[str, str]] = []
    for author in authors:
        author_id = str(author["author_platform_id"])
        if author_id.startswith("post:"):
            unrecovered.append({"author_platform_id": author_id, "reason": "missing_author_id"})
            continue
        works, reason = douyin_works(author["rows"])
        if reason:
            unrecovered.append({"author_platform_id": author_id, "reason": reason})
            continue
        ready.append({**author, "works": works})
    return ready, unrecovered


def build_child_command(
    *,
    keyword: str,
    targets_path: Path,
    output_path: Path,
    headless: bool,
) -> list[str]:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "douyin_author_profile_worker.py"),
        "--targets",
        str(targets_path),
        "--keyword",
        keyword,
        "--output",
        str(output_path),
    ]
    if headless:
        command.append("--headless")
    return command


def apply_douyin_observation(
    connection: sqlite3.Connection,
    *,
    rows: list[sqlite3.Row],
    observation: dict[str, Any],
    run_id: str,
    observed_at: str,
) -> tuple[int, str]:
    fields = observation.get("fields") if isinstance(observation.get("fields"), dict) else {}
    changed = 0
    still_null = False
    backfill = {
        "observed_at": observed_at,
        "run_id": run_id,
        "source_aweme_id": str(observation.get("source_aweme_id") or ""),
        "author_verify_evidence": observation.get("author_verify_evidence") or {},
    }
    for row in rows:
        current = connection.execute(
            """
            SELECT id, author_profile_url, author_description, author_verified,
                   author_verified_text, author_json
            FROM web_posts WHERE id=? AND platform_key='douyin'
            """,
            (row["id"],),
        ).fetchone()
        assignments = null_assignments(current, fields, DOUYIN_COLUMNS)
        if assignments:
            update_null_columns(
                connection,
                platform="douyin",
                row_id=int(current["id"]),
                assignments=assignments,
                author_json=merge_profile_backfill(str(current["author_json"] or ""), backfill),
            )
            changed += 1
        after = connection.execute(
            """
            SELECT author_profile_url, author_description, author_verified
            FROM web_posts WHERE id=?
            """,
            (row["id"],),
        ).fetchone()
        if any(after[column] is None for column in ("author_profile_url", "author_description", "author_verified")):
            still_null = True
    if changed == 0:
        return 0, "profile_insufficient"
    return changed, "partial" if still_null else "success"


def child_payload(child_run: dict[str, Any]) -> dict[str, Any]:
    wrapper = extract_stdout_json(str(child_run.get("stdout") or ""))
    path_value = str(wrapper.get("child_summary") or "")
    blocker = str(wrapper.get("runtime_blocker") or "")
    payload: dict[str, Any] = {}
    if path_value:
        try:
            loaded = json.loads(Path(path_value).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            payload = loaded
    if blocker and not payload.get("runtime_blocker"):
        payload["runtime_blocker"] = blocker
    if not payload:
        payload = {
            "observations": [],
            "unrecovered": [],
            "runtime_blocker": blocker or "browser_launch_failed",
        }
    cleaned = public_payload(payload)
    return cleaned if isinstance(cleaned, dict) else {}


def _reason_counts(items: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        reason = str(item.get("reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _sample(authors: list[dict[str, Any]], platform: str) -> list[dict[str, Any]]:
    sample = []
    for author in authors[:10]:
        nulls: set[str] = set()
        for row in author["rows"]:
            for column in COLUMNS[platform]:
                if row[column] is None:
                    nulls.add(column)
        sample.append(
            {
                "author_platform_id": author["author_platform_id"],
                "row_count": len(author["rows"]),
                "null_columns": sorted(nulls),
            }
        )
    return sample


def _report(
    *,
    status: str,
    run_id: str,
    platform: str,
    dry_run: bool,
    authors: list[dict[str, Any]],
    before: dict[str, int],
    after: dict[str, int] | None,
    success: int = 0,
    partial: int = 0,
    unrecovered: list[dict[str, str]] | None = None,
    changed_row_count: int = 0,
    batch_plan: list[dict[str, Any]] | None = None,
    backup: dict[str, Any] | None = None,
    runtime_blocker: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    unrecovered = unrecovered or []
    # 同一作者多行失败只在作者列表里保留一次，原因计数仍按行。
    seen: set[str] = set()
    author_ids: list[str] = []
    for item in unrecovered:
        author_id = str(item.get("author_platform_id") or "")
        if author_id and author_id not in seen:
            seen.add(author_id)
            author_ids.append(author_id)
    report = {
        "status": status,
        "run_id": run_id,
        "platform": platform,
        "dry_run": dry_run,
        "target_author_count": len(authors),
        "target_row_count": sum(len(author["rows"]) for author in authors),
        "success_count": success,
        "partial_count": partial,
        "unrecovered_count": len(author_ids),
        "unrecovered_by_reason": _reason_counts(unrecovered),
        "changed_row_count": changed_row_count,
        "column_counts": {
            column: {"before": before.get(column, 0), "after": (after or before).get(column, 0)}
            for column in COLUMNS[platform]
        },
        "unrecovered_author_ids": author_ids,
        "sample": _sample(authors, platform),
        "batch_plan": batch_plan or [],
        "backup": backup,
        "runtime_blocker": runtime_blocker,
        "values_are_backfill_time": platform == "douyin",
    }
    if extra:
        report.update(extra)
    cleaned = public_payload(report)
    return cleaned if isinstance(cleaned, dict) else report


def _chunked(values: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _finish_state_success(state: FrozenExecutionState, evidence: dict[str, Any]) -> None:
    state.complete("command_executed", evidence=evidence)
    state.begin("artifacts_verified")
    state.complete("artifacts_verified", evidence={"skipped": True, "reason": "author_profile_columns_only"})
    state.begin("persistence_verified")
    state.complete("persistence_verified", evidence=evidence)
    state.finalize(outcome="completed", evidence=evidence)


_MISSING = object()


def _format_count(value: Any) -> str:
    if value is _MISSING:
        return "缺失"
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False)


def _is_exact_zero(value: Any) -> bool:
    return type(value) is int and value == 0


def _is_absent_count(value: Any) -> bool:
    return value is None or value is _MISSING


def _read_following(text: str | None) -> tuple[Any, str]:
    try:
        payload = json.loads("" if text is None else str(text))
    except json.JSONDecodeError:
        return _MISSING, "无法解析"
    if not isinstance(payload, dict):
        return _MISSING, "不是对象"
    if "following_count" not in payload:
        return _MISSING, ""
    return payload["following_count"], ""


# #182 解析修复部署前入库的行才可能是补零库存。此后关注数缺键写 NULL，三处同为 0 只能来自
# 平台键上的真实 0，不得再改；这类行记为异常，由人工核对。值与 web_posts.created_at 同为 UTC。
ZHIHU_FOLLOWING_FIX_CREATED_BEFORE = "2026-11-14 00:00:00"


def classify_zhihu_following_row(row: sqlite3.Row) -> tuple[str, str]:
    """修复前入库、三处同为整数 0 才是目标；三处都缺失则跳过；其余记为异常且不改。"""
    column = row["author_following_count"]
    author_value, author_error = _read_following(row["author_json"])
    raw_value, raw_error = _read_following(row["raw_sample_json"])
    if author_error:
        return "anomaly", f"author_json {author_error}，不能确认三处关注数同为 0"
    if raw_error:
        return "anomaly", f"raw_sample_json {raw_error}，不能确认三处关注数同为 0"
    observed = (column, author_value, raw_value)
    if all(_is_exact_zero(value) for value in observed):
        created_at = str(row["created_at"] or "")
        if created_at and created_at < ZHIHU_FOLLOWING_FIX_CREATED_BEFORE:
            return "target", ""
        return "anomaly", (
            f"入库时间 {created_at or '缺失'} 不早于 {ZHIHU_FOLLOWING_FIX_CREATED_BEFORE}（UTC），"
            "三处同为 0 可能是真实值，不自动改"
        )
    if all(_is_absent_count(value) for value in observed):
        return "absent", ""
    detail = (
        f"列={_format_count(column)}，"
        f"author_json={_format_count(author_value)}，"
        f"raw_sample_json={_format_count(raw_value)}"
    )
    return "anomaly", f"三处关注数不是同为 0：{detail}"


def null_following_count(text: str) -> str:
    """只把对象里的 following_count 从整数 0 改成 null，保留其余键的顺序和值。"""
    payload = json.loads(text)
    if (
        not isinstance(payload, dict)
        or type(payload.get("following_count")) is not int
        or payload["following_count"] != 0
    ):
        raise ValueError("following_count is not integer zero")
    payload["following_count"] = None
    return json.dumps(payload, ensure_ascii=False)


def load_zhihu_following_rows(
    connection: sqlite3.Connection,
    author_ids: list[str],
) -> dict[str, Any]:
    clause, params = _author_id_clause(author_ids)
    rows = connection.execute(
        f"""
        SELECT id, platform_post_id, author_platform_id, author_profile_url,
               author_description, author_verified, author_verified_text,
               author_json, raw_sample_json, content_text, author_display_name,
               author_followers_count, author_following_count, published_at, created_at
        FROM web_posts
        WHERE platform_key='zhihu'{clause}
        ORDER BY created_at DESC, id DESC
        """,
        params,
    ).fetchall()
    targets: list[sqlite3.Row] = []
    anomalies: list[dict[str, Any]] = []
    for row in rows:
        kind, reason = classify_zhihu_following_row(row)
        if kind == "target":
            targets.append(row)
            continue
        if kind != "anomaly":
            continue
        anomalies.append(
            {
                "id": int(row["id"]),
                "platform_post_id": str(row["platform_post_id"] or ""),
                "author_platform_id": str(row["author_platform_id"] or ""),
                "reason": reason,
            }
        )
    return {"targets": targets, "anomalies": anomalies}


def apply_zhihu_following(
    connection: sqlite3.Connection,
    authors: list[dict[str, Any]],
) -> dict[str, Any]:
    changed = 0
    success = 0
    partial = 0
    unrecovered: list[dict[str, str]] = []
    for author in authors:
        row_states: list[str] = []
        for row in author["rows"]:
            current = connection.execute(
                """
                SELECT id, author_following_count, author_json, raw_sample_json, created_at
                FROM web_posts
                WHERE id=? AND platform_key='zhihu'
                """,
                (row["id"],),
            ).fetchone()
            kind, reason = ("anomaly", "following_count_not_uniform_zero")
            if current is not None:
                kind, reason = classify_zhihu_following_row(current)
            if current is None or kind != "target":
                row_states.append("unrecovered")
                unrecovered.append(
                    {
                        "author_platform_id": str(author["author_platform_id"]),
                        "reason": reason or "following_count_not_uniform_zero",
                    }
                )
                continue
            updated = connection.execute(
                """
                UPDATE web_posts
                SET author_following_count=NULL, author_json=?, raw_sample_json=?
                WHERE id=? AND platform_key='zhihu' AND author_following_count=0
                """,
                (
                    null_following_count(str(current["author_json"])),
                    null_following_count(str(current["raw_sample_json"])),
                    int(current["id"]),
                ),
            )
            if updated.rowcount != 1:
                row_states.append("unrecovered")
                unrecovered.append(
                    {
                        "author_platform_id": str(author["author_platform_id"]),
                        "reason": "following_count_not_uniform_zero",
                    }
                )
                continue
            changed += 1
            row_states.append("success")
        if row_states and all(state == "success" for state in row_states):
            success += 1
        elif any(state == "success" for state in row_states):
            partial += 1
    return {
        "changed_row_count": changed,
        "success_count": success,
        "partial_count": partial,
        "unrecovered": unrecovered,
    }


def _merged_extra(
    base: dict[str, Any] | None,
    extra: dict[str, Any] | None,
) -> dict[str, Any] | None:
    merged = dict(base or {})
    if extra:
        merged.update(extra)
    return merged or None


def run_offline(
    *,
    platform: str,
    db_path: Path,
    authors: list[dict[str, Any]],
    before: dict[str, int],
    dry_run: bool,
    run_id: str,
    run_dir: Path,
    state: FrozenExecutionState,
    summary_path: Path,
    apply: Callable[[sqlite3.Connection, list[dict[str, Any]]], dict[str, Any]],
    report_extra: dict[str, Any] | None = None,
) -> int:
    plan = [{"batch": 1, "mode": "offline", "target_author_count": len(authors), "target_row_count": sum(len(a["rows"]) for a in authors)}]
    if dry_run:
        report = _report(
            status="planned",
            run_id=run_id,
            platform=platform,
            dry_run=True,
            authors=authors,
            before=before,
            after=before,
            batch_plan=plan,
            extra=report_extra,
        )
        write_json(summary_path, report)
        print(json.dumps({"summary": str(summary_path), **_public_counts(report)}, ensure_ascii=False, indent=2))
        return 0
    if not authors:
        _complete_no_op(state, reason="no_author_profile_targets")
        report = _report(
            status="completed",
            run_id=run_id,
            platform=platform,
            dry_run=False,
            authors=[],
            before=before,
            after=before,
            extra=_merged_extra(report_extra, {"no_op": True}),
        )
        write_json(summary_path, report)
        print(json.dumps({"summary": str(summary_path), **_public_counts(report)}, ensure_ascii=False, indent=2))
        return 0
    backup_path = AUTHOR_PROFILE_BACKFILL_BACKUP_ROOT / run_id / db_path.name
    latch = DeferredTerminationSignals(INTERRUPT_SIGNALS)
    latch.install()
    backup: dict[str, Any] = {}
    outcome = "failed"
    fatal = ""
    applied: dict[str, Any] = {
        "changed_row_count": 0,
        "success_count": 0,
        "partial_count": 0,
        "unrecovered": [],
    }
    try:
        if latch.signal_received is None:
            backup = create_sqlite_backup(db_path, backup_path)
            state.begin("command_executed")
            connection = sqlite3.connect(db_path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("BEGIN IMMEDIATE")
                applied = apply(connection, authors)
                if latch.signal_received is None:
                    connection.execute("COMMIT")
                else:
                    connection.execute("ROLLBACK")
                    applied = {
                        "changed_row_count": 0,
                        "success_count": 0,
                        "partial_count": 0,
                        "unrecovered": [],
                    }
            finally:
                connection.close()
        if latch.signal_received is None:
            _finish_state_success(state, {"backup": backup, "changed_row_count": applied["changed_row_count"]})
            outcome = "completed"
    except Exception as exc:
        fatal = f"author_profile_backfill_exception:{type(exc).__name__}"
        _state_fail_open(state, fatal)
    finally:
        signum = latch.signal_received
        latch.restore()
    after = before
    with connect_readonly(db_path) as connection:
        after = column_counts(connection, platform)
    if signum is not None:
        terminalize_repair_interrupt(
            state,
            error=operator_interrupt_error(int(signum)),
            evidence={"backup": backup},
        )
        outcome = "failed"
        fatal = operator_interrupt_error(int(signum))
    error_extra = (
        {"error": fatal, "interrupt": interrupt_details(int(signum), forced_termination=False)}
        if signum is not None
        else ({"error": fatal} if fatal else None)
    )
    report = _report(
        status=outcome,
        run_id=run_id,
        platform=platform,
        dry_run=False,
        authors=authors,
        before=before,
        after=after,
        success=int(applied["success_count"]),
        partial=int(applied["partial_count"]),
        unrecovered=list(applied["unrecovered"]),
        changed_row_count=int(applied["changed_row_count"]),
        batch_plan=plan,
        backup=backup or None,
        extra=_merged_extra(report_extra, error_extra),
    )
    write_json(summary_path, report)
    if signum is not None:
        print_repair_stream(operator_interrupt_error(int(signum)), sys.stderr)
        return 128 + int(signum)
    print(json.dumps({"summary": str(summary_path), **_public_counts(report)}, ensure_ascii=False, indent=2))
    return 0 if outcome == "completed" else 2


def run_xhs(
    *,
    db_path: Path,
    authors: list[dict[str, Any]],
    before: dict[str, int],
    dry_run: bool,
    run_id: str,
    run_dir: Path,
    state: FrozenExecutionState,
    summary_path: Path,
    verified_fields: Any,
) -> int:
    return run_offline(
        platform="xhs",
        db_path=db_path,
        authors=authors,
        before=before,
        dry_run=dry_run,
        run_id=run_id,
        run_dir=run_dir,
        state=state,
        summary_path=summary_path,
        apply=lambda connection, selected: apply_xhs_authors(connection, selected, verified_fields),
    )


def _public_counts(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "platform": report["platform"],
        "dry_run": report["dry_run"],
        "target_author_count": report["target_author_count"],
        "target_row_count": report["target_row_count"],
        "success_count": report["success_count"],
        "partial_count": report["partial_count"],
        "unrecovered_count": report["unrecovered_count"],
        "changed_row_count": report["changed_row_count"],
        "runtime_blocker": report["runtime_blocker"],
        "status": report["status"],
    }


def run_douyin(
    *,
    db_path: Path,
    authors: list[dict[str, Any]],
    before: dict[str, int],
    dry_run: bool,
    run_id: str,
    run_dir: Path,
    state: FrozenExecutionState,
    summary_path: Path,
    keyword: str,
    batch_size: int,
    headless: bool,
) -> int:
    ready, skipped = prepare_douyin_authors(authors)
    batch_plan: list[dict[str, Any]] = []
    for index, batch in enumerate(_chunked(ready, batch_size), start=1):
        manifest = {
            "authors": [
                {"author_platform_id": author["author_platform_id"], "works": author["works"]}
                for author in batch
            ]
        }
        targets_path = write_json(run_dir / "batches" / f"batch-{index:04d}-targets.json", manifest)
        output_path = run_dir / "children" / f"batch-{index:04d}" / "observations.json"
        command = build_child_command(
            keyword=keyword,
            targets_path=targets_path,
            output_path=output_path,
            headless=headless,
        )
        batch_plan.append(
            {
                "batch": index,
                "mode": "douyin_author_profile",
                "author_count": len(batch),
                "author_ids": [author["author_platform_id"] for author in batch],
                "work_count": sum(len(author["works"]) for author in batch),
                "targets_path": str(targets_path),
                "output_path": str(output_path),
                "command": command,
                "authors": batch,
            }
        )
    if dry_run:
        public_plan = [{key: value for key, value in item.items() if key != "authors"} for item in batch_plan]
        report = _report(
            status="planned",
            run_id=run_id,
            platform="douyin",
            dry_run=True,
            authors=authors,
            before=before,
            after=before,
            unrecovered=skipped,
            batch_plan=public_plan,
        )
        write_json(summary_path, report)
        print(json.dumps({"summary": str(summary_path), **_public_counts(report)}, ensure_ascii=False, indent=2))
        return 0
    if not ready and not skipped:
        _complete_no_op(state, reason="no_author_profile_targets")
        report = _report(
            status="completed",
            run_id=run_id,
            platform="douyin",
            dry_run=False,
            authors=[],
            before=before,
            after=before,
            extra={"no_op": True},
        )
        write_json(summary_path, report)
        print(json.dumps({"summary": str(summary_path), **_public_counts(report)}, ensure_ascii=False, indent=2))
        return 0
    latch = DeferredTerminationSignals(INTERRUPT_SIGNALS)
    latch.install()
    backup: dict[str, Any] = {}
    success = 0
    partial = 0
    changed = 0
    unrecovered = list(skipped)
    blocker = ""
    interrupt_signum: int | None = None
    forced = False
    batch_results: list[dict[str, Any]] = []
    try:
        if ready and latch.signal_received is None:
            backup = create_sqlite_backup(
                db_path,
                AUTHOR_PROFILE_BACKFILL_BACKUP_ROOT / run_id / db_path.name,
            )
            state.begin("command_executed")
        for item in batch_plan:
            if latch.signal_received is not None:
                interrupt_signum = int(latch.signal_received)
                break
            child_run = run_repair_child(
                item["command"],
                cwd=PROJECT_ROOT,
                log_dir=run_dir / "logs" / f"batch-{int(item['batch']):04d}",
                state_path=state.path,
                interrupt_signal=lambda: latch.signal_received,
                grace_seconds=CHILD_INTERRUPT_GRACE_SECONDS,
            )
            if child_run.get("interrupt_signum") is not None:
                interrupt_signum = int(child_run["interrupt_signum"])
                forced = bool(child_run.get("forced_termination"))
                batch_results.append(interrupted_batch_result(item, child_run))
                break
            payload = child_payload(child_run)
            observed_at = utc_iso()
            observations = {
                str(entry.get("author_platform_id") or ""): entry
                for entry in payload.get("observations") or []
                if isinstance(entry, dict)
            }
            connection = sqlite3.connect(db_path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("BEGIN IMMEDIATE")
                for author in item["authors"]:
                    observation = observations.get(author["author_platform_id"])
                    if observation is None:
                        continue
                    row_changes, status = apply_douyin_observation(
                        connection,
                        rows=author["rows"],
                        observation=observation,
                        run_id=run_id,
                        observed_at=observed_at,
                    )
                    changed += row_changes
                    if status == "success":
                        success += 1
                    elif status == "partial":
                        partial += 1
                    else:
                        unrecovered.append(
                            {"author_platform_id": author["author_platform_id"], "reason": status}
                        )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
            for entry in payload.get("unrecovered") or []:
                if isinstance(entry, dict):
                    unrecovered.append(
                        {
                            "author_platform_id": str(entry.get("author_platform_id") or ""),
                            "reason": str(entry.get("reason") or "unknown"),
                        }
                    )
            child_blocker = str(payload.get("runtime_blocker") or "")
            batch_results.append(
                {
                    "batch": item["batch"],
                    "author_count": item["author_count"],
                    "exit_code": child_run.get("returncode"),
                    "runtime_blocker": child_blocker,
                }
            )
            if child_blocker:
                blocker = child_blocker
                break
        if interrupt_signum is None and latch.signal_received is not None:
            interrupt_signum = int(latch.signal_received)
        if interrupt_signum is not None:
            pass
        elif blocker:
            state.fail("command_executed", error=blocker, evidence={"batches": batch_results, "backup": backup})
        elif ready:
            _finish_state_success(state, {"batches": batch_results, "backup": backup, "changed_row_count": changed})
        else:
            _complete_no_op(state, reason="no_actionable_douyin_authors")
    except Exception as exc:
        if latch.signal_received is not None:
            interrupt_signum = int(latch.signal_received)
        else:
            blocker = blocker or f"author_profile_backfill_exception:{type(exc).__name__}"
            _state_fail_open(state, blocker)
    finally:
        held = latch.signal_received
        latch.restore()
    if interrupt_signum is None and held is not None:
        interrupt_signum = int(held)
    if interrupt_signum is not None:
        terminalize_repair_interrupt(
            state,
            error=operator_interrupt_error(interrupt_signum),
            evidence={"backup": backup, "batches": batch_results},
        )
    after = before
    with connect_readonly(db_path) as connection:
        after = column_counts(connection, "douyin")
    status = "failed" if interrupt_signum is not None or blocker else "completed"
    public_plan = [{key: value for key, value in item.items() if key != "authors"} for item in batch_plan]
    extra = None
    if interrupt_signum is not None:
        extra = {"interrupt": interrupt_details(interrupt_signum, forced_termination=forced)}
    report = _report(
        status=status,
        run_id=run_id,
        platform="douyin",
        dry_run=False,
        authors=authors,
        before=before,
        after=after,
        success=success,
        partial=partial,
        unrecovered=unrecovered,
        changed_row_count=changed,
        batch_plan=public_plan,
        backup=backup or None,
        runtime_blocker=blocker,
        extra=extra,
    )
    write_json(summary_path, report)
    if interrupt_signum is not None:
        print_repair_stream(operator_interrupt_error(interrupt_signum), sys.stderr)
        return 128 + interrupt_signum
    print(json.dumps({"summary": str(summary_path), **_public_counts(report)}, ensure_ascii=False, indent=2))
    return 2 if blocker else 0


def main(argv: list[str] | None = None, *, verified_fields: Any = None) -> int:
    args = parse_args(argv)
    if args.max_authors < 0 or args.batch_size <= 0:
        raise SystemExit("--max-authors 不能为负；--batch-size 必须为正")
    if args.platform == "douyin" and not str(args.keyword or "").strip():
        raise SystemExit("抖音回填需要非空的 --keyword")
    db_path = Path(args.db).expanduser().resolve()
    if (
        not args.dry_run
        and db_path == DEFAULT_DB.resolve()
        and not args.confirm_default_db_backfill
    ):
        raise SystemExit("写默认库需要 --confirm-default-db-backfill")
    run_id = f"{args.platform}-{utc_stamp()}"
    run_dir = ensure_dir(AUTHOR_PROFILE_BACKFILL_OUTPUT / run_id)
    runtime_dir = ensure_dir(AUTHOR_PROFILE_BACKFILL_RUNTIME_ROOT / run_id)
    state_path = runtime_dir / "execution_state.json"
    summary_path = run_dir / "run_summary.json"
    anomalies: list[dict[str, Any]] = []
    with connect_readonly(db_path) as connection:
        if args.platform == "zhihu":
            classified = load_zhihu_following_rows(connection, list(args.author_ids or []))
            rows = classified["targets"]
            anomalies = list(classified["anomalies"])
        else:
            rows = load_rows(connection, args.platform, list(args.author_ids or []))
        before = column_counts(connection, args.platform)
    authors = group_authors(rows, max_authors=args.max_authors)
    manifest_path = write_json(
        run_dir / "targets.json",
        {
            "platform": args.platform,
            "authors": [
                {
                    "author_platform_id": author["author_platform_id"],
                    "row_ids": [int(row["id"]) for row in author["rows"]],
                }
                for author in authors
            ],
        },
    )
    state = FrozenExecutionState.create(
        state_path,
        run_id=run_id,
        job_key=f"author_profile_backfill:{args.platform}",
        site_key=args.platform,
        job_kind="author_profile_backfill",
        plan={
            "run_id": run_id,
            "platform": args.platform,
            "target_author_count": len(authors),
            "dry_run": bool(args.dry_run),
        },
        frozen_inputs=[FORMAL_CRAWL_CONTRACT, manifest_path],
        dry_run=bool(args.dry_run),
    )
    common = {
        "db_path": db_path,
        "authors": authors,
        "before": before,
        "dry_run": bool(args.dry_run),
        "run_id": run_id,
        "run_dir": run_dir,
        "state": state,
        "summary_path": summary_path,
    }
    if args.platform == "xhs":
        if verified_fields is None:
            raise SystemExit("小红书回填需要注入与入库相同的认证计算函数")
        return run_xhs(**common, verified_fields=verified_fields)
    if args.platform == "zhihu":
        return run_offline(
            platform="zhihu",
            apply=apply_zhihu_following,
            report_extra={"anomalies": anomalies, "anomaly_count": len(anomalies)},
            **common,
        )
    return run_douyin(
        **common,
        keyword=str(args.keyword),
        batch_size=int(args.batch_size),
        headless=bool(args.headless),
    )
