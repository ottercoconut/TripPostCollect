from __future__ import annotations

import asyncio

from support.douyin import config
from support.douyin import douyin_store
from trippostcollect.platforms.douyin.parser import douyin_source_asset_key


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
        "desc": "body",
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
    assert captured["content_detail_source"] == "aweme_detail"
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
