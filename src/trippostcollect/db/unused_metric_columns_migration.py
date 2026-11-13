"""schema v24：删除 web_posts 上没有使用的转发、浏览和平台等级列。

已登记版本 24 时直接返回。三列都不在表上时只登记版本。列还在时，先在同一个
SAVEPOINT 里校验，任一条件不满足就回滚，不删列、不改 JSON。
"""

from __future__ import annotations

from hashlib import sha256
import json
import re
import sqlite3
from typing import Any


MIGRATION_VERSION = 24
MIGRATION_NAME = "remove_unused_metric_columns"
RETIRED_COLUMNS = (
    "post_reposts_count",
    "post_views_count",
    "author_platform_level",
)
_SAVEPOINT = "remove_unused_metric_columns_v24"
_COLUMN_RE = {column: re.compile(rf"\b{re.escape(column)}\b") for column in RETIRED_COLUMNS}
_VIEW_COUNT_EQUAL = """
(
    json_type(raw_sample_json, '$.view_count') = 'integer'
    AND CAST(json_extract(raw_sample_json, '$.view_count') AS INTEGER)
        = CAST(post_views_count AS INTEGER)
)
OR (
    json_type(raw_sample_json, '$.view_count') = 'real'
    AND CAST(json_extract(raw_sample_json, '$.view_count') AS REAL)
        = CAST(post_views_count AS REAL)
    AND CAST(json_extract(raw_sample_json, '$.view_count') AS INTEGER)
        = CAST(post_views_count AS INTEGER)
)
OR (
    json_type(raw_sample_json, '$.view_count') = 'text'
    AND TRIM(json_extract(raw_sample_json, '$.view_count'))
        = printf('%d', CAST(json_extract(raw_sample_json, '$.view_count') AS INTEGER))
    AND CAST(json_extract(raw_sample_json, '$.view_count') AS INTEGER)
        = CAST(post_views_count AS INTEGER)
)
"""


class UnusedMetricColumnMigrationError(RuntimeError):
    """v24 校验或迁移后核对失败。数据保持进入本迁移之前的状态。"""


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _column_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in conn.execute("PRAGMA table_info(web_posts)")}


def _migration_applied(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version=?",
        (MIGRATION_VERSION,),
    ).fetchone()
    return bool(row)


