from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from trippostcollect.platforms.xhs import core as xhs_core
from trippostcollect.platforms.xhs import parser as xhs_store

from .support import XiaoHongShuCrawler, config


@pytest.mark.asyncio
async def test_image_mode_never_reaches_video_method_or_store(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    # T09：视频方法与视频写出出口随退出切片删除；根 crawler 与 store 出口都不得存在视频写出路径。
    for owner in (XiaoHongShuCrawler, xhs_core, xhs_store):
        assert not hasattr(owner, "get_notice_video"), owner
        assert not hasattr(owner, "update_xhs_note_video"), owner
    crawler = XiaoHongShuCrawler()
    real_get_note_images = crawler.get_note_images
    crawler.get_note_images = AsyncMock(return_value=None)

    await crawler.get_notice_media({"note_id": "image-only"})

    crawler.get_note_images.assert_awaited_once_with({"note_id": "image-only"})

    # 视频记录经同一媒体入口时不产生任何下载或写出调用。
    crawler.get_note_images = real_get_note_images
    crawler.xhs_client = AsyncMock()
    crawler.update_xhs_note = AsyncMock(return_value=None)
    crawler.update_xhs_note_images = AsyncMock(return_value=None)
    crawler.record_xhs_note_image_failure = AsyncMock(return_value=None)
    content_sink_factory = MagicMock()
    monkeypatch.setattr(crawler.ports, "content_sink_factory", content_sink_factory)

    await crawler.get_notice_media({
        "note_id": "video-only",
        "type": "video",
        "video": {"media": {"stream": {"h264": [{"master_url": "https://video.test/v.mp4"}]}}},
    })

    crawler.xhs_client.get_note_media.assert_not_awaited()
    crawler.update_xhs_note.assert_not_awaited()
    crawler.update_xhs_note_images.assert_not_awaited()
    crawler.record_xhs_note_image_failure.assert_not_awaited()
    content_sink_factory.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_media_mode_reaches_no_media_method(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", False)
    crawler = XiaoHongShuCrawler()
    crawler.get_note_images = AsyncMock(return_value=None)
    crawler.get_notice_video = AsyncMock(return_value=None)

    await crawler.get_notice_media({"note_id": "disabled"})

    crawler.get_note_images.assert_not_awaited()
    crawler.get_notice_video.assert_not_awaited()


def test_xhs_asset_projection_excludes_avatar_and_chooses_one_url():
    assets = xhs_store._xhs_image_assets(
        {
            "image_list": [
                {
                    "url_default": "https://img.test/notes/a?format=webp",
                    "url_pre": "https://img.test/notes/a?format=jpg",
                }
            ],
            "user": {"avatar": "https://avatar.test/profile.jpg"},
        }
    )

    assert assets == [
        {
            "url": "https://img.test/notes/a?format=webp",
            "source_index": 0,
            "source_asset_key": "xhs:path:/notes/a",
        }
    ]
    assert "avatar" not in str(assets).lower()
