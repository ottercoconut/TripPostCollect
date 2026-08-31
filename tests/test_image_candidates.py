"""TripPostCollect tests for image candidates."""

from __future__ import annotations

import json
import sys
from importlib import import_module
from pathlib import Path

import pytest

from trippostcollect.artifacts.image_candidates import (
    content_image_candidates,
    normalize_image_url,
    source_asset_key_for_image,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

mediacrawler_crawl = import_module("mediacrawler_crawl")


def candidate_urls(platform_key: str, record: dict) -> list[str]:
    return [item.source_url for item in content_image_candidates(platform_key, record)]


def test_bilibili_uses_observed_detail_images_not_search_preview_or_avatar() -> None:
    record = {
        "content_id": "cv-1",
        "content_images_detail_status": "detail_observed",
        "image_urls": [
            " //i0.hdslb.com/bfs/article/body-a.jpg ",
            "https://i0.hdslb.com/bfs/article/body-b.png",
            "https://i0.hdslb.com/bfs/article/body-b.png",
        ],
        "search_preview_image": "https://i0.hdslb.com/bfs/archive/preview.jpg",
        "avatar_url": "https://i0.hdslb.com/bfs/face/avatar.jpg",
    }

    candidates = content_image_candidates("bilibili", record)

    assert [item.source_url for item in candidates] == [
        "https://i0.hdslb.com/bfs/article/body-a.jpg",
        "https://i0.hdslb.com/bfs/article/body-b.png",
    ]
    assert [item.source_index for item in candidates] == [0, 1]
    assert {item.source_key for item in candidates} == {"image_urls"}
    assert {item.image_role for item in candidates} == {"content"}
    assert all(item.source_asset_key for item in candidates)

    record["content_images_detail_status"] = "unobserved"
    assert content_image_candidates("bilibili", record) == []


def test_weibo_keeps_normalized_mblog_pic_order_only() -> None:
    record = {
        "note_id": "wb-1",
        "image_list_source": "mblog.pics",
        "image_list": [
            "https://wx1.sinaimg.cn/large/pid-a.jpg",
            "//wx2.sinaimg.cn/large/pid-b.webp",
            "https://wx1.sinaimg.cn/large/pid-a.jpg",
        ],
        "avatar_url": "https://tvax.test/avatar.jpg",
        "author_profile_url": "https://weibo.test/u/profile-image",
    }

    assert candidate_urls("weibo", record) == [
        "https://wx1.sinaimg.cn/large/pid-a.jpg",
        "https://wx2.sinaimg.cn/large/pid-b.webp",
    ]

    record["image_list_source"] = "search.preview"
    assert content_image_candidates("weibo", record) == []


def test_xhs_selects_one_url_per_object_and_deduplicates_cdn_variants() -> None:
    record = {
        "note_id": "xhs-1",
        "image_list": [
            {
                "url_default": "https://sns-img-a.test/notes_pre_post/asset-a?imageView2=1",
                "url": "https://sns-img-b.test/notes_pre_post/asset-a?imageView2=2",
                "url_pre": "https://sns-img-c.test/notes_pre_post/asset-a?imageView2=3",
            },
            {"url": "http://sns-img-d.test/notes_pre_post/asset-a?signature=changed"},
            {
                "url_default": "not-a-url",
                "url": "https://sns-img.test/notes_post/asset-b",
            },
        ],
        "avatar_url": "https://sns-avatar.test/avatar.jpg",
    }

    assert candidate_urls("xhs", record) == [
        "https://sns-img-a.test/notes_pre_post/asset-a?imageView2=1",
        "https://sns-img.test/notes_post/asset-b",
    ]


def test_douyin_only_projects_note_download_urls() -> None:
    record = {
        "aweme_id": "dy-1",
        "note_download_url": (
            "https://p3-sign.test/note-a.jpeg?signature=one,"
            " //p3-sign.test/note-b.webp?signature=two,"
            "https://p3-sign.test/note-a.jpeg?signature=one"
        ),
        "cover_url": "https://p3-sign.test/cover.jpeg",
        "video_download_url": "https://video.test/play.mp4",
        "music_download_url": "https://music.test/play.mp3",
        "avatar_url": "https://avatar.test/user.jpeg",
    }

    assert candidate_urls("douyin", record) == [
        "https://p3-sign.test/note-a.jpeg?signature=one",
        "https://p3-sign.test/note-b.webp?signature=two",
    ]


def test_zhihu_excludes_formula_author_and_profile_urls() -> None:
    record = {
        "content_id": "answer-1",
        "image_list": [
            "https://pic1.zhimg.com/v2-body-a.jpg",
            "https://www.zhihu.com/equation?tex=x%2By",
            "//pic2.zhimg.com/v2-body-b.webp",
        ],
        "avatar_url": "https://pic1.zhimg.com/v2-avatar.jpg",
        "author_profile_url": "https://www.zhihu.com/people/profile-image",
    }

    assert candidate_urls("zhihu", record) == [
        "https://pic1.zhimg.com/v2-body-a.jpg",
        "https://pic2.zhimg.com/v2-body-b.webp",
    ]


def test_zhihu_deduplicates_known_zhimg_transform_variants_by_asset_path() -> None:
    first = "https://pic1.zhimg.com/v2-body-a_r.jpg"
    record = {
        "content_id": "answer-variants",
        "image_list": [
            first,
            "https://pic2.zhimg.com/v2-body-a_1440w.jpg",
            "https://pic3.zhimg.com/v2-body-a_720w.webp?source=answer",
            "https://external.test/v2-body-a_1440w.jpg",
        ],
    }

    candidates = content_image_candidates("zhihu", record)

    assert [item.source_url for item in candidates] == [
        first,
        "https://external.test/v2-body-a_1440w.jpg",
    ]
    assert [item.source_index for item in candidates] == [0, 1]
    assert candidates[0].source_asset_key == source_asset_key_for_image(
        "zhihu",
        "https://pic2.zhimg.com/v2-body-a_1440w.jpg",
    )


@pytest.mark.parametrize(
    ("platform_key", "record"),
    [
        ("bilibili", {"content_images_detail_status": "detail_observed"}),
        ("weibo", {}),
        ("xhs", {}),
        ("douyin", {}),
        ("zhihu", {}),
    ],
)
def test_non_content_fields_never_become_content_candidates(
    platform_key: str,
    record: dict,
) -> None:
    record.update(
        {
            "avatar_url": "https://example.test/avatar.jpg",
            "author_avatar": "https://example.test/author-avatar.jpg",
            "author_profile_url": "https://example.test/profile-image.jpg",
            "cover_url": "https://example.test/cover.jpg",
            "video_download_url": "https://example.test/video.mp4",
            "music_download_url": "https://example.test/music.jpg",
            "search_preview_image": "https://example.test/preview.jpg",
        }
    )

    assert content_image_candidates(platform_key, record) == []


def test_avatar_is_removed_from_row_and_image_relationships() -> None:
    record = {
        "note_id": "xhs-2",
        "image_list": "https://example.test/body.jpg",
        "avatar_url": "//example.test/avatar.jpg",
    }

    row = mediacrawler_crawl.row_for_record(
        "xhs",
        record,
        artifact_dir="outputs/test",
        captured_at="2026-08-07T00:00:00+00:00",
        keyword="青岛旅游",
    )

    assert row["post_images_count"] == 1
    assert [item["role"] for item in row["_image_items"]] == ["content"]
    assert row["_image_items"][0]["source_index"] == 0
    assert "author_avatar_url" not in row
    assert "avatar_url" not in json.loads(row["author_json"])
    assert "avatar_url" not in json.loads(row["raw_sample_json"])


def test_normalize_image_url_rejects_non_http_values() -> None:
    assert normalize_image_url(" //EXAMPLE.test/body.jpg ) ") == "https://example.test/body.jpg"
    assert normalize_image_url("data:image/png;base64,abc") is None
    assert normalize_image_url("file:///tmp/body.jpg") is None
    assert normalize_image_url("javascript:alert(1)") is None


def test_unknown_platform_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported image candidate platform"):
        content_image_candidates("unknown", {})
