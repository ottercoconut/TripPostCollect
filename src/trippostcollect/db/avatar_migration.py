"""Idempotent schema-v18 migration removing persisted author-avatar data."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
import sqlite3
from typing import Any, Iterable

from trippostcollect.records.sanitization import (
    AUTHOR_AVATAR_KEYS,
    discover_author_avatar_urls,
    sanitize_author_avatar_data,
    serialized_avatar_profile_keys,
)


MIGRATION_VERSION = 18
MIGRATION_NAME = "remove_author_avatars_and_serialized_xhs_profiles"


class AvatarMigrationError(RuntimeError):
    pass


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    return [
        (str(row[1]), str(row[2] or ""))
        for row in conn.execute(f"PRAGMA table_info({_quote(table)})")
    ]


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {name for name, _ in _columns(conn, table)}


def _migration_applied(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version=?",
        (MIGRATION_VERSION,),
    ).fetchone()
    return bool(row)


def _canonical_url(value: str) -> str:
    candidate = value.strip()
    return f"https:{candidate}" if candidate.startswith("//") else candidate


def _hash_rows(conn: sqlite3.Connection, sql: str) -> str:
    digest = sha256()
    for row in conn.execute(sql):
        digest.update(
            json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), default=str).encode(
                "utf-8"
            )
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _invariants(conn: sqlite3.Connection) -> dict[str, Any]:
    author_columns = [
        column
        for column in (
            "id",
            "author_display_name",
            "author_platform_id",
            "author_profile_url",
            "author_description",
            "author_followers_count",
            "author_following_count",
            "author_posts_count",
            "author_platform_level",
            "author_verified",
            "author_verified_text",
        )
        if column in _column_names(conn, "web_posts")
    ]
    author_projection = ", ".join(_quote(column) for column in author_columns)
    mismatch = conn.execute(
        """
        SELECT COUNT(*)
        FROM web_posts AS post
        LEFT JOIN (
            SELECT web_post_id, COUNT(*) AS content_count
            FROM web_post_images
            WHERE image_role='content'
            GROUP BY web_post_id
        ) AS images ON images.web_post_id=post.id
        WHERE post.post_images_count != COALESCE(images.content_count, 0)
        """
    ).fetchone()[0]
    return {
        "web_posts": int(conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0]),
        "content_images": int(
            conn.execute(
                "SELECT COUNT(*) FROM web_post_images WHERE image_role='content'"
            ).fetchone()[0]
        ),
        "post_images_count_mismatches": int(mismatch),
        "author_fields_sha256": _hash_rows(
            conn,
            f"SELECT {author_projection} FROM web_posts ORDER BY id",
        ),
        "content_relationships_sha256": _hash_rows(
            conn,
            """
            SELECT id, web_post_id, image_index, image_url, image_role,
                   local_path, width, height, mime_type, sha256, created_at
            FROM web_post_images
            WHERE image_role='content'
            ORDER BY id
            """,
        ),
        "post_image_counts_sha256": _hash_rows(
            conn,
            "SELECT id, post_images_count FROM web_posts ORDER BY id",
        ),
    }


def _json_columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (table, column)
        for table in _tables(conn)
        for column, _ in _columns(conn, table)
        if column.endswith("_json")
    ]


def _validate_json_columns(conn: sqlite3.Connection) -> None:
    for table, column in _json_columns(conn):
        row = conn.execute(
            f"""
            SELECT rowid
            FROM {_quote(table)}
            WHERE {_quote(column)} IS NOT NULL
              AND TRIM({_quote(column)})<>''
              AND json_valid({_quote(column)})=0
            LIMIT 1
            """
        ).fetchone()
        if row:
            raise AvatarMigrationError(
                f"invalid JSON in {table}.{column} rowid={int(row[0])}"
            )


def _iter_json_payloads(
    conn: sqlite3.Connection,
) -> Iterable[tuple[str, str, int, str, Any]]:
    for table, column in _json_columns(conn):
        evidence_keys = sorted(AUTHOR_AVATAR_KEYS | serialized_avatar_profile_keys())
        alias_predicate = " OR ".join(
            f"INSTR(LOWER({_quote(column)}), ?) > 0" for _ in evidence_keys
        )
        sql = (
            f"SELECT rowid, {_quote(column)} FROM {_quote(table)} "
            f"WHERE {_quote(column)} IS NOT NULL AND TRIM({_quote(column)})<>'' "
            f"AND ({alias_predicate})"
        )
        cursor = conn.execute(
            sql,
            [f'"{alias.casefold()}"' for alias in evidence_keys],
        )
        while rows := cursor.fetchmany(200):
            for rowid, raw_value in rows:
                text = str(raw_value)
                try:
                    payload = json.loads(text)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise AvatarMigrationError(
                        f"invalid JSON in {table}.{column} rowid={rowid}"
                    ) from exc
                yield table, column, int(rowid), text, payload


def _explicit_avatar_urls(
    conn: sqlite3.Connection,
) -> set[str]:
    urls: set[str] = set()
    if "author_avatar_url" in _column_names(conn, "web_posts"):
        urls.update(
            str(row[0]).strip()
            for row in conn.execute(
                "SELECT author_avatar_url FROM web_posts "
                "WHERE author_avatar_url IS NOT NULL AND TRIM(author_avatar_url)<>''"
            )
        )
    urls.update(
        str(row[0]).strip()
        for row in conn.execute(
            "SELECT image_url FROM web_post_images WHERE image_role='author_avatar'"
        )
        if str(row[0]).strip()
    )
    for _, _, _, _, payload in _iter_json_payloads(conn):
        urls.update(discover_author_avatar_urls(payload))
    return urls


def _assert_no_content_url_conflicts(conn: sqlite3.Connection, avatar_urls: set[str]) -> None:
    avatar_identities = {_canonical_url(value) for value in avatar_urls}
    conflicts = [
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT image_url FROM web_post_images WHERE image_role='content'"
        )
        if _canonical_url(str(row[0])) in avatar_identities
    ]
    if conflicts:
        raise AvatarMigrationError(
            f"avatar URLs overlap content-image relationships: {conflicts[:3]!r}"
        )


def _sanitize_json_columns(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter[str]] = {}
    for table, column, rowid, raw_text, payload in _iter_json_payloads(conn):
        result = sanitize_author_avatar_data(payload)
        if not result.changed:
            continue
        serialized = json.dumps(
            result.value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            f"UPDATE {_quote(table)} SET {_quote(column)}=? WHERE rowid=?",
            (serialized, rowid),
        )
        counter = counts.setdefault(f"{table}.{column}", Counter())
        counter["rows"] += 1
        counter["removed_keys"] += result.removed_keys
        counter["removed_values"] += result.removed_values
        if serialized == raw_text:
            raise AvatarMigrationError(
                f"sanitization reported a change without changing {table}.{column} rowid={rowid}"
            )
    return {key: dict(value) for key, value in sorted(counts.items())}


def _rebuild_image_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE web_post_images_v18 (
            id INTEGER PRIMARY KEY,
            web_post_id INTEGER NOT NULL REFERENCES web_posts(id) ON DELETE CASCADE,
            image_index INTEGER NOT NULL,
            image_url TEXT NOT NULL,
            image_role TEXT NOT NULL DEFAULT 'content',
            local_path TEXT,
            width INTEGER,
            height INTEGER,
            mime_type TEXT,
            sha256 TEXT,
            raw_image_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (image_role IN ('content', 'page'))
        )
        """
    )
    conn.execute(
        """
        INSERT INTO web_post_images_v18 (
            id, web_post_id, image_index, image_url, image_role, local_path,
            width, height, mime_type, sha256, raw_image_json, created_at
        )
        SELECT
            id, web_post_id, image_index, image_url, image_role, local_path,
            width, height, mime_type, sha256, raw_image_json, created_at
        FROM web_post_images
        WHERE image_role IN ('content', 'page')
        """
    )
    conn.execute("DROP TABLE web_post_images")
    conn.execute("ALTER TABLE web_post_images_v18 RENAME TO web_post_images")
    conn.execute(
        """
        CREATE UNIQUE INDEX idx_web_post_images_unique
        ON web_post_images(web_post_id, image_role, image_index)
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_web_post_images_post
        ON web_post_images(web_post_id, image_index)
        """
    )


