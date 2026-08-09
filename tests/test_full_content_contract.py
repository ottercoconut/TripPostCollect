from __future__ import annotations

import sys
from copy import deepcopy
from importlib import import_module
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

mediacrawler_crawl = import_module("mediacrawler_crawl")


def formal_records() -> dict[str, dict]:
    common = {
        "followers_count": 10,
        "followers_observed": True,
    }
    return {
        "bilibili": {
            **common,
            "id": "bili-1",
            "content_text": "B站完整正文",
            "content_detail_status": "detail_observed",
            "content_detail_source": "article_view_api",
            "content_images_detail_status": "detail_observed",
            "image_urls": ["https://i0.hdslb.com/bfs/article/body.jpg"],
            "pubdate": 1_700_000_000,
            "mid": "author-bili",
            "author": "author",
            "author_followers_source": "relation_stat",
            "liked_count": 1,
            "comment_count": 2,
            "view_count": 3,
        },
        "weibo": {
            **common,
            "note_id": "weibo-1",
            "content": "微博完整正文",
            "content_detail_status": "detail_observed",
            "content_detail_source": "mobile_detail",
            "image_list_source": "mblog.pics",
            "image_list": ["https://wx1.sinaimg.cn/large/body.jpg"],
            "create_time": 1_700_000_000,
            "creator_hash": "author-weibo",
            "nickname": "author",
            "author_followers_source": "search_author",
            "liked_count": 1,
            "comments_count": 2,
            "shared_count": 3,
        },
        "xhs": {
            **common,
            "note_id": "xhs-1",
            "title": "标题",
            "desc": "小红书完整正文",
            "content_detail_status": "detail_observed",
            "content_detail_source": "note_detail",
            "image_list": [{"url_default": "https://sns-img.test/body.jpg"}],
            "time": 1_700_000_000_000,
            "creator_hash": "author-xhs",
            "nickname": "author",
            "author_followers_source": "creator_profile",
            "liked_count": 1,
            "collected_count": 2,
            "comment_count": 3,
            "share_count": 4,
        },
        "douyin": {
            **common,
            "aweme_id": "douyin-1",
            "desc": "抖音完整正文",
            "content_detail_status": "detail_observed",
            "content_detail_source": "aweme_detail",
            "note_download_url": "https://p3-sign.test/body.jpg",
            "create_time": 1_700_000_000,
            "creator_hash": "author-douyin",
            "nickname": "author",
            "author_followers_source": "creator_profile",
            "liked_count": 1,
            "collected_count": 2,
            "comment_count": 3,
            "share_count": 4,
        },
        "zhihu": {
            **common,
            "content_id": "zhihu-1",
            "content_type": "answer",
            "title": "问题标题",
            "content_text": "知乎完整正文",
            "content_detail_status": "detail_observed",
            "content_detail_source": "answer_detail",
            "image_list": ["https://pic1.zhimg.com/v2-body.jpg"],
            "created_time": 1_700_000_000,
            "creator_hash": "author-zhihu",
            "user_nickname": "author",
            "author_followers_source": "search_author",
            "voteup_count": 1,
            "comment_count": 2,
        },
    }


@pytest.mark.parametrize("platform_key", sorted(mediacrawler_crawl.PLATFORMS))
def test_all_platforms_require_trusted_complete_body_provenance(platform_key: str) -> None:
    record = formal_records()[platform_key]

    assert mediacrawler_crawl.validate_formal_record(platform_key, record, set())["valid"] is True

    missing_status = deepcopy(record)
    missing_status.pop("content_detail_status")
    validation = mediacrawler_crawl.validate_formal_record(platform_key, missing_status, set())
    assert "content_detail_unobserved" in validation["reasons"]

    untrusted_source = deepcopy(record)
    untrusted_source["content_detail_source"] = "search_excerpt"
    validation = mediacrawler_crawl.validate_formal_record(platform_key, untrusted_source, set())
    assert "untrusted_content_detail_source" in validation["reasons"]


@pytest.mark.parametrize("platform_key", sorted(mediacrawler_crawl.PLATFORMS))
def test_title_or_summary_cannot_replace_the_authoritative_body(platform_key: str) -> None:
    record = formal_records()[platform_key]
    for field in mediacrawler_crawl.CONTENT_BODY_FIELDS[platform_key]:
        record.pop(field, None)
    record.update({"title": "只有标题", "desc": "搜索摘要"})
    if platform_key in {"xhs", "douyin"}:
        record.pop("desc")

    validation = mediacrawler_crawl.validate_formal_record(platform_key, record, set())

    assert "missing_content" in validation["reasons"]
    assert validation["valid"] is False

