from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import replace
from importlib import import_module
from pathlib import Path

from trippostcollect.db.bootstrap import bootstrap_connection


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

repair = import_module("repair_bilibili_articles")


def create_target(path: Path, count: int = 2) -> None:
    with sqlite3.connect(path) as connection:
        bootstrap_connection(connection, sync_jobs=False)
        for index in range(1, count + 1):
            post_id = str(1000 + index)
            old_text = f"旧搜索摘要-{post_id}"
            raw = {
                "id": post_id,
                "content_id": post_id,
                "content_type": "article",
                "title": f"旧标题-{post_id}",
                "desc": old_text,
                "content_text": old_text,
                "image_urls": [f"https://example.test/preview-{post_id}.jpg"],
                "pubdate": 1_700_000_000,
                "published_at": 1_700_000_000,
                "author": "作者",
                "nickname": "作者",
                "mid": "88",
                "user_id": "88",
                "like": 1,
                "reply": 2,
                "view": 3,
                "liked_count": 1,
                "comment_count": 2,
                "view_count": 3,
                "followers_count": 50,
                "author_followers_count": 50,
                "followers_observed": True,
                "author_followers_source": "relation_stat",
                "source_keyword": "青岛旅游",
            }
            connection.execute(
                """
                INSERT INTO web_posts (
                    platform_key, platform_post_id, source_type, source_url,
                    canonical_url, title, author_display_name, author_platform_id,
                    published_at, captured_at, keyword, content_text, content_length,
                    metrics_json, author_json, raw_sample_json, artifact_dir,
                    capture_method, status, author_followers_count,
                    post_likes_count, post_comments_count, post_views_count,
                    post_images_count
                ) VALUES (
                    'bilibili', ?, 'mediacrawler', ?, ?, ?, '作者', '88',
                    '2023-11-15T06:13:20+08:00', '2026-08-02T00:00:00+00:00',
                    '青岛旅游', ?, ?, ?, ?, ?, '/original/artifact',
                    'import', 'captured', 50, 1, 2, 3, 1
                )
                """,
                (
                    post_id,
                    f"https://www.bilibili.com/read/cv{post_id}/",
                    f"https://www.bilibili.com/read/cv{post_id}/",
                    f"旧标题-{post_id}",
                    old_text,
                    len(old_text),
                    json.dumps(
                        {
                            "liked_count": 1,
                            "comments_count": 2,
                            "views_count": 3,
                        }
                    ),
                    json.dumps(
                        {
                            "followers_observed": True,
                            "followers_source": "relation_stat",
                        }
                    ),
                    json.dumps(raw, ensure_ascii=False),
                ),
            )
            web_post_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            connection.execute(
                """
                INSERT INTO web_post_images (
                    web_post_id, image_index, image_url, image_role, raw_image_json
                ) VALUES (?, 0, ?, 'content', '{}')
                """,
                (web_post_id, f"https://example.test/preview-{post_id}.jpg"),
            )
        connection.commit()


def config_for(
    target: Path,
    state: Path,
    backup: Path,
    report_dir: Path,
    *,
    apply: bool,
    expected_sha256: str,
) -> repair.RepairConfig:
    return repair.RepairConfig(
        db_path=target,
        state_db_path=state,
        report_dir=report_dir,
        backup_path=backup,
        expected_baseline_sha256=expected_sha256,
        apply=apply,
        confirm_default_db_repair=False,
        max_items=10,
        session_size=10,
        pacing_min=0,
        pacing_max=0,
        session_pause_min=0,
        session_pause_max=0,
        retry_delay_seconds=0,
        only_ids=frozenset(),
    )


def fake_detail(post_id: str, cookie_header: str):
    return (
        {
            "title": f"详情标题-{post_id}",
            "content": f"第一段完整正文-{post_id}\n第二段完整正文",
            "image_urls": [f"https://example.test/detail-{post_id}.jpg"],
            "opus": {"content": {"paragraphs": []}},
        },
        1,
        0.0,
    )


def test_dry_run_creates_manifest_without_changing_target(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target)
    baseline = repair.sha256_file(target)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    config = config_for(
        target,
        tmp_path / "state.sqlite",
        backup,
        tmp_path / "reports",
        apply=False,
        expected_sha256=baseline,
    )

    return_code, result = repair.run_repair(config)

    assert return_code == 0
    assert result["selected_count"] == 2
    assert result["stopped_reason"] == "dry_run"
    assert repair.sha256_file(target) == baseline
    with sqlite3.connect(config.state_db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM repair_items").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM repair_items WHERE status='pending'"
        ).fetchone()[0] == 2