def _avatar_residuals(conn: sqlite3.Connection, known_urls: set[str]) -> dict[str, Any]:
    image_table_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='web_post_images'"
    ).fetchone()
    image_table_sql = str(image_table_sql_row[0] or "") if image_table_sql_row else ""
    json_key_rows = 0
    json_url_rows = 0
    known_url_set = {value.strip() for value in known_urls}
    for _, _, _, _, payload in _iter_json_payloads(conn):
        if discover_author_avatar_urls(payload):
            json_key_rows += 1

        def contains_url(node: Any) -> bool:
            if isinstance(node, dict):
                return any(contains_url(value) for value in node.values())
            if isinstance(node, list):
                return any(contains_url(value) for value in node)
            return isinstance(node, str) and node.strip() in known_url_set

        json_url_rows += int(contains_url(payload))
    return {
        "author_avatar_column_present": "author_avatar_url"
        in _column_names(conn, "web_posts"),
        "author_avatar_image_rows": int(
            conn.execute(
                "SELECT COUNT(*) FROM web_post_images WHERE image_role='author_avatar'"
            ).fetchone()[0]
        ),
        "image_schema_mentions_author_avatar": "author_avatar" in image_table_sql,
        "json_rows_with_avatar_keys": json_key_rows,
        "json_rows_with_known_avatar_urls": json_url_rows,
    }


