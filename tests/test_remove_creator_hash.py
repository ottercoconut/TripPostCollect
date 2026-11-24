"""移除 author_json.creator_hash：新入库不再写该键，历史行由 schema v25 删除。

作者平台 ID 只留在 web_posts.author_platform_id。内存记录与访问门禁改读同一字段名。
历史 raw_sample_json 仍可能只有 creator_hash；修复链必须改读列，不能再读这个快照键。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from importlib import import_module
from pathlib import Path

import pytest

from trippostcollect.application.repair import load_post_repair_fallbacks
from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.db.content import row_for_record
from trippostcollect.platforms.bilibili.parser import normalize_bilibili_article_record
from trippostcollect.platforms.douyin import parser as douyin_parser
from trippostcollect.platforms.weibo import parser as weibo_parser
from trippostcollect.platforms.xhs import parser as xhs_parser
from trippostcollect.platforms.zhihu.parser import ZhihuExtractor, update_zhihu_content
from trippostcollect.records.formal import merge_repair_fallback_metadata


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

repair_details = import_module("repair_post_details")

MIGRATION_VERSION = 25
MIGRATION_NAME = "remove_author_json_creator_hash"
ZHIHU_AUTHOR_ID = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"


def _clock() -> int:
    return 1_700_000_000_000


def _weibo_record() -> dict:
    parsed = weibo_parser.parse_weibo_detail(
        {
            "idstr": "5100000000000001",
            "created_at": "Sat Jun 14 12:00:00 +0800 2025",
            "text_raw": "青岛栈桥正文",
            "attitudes_count": 1,
            "comments_count": 1,
            "reposts_count": 1,
        },
        {"id": 7654321, "screen_name": "青岛 旅行者", "followers_count": 10},
        mblogid="Abc",
        source_keyword="青岛旅游",
        current_timestamp=_clock,
    )
    assert parsed.record is not None
    return parsed.record


def _douyin_record() -> dict:
    return douyin_parser.update_douyin_aweme(
        {
            "aweme_id": "7234567890123456",
            "aweme_type": 68,
            "desc": "青岛崂山正文",
            "create_time": 1_700_000_000,
            "author": {"uid": "9876543210", "nickname": "崂山 小王"},
            "statistics": {},
        },
        source_keyword="青岛旅游",
        current_timestamp=_clock,
        save_data_option="jsonl",
        enable_get_medias=True,
        content_detail_source="search_aweme_info",
        expected_aweme_id="7234567890123456",
    )


def _zhihu_record() -> dict:
    answer = {
        "id": "100200300",
        "type": "answer",
        "question": {"id": "10"},
        "content": "<p>青岛八大关正文</p>",
        "created_time": 1_700_000_000,
        "author": {
            "id": ZHIHU_AUTHOR_ID,
            "url_token": "qingdao-traveler",
            "name": "八大关的风",
            "follower_count": 3,
        },
    }
    (content,) = ZhihuExtractor().extract_contents_from_search(
        {"data": [{"type": "search_result", "object": answer}]}
    )
    return update_zhihu_content(content, source_keyword="青岛旅游")


def _xhs_record() -> dict:
    return xhs_parser.update_xhs_note(
        {
            "note_id": "64b95d01000000000c034587",
            "type": "normal",
            "title": "青岛海边",
            "desc": "青岛海边正文",
            "time": 1_700_000_000_000,
            "xsec_token": "token",
            "user": {"user_id": "5f0000000000000001000001", "nickname": "海边 的猫"},
            "creator_profile": {"fans_count": 10},
            "interact_info": {},
            "image_list": [],
        },
        source_keyword="青岛旅游",
        current_timestamp=_clock,
        save_data_option="jsonl",
    )


def _bilibili_record() -> dict:
    record = normalize_bilibili_article_record(
        {
            "id": "123",
            "title": "青岛专栏",
            "desc": "搜索摘要",
            "author": "专栏作者",
            "mid": "456",
            "pubdate": 1_700_000_000,
        },
        "青岛旅游",
    )
    assert record is not None
    return record


PARSED = {
    "weibo": (_weibo_record, "7654321"),
    "douyin": (_douyin_record, "9876543210"),
    "zhihu": (_zhihu_record, ZHIHU_AUTHOR_ID),
    "xhs": (_xhs_record, "5f0000000000000001000001"),
    "bilibili": (_bilibili_record, "456"),
}


def _project(platform_key: str, record: dict) -> dict:
    return row_for_record(
        platform_key,
        record,
        artifact_dir="/tmp/artifact",
        captured_at="2026-11-24T00:00:00+08:00",
        keyword="青岛旅游",
    )


@pytest.mark.parametrize("platform_key", sorted(PARSED))
def test_imported_author_json_drops_creator_hash(platform_key: str) -> None:
    """五个平台各经真实解析器入库：author_json 不再含 creator_hash，列上的作者 ID 正确。"""

    build, author_id = PARSED[platform_key]
    record = build()
    if platform_key != "bilibili":
        assert record.get("author_platform_id") == author_id
        assert "creator_hash" not in record
    row = _project(platform_key, record)
    author = json.loads(row["author_json"])
    raw = json.loads(row["raw_sample_json"])
    assert row["author_platform_id"] == author_id
    assert "creator_hash" not in author
    assert "author_platform_id" not in author
    assert "creator_hash" not in raw


def test_creator_hash_alone_is_not_an_author_id_alias() -> None:
    """不保留兼容层：只带旧键的记录不能再填上作者 ID 列。"""

    row = _project("weibo", {"creator_hash": "legacy-only", "nickname": "作者", "content": "正文"})
    assert row["author_platform_id"] in (None, "")
    assert "creator_hash" not in json.loads(row["author_json"])


def _open_bootstrapped() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_connection(conn, sync_jobs=False)
    conn.execute("DELETE FROM schema_migrations WHERE version=?", (MIGRATION_VERSION,))
    conn.commit()
    return conn


def _insert_post(
    conn: sqlite3.Connection,
    *,
    post_id: str,
    author_platform_id: str | None,
    author: dict,
    raw: dict,
) -> None:
    conn.execute(
        """
        INSERT INTO web_posts(
            platform_key, platform_post_id, source_type, source_url, captured_at,
            author_platform_id, author_json, raw_sample_json
        ) VALUES ('weibo', ?, 'mediacrawler_search', 'https://weibo.com/1/x',
                  '2026-01-01T00:00:00+08:00', ?, ?, ?)
        """,
        (
            post_id,
            author_platform_id,
            json.dumps(author, ensure_ascii=False, sort_keys=True),
            json.dumps(raw, ensure_ascii=False, sort_keys=True),
        ),
    )


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT platform_post_id, author_platform_id, author_json, raw_sample_json
        FROM web_posts ORDER BY id
        """
    ).fetchall()


