"""#191 知乎纯文字回答/文章可入库。临时 SQLite，不访问平台、不写正式库。"""

from __future__ import annotations

import sqlite3
import sys
from copy import deepcopy
from importlib import import_module
from pathlib import Path

from trippostcollect.artifacts.image_completion import verify_image_artifacts


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

mediacrawler_crawl = import_module("mediacrawler_crawl")


def zhihu_record(*, content_id: str, content_type: str, source: str, status: str = "detail_observed") -> dict:
    return {
        "content_id": content_id,
        "content_type": content_type,
        "question_id": "90" if content_type == "answer" else "",
        "title": "青岛问题",
        "content_text": "青岛纯文字正文",
        "content_detail_status": status,
        "content_detail_source": source,
        "image_list": [],
        "created_time": 1_700_000_000,
        "creator_hash": f"author-{content_id}",
        "user_nickname": "作者",
        "followers_count": 8,
        "followers_observed": True,
        "author_followers_source": "author_page",
        "voteup_count": 1,
        "comment_count": 2,
        "collected_count": 3,
        "content_visit_refs": {"images": []},
    }


def test_observed_zhihu_answer_and_article_without_images_are_valid() -> None:
    answer = zhihu_record(content_id="text-answer", content_type="answer", source="answer_detail")
    article = zhihu_record(content_id="text-article", content_type="article", source="article_detail")
    for record in (answer, article):
        validation = mediacrawler_crawl.validate_formal_record("zhihu", record, set())
        assert validation["valid"] is True, validation
        assert validation["content_image_count"] == 0


def test_unobserved_zhihu_text_stays_content_detail_unobserved() -> None:
    record = zhihu_record(
        content_id="text-answer",
        content_type="answer",
        source="answer_detail",
        status="request_failed",
    )
    validation = mediacrawler_crawl.validate_formal_record("zhihu", record, set())
    assert validation["valid"] is False
    assert "content_detail_unobserved" in validation["reasons"]
    assert "missing_content_image" not in validation["reasons"]


def test_other_platforms_still_require_a_content_image() -> None:
    records = {
        "bilibili": {
            "id": "bili-1",
            "content_text": "正文",
            "content_detail_status": "detail_observed",
            "content_detail_source": "article_view_api",
            "content_images_detail_status": "detail_observed",
            "image_urls": [],
            "pubdate": 1_700_000_000,
            "mid": "author",
            "author": "author",
            "followers_count": 10,
            "followers_observed": True,
            "author_followers_source": "relation_stat",
            "liked_count": 1,
            "comment_count": 2,
        },
        "weibo": {
            "note_id": "weibo-1",
            "content": "正文",
            "content_detail_status": "detail_observed",
            "content_detail_source": "mobile_detail",
            "image_list_source": "mblog.pics",
            "image_list": [],
            "create_time": 1_700_000_000,
            "creator_hash": "author",
            "nickname": "author",
            "followers_count": 10,
            "followers_observed": True,
            "author_followers_source": "search_author",
            "liked_count": 1,
            "comments_count": 2,
            "shared_count": 3,
        },
        "douyin": {
            "aweme_id": "douyin-1",
            "desc": "正文",
            "content_detail_status": "detail_observed",
            "content_detail_source": "aweme_detail",
            "note_download_url": [],
            "create_time": 1_700_000_000,
            "creator_hash": "author",
            "nickname": "author",
            "followers_count": 10,
            "followers_observed": True,
            "author_followers_source": "creator_profile",
            "liked_count": 1,
            "collected_count": 2,
            "comment_count": 3,
            "share_count": 4,
        },
        "xhs": {
            "note_id": "xhs-1",
            "desc": "正文",
            "content_detail_status": "detail_observed",
            "content_detail_source": "note_detail",
            "image_list": [],
            "time": 1_700_000_000_000,
            "creator_hash": "author",
            "nickname": "author",
            "followers_count": 10,
            "followers_observed": True,
            "author_followers_source": "creator_profile",
            "liked_count": 1,
            "collected_count": 2,
            "comment_count": 3,
            "share_count": 4,
        },
    }
    for platform_key, record in records.items():
        validation = mediacrawler_crawl.validate_formal_record(platform_key, record, set())
        assert validation["valid"] is False
        assert "missing_content_image" in validation["reasons"]


def test_zero_image_zhihu_posts_materialize_and_import(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    media_root = project_root / "media"
    records = [
        zhihu_record(content_id="text-answer", content_type="answer", source="answer_detail"),
        zhihu_record(content_id="text-article", content_type="article", source="article_detail"),
    ]
    selected = [
        {
            "platform": "zhihu",
            "identity": f"zhihu:id:{record['content_id']}",
            "record": record,
            "manifest_paths": [],
            "is_new": True,
            "topic_relevant": True,
        }
        for record in records
    ]
    report, materialized, complete = mediacrawler_crawl.materialize_formal_record_images(
        deepcopy(selected),
        project_root=project_root,
        media_root=media_root,
        promote=True,
    )
    evidence = verify_image_artifacts(
        {"image_materialization": report},
        project_root=project_root,
        expect_promotion=True,
    )
    assert report["complete"] is True
    assert report["candidate_posts"] == report["complete_posts"] == 2
    assert report["expected_images"] == report["downloaded_images"] == 0
    assert report["validated_images"] == report["unique_images"] == 0
    assert report["promoted_images"] == report["sha256_duplicate_images"] == 0
    assert report["failures"] == []
    assert evidence["ok"] is True
    assert evidence["expected_images"] == 0
    assert complete == set(materialized)
    for item in selected:
        item["materialized_images"] = materialized[item["identity"]]
    imported = mediacrawler_crawl.import_valid_records(
        {
            "captured_at": "2026-11-14T00:00:00+00:00",
            "keyword": "青岛",
            "batch_dir": str(project_root),
        },
        selected,
        project_root / "formal.sqlite",
        project_root=project_root,
        media_root=media_root,
        require_local_images=True,
    )
    assert imported["inserted_rows"] == 2
    with sqlite3.connect(project_root / "formal.sqlite") as conn:
        counts = conn.execute(
            "SELECT platform_post_id, post_images_count FROM web_posts ORDER BY platform_post_id"
        ).fetchall()
        image_rows = conn.execute("SELECT COUNT(*) FROM web_post_images").fetchone()[0]
    assert counts == [("text-answer", 0), ("text-article", 0)]
    assert image_rows == 0
