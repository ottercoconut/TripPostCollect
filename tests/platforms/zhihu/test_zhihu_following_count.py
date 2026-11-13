"""#182：知乎关注数缺键为 None，真实 0 仍为 0，入库写成 NULL。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.db.content import row_for_record, upsert_web_post
from trippostcollect.platforms.zhihu.models import ZhihuContent, ZhihuCreator
from trippostcollect.platforms.zhihu.parser import ZhihuExtractor, update_zhihu_content


def _author(**overrides: object) -> dict:
    author = {
        "id": "author-1",
        "url_token": "qingdao",
        "name": "青岛作者",
        "follower_count": 3,
    }
    author.update(overrides)
    return author


def _content(author: dict):
    payload = {
        "data": [
            {
                "type": "search_result",
                "object": {
                    "id": "100",
                    "type": "answer",
                    "question": {"id": "10"},
                    "created_time": 1_700_000_000,
                    "content": "<p>青岛正文</p>",
                    "author": author,
                },
            }
        ]
    }
    (content,) = ZhihuExtractor().extract_contents_from_search(payload)
    return content


def test_model_following_defaults_are_none() -> None:
    assert ZhihuContent().following_count is None
    assert ZhihuCreator().follows is None


@pytest.mark.parametrize(
    ("author", "expected"),
    [
        (_author(), None),
        (_author(following_count=None), None),
        (_author(following_count=""), None),
        (_author(following_count=0), 0),
        (_author(following_count=37), 37),
        (_author(followingCount=37), 37),
        (_author(following_count="", followingCount=37), 37),
    ],
)
def test_search_author_following_count(author: dict, expected: int | None) -> None:
    content = _content(author)
    assert content.following_count == expected
    assert content.followers_count == 3


def test_missing_follower_count_still_defaults_to_zero() -> None:
    author = _author()
    author.pop("follower_count")
    content = _content(author)
    assert content.followers_count == 0
    assert content.followers_observed is False
    assert content.following_count is None


def test_missing_following_count_imports_as_null(tmp_path: Path) -> None:
    record = update_zhihu_content(_content(_author()), source_keyword="青岛旅游")
    assert record["following_count"] is None
    row = row_for_record(
        "zhihu",
        record,
        artifact_dir=str(tmp_path),
        captured_at="2026-11-13T00:00:00+08:00",
        keyword="青岛旅游",
    )
    assert row["author_following_count"] is None
    assert json.loads(row["author_json"])["following_count"] is None
    assert json.loads(row["raw_sample_json"])["following_count"] is None
    assert row["author_followers_count"] == 3
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as connection:
        bootstrap_connection(connection, sync_jobs=False)
        upsert_web_post(
            connection,
            row,
            project_root=tmp_path,
            media_root=tmp_path / "data" / "media",
        )
        connection.commit()
        stored = connection.execute(
            "SELECT author_following_count, author_json, author_followers_count FROM web_posts"
        ).fetchone()
    assert stored[0] is None
    author = json.loads(stored[1])
    assert "following_count" in author
    assert author["following_count"] is None
    assert stored[2] == 3