def test_migration_removes_creator_hash_and_is_idempotent() -> None:
    with _open_bootstrapped() as conn:
        _insert_post(
            conn,
            post_id="keep-keys",
            author_platform_id="7654321",
            author={"creator_hash": "7654321", "followers_count": 10, "nickname": "青岛"},
            raw={"creator_hash": "7654321", "content": "历史快照"},
        )
        _insert_post(
            conn,
            post_id="empty-string",
            author_platform_id="",
            author={"creator_hash": "", "nickname": "空"},
            raw={"note": "empty"},
        )
        _insert_post(
            conn,
            post_id="json-null",
            author_platform_id=None,
            author={"creator_hash": None, "nickname": "空值"},
            raw={"note": "null"},
        )
        _insert_post(
            conn,
            post_id="absent",
            author_platform_id="88",
            author={"nickname": "无此键", "followers_count": 1},
            raw={"creator_hash": "88"},
        )
        _insert_post(
            conn,
            post_id="numeric",
            author_platform_id="88",
            author={"creator_hash": 88, "nickname": "数字"},
            raw={"note": "number"},
        )
        conn.commit()
        raw_before = [row["raw_sample_json"] for row in _rows(conn)]

        bootstrap_connection(conn, sync_jobs=False)
        migrated = _rows(conn)
        authors = [json.loads(row["author_json"]) for row in migrated]
        assert authors[0] == {"followers_count": 10, "nickname": "青岛"}
        assert authors[1] == {"nickname": "空"}
        assert authors[2] == {"nickname": "空值"}
        assert authors[3] == {"followers_count": 1, "nickname": "无此键"}
        assert authors[4] == {"nickname": "数字"}
        assert [row["raw_sample_json"] for row in migrated] == raw_before
        assert [row["author_platform_id"] for row in migrated] == ["7654321", "", None, "88", "88"]
        assert conn.execute(
            "SELECT name FROM schema_migrations WHERE version=?",
            (MIGRATION_VERSION,),
        ).fetchone()[0] == MIGRATION_NAME

        author_text = [row["author_json"] for row in migrated]
        bootstrap_connection(conn, sync_jobs=False)
        assert [row["author_json"] for row in _rows(conn)] == author_text
        assert [row["raw_sample_json"] for row in _rows(conn)] == raw_before
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=?",
            (MIGRATION_VERSION,),
        ).fetchone()[0] == 1