def _count(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def _violations(conn: sqlite3.Connection, present: set[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    if "post_reposts_count" in present:
        counts["post_reposts_count 非空"] = _count(
            conn,
            "SELECT COUNT(*) FROM web_posts WHERE post_reposts_count IS NOT NULL",
        )
    if "author_platform_level" in present:
        counts["author_platform_level 非空（空串计为有值）"] = _count(
            conn,
            "SELECT COUNT(*) FROM web_posts WHERE author_platform_level IS NOT NULL",
        )
    if "post_views_count" in present:
        counts["post_views_count 出现在非 bilibili"] = _count(
            conn,
            """
            SELECT COUNT(*) FROM web_posts
            WHERE post_views_count IS NOT NULL AND platform_key != 'bilibili'
            """,
        )
        counts["post_views_count 与 raw_sample_json.view_count 整数不相等"] = _count(
            conn,
            f"""
            SELECT COUNT(*) FROM web_posts
            WHERE post_views_count IS NOT NULL
              AND NOT ({_VIEW_COUNT_EQUAL})
            """,
        )
    return {label: count for label, count in counts.items() if count}


def _violation_message(violations: dict[str, int]) -> str:
    detail = "；".join(f"{label} {count} 行" for label, count in violations.items())
    return f"未使用指标列迁移已拒绝：{detail}"


def _schema_references(conn: sqlite3.Connection, present: set[str]) -> list[str]:
    hits: list[str] = []
    for row in conn.execute("PRAGMA index_list(web_posts)"):
        index_name = str(row[1])
        for info in conn.execute(f"PRAGMA index_info({_quote(index_name)})"):
            column = str(info[2] or "")
            if column in present:
                hits.append(f"index:{index_name}.{column}")
    for kind, name, sql in conn.execute(
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE type IN ('index', 'view', 'trigger') AND sql IS NOT NULL
        """
    ):
        text = str(sql)
        for column in present:
            if _COLUMN_RE[column].search(text):
                hits.append(f"{kind}:{name}.{column}")
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='web_posts'"
    ).fetchone()
    if table_sql and table_sql[0]:
        for match in re.finditer(r"CHECK\s*\((.*?)\)", str(table_sql[0]), re.IGNORECASE | re.DOTALL):
            body = match.group(1)
            for column in present:
                if _COLUMN_RE[column].search(body):
                    hits.append(f"check:{column}")
    return sorted(set(hits))


def _digest(conn: sqlite3.Connection, sql: str) -> str:
    digest = sha256()
    for row in conn.execute(sql):
        digest.update(
            json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), default=str).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    metrics: dict[int, str] = {}
    for row_id, original, touches, removed in conn.execute(
        """
        SELECT id,
               metrics_json,
               json_type(metrics_json, '$.reposts_count') IS NOT NULL
                   OR json_type(metrics_json, '$.views_count') IS NOT NULL,
               json_remove(metrics_json, '$.reposts_count', '$.views_count')
        FROM web_posts
        ORDER BY id
        """
    ):
        metrics[int(row_id)] = str(removed if touches else original)
    return {
        "web_posts": _count(conn, "SELECT COUNT(*) FROM web_posts"),
        "bilibili_rows": _count(
            conn, "SELECT COUNT(*) FROM web_posts WHERE platform_key='bilibili'"
        ),
        "bilibili_view_count_rows": _count(
            conn,
            """
            SELECT COUNT(*) FROM web_posts
            WHERE platform_key='bilibili'
              AND json_type(raw_sample_json, '$.view_count') IS NOT NULL
              AND json_type(raw_sample_json, '$.view_count') != 'null'
            """,
        ),
        "raw_sample_sha256": _digest(
            conn, "SELECT id, raw_sample_json FROM web_posts ORDER BY id"
        ),
        "view_count_sha256": _digest(
            conn,
            """
            SELECT id, json_extract(raw_sample_json, '$.view_count')
            FROM web_posts
            WHERE platform_key='bilibili'
            ORDER BY id
            """,
        ),
        "metrics_json": metrics,
    }


def _assert_preserved(conn: sqlite3.Connection, before: dict[str, Any]) -> None:
    after_metrics = {
        int(row_id): str(text)
        for row_id, text in conn.execute("SELECT id, metrics_json FROM web_posts ORDER BY id")
    }
    if after_metrics != before["metrics_json"]:
        raise UnusedMetricColumnMigrationError("metrics_json 的其余键在 json_remove 之后发生变化")
    after = {
        "web_posts": _count(conn, "SELECT COUNT(*) FROM web_posts"),
        "bilibili_rows": _count(
            conn, "SELECT COUNT(*) FROM web_posts WHERE platform_key='bilibili'"
        ),
        "bilibili_view_count_rows": _count(
            conn,
            """
            SELECT COUNT(*) FROM web_posts
            WHERE platform_key='bilibili'
              AND json_type(raw_sample_json, '$.view_count') IS NOT NULL
              AND json_type(raw_sample_json, '$.view_count') != 'null'
            """,
        ),
        "raw_sample_sha256": _digest(
            conn, "SELECT id, raw_sample_json FROM web_posts ORDER BY id"
        ),
        "view_count_sha256": _digest(
            conn,
            """
            SELECT id, json_extract(raw_sample_json, '$.view_count')
            FROM web_posts
            WHERE platform_key='bilibili'
            ORDER BY id
            """,
        ),
    }
    if after["web_posts"] != before["web_posts"]:
        raise UnusedMetricColumnMigrationError(
            f"web_posts 行数变化：before={before['web_posts']} after={after['web_posts']}"
        )
    if after["raw_sample_sha256"] != before["raw_sample_sha256"]:
        raise UnusedMetricColumnMigrationError("raw_sample_json 在迁移中被改写")
    if (
        after["bilibili_rows"] != before["bilibili_rows"]
        or after["bilibili_view_count_rows"] != before["bilibili_view_count_rows"]
        or after["view_count_sha256"] != before["view_count_sha256"]
    ):
        raise UnusedMetricColumnMigrationError(
            "B站 raw_sample_json.view_count 未能保持："
            f"rows {before['bilibili_view_count_rows']}/{before['bilibili_rows']} -> "
            f"{after['bilibili_view_count_rows']}/{after['bilibili_rows']}"
        )
    leftover = _count(
        conn,
        """
        SELECT COUNT(*) FROM web_posts
        WHERE json_type(metrics_json, '$.reposts_count') IS NOT NULL
           OR json_type(metrics_json, '$.views_count') IS NOT NULL
        """,
    )
    if leftover:
        raise UnusedMetricColumnMigrationError(
            f"metrics_json 仍含 reposts_count 或 views_count：{leftover} 行"
        )


def _record(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
        (MIGRATION_VERSION, MIGRATION_NAME),
    )


def _rollback(conn: sqlite3.Connection) -> None:
    conn.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
    conn.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")


def migrate_remove_unused_metric_columns(conn: sqlite3.Connection) -> dict[str, Any]:
    if _migration_applied(conn):
        return {
            "version": MIGRATION_VERSION,
            "name": MIGRATION_NAME,
            "already_applied": True,
            "applied": False,
            "registered_only": False,
        }

    present = [column for column in RETIRED_COLUMNS if column in _column_names(conn)]
    if not present:
        _record(conn)
        return {
            "version": MIGRATION_VERSION,
            "name": MIGRATION_NAME,
            "already_applied": False,
            "applied": False,
            "registered_only": True,
            "columns_dropped": [],
        }

    conn.execute(f"SAVEPOINT {_SAVEPOINT}")
    try:
        violations = _violations(conn, set(present))
        if violations:
            raise UnusedMetricColumnMigrationError(_violation_message(violations))
        references = _schema_references(conn, set(present))
        if references:
            raise UnusedMetricColumnMigrationError(
                "未使用指标列仍被模式对象引用：" + ", ".join(references)
            )
        before = _snapshot(conn)
        for column in present:
            conn.execute(f"ALTER TABLE web_posts DROP COLUMN {_quote(column)}")
        conn.execute(
            """
            UPDATE web_posts
            SET metrics_json = json_remove(metrics_json, '$.reposts_count', '$.views_count')
            WHERE json_type(metrics_json, '$.reposts_count') IS NOT NULL
               OR json_type(metrics_json, '$.views_count') IS NOT NULL
            """
        )
        _assert_preserved(conn, before)
        remaining = [column for column in RETIRED_COLUMNS if column in _column_names(conn)]
        if remaining:
            raise UnusedMetricColumnMigrationError(f"退役列仍在：{remaining}")
        _record(conn)
    except BaseException:
        _rollback(conn)
        raise
    conn.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
    return {
        "version": MIGRATION_VERSION,
        "name": MIGRATION_NAME,
        "already_applied": False,
        "applied": True,
        "registered_only": False,
        "columns_dropped": list(present),
        "web_posts": before["web_posts"],
        "bilibili_rows": before["bilibili_rows"],
        "bilibili_view_count_rows": before["bilibili_view_count_rows"],
    }
