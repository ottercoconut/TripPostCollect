"""schema v24：删除未使用的转发、浏览和平台等级列。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.db.unused_metric_columns_migration import (
    UnusedMetricColumnMigrationError,
    migrate_remove_unused_metric_columns,
)


RETIRED_COLUMNS = ("post_reposts_count", "post_views_count", "author_platform_level")


def legacy_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE web_posts(
            id INTEGER PRIMARY KEY,
            platform_key TEXT NOT NULL,
            author_platform_level TEXT,
            post_reposts_count INTEGER,
            post_views_count INTEGER,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            raw_sample_json TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    return conn


def insert_post(
    conn: sqlite3.Connection,
    *,
    post_id: int,
    platform_key: str,
    reposts: int | None = None,
    views: int | None = None,
    level: str | None = None,
    metrics: dict | None = None,
    raw: dict | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO web_posts(
            id, platform_key, author_platform_level, post_reposts_count,
            post_views_count, metrics_json, raw_sample_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            post_id,
            platform_key,
            level,
            reposts,
            views,
            json.dumps(metrics if metrics is not None else {}, ensure_ascii=False, sort_keys=True),
            json.dumps(raw if raw is not None else {}, ensure_ascii=False, sort_keys=True),
        ),
    )


def state(conn: sqlite3.Connection) -> tuple:
    columns = [row[1] for row in conn.execute("PRAGMA table_info(web_posts)")]
    rows = [
        tuple(row)
        for row in conn.execute(
            """
            SELECT id, platform_key, author_platform_level, post_reposts_count,
                   post_views_count, metrics_json, raw_sample_json
            FROM web_posts
            ORDER BY id
            """
        )
    ]
    versions = conn.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    ).fetchall()
    objects = conn.execute(
        """
        SELECT type, name FROM sqlite_master
        WHERE type IN ('index', 'view', 'trigger') AND name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    return columns, rows, [tuple(item) for item in versions], [tuple(item) for item in objects]


def valid_posts(conn: sqlite3.Connection) -> None:
    insert_post(
        conn,
        post_id=1,
        platform_key="bilibili",
        views=12,
        metrics={
            "comments_count": 2,
            "favorites_count": None,
            "kept": 7,
            "liked_count": 1,
            "reposts_count": None,
            "shares_count": 4,
            "views_count": 12,
        },
        raw={"title": "青岛", "view_count": "12"},
    )
    insert_post(
        conn,
        post_id=2,
        platform_key="weibo",
        metrics={
            "comments_count": 1,
            "liked_count": 3,
            "reposts_count": None,
            "shares_count": 8,
            "views_count": None,
        },
        raw={"id": "w", "text": "正文"},
    )
    insert_post(
        conn,
        post_id=3,
        platform_key="xhs",
        metrics={"liked_count": 5, "shares_count": 1},
        raw={"id": "x"},
    )


def test_migration_drops_columns_and_metric_keys_then_skips() -> None:
    with legacy_connection() as conn:
        valid_posts(conn)
        untouched_metrics = conn.execute(
            "SELECT metrics_json FROM web_posts WHERE id=3"
        ).fetchone()[0]
        raw_before = [
            row[0]
            for row in conn.execute("SELECT raw_sample_json FROM web_posts ORDER BY id")
        ]

        report = migrate_remove_unused_metric_columns(conn)
        repeated = migrate_remove_unused_metric_columns(conn)

        assert report["applied"] is True
        assert report["columns_dropped"] == list(RETIRED_COLUMNS)
        assert report["web_posts"] == 3
        assert report["bilibili_rows"] == report["bilibili_view_count_rows"] == 1
        assert repeated == {
            "version": 24,
            "name": "remove_unused_metric_columns",
            "already_applied": True,
            "applied": False,
            "registered_only": False,
        }
        assert set(RETIRED_COLUMNS).isdisjoint(
            {row[1] for row in conn.execute("PRAGMA table_info(web_posts)")}
        )
        assert conn.execute(
            "SELECT name FROM schema_migrations WHERE version=24"
        ).fetchone()[0] == "remove_unused_metric_columns"
        metrics = [
            json.loads(row[0])
            for row in conn.execute("SELECT metrics_json FROM web_posts ORDER BY id")
        ]
        assert metrics[0] == {
            "comments_count": 2,
            "favorites_count": None,
            "kept": 7,
            "liked_count": 1,
            "shares_count": 4,
        }
        assert metrics[1]["liked_count"] == 3 and metrics[1]["shares_count"] == 8
        assert "reposts_count" not in metrics[1] and "views_count" not in metrics[1]
        assert conn.execute("SELECT metrics_json FROM web_posts WHERE id=3").fetchone()[0] == (
            untouched_metrics
        )
        assert [
            row[0]
            for row in conn.execute("SELECT raw_sample_json FROM web_posts ORDER BY id")
        ] == raw_before
        assert json.loads(raw_before[0])["view_count"] == "12"
        assert conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] == 3


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda conn: conn.execute(
                "UPDATE web_posts SET post_reposts_count=0 WHERE id=2"
            ),
            "post_reposts_count 非空 1 行",
        ),
        (
            lambda conn: conn.execute(
                "UPDATE web_posts SET author_platform_level='' WHERE id=2"
            ),
            "author_platform_level 非空（空串计为有值） 1 行",
        ),
        (
            lambda conn: conn.execute(
                """
                UPDATE web_posts
                SET post_views_count=1,
                    raw_sample_json=json_set(raw_sample_json, '$.view_count', 1)
                WHERE id=2
                """
            ),
            "post_views_count 出现在非 bilibili 1 行",
        ),
        (
            lambda conn: conn.execute(
                "UPDATE web_posts SET post_views_count=5 WHERE id=1"
            ),
            "post_views_count 与 raw_sample_json.view_count 整数不相等 1 行",
        ),
    ],
)
def test_each_violation_rolls_back_without_deleting_data(mutate, message: str) -> None:
    with legacy_connection() as conn:
        valid_posts(conn)
        mutate(conn)
        before = state(conn)

        with pytest.raises(UnusedMetricColumnMigrationError, match=message):
            migrate_remove_unused_metric_columns(conn)

        assert state(conn) == before
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=24"
        ).fetchone()[0] == 0


