"""作者资料回填。

小红书离线补认证两列，抖音联网补主页、简介和认证：只填当前为 NULL 的列，不覆盖已有值。
知乎有两条互斥的离线修正，一次命令只跑一条：默认把三处同为 0 的关注数改为 NULL；
``--org-identity`` 只把作者页 ``isOrg`` 为 JSON 布尔 true 的机构号写成认证。
两条都不访问平台，也不改本次目标以外的列或 JSON 键。
取不回或不一致的行保留原值，不删除。抖音写入的是回填时刻的作者资料，不是原始抓取时刻的。
"""

from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

from trippostcollect.application.repair_runtime import (
    _complete_no_op,
    _state_fail_open,
    create_sqlite_backup,
    interrupt_details,
    print_repair_stream,
    run_repair_child,  # noqa: F401  测试用来确认 dry-run 不启动 child
    terminalize_repair_interrupt,
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
    ensure_dir,
)
from trippostcollect.records.sanitization import public_payload
from trippostcollect.runtime.process import operator_interrupt_error
from trippostcollect.xhs.leases import DeferredTerminationSignals


PLATFORMS = ("xhs", "zhihu")
XHS_COLUMNS = ("author_verified", "author_verified_text")
ZHIHU_COLUMNS = ("author_following_count",)
ZHIHU_ORG_COLUMNS = ("author_verified", "author_verified_text")
COLUMNS = {"xhs": XHS_COLUMNS, "zhihu": ZHIHU_COLUMNS}
ORG_IDENTITY_PREFIX = "机构号"
CHILD_INTERRUPT_GRACE_SECONDS = None
INTERRUPT_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="回填小红书认证，或离线修正知乎关注数与机构号认证。",
    )
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--org-identity",
        action="store_true",
        help="只回填知乎机构号认证。与默认的关注数修正互斥，不能和其他平台一起用。",
    )
    parser.add_argument(
        "--confirm-default-db-backfill",
        action="store_true",
        help="写默认库 data/trippostcollect.sqlite 时必须显式确认。",
    )
    parser.add_argument("--max-authors", type=int, default=0, help="本轮最多处理的作者数；0 表示全部。")
    parser.add_argument("--author-id", action="append", dest="author_ids", default=[])
    parser.add_argument("--keyword", default="青岛旅游", help="保留的搜索词参数，当前平台不使用。")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--headless", action="store_true")
    return parser.parse_args(argv)


