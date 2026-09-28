"""TripPostCollect tests for full content contract."""

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
EXPECTED_PLATFORMS = ("bilibili", "douyin", "weibo", "xhs", "zhihu")
PUBLISHED_AT_FIELDS = {
    "bilibili": "pubdate",
    "douyin": "create_time",
    "weibo": "create_time",
    "xhs": "time",
    "zhihu": "created_time",
}


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


def test_formal_contract_covers_exactly_five_platforms() -> None:
    assert set(mediacrawler_crawl.PLATFORMS) == set(EXPECTED_PLATFORMS)
    assert set(formal_records()) == set(EXPECTED_PLATFORMS)
    assert set(mediacrawler_crawl.FOLLOWERS_REQUIRED_PLATFORMS) == set(EXPECTED_PLATFORMS)
    assert set(PUBLISHED_AT_FIELDS) == set(EXPECTED_PLATFORMS)


@pytest.mark.parametrize("platform_key", EXPECTED_PLATFORMS)
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


@pytest.mark.parametrize("platform_key", EXPECTED_PLATFORMS)
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


@pytest.mark.parametrize("platform_key", EXPECTED_PLATFORMS)
def test_observed_zero_followers_is_valid(platform_key: str) -> None:
    record = formal_records()[platform_key]
    record["followers_count"] = 0

    validation = mediacrawler_crawl.validate_formal_record(platform_key, record, set())

    assert validation["valid"] is True
    assert validation["reasons"] == []
    assert validation["followers_count"] == 0
    assert validation["followers_observed"] is True
    assert validation["followers_source"] == record["author_followers_source"]


@pytest.mark.parametrize("platform_key", EXPECTED_PLATFORMS)
@pytest.mark.parametrize(
    ("field", "remove", "value", "reason"),
    [
        pytest.param("followers_count", True, None, "missing_followers_count", id="count-missing"),
        pytest.param("followers_count", False, None, "missing_followers_count", id="count-null"),
        pytest.param("author_followers_source", True, None, "missing_followers_source", id="source-missing"),
        pytest.param("author_followers_source", False, "", "missing_followers_source", id="source-empty"),
        pytest.param("followers_observed", True, None, "followers_not_observed", id="observed-missing"),
        pytest.param("followers_observed", False, False, "followers_not_observed", id="observed-false"),
    ],
)
def test_incomplete_follower_evidence_is_invalid(
    platform_key: str, field: str, remove: bool, value: object, reason: str
) -> None:
    record = formal_records()[platform_key]
    if remove:
        record.pop(field)
    else:
        record[field] = value

    validation = mediacrawler_crawl.validate_formal_record(platform_key, record, set())

    assert validation["valid"] is False
    assert validation["reasons"] == [reason]


@pytest.mark.parametrize("platform_key", EXPECTED_PLATFORMS)
def test_missing_platform_time_cannot_be_replaced_by_capture_time(platform_key: str) -> None:
    record = formal_records()[platform_key]
    record.pop(PUBLISHED_AT_FIELDS[platform_key])
    captured_at = "2026-09-27T12:00:00+08:00"
    record["captured_at"] = captured_at

    validation = mediacrawler_crawl.validate_formal_record(platform_key, record, set())
    row = mediacrawler_crawl.row_for_record(
        platform_key,
        record,
        artifact_dir="synthetic-contract-fixture",
        captured_at=captured_at,
        keyword="青岛",
    )

    assert validation["valid"] is False
    assert validation["reasons"] == ["missing_published_at"]
    assert mediacrawler_crawl.published_at_for_record(record) is None
    assert row["published_at"] is None
    assert row["captured_at"] == captured_at


@pytest.mark.parametrize("platform_key", EXPECTED_PLATFORMS)
@pytest.mark.parametrize("detail_status", ("request_failed", "parse_failed", "unobserved"))
def test_failed_body_status_cannot_pass_with_body_and_trusted_source(
    platform_key: str, detail_status: str
) -> None:
    record = formal_records()[platform_key]
    record["content_detail_status"] = detail_status

    validation = mediacrawler_crawl.validate_formal_record(platform_key, record, set())

    assert validation["valid"] is False
    assert validation["reasons"] == ["content_detail_unobserved"]
