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
    ("content", "keyword", "expected"),
    [
        ("去青岛看海", "别的词", True),
        ("完整关键词在正文：崂山徒步攻略", "崂山徒步攻略", True),
        ("正文包含 ＡＢＣ  攻略", "abc 攻略", True),
        ("正文", "", False),
        ("正文", "青岛旅游", False),
        ("青 岛", "", False),
    ],
)
def test_topic_relevance_normalized_literal_matching(
    content: str,
    keyword: str,
    expected: bool,
) -> None:
    assert is_topic_relevant(content_text=content, keyword=keyword) is expected


def test_title_is_not_an_independent_topic_source() -> None:
    assert is_topic_relevant(content_text="普通正文", keyword="青岛旅游") is False


def test_raw_json_is_outside_classifier_inputs() -> None:
    raw_sample_json = '{"source_keyword":"青岛旅游"}'
    assert "青岛" in raw_sample_json
    assert is_topic_relevant(content_text="普通正文", keyword="") is False


@pytest.mark.parametrize(
    ("platform", "record", "expected_content_text", "expected"),
    [
        ("bilibili", {"title": "普通", "content_text": "青岛正文"}, "青岛正文", True),
        ("weibo", {"content": "青岛正文"}, "青岛正文", True),
        ("douyin", {"title": "青岛标题", "desc": "正文"}, "正文", False),
        ("xhs", {"title": "青岛标题", "desc": "正文"}, "青岛标题\n正文", True),
        ("zhihu", {"title": "标题", "content_text": "含崂山徒步"}, "标题\n含崂山徒步", True),
    ],
)
def test_five_platform_web_post_content_projection_is_the_only_classifier_input(
    platform: str,
    record: dict[str, str],
    expected_content_text: str,
    expected: bool,
) -> None:
    keyword = "崂山徒步"
    content_text = mediacrawler_crawl.content_text_for_record(platform, record)
    assert content_text == mediacrawler_crawl.web_post_content_text(platform, record)
    assert content_text == expected_content_text
    assert is_topic_relevant(content_text=content_text, keyword=keyword) is expected


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
            ("relevant", "https://example.test/relevant", "标题", "别的词", "青岛正文", "{}"),
            ("keyword", "https://example.test/keyword", "标题", "崂山徒步", "包含崂山徒步的正文", "{}"),
            (
                "irrelevant",
                "https://example.test/irrelevant",
                "青岛标题",
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
