"""小红书媒体策略：只截获正文图片；视频、实况流与头像从不进入图片窗口或写出。"""

from __future__ import annotations

import pytest

from trippostcollect.platforms.xhs import core as xhs_core
from trippostcollect.platforms.xhs import parser as xhs_store
from trippostcollect.platforms.xhs.capture import XHS_IMAGE_RULE

from .support import XiaoHongShuCrawler


def test_no_video_download_or_store_path_exists():
    # T09：视频方法与视频写出出口随退出切片删除；#71 删除直连下载入口，图片只经窗口截获。
    for owner in (XiaoHongShuCrawler, xhs_core, xhs_store):
        for name in ("get_notice_video", "update_xhs_note_video", "get_notice_media", "get_note_images"):
            assert not hasattr(owner, name), (owner, name)


@pytest.mark.parametrize(
    ("url", "resource_type", "captured"),
    [
        ("https://sns-webpic-qc.xhscdn.com/202601/n/notes_pre_post/a!nd_dft_wlteh_webp_3", "image", True),
        # 实况图的视频流与笔记视频是 media 请求，图片规则不认领。
        ("https://sns-video-qc.xhscdn.com/stream/1/110/259/live.mp4", "media", False),
        ("https://sns-webpic-qc.xhscdn.com/202601/n/notes_pre_post/a!nd_dft_wlteh_webp_3", "media", False),
        # 头像主机永不认领。
        ("https://sns-avatar-qc.xhscdn.com/avatar/a.jpg", "image", False),
        ("https://www.xiaohongshu.com/fe_api/img.png", "image", False),
    ],
)
def test_image_rule_never_claims_video_streams_or_avatars(url, resource_type, captured):
    assert XHS_IMAGE_RULE.matches("GET", url, resource_type) is captured


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
