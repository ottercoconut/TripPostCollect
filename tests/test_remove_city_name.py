from __future__ import annotations

import json
import sqlite3
import sys
from importlib import import_module
from pathlib import Path

from trippostcollect.db.bootstrap import (
    ensure_scheduler_schema,
    migrate_remove_city_name,
    sync_config_jobs,
)


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

keyword_from_capture = import_module("import_ctf_captures").keyword_from_capture
replace_capture_images = import_module("import_ctf_captures").replace_capture_images
replace_web_post_images_from_capture = import_module(
    "import_ctf_captures"
).replace_web_post_images_from_capture
validate_images = import_module("import_ctf_captures").validate_images
web_post_for_capture = import_module("import_ctf_captures").web_post_for_capture
row_for_record = import_module("mediacrawler_crawl").row_for_record


def test_content_migration_removes_city_name_and_legacy_city_rows() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE ctf_captures(id INTEGER PRIMARY KEY);
            CREATE TABLE web_posts(
                id INTEGER PRIMARY KEY,
                source_capture_id INTEGER REFERENCES ctf_captures(id) ON DELETE SET NULL,
                city_name TEXT,
                keyword TEXT
            );
            CREATE INDEX idx_web_posts_city ON web_posts(city_name);
            CREATE TABLE web_post_images(
                id INTEGER PRIMARY KEY,
                web_post_id INTEGER REFERENCES web_posts(id) ON DELETE CASCADE
            );
            CREATE TABLE cities(id INTEGER PRIMARY KEY, city_name TEXT);
            INSERT INTO ctf_captures(id) VALUES (1), (2), (3);
            INSERT INTO web_posts(id, source_capture_id, city_name, keyword)
            VALUES
                (1, 1, '青岛市', '崂山攻略'),
                (2, 2, '济南市', '济南旅游'),
                (3, 3, NULL, NULL);
            INSERT INTO web_post_images(id, web_post_id) VALUES (1, 1), (2, 2), (3, 3);
            """
        )

        migrate_remove_city_name(conn)

        assert conn.execute("SELECT id FROM web_posts").fetchall() == [(1,)]
        assert conn.execute("SELECT id FROM web_post_images").fetchall() == [(1,)]
        assert conn.execute("SELECT id FROM ctf_captures").fetchall() == [(1,), (2,), (3,)]
        assert "city_name" not in {
            row[1] for row in conn.execute("PRAGMA table_info(web_posts)").fetchall()
        }
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name IN ('cities', 'idx_web_posts_city')"
        ).fetchone() is None


def test_scheduler_migration_uses_config_scope_not_keyword_text() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_scheduler_schema(conn)
        conn.execute(
            """
            INSERT INTO crawl_jobs(job_key, site_key, target_url, job_kind, next_run_at, params_json)
            VALUES (?, 'weibo', '', 'mediacrawler_search', datetime('now'), ?)
            """,
            ("legacy-yantai", json.dumps({"keyword": "烟台旅游"}, ensure_ascii=False)),
        )
        sync_config_jobs(
            conn,
            {
                "jobs": [
                    {
                        "job_key": "laoshan",
                        "site_key": "weibo",
                        "target_url": "",
                        "job_kind": "mediacrawler_search",
                        "params": {"keyword": "崂山攻略"},
                    }
                ]
            },
        )

        row = conn.execute("SELECT job_key, params_json FROM crawl_jobs").fetchone()
        assert row[0] == "laoshan"
        assert json.loads(row[1])["keyword"] == "崂山攻略"


def test_page_evidence_preserves_optional_keyword() -> None:
    assert keyword_from_capture(
        {"raw_meta_json": json.dumps({"keyword": "崂山攻略"}, ensure_ascii=False)}
    ) == "崂山攻略"
    assert keyword_from_capture({"raw_meta_json": "{}"}) is None


def test_structured_import_accepts_keyword_without_city_name() -> None:
    row = row_for_record(
        "weibo",
        {"source_keyword": "崂山攻略"},
        artifact_dir="/tmp/artifact",
        captured_at="2026-07-29T00:00:00+08:00",
        keyword="青岛旅游",
    )
    assert row["keyword"] == "崂山攻略"


def test_page_evidence_row_removes_avatar_data_and_does_not_count_page_images() -> None:
    row = web_post_for_capture(
        {
            "site_key": "zhihu",
            "visible_text_path": None,
            "title": "青岛页面证据",
            "ok": 1,
            "content_ready": 1,
            "capture_kind": "resource",
            "body_text_length": 6,
            "root_text_length": 6,
            "image_count": 3,
            "loaded_image_count": 3,
            "flag_count": 0,
            "saved_images": 3,
            "target_url": "https://example.test/post",
            "final_url": "https://example.test/post",
            "published_at": "2026-08-15T12:00:00+08:00",
            "captured_at": "2026-08-15T13:00:00+08:00",
            "artifact_dir": "temp/page-evidence",
            "raw_meta_json": json.dumps(
                {
                    "keyword": "青岛旅游",
                    "avatar_url": "https://avatar.test/author.jpg",
                    "nested": {"user_avatar": None},
                },
                ensure_ascii=False,
            ),
        },
        1,
    )

    assert row is not None
    assert "author_avatar_url" not in row
    assert row["post_images_count"] == 0
    assert json.loads(row["raw_sample_json"]) == {"keyword": "青岛旅游", "nested": {}}


def test_page_evidence_rejects_legacy_unclassified_image_responses(tmp_path: Path) -> None:
    images_path = tmp_path / "images.json"
    images_path.write_text(
        json.dumps(
            [
                {
                    "url": "https://avatar.test/author.jpg",
                    "ok": True,
                    "saved_path": str(tmp_path / "image.jpg"),
                }
            ]
        ),
        encoding="utf-8",
    )
    with sqlite3.connect(":memory:") as conn:
        conn.executescript(
            """
            CREATE TABLE ctf_capture_images(
                id INTEGER PRIMARY KEY,
                ctf_capture_id INTEGER NOT NULL
            );
            CREATE TABLE web_post_images(
                id INTEGER PRIMARY KEY,
                web_post_id INTEGER NOT NULL
            );
            INSERT INTO ctf_capture_images(ctf_capture_id) VALUES (1);
            INSERT INTO web_post_images(web_post_id) VALUES (2);
            """
        )

        assert replace_capture_images(conn, 1, str(images_path)) == 0
        assert replace_web_post_images_from_capture(conn, 2, str(images_path)) == 0
        assert conn.execute("SELECT COUNT(*) FROM ctf_capture_images").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM web_post_images").fetchone()[0] == 0


def test_page_evidence_validation_accepts_aggregate_counts_without_image_records(
    tmp_path: Path,
) -> None:
    images_path = tmp_path / "images.json"
    failed_path = tmp_path / "failed_images.json"
    images_path.write_text("[]", encoding="utf-8")
    failed_path.write_text("[]", encoding="utf-8")

    result = validate_images(
        {
            "artifacts": {
                "images_json": str(images_path),
                "failed_images_json": str(failed_path),
            },
            "image_summary": {
                "total_requests": 12,
                "successful_responses": 10,
                "http_failed_responses": 1,
                "request_failed": 1,
                "saved_images": 0,
            },
        }
    )

    assert result["checked"] is True
    assert result["warnings"] == []
    assert result["computed"] == {
        "unclassified_image_records": 0,
        "unclassified_failed_image_records": 0,
    }