def test_apply_updates_same_rows_and_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target)
    baseline = repair.sha256_file(target)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    config = config_for(
        target,
        tmp_path / "state.sqlite",
        backup,
        tmp_path / "reports",
        apply=True,
        expected_sha256=baseline,
    )
    monkeypatch.setattr(repair, "fetch_bilibili_article_detail_with_retry", fake_detail)

    first_code, first = repair.run_repair(config)
    second_code, second = repair.run_repair(config)

    assert first_code == second_code == 0
    assert first["updated"] == 2
    assert second["selected_count"] == 0
    assert second["updated"] == 0
    with sqlite3.connect(target) as connection:
        rows = connection.execute(
            """
            SELECT platform_post_id, keyword, artifact_dir, content_text,
                   post_images_count, capture_method,
                   json_extract(raw_sample_json, '$.content_detail_status')
            FROM web_posts ORDER BY id
            """
        ).fetchall()
        images = connection.execute(
            "SELECT image_url FROM web_post_images ORDER BY web_post_id, image_index"
        ).fetchall()
    assert len(rows) == 2
    assert all(row[1] == "青岛旅游" for row in rows)
    assert all(row[2] == "/original/artifact" for row in rows)
    assert all(row[3].startswith("第一段完整正文") for row in rows)
    assert all(row[4:] == (1, "import", "detail_observed") for row in rows)
    assert images == [
        ("https://example.test/detail-1001.jpg",),
        ("https://example.test/detail-1002.jpg",),
    ]


def test_retryable_failure_preserves_original_row(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=1)
    baseline = repair.sha256_file(target)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    config = config_for(
        target,
        tmp_path / "state.sqlite",
        backup,
        tmp_path / "reports",
        apply=True,
        expected_sha256=baseline,
    )

    config = replace(config, retry_delay_seconds=300)
    calls = 0

    def fail(post_id, cookie_header):
        nonlocal calls
        calls += 1
        raise repair.BilibiliArticleDetailError(
            "rate limited",
            retryable=True,
            code=-509,
            attempts=3,
            retry_wait_seconds=12.0,
        )

    monkeypatch.setattr(repair, "fetch_bilibili_article_detail_with_retry", fail)

    return_code, result = repair.run_repair(config)
    cooldown_code, cooldown_result = repair.run_repair(config)

    assert return_code == 2
    assert result["retryable"] == 1
    assert cooldown_code == 3
    assert cooldown_result["selected_count"] == 0
    assert cooldown_result["stopped_reason"] == "global_cooldown"
    assert calls == 1
    with sqlite3.connect(target) as connection:
        row = connection.execute(
            "SELECT content_text, post_images_count, capture_method FROM web_posts"
        ).fetchone()
    assert row == ("旧搜索摘要-1001", 1, "import")
    with sqlite3.connect(config.state_db_path) as connection:
        assert connection.execute("SELECT status FROM repair_items").fetchone()[0] == "retryable"
        assert connection.execute(
            "SELECT value FROM repair_meta WHERE key='global_next_request_at'"
        ).fetchone()[0]


def test_optimistic_lock_conflict_does_not_overwrite(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=1)
    baseline = repair.sha256_file(target)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    state = tmp_path / "state.sqlite"
    report_dir = tmp_path / "reports"
    dry_run = config_for(
        target,
        state,
        backup,
        report_dir,
        apply=False,
        expected_sha256=baseline,
    )
    repair.run_repair(dry_run)
    with sqlite3.connect(target) as connection:
        connection.execute(
            "UPDATE web_posts SET content_text='并发修改', content_length=4 WHERE platform_key='bilibili'"
        )
        connection.commit()
    apply_config = config_for(
        target,
        state,
        backup,
        report_dir,
        apply=True,
        expected_sha256=baseline,
    )
    monkeypatch.setattr(repair, "fetch_bilibili_article_detail_with_retry", fake_detail)

    return_code, result = repair.run_repair(apply_config)

    assert return_code == 0
    assert result["conflict"] == 1
    with sqlite3.connect(target) as connection:
        row = connection.execute(
            "SELECT content_text, capture_method FROM web_posts"
        ).fetchone()
    assert row == ("并发修改", "import")
