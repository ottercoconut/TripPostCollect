"""schema v25：从 web_posts.author_json 删除 creator_hash。

作者平台 ID 只留在 author_platform_id 列。已登记版本 25 时直接返回。
键不存在的行不改。键存在时先按字符串比较列值，有不一致则整次迁移失败，不改任何行。

比较规则（不去首尾空白）：
- JSON null 只等于 SQL NULL，不等于空串；
- 空串只等于空串，不等于 NULL；
- 其余 JSON 类型用 CAST(... AS TEXT) 与列文本比较（数字 88 等于文本 '88'）。
raw_sample_json 不改。无效 JSON 看不到该键，保持原样，避免 json_remove 把整列写成 NULL。
"""

from __future__ import annotations

import sqlite3
from typing import Any


MIGRATION_VERSION = 25
MIGRATION_NAME = "remove_author_json_creator_hash"
_SAVEPOINT = "remove_author_json_creator_hash_v25"

# 键存在，且不能按上面的规则与 author_platform_id 看成同一个字符串。
_MISMATCH_SQL = """
SELECT COUNT(*) FROM web_posts
WHERE json_type(author_json, '$.creator_hash') IS NOT NULL
  AND NOT (
    (
      json_type(author_json, '$.creator_hash') = 'null'
      AND author_platform_id IS NULL
    )
    OR (
      json_type(author_json, '$.creator_hash') != 'null'
      AND author_platform_id IS NOT NULL
      AND CAST(json_extract(author_json, '$.creator_hash') AS TEXT) = author_platform_id
    )
  )
"""


class CreatorHashMigrationError(RuntimeError):
    """v25 校验失败。author_json 与 raw_sample_json 保持进入本迁移之前的状态。"""


def _migration_applied(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version=?",
        (MIGRATION_VERSION,),
    ).fetchone()
    return bool(row)


def _mismatch_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(_MISMATCH_SQL).fetchone()[0])


def _mismatch_message(count: int) -> str:
    return (
        "移除 author_json.creator_hash 已拒绝："
        f"creator_hash 与 author_platform_id 不一致 {count} 行"
        "（JSON null 只等于列 NULL，空串只等于空串，其余按文本比较，不去空白）"
    )


def _rollback(conn: sqlite3.Connection) -> None:
    conn.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
    conn.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")


def migrate_remove_author_json_creator_hash(conn: sqlite3.Connection) -> dict[str, Any]:
    if _migration_applied(conn):
        return {
            "version": MIGRATION_VERSION,
            "name": MIGRATION_NAME,
            "already_applied": True,
            "applied": False,
        }

    conn.execute(f"SAVEPOINT {_SAVEPOINT}")
    try:
        mismatches = _mismatch_count(conn)
        if mismatches:
            raise CreatorHashMigrationError(_mismatch_message(mismatches))
        raw_before = [
            row[0]
            for row in conn.execute("SELECT raw_sample_json FROM web_posts ORDER BY id")
        ]
        updated = conn.execute(
            """
            UPDATE web_posts
            SET author_json = json_remove(author_json, '$.creator_hash')
            WHERE json_valid(author_json)
              AND json_type(author_json, '$.creator_hash') IS NOT NULL
            """
        ).rowcount
        leftover = _mismatch_count(conn)
        if leftover:
            raise CreatorHashMigrationError(
                f"author_json.creator_hash 删除后仍不一致 {leftover} 行"
            )
        still_present = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM web_posts
                WHERE json_type(author_json, '$.creator_hash') IS NOT NULL
                """
            ).fetchone()[0]
        )
        if still_present:
            raise CreatorHashMigrationError(
                f"author_json 仍含 creator_hash：{still_present} 行"
            )
        raw_after = [
            row[0]
            for row in conn.execute("SELECT raw_sample_json FROM web_posts ORDER BY id")
        ]
        if raw_after != raw_before:
            raise CreatorHashMigrationError("raw_sample_json 在迁移中被改写")
        conn.execute(
            "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
            (MIGRATION_VERSION, MIGRATION_NAME),
        )
    except BaseException:
        _rollback(conn)
        raise
    conn.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
    return {
        "version": MIGRATION_VERSION,
        "name": MIGRATION_NAME,
        "already_applied": False,
        "applied": True,
        "rows_updated": int(updated),
    }