def _assert_clean(residuals: dict[str, Any]) -> None:
    if any(bool(value) for value in residuals.values()):
        raise AvatarMigrationError(f"avatar persistence residuals remain: {residuals!r}")


def migrate_remove_author_avatars(conn: sqlite3.Connection) -> dict[str, Any]:
    already_applied = _migration_applied(conn)
    _validate_json_columns(conn)
    avatar_urls = _explicit_avatar_urls(conn)
    if already_applied:
        residuals = _avatar_residuals(conn, avatar_urls)
        _assert_clean(residuals)
        return {
            "version": MIGRATION_VERSION,
            "name": MIGRATION_NAME,
            "applied": False,
            "avatar_url_evidence_count": len(avatar_urls),
            "residuals": residuals,
            "invariants": _invariants(conn),
        }

    before = _invariants(conn)
    role_counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT image_role, COUNT(*) FROM web_post_images GROUP BY image_role ORDER BY image_role"
        )
    }
    platform_avatar_counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            """
            SELECT post.platform_key, COUNT(*)
            FROM web_post_images AS image
            JOIN web_posts AS post ON post.id=image.web_post_id
            WHERE image.image_role='author_avatar'
            GROUP BY post.platform_key
            ORDER BY post.platform_key
            """
        )
    }
    _assert_no_content_url_conflicts(conn, avatar_urls)

    conn.execute("SAVEPOINT remove_author_avatars_v18")
    try:
        json_changes = _sanitize_json_columns(conn)
        _rebuild_image_table(conn)
        if "author_avatar_url" in _column_names(conn, "web_posts"):
            conn.execute("ALTER TABLE web_posts DROP COLUMN author_avatar_url")
        conn.execute(
            "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
            (MIGRATION_VERSION, MIGRATION_NAME),
        )
        after = _invariants(conn)
        if before != after:
            raise AvatarMigrationError(
                f"non-avatar invariants changed: before={before!r} after={after!r}"
            )
        foreign_key_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_violations:
            raise AvatarMigrationError(
                f"foreign-key violations after avatar migration: {foreign_key_violations[:3]!r}"
            )
        residuals = _avatar_residuals(conn, avatar_urls)
        _assert_clean(residuals)
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT remove_author_avatars_v18")
        conn.execute("RELEASE SAVEPOINT remove_author_avatars_v18")
        raise
    conn.execute("RELEASE SAVEPOINT remove_author_avatars_v18")
    return {
        "version": MIGRATION_VERSION,
        "name": MIGRATION_NAME,
        "applied": True,
        "avatar_url_evidence_count": len(avatar_urls),
        "image_role_counts_before": role_counts,
        "platform_avatar_counts_before": platform_avatar_counts,
        "json_changes": json_changes,
        "before": before,
        "after": after,
        "residuals": residuals,
    }


def avatar_aliases() -> list[str]:
    return sorted(AUTHOR_AVATAR_KEYS)