def connect_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def column_counts(
    connection: sqlite3.Connection,
    platform: str,
    columns: tuple[str, ...] | None = None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for column in columns or COLUMNS[platform]:
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
    if platform != "xhs":
        raise ValueError(f"unsupported author backfill platform: {platform}")
    where = "platform_key='xhs' AND author_verified IS NULL"
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


def _reason_counts(items: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        reason = str(item.get("reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _sample(
    authors: list[dict[str, Any]],
    platform: str,
    columns: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    selected = columns or COLUMNS[platform]
    sample = []
    for author in authors[:10]:
        nulls: set[str] = set()
        for row in author["rows"]:
            for column in selected:
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
    columns: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    selected = columns or COLUMNS[platform]
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
            for column in selected
        },
        "unrecovered_author_ids": author_ids,
        "sample": _sample(authors, platform, selected),
        "batch_plan": batch_plan or [],
        "backup": backup,
        "runtime_blocker": runtime_blocker,
        "values_are_backfill_time": False,
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


def org_identity_text(current: Any) -> str:
    """有原文字时写成“机构号、<原文字>”；空文字写成“机构号”。已以“机构号”开头的不重复加。"""

    if not isinstance(current, str) or current == "":
        return ORG_IDENTITY_PREFIX
    if current.startswith(ORG_IDENTITY_PREFIX):
        return current
    return f"{ORG_IDENTITY_PREFIX}、{current}"


def _org_identity_predicate() -> str:
    # json_extract 会把 JSON true 收成整数 1，和数字 1 无法区分。json_type 才是严格布尔。
    return f"""
platform_key='zhihu'
AND json_valid(raw_sample_json)
AND json_type(raw_sample_json, '$.author_page_badges.isOrg') = 'true'
AND NOT (
    author_verified = 1
    AND IFNULL(author_verified_text, '') LIKE '{ORG_IDENTITY_PREFIX}%'
)
"""


def write_org_identity_sample(text: str, verified_text: str) -> str:
    """只改 raw_sample_json 的 author_verified 与 verified_text，保留其余键和顺序。"""

    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("raw_sample_json is not an object")
    payload["author_verified"] = 1
    payload["verified_text"] = verified_text
    return json.dumps(payload, ensure_ascii=False)


def zhihu_verification_distribution(connection: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """知乎全表认证分布：author_verified 的 1/0/null，以及文字是否以“机构号”开头。"""

    verified = {"1": 0, "0": 0, "null": 0}
    text_kinds = {"org_prefix": 0, "other": 0, "empty": 0}
    rows = connection.execute(
        """
        SELECT author_verified, author_verified_text
        FROM web_posts
        WHERE platform_key='zhihu'
        """
    )
    for row in rows:
        value = row["author_verified"]
        if value == 1:
            verified["1"] += 1
        elif value == 0:
            verified["0"] += 1
        else:
            verified["null"] += 1
        raw_text = row["author_verified_text"]
        if raw_text is None or raw_text == "":
            text_kinds["empty"] += 1
        elif isinstance(raw_text, str) and raw_text.startswith(ORG_IDENTITY_PREFIX):
            text_kinds["org_prefix"] += 1
        else:
            text_kinds["other"] += 1
    return {"author_verified": verified, "author_verified_text": text_kinds}


def load_zhihu_org_rows(
    connection: sqlite3.Connection,
    author_ids: list[str],
) -> list[sqlite3.Row]:
    """作者页 isOrg 为 JSON true、且尚未写成机构号认证的知乎行。"""

    clause, params = _author_id_clause(author_ids)
    return list(
        connection.execute(
            f"""
            SELECT id, platform_post_id, author_platform_id, author_profile_url,
                   author_description, author_verified, author_verified_text,
                   author_json, raw_sample_json, content_text, author_display_name,
                   author_followers_count, author_following_count, published_at, created_at
            FROM web_posts
            WHERE {_org_identity_predicate()}{clause}
            ORDER BY created_at DESC, id DESC
            """,
            params,
        ).fetchall()
    )


def apply_zhihu_org_identity(
    connection: sqlite3.Connection,
    authors: list[dict[str, Any]],
) -> dict[str, Any]:
    changed = 0
    success = 0
    partial = 0
    unrecovered: list[dict[str, str]] = []
    predicate = _org_identity_predicate()
    for author in authors:
        row_states: list[str] = []
        for row in author["rows"]:
            current = connection.execute(
                f"""
                SELECT id, author_verified_text, raw_sample_json
                FROM web_posts
                WHERE id=? AND {predicate}
                """,
                (row["id"],),
            ).fetchone()
            if current is None:
                row_states.append("unrecovered")
                unrecovered.append(
                    {
                        "author_platform_id": str(author["author_platform_id"]),
                        "reason": "org_identity_not_target",
                    }
                )
                continue
            text = org_identity_text(current["author_verified_text"])
            try:
                sample = write_org_identity_sample(str(current["raw_sample_json"]), text)
            except (json.JSONDecodeError, ValueError, TypeError):
                row_states.append("unrecovered")
                unrecovered.append(
                    {
                        "author_platform_id": str(author["author_platform_id"]),
                        "reason": "raw_sample_json_not_object",
                    }
                )
                continue
            updated = connection.execute(
                f"""
                UPDATE web_posts
                SET author_verified=1, author_verified_text=?, raw_sample_json=?
                WHERE id=? AND {predicate}
                """,
                (text, sample, int(current["id"])),
            )
            if updated.rowcount != 1:
                row_states.append("unrecovered")
                unrecovered.append(
                    {
                        "author_platform_id": str(author["author_platform_id"]),
                        "reason": "org_identity_not_target",
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
    columns: tuple[str, ...] | None = None,
    distribution: Callable[[sqlite3.Connection], dict[str, Any]] | None = None,
) -> int:
    selected = columns or COLUMNS[platform]
    dist_before = None
    if distribution is not None:
        with connect_readonly(db_path) as connection:
            dist_before = distribution(connection)

    def observed_extra(*, reread: bool) -> dict[str, Any] | None:
        if distribution is None or dist_before is None:
            return None
        after_dist = dist_before
        if reread:
            with connect_readonly(db_path) as connection:
                after_dist = distribution(connection)
        return {"verification_distribution": {"before": dist_before, "after": after_dist}}

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
            extra=_merged_extra(report_extra, observed_extra(reread=False)),
            columns=selected,
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
            extra=_merged_extra(
                _merged_extra(report_extra, {"no_op": True}),
                observed_extra(reread=False),
            ),
            columns=selected,
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
        after = column_counts(connection, platform, selected)
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
        extra=_merged_extra(_merged_extra(report_extra, observed_extra(reread=True)), error_extra),
        columns=selected,
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


def main(argv: list[str] | None = None, *, verified_fields: Any = None) -> int:
    args = parse_args(argv)
    if args.max_authors < 0 or args.batch_size <= 0:
        raise SystemExit("--max-authors 不能为负；--batch-size 必须为正")
    if args.org_identity and args.platform != "zhihu":
        raise SystemExit("--org-identity 只适用于 --platform zhihu")
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
    org_identity = args.platform == "zhihu" and bool(args.org_identity)
    count_columns = ZHIHU_ORG_COLUMNS if org_identity else None
    with connect_readonly(db_path) as connection:
        if org_identity:
            rows = load_zhihu_org_rows(connection, list(args.author_ids or []))
        elif args.platform == "zhihu":
            classified = load_zhihu_following_rows(connection, list(args.author_ids or []))
            rows = classified["targets"]
            anomalies = list(classified["anomalies"])
        else:
            rows = load_rows(connection, args.platform, list(args.author_ids or []))
        before = column_counts(connection, args.platform, count_columns)
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
            **(
                {"zhihu_mode": "org_identity" if org_identity else "following_null"}
                if args.platform == "zhihu"
                else {}
            ),
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
    if org_identity:
        return run_offline(
            platform="zhihu",
            apply=apply_zhihu_org_identity,
            report_extra={"mode": "org_identity"},
            columns=ZHIHU_ORG_COLUMNS,
            distribution=zhihu_verification_distribution,
            **common,
        )
    if args.platform == "zhihu":
        return run_offline(
            platform="zhihu",
            apply=apply_zhihu_following,
            report_extra={"anomalies": anomalies, "anomaly_count": len(anomalies)},
            **common,
        )
    raise SystemExit(f"unsupported author backfill platform: {args.platform}")