def test_combined_violations_report_every_count_and_roll_back() -> None:
    with legacy_connection() as conn:
        valid_posts(conn)
        conn.execute("UPDATE web_posts SET post_reposts_count=2 WHERE id=3")
        conn.execute("UPDATE web_posts SET author_platform_level='Lv6' WHERE id=3")
        conn.execute("UPDATE web_posts SET post_views_count=4 WHERE id=2")
        before = state(conn)

        with pytest.raises(UnusedMetricColumnMigrationError) as caught:
            migrate_remove_unused_metric_columns(conn)

        message = str(caught.value)
        assert "post_reposts_count 非空 1 行" in message
        assert "author_platform_level 非空（空串计为有值） 1 行" in message
        assert "post_views_count 出现在非 bilibili 1 行" in message
        assert state(conn) == before


def test_applied_version_skips_even_when_columns_remain() -> None:
    with legacy_connection() as conn:
        insert_post(conn, post_id=1, platform_key="weibo", reposts=9, level="Lv1", views=4)
        conn.execute(
            "INSERT INTO schema_migrations(version, name) VALUES (24, 'remove_unused_metric_columns')"
        )
        before = state(conn)

        report = migrate_remove_unused_metric_columns(conn)

        assert report["already_applied"] is True
        assert state(conn) == before


def test_database_without_the_columns_only_registers_the_version() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE web_posts(
                id INTEGER PRIMARY KEY,
                platform_key TEXT NOT NULL,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                raw_sample_json TEXT NOT NULL DEFAULT '{}'
            );
            INSERT INTO web_posts(id, platform_key, metrics_json, raw_sample_json)
            VALUES (
                1, 'bilibili',
                '{"liked_count":2,"views_count":1}',
                '{"view_count":9}'
            );
            """
        )

        report = migrate_remove_unused_metric_columns(conn)

        assert report["registered_only"] is True
        assert report["applied"] is False
        assert conn.execute("SELECT metrics_json, raw_sample_json FROM web_posts").fetchone() == (
            '{"liked_count":2,"views_count":1}',
            '{"view_count":9}',
        )
        assert conn.execute(
            "SELECT name FROM schema_migrations WHERE version=24"
        ).fetchone()[0] == "remove_unused_metric_columns"


@pytest.mark.parametrize(
    ("statement", "label"),
    [
        (
            "CREATE INDEX idx_retired_views ON web_posts(post_views_count)",
            "index:idx_retired_views.post_views_count",
        ),
        (
            "CREATE VIEW retired_views AS SELECT id, post_views_count FROM web_posts",
            "view:retired_views.post_views_count",
        ),
        (
            """
            CREATE TRIGGER retired_level
            AFTER UPDATE ON web_posts
            BEGIN
                SELECT author_platform_level FROM web_posts WHERE id=NEW.id;
            END
            """,
            "trigger:retired_level.author_platform_level",
        ),
    ],
)
def test_schema_reference_rolls_back_without_dropping_columns(statement: str, label: str) -> None:
    with legacy_connection() as conn:
        valid_posts(conn)
        conn.execute(statement)
        before = state(conn)

        with pytest.raises(UnusedMetricColumnMigrationError, match=label):
            migrate_remove_unused_metric_columns(conn)

        assert state(conn) == before


def test_failure_after_drop_restores_columns_and_rows() -> None:
    with legacy_connection() as conn:
        valid_posts(conn)
        conn.execute(
            """
            CREATE TRIGGER metrics_guard
            BEFORE UPDATE OF metrics_json ON web_posts
            BEGIN
                SELECT RAISE(ABORT, 'metrics_json locked');
            END
            """
        )
        before = state(conn)

        with pytest.raises(sqlite3.Error, match="metrics_json locked"):
            migrate_remove_unused_metric_columns(conn)

        assert state(conn) == before


def test_fresh_bootstrap_registers_version_without_the_columns() -> None:
    with sqlite3.connect(":memory:") as conn:
        first = bootstrap_connection(conn, sync_jobs=False)
        second = bootstrap_connection(conn, sync_jobs=False)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(web_posts)")}
        assert set(RETIRED_COLUMNS).isdisjoint(columns)
        assert first["unused_metric_columns_migration"]["registered_only"] is True
        assert first["avatar_migration"]["version"] == 18
        assert second["unused_metric_columns_migration"]["already_applied"] is True
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=24 AND name='remove_unused_metric_columns'"
        ).fetchone()[0] == 1
