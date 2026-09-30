from __future__ import annotations

from io import BytesIO
import json

from PIL import Image
import pytest

from dataclasses import replace
from support.weibo_adapter import settings, crawler as make_crawler
from trippostcollect.platforms import entry
from trippostcollect.platforms.weibo import parser as weibo_store
from trippostcollect.application.contracts import ImageStagingError

config = settings()


def WeiboStoreImage():
    return entry.weibo_dependencies(config)[1].image_stager()


def png_bytes(color: str = "red") -> bytes:
    output = BytesIO()
    Image.new("RGB", (7, 5), color=color).save(output, format="PNG")
    return output.getvalue()


def test_persisted_content_text_is_the_topic_classification_source():
    assert (
        weibo_store.persisted_weibo_content_text(
            {"text": "<span>青</span><span>岛</span>攻略"}
        )
        == "青岛攻略"
    )
    assert (
        weibo_store.persisted_weibo_content_text(
            {"text": '<a title="青岛">普通正文</a>'}
        )
        == "普通正文"
    )


@pytest.mark.asyncio
async def test_store_uses_true_format_post_directory_and_schema_v1_manifest(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    rows = await WeiboStoreImage().store_post_images(
        "note-123",
        [
            {
                "pid": "pid-abc",
                "source_index": 0,
                "url": "https://wx1.sinaimg.cn/large/fake.jpg?token=secret",
                "content": png_bytes(),
            }
        ],
    )

    stored = tmp_path / "weibo" / "images" / "note-123" / "000.png"
    assert stored.read_bytes() == png_bytes()
    assert not list(tmp_path.rglob("*.part"))
    assert rows[0]["staging_path"] == "weibo/images/note-123/000.png"
    assert rows[0]["mime_type"] == "image/png"
    assert rows[0]["source_asset_key"] == "weibo:pid:pid-abc"
    manifest_rows = [
        json.loads(line)
        for line in (tmp_path / "weibo" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert manifest_rows == rows
    assert "cookie" not in json.dumps(manifest_rows).lower()


@pytest.mark.asyncio
async def test_invalid_second_image_does_not_promote_partial_post(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    with pytest.raises(ImageStagingError) as exc_info:
        await WeiboStoreImage().store_post_images(
            "atomic-note",
            [
                {
                    "pid": "pid-one",
                    "source_index": 0,
                    "url": "https://wx.test/one.jpg",
                    "content": png_bytes(),
                },
                {
                    "pid": "pid-two",
                    "source_index": 1,
                    "url": "https://wx.test/two.jpg",
                    "content": b"<html>not an image</html>",
                },
            ],
        )

    assert exc_info.value.code == "image_non_raster_response"
    assert exc_info.value.source_index == 1
    assert not (tmp_path / "weibo" / "images" / "atomic-note").exists()
    assert not (tmp_path / "weibo" / "image_manifest.jsonl").exists()


@pytest.mark.asyncio
async def test_note_store_keeps_pid_order_and_ignores_avatar(monkeypatch):
    captured = {}

    class FakeStore:
        async def store_content(self, content_item):
            captured.update(content_item)

    crawler = make_crawler(config)
    crawler.ports = replace(crawler.ports, store_factory=lambda: FakeStore())
    await crawler.update_weibo_note(
        {
            "mblog": {
                "id": "note-asset-order",
                "text": "body",
                "content_detail_status": "detail_observed",
                "content_detail_source": "mobile_detail",
                "created_at": "Sat Jun 14 12:00:00 +0800 2025",
                "attitudes_count": 1,
                "comments_count": 2,
                "reposts_count": 3,
                "pics": [
                    {"pid": "pid-1", "large": {"url": "https://wx.test/1.jpg"}},
                    {"pid": "pid-2", "url": "https://wx.test/2.jpg"},
                ],
                "user": {
                    "id": "author",
                    "screen_name": "author",
                    "followers_count": 10,
                    "avatar_hd": "https://wx.test/avatar.jpg",
                    "profile_url": "https://weibo.test/u/author",
                },
            }
        }
    )

    assert captured["image_list"] == [
        "https://wx.test/1.jpg",
        "https://wx.test/2.jpg",
    ]
    assert captured["image_assets"] == [
        {"pid": "pid-1", "url": "https://wx.test/1.jpg", "source_index": 0},
        {"pid": "pid-2", "url": "https://wx.test/2.jpg", "source_index": 1},
    ]
    assert captured["image_list_source"] == "mblog.pics"
    assert captured["content_detail_status"] == "detail_observed"
    assert captured["content_detail_source"] == "mobile_detail"
    assert "avatar" not in json.dumps(captured["image_assets"]).lower()
