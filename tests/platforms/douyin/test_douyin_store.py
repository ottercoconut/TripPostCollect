from __future__ import annotations

import asyncio

import json

import pytest

from support.douyin import config
from support.douyin import douyin_store
from trippostcollect.artifacts.image_candidates import content_image_candidates
from trippostcollect.platforms.douyin import parser
from trippostcollect.platforms.douyin.parser import douyin_source_asset_key
from trippostcollect.records.formal import validate_formal_record


def test_assets_keep_uri_order_and_ignore_transport_signature() -> None:
    first = douyin_store._extract_note_image_assets(
        {
            "images": [
                {
                    "uri": "stable-uri",
                    "url_list": ["https://p3.test/body.jpeg?signature=one"],
                }
            ]
        }
    )
    second = douyin_store._extract_note_image_assets(
        {
            "images": [
                {
                    "uri": "stable-uri",
                    "url_list": ["https://p9.test/other.webp?signature=two"],
                }
            ]
        }
    )

    assert first[0]["source_asset_key"] == "douyin:uri:stable-uri"
    assert second[0]["source_asset_key"] == first[0]["source_asset_key"]
    assert first[0]["source_index"] == 0
    assert douyin_source_asset_key(
        None, "https://p3.test/same/path.jpeg?signature=one"
    ) == douyin_source_asset_key(
        None, "https://p9.test/same/path.jpeg?signature=two"
    )


def test_jsonl_record_keeps_only_body_assets_in_image_metadata(monkeypatch):
    captured = {}

    class Store:
        async def store_content(self, content_item):
            captured.update(content_item)

    monkeypatch.setattr(config, "SAVE_DATA_OPTION", "jsonl")
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(
        douyin_store.DouyinStoreFactory,
        "create_store",
        staticmethod(lambda: Store()),
    )
    aweme = {
        "aweme_id": "record-1",
        "aweme_type": 68,
        "desc": "body",
        "create_time": 1_700_000_000,
        "author": {"uid": "author", "nickname": "name"},
        "statistics": {},
        "images": [
            {
                "uri": "body-uri",
                "url_list": ["https://p3.test/body.jpg?signature=fresh"],
            }
        ],
        "video": {
            "raw_cover": {"url_list": ["", "https://media.test/cover.jpg"]},
            "play_addr": {"url_list": ["", "https://media.test/video.mp4"]},
        },
        "music": {"play_url": {"uri": "https://media.test/music.mp3"}},
    }

    asyncio.run(douyin_store.update_douyin_aweme(aweme))

    assert captured["image_list_source"] == "aweme.images"
    assert captured["content_detail_status"] == "detail_observed"
    # #116：搜索路径直接用搜索卡片 aweme_info，来源如实标为 search_aweme_info。
    assert captured["content_detail_source"] == "search_aweme_info"
    assert captured["image_assets"] == [
        {
            "uri": "body-uri",
            "url": "https://p3.test/body.jpg?signature=fresh",
            "source_index": 0,
            "source_asset_key": "douyin:uri:body-uri",
        }
    ]
    serialized = str(captured["image_assets"])
    assert "cover.jpg" not in serialized
    assert "video.mp4" not in serialized
    assert "music.mp3" not in serialized
    # #116：内容记录不再带封面、视频、音乐地址字段。
    assert not {"cover_url", "video_download_url", "music_download_url"} & set(captured)
    assert "media.test" not in json.dumps(captured, ensure_ascii=False)


def image_note(**overrides):
    aweme = {
        "aweme_id": "note-1",
        "aweme_type": 68,
        "desc": "青岛栈桥正文",
        "create_time": 1_700_000_000,
        "author": {"uid": "author-uid", "nickname": "name", "follower_count": 0},
        "statistics": {"digg_count": 1, "collect_count": 2, "comment_count": 3, "share_count": 4},
        "images": [{"uri": "body-uri", "url_list": ["https://p3.test/body.jpg"]}],
    }
    aweme.update(overrides)
    return aweme


def project(aweme, *, source="search_aweme_info", expected_aweme_id="note-1"):
    return parser.update_douyin_aweme(
        aweme,
        source_keyword="青岛旅游",
        current_timestamp=lambda: 1,
        save_data_option="jsonl",
        enable_get_medias=True,
        content_detail_source=source,
        expected_aweme_id=expected_aweme_id,
    )


@pytest.mark.parametrize("source", ["search_aweme_info", "aweme_detail"])
def test_content_source_is_labelled_as_passed_and_both_sources_pass_formal_gate(source):
    record = project(image_note(creator_profile={"status_code": 0, "user": {"follower_count": 7}}), source=source)

    assert record["content_detail_source"] == source
    assert record["content_detail_status"] == "detail_observed"
    validation = validate_formal_record(
        "douyin", record, set(), content_image_candidates=content_image_candidates,
    )
    assert validation["valid"], validation["reasons"]


@pytest.mark.parametrize(
    "overrides, expected_aweme_id",
    [
        ({"desc": ""}, "note-1"),
        ({"desc": "   "}, "note-1"),
        ({"create_time": None}, "note-1"),
        ({"author": {"nickname": "name"}}, "note-1"),
        ({"aweme_type": 0, "images": []}, "note-1"),
        ({}, "other-id"),
    ],
)
def test_content_detail_requires_matching_image_note_with_desc_time_and_uid(overrides, expected_aweme_id):
    record = project(image_note(**overrides), expected_aweme_id=expected_aweme_id)

    assert record["content_detail_status"] == "detail_unobserved"
    validation = validate_formal_record(
        "douyin", record, set(), content_image_candidates=content_image_candidates,
    )
    assert "content_detail_unobserved" in validation["reasons"]


def test_empty_desc_is_not_replaced_by_title_or_cover_text():
    record = project(image_note(desc="", title="青岛标题", preview_title="封面文字"))

    assert record["desc"] == ""
    assert record["title"] == ""
    assert record["content_detail_status"] == "detail_unobserved"


@pytest.mark.parametrize(
    "author, creator_profile",
    [
        ({"uid": "author-uid", "nickname": "name", "follower_count": 0}, {}),
        ({"uid": "author-uid", "nickname": "name", "follower_count": 99}, {}),
        # 作者资料里非 user 节点的 fans 键不能被递归扫描成已观测。
        ({"uid": "author-uid", "nickname": "name"}, {"status_code": 0, "extra": {"fans": 5}}),
    ],
)
def test_search_author_followers_are_never_observed(author, creator_profile):
    record = project(image_note(author=author, creator_profile=creator_profile))

    assert record["followers_observed"] is False
    assert record["author_followers_source"] == "missing"
    assert record["followers_count"] is None
    assert "author_followers_zero_suspicious" not in record


@pytest.mark.parametrize(
    "user, expected",
    [
        ({"follower_count": 0}, 0),
        ({"follower_count": 12}, 12),
        ({"mplatform_followers_count": 34}, 34),
        ({"follower_count": 0, "mplatform_followers_count": 56}, 56),
    ],
)
def test_creator_profile_user_node_is_the_only_follower_source(user, expected):
    record = project(image_note(creator_profile={"status_code": 0, "user": user}))

    assert record["followers_observed"] is True
    assert record["author_followers_source"] == "creator_profile"
    assert record["followers_count"] == expected
    assert record["fans_count"] == expected
