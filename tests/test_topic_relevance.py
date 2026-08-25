from __future__ import annotations

import sqlite3
import sys
from importlib import import_module
from pathlib import Path

import pytest

from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.db.topic_relevance_migration import migrate_topic_relevance
from trippostcollect.records.topic_relevance import is_topic_relevant


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
mediacrawler_crawl = import_module("mediacrawler_crawl")


@pytest.mark.parametrize(
    ("title", "content", "keyword", "expected"),
    [
        ("青岛散步", "正文", "别的词", True),
        ("标题", "去青岛看海", "别的词", True),
        ("崂山攻略", "完整关键词在正文：崂山徒步攻略", "崂山徒步攻略", True),
        ("ＡＢＣ  攻略", "正文", "abc 攻略", True),
        ("标题", "正文", "", False),
        ("标题", "正文", "青岛旅游", False),
        ("青 岛", "正文", "", False),
    ],
)
def test_topic_relevance_normalized_literal_matching(
    title: str,
    content: str,
    keyword: str,
    expected: bool,
) -> None:
    assert is_topic_relevant(title=title, content_text=content, keyword=keyword) is expected


def test_raw_json_is_outside_classifier_inputs() -> None:
    raw_sample_json = '{"source_keyword":"青岛旅游"}'
    assert "青岛" in raw_sample_json
    assert is_topic_relevant(title="普通标题", content_text="普通正文", keyword="") is False


@pytest.mark.parametrize(
    ("platform", "root_record", "child_title", "child_body"),
    [
        ("bilibili", {"title": "普通", "content_text": "青岛正文"}, "普通", "青岛正文"),
        ("weibo", {"content": "青岛正文"}, "", "青岛正文"),
        ("douyin", {"title": "青岛标题", "desc": "正文"}, "青岛标题", "正文"),
        ("xhs", {"title": "标题", "desc": "含崂山徒步"}, "标题", "含崂山徒步"),
        ("zhihu", {"title": "标题", "content_text": "含崂山徒步"}, "标题", "含崂山徒步"),
    ],
)
def test_five_platform_child_projection_matches_root_classifier(
    platform: str,
    root_record: dict[str, str],
    child_title: str,
    child_body: str,
) -> None:
    keyword = "崂山徒步"
    root_result = is_topic_relevant(
        title=root_record.get("title"),
        content_text=mediacrawler_crawl.content_text_for_record(platform, root_record),
        keyword=keyword,
    )
    child_result = is_topic_relevant(
        title=child_title,
        content_text=child_body,
        keyword=keyword,
    )
    assert child_result is root_result


def test_schema_v20_backfills_history_and_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_connection(conn, sync_scheduler=False, sync_jobs=False)
    conn.executemany(
        """
        INSERT INTO web_posts (
            platform_key, platform_post_id, source_type, source_url, title,
            captured_at, keyword, topic_relevant, content_text,
            raw_sample_json, metrics_json, author_json, capture_method, status
        ) VALUES ('bilibili', ?, 'test', ?, ?, '2026-08-26T00:00:00+08:00', ?, 0, ?, ?, '{}', '{}', 'import', 'captured')
        """,
        [
            ("relevant", "https://example.test/relevant", "青岛标题", "别的词", "正文", "{}"),
            ("keyword", "https://example.test/keyword", "标题", "崂山徒步", "包含崂山徒步的正文", "{}"),
            (
                "irrelevant",
                "https://example.test/irrelevant",
                "标题",
                "青岛旅游",
                "普通正文",
                '{"source_keyword":"青岛旅游"}',
            ),
        ],
    )
    conn.execute("DELETE FROM schema_migrations WHERE version=20")

    first = migrate_topic_relevance(conn)
    protected_before = first["invariants"]
    second = migrate_topic_relevance(conn)

    assert first["rows_updated"] == 3
    assert first["distribution"]["relevant"] == 2
    assert first["distribution"]["irrelevant"] == 1
    assert second["already_applied"] is True
    assert second["rows_updated"] == 0
    assert second["invariants"] == protected_before
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