def test_migration_rejects_mismatch_without_writing() -> None:
    with _open_bootstrapped() as conn:
        _insert_post(
            conn,
            post_id="good",
            author_platform_id="7654321",
            author={"creator_hash": "7654321", "nickname": "一致"},
            raw={"creator_hash": "7654321"},
        )
        _insert_post(
            conn,
            post_id="bad-empty-vs-null",
            author_platform_id=None,
            author={"creator_hash": "", "nickname": "空串不等于 NULL"},
            raw={"creator_hash": "snapshot"},
        )
        conn.commit()
        before = [(row["author_json"], row["raw_sample_json"]) for row in _rows(conn)]

        with pytest.raises(RuntimeError, match=r"不一致 1 行"):
            bootstrap_connection(conn, sync_jobs=False)

        assert [(row["author_json"], row["raw_sample_json"]) for row in _rows(conn)] == before
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=?",
            (MIGRATION_VERSION,),
        ).fetchone()[0] == 0


def test_fresh_bootstrap_records_creator_hash_migration() -> None:
    with sqlite3.connect(":memory:") as conn:
        bootstrap_connection(conn, sync_jobs=False)
        row = conn.execute(
            "SELECT name FROM schema_migrations WHERE version=?",
            (MIGRATION_VERSION,),
        ).fetchone()
    assert row[0] == MIGRATION_NAME


def test_repair_historical_snapshot_uses_author_platform_id_column(tmp_path: Path) -> None:
    """历史快照只有 creator_hash 时，修复链仍从列得到作者 ID，且不把旧键抄回新记录。"""

    db_path = tmp_path / "repair.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE web_posts(
                platform_key TEXT,
                platform_post_id TEXT,
                author_platform_id TEXT,
                author_display_name TEXT,
                author_followers_count INTEGER,
                author_profile_url TEXT,
                author_description TEXT,
                raw_sample_json TEXT,
                keyword TEXT,
                published_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO web_posts VALUES (
                'weibo', 'same', '7654321', '作者', NULL, NULL, NULL, ?, '青岛旅游',
                '2026-01-01T00:00:00+08:00'
            )
            """,
            (json.dumps({"creator_hash": "7654321", "nickname": "作者"}),),
        )
        conn.execute(
            """
            INSERT INTO web_posts VALUES (
                'douyin', 'differ', 'from-column', '作者', NULL, NULL, NULL, ?, '青岛旅游',
                '2026-01-01T00:00:00+08:00'
            )
            """,
            (json.dumps({"creator_hash": "from-snapshot"}),),
        )
        same = conn.execute("SELECT * FROM web_posts WHERE platform_post_id='same'").fetchone()
        differ = conn.execute("SELECT * FROM web_posts WHERE platform_post_id='differ'").fetchone()

    fallbacks = load_post_repair_fallbacks(db_path, "weibo", {"same"})
    assert fallbacks["same"]["author_platform_id"] == "7654321"
    assert "creator_hash" not in fallbacks["same"]

    script_same = repair_details._repair_fallback_for_row(
        "weibo", same, json.loads(same["raw_sample_json"])
    )
    assert script_same["author_platform_id"] == "7654321"
    assert "creator_hash" not in script_same

    script_differ = repair_details._repair_fallback_for_row(
        "douyin", differ, json.loads(differ["raw_sample_json"])
    )
    assert script_differ["author_platform_id"] == "from-column"
    assert "creator_hash" not in script_differ
    merged = merge_repair_fallback_metadata("douyin", {"desc": "新正文"}, script_differ)
    assert merged.get("author_platform_id") == "from-column"
    assert "creator_hash" not in merged
    row = _project("douyin", merged)
    assert row["author_platform_id"] == "from-column"
    assert "creator_hash" not in json.loads(row["author_json"])
