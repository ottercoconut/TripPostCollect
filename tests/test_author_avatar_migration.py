from __future__ import annotations

import json
import sqlite3

import pytest

from trippostcollect.db.avatar_migration import (
    AvatarMigrationError,
    migrate_remove_author_avatars,
)
from trippostcollect.db.bootstrap import bootstrap_connection


AVATAR_URL = "https://avatar.test/author.jpg"
CONTENT_URL = "https://images.test/body.jpg"


def legacy_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE web_posts(
            id INTEGER PRIMARY KEY,
            platform_key TEXT NOT NULL,
            author_display_name TEXT,
            author_platform_id TEXT,
            author_profile_url TEXT,
            author_avatar_url TEXT,
            author_description TEXT,
            author_followers_count INTEGER,
            author_following_count INTEGER,
            author_posts_count INTEGER,
            author_platform_level TEXT,
            author_verified INTEGER,
            author_verified_text TEXT,
            post_images_count INTEGER NOT NULL DEFAULT 0,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            author_json TEXT NOT NULL DEFAULT '{}',
            raw_sample_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE web_post_images(
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
            CHECK (image_role IN ('content', 'page', 'author_avatar'))
        );
        CREATE UNIQUE INDEX idx_web_post_images_unique
        ON web_post_images(web_post_id, image_role, image_index);
        CREATE INDEX idx_web_post_images_post
        ON web_post_images(web_post_id, image_index);
        """
    )
    return conn


def insert_legacy_post(conn: sqlite3.Connection, post_id: int, platform_key: str) -> None:
    conn.execute(
        """
        INSERT INTO web_posts(
            id, platform_key, author_display_name, author_platform_id,
            author_profile_url, author_avatar_url, author_description,
            author_followers_count, author_following_count, author_posts_count,
            author_platform_level, author_verified, author_verified_text,
            post_images_count, metrics_json, author_json, raw_sample_json
        ) VALUES (?, ?, '作者', ?, 'https://example.test/profile', ?, '简介',
                  42, 3, 8, 'level-1', 1, 'verified', 1, '{}', ?, ?)
        """,
        (
            post_id,
            platform_key,
            f"author-{post_id}",
            AVATAR_URL,
            json.dumps(
                {
                    "nickname": "作者",
                    "avatar_url": AVATAR_URL,
                    "followers_count": 42,
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "title": "青岛",
                    "nested": {"author_avatar": AVATAR_URL},
                    "copied_avatar_value": AVATAR_URL,
                },
                ensure_ascii=False,
            ),
        ),
    )
    conn.execute(
        """
        INSERT INTO web_post_images(
            web_post_id, image_index, image_url, image_role, local_path,
            width, height, mime_type, sha256, raw_image_json
        ) VALUES (?, 0, ?, 'content', 'data/media/body.jpg', 10, 20,
                  'image/jpeg', 'content-sha', '{"source":"body"}')
        """,
        (post_id, CONTENT_URL),
    )
    conn.execute(
        """
        INSERT INTO web_post_images(
            web_post_id, image_index, image_url, image_role, raw_image_json
        ) VALUES (?, 0, ?, 'author_avatar', '{"source_key":"avatar_url"}')
        """,
        (post_id, AVATAR_URL),
    )


def test_v17_migration_removes_avatar_data_and_is_idempotent() -> None:
    with legacy_connection() as conn:
        insert_legacy_post(conn, 1, "xhs")
        insert_legacy_post(conn, 2, "zhihu")
        before_author_fields = conn.execute(
            """
            SELECT id, author_display_name, author_platform_id, author_profile_url,
                   author_description, author_followers_count, author_following_count,
                   author_posts_count, author_platform_level, author_verified,
                   author_verified_text
            FROM web_posts ORDER BY id
            """
        ).fetchall()
        before_content = conn.execute(
            """
            SELECT id, web_post_id, image_index, image_url, local_path, width,
                   height, mime_type, sha256
            FROM web_post_images WHERE image_role='content' ORDER BY id
            """
        ).fetchall()

        report = migrate_remove_author_avatars(conn)
        repeated = migrate_remove_author_avatars(conn)

        assert report["applied"] is True
        assert report["image_role_counts_before"]["author_avatar"] == 2
        assert report["platform_avatar_counts_before"] == {"xhs": 1, "zhihu": 1}
        assert repeated["applied"] is False
        assert "author_avatar_url" not in {
            row[1] for row in conn.execute("PRAGMA table_info(web_posts)")
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM web_post_images WHERE image_role='author_avatar'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT id, author_json, raw_sample_json FROM web_posts ORDER BY id"
        ).fetchall() == [
            (
                1,
                '{"followers_count":42,"nickname":"作者"}',
                '{"nested":{},"title":"青岛"}',
            ),
            (
                2,
                '{"followers_count":42,"nickname":"作者"}',
                '{"nested":{},"title":"青岛"}',
            ),
        ]
        assert before_author_fields == conn.execute(
            """
            SELECT id, author_display_name, author_platform_id, author_profile_url,
                   author_description, author_followers_count, author_following_count,
                   author_posts_count, author_platform_level, author_verified,
                   author_verified_text
            FROM web_posts ORDER BY id
            """
        ).fetchall()
        assert before_content == conn.execute(
            """
            SELECT id, web_post_id, image_index, image_url, local_path, width,
                   height, mime_type, sha256
            FROM web_post_images WHERE image_role='content' ORDER BY id
            """
        ).fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO web_post_images(web_post_id, image_index, image_url, image_role)
                VALUES (1, 0, ?, 'author_avatar')
                """,
                (AVATAR_URL,),
            )


def test_v17_migration_refuses_avatar_content_url_overlap_without_changes() -> None:
    with legacy_connection() as conn:
        insert_legacy_post(conn, 1, "xhs")
        conn.execute(
            "UPDATE web_post_images SET image_url=? WHERE image_role='content'",
            (AVATAR_URL,),
        )

        with pytest.raises(AvatarMigrationError, match="overlap content-image"):
            migrate_remove_author_avatars(conn)

        assert "author_avatar_url" in {
            row[1] for row in conn.execute("PRAGMA table_info(web_posts)")
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM web_post_images WHERE image_role='author_avatar'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=17"
        ).fetchone()[0] == 0


def test_fresh_bootstrap_uses_clean_v17_schema() -> None:
    with sqlite3.connect(":memory:") as conn:
        result = bootstrap_connection(conn, sync_jobs=False)

        assert result["avatar_migration"]["version"] == 17
        assert "author_avatar_url" not in {
            row[1] for row in conn.execute("PRAGMA table_info(web_posts)")
        }
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='web_post_images'"
        ).fetchone()[0]
        assert "author_avatar" not in table_sql
