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


def test_persisted_content_keeps_line_breaks_and_unescapes_entities():
    # 离线样本：移动搜索 mblog.text 的真实形态（<br /> 分段、实体转义、话题链接）。
    text = (
        '第一段&amp;青岛<br />第二段 &quot;栈桥&quot;<br/>'
        '<a href="https://m.weibo.cn/search?containerid=231522">#青岛旅游#</a>&lt;完&gt;<BR>尾'
    )
    assert weibo_store.persisted_weibo_content_text({"text": text}) == (
        '第一段&青岛\n第二段 "栈桥"\n#青岛旅游#<完>\n尾'
    )


def test_created_at_timestamp_and_shanghai_datetime_agree():
    # 上游 04631ba 的时区修复：时间戳按 UTC 换算，展示时间为东八区，两者表示同一时刻。
    created = weibo_store.weibo_created_at({"created_at": "Sat Jun 14 12:00:00 +0800 2025"})
    assert created == (1749873600, "2025-06-14 12:00:00+08:00")
    from datetime import datetime

    assert datetime.fromisoformat(created[1]).timestamp() == created[0]
    utc = weibo_store.weibo_created_at({"created_at": "Sat Jun 14 04:00:00 +0000 2025"})
    assert utc == created


@pytest.mark.parametrize("value", [None, "", "not a date", 1749873600, "2025-06-14 12:00:00"])
def test_unparseable_created_at_is_not_projected(value):
    mblog = {"id": "n1", "text": "正文", "created_at": value, "user": {"id": "u", "screen_name": "名"}}
    assert weibo_store.weibo_created_at(mblog) is None
    assert weibo_store.update_weibo_note(
        {"mblog": mblog}, source_keyword="青岛", current_timestamp=lambda: 0,
    ) is None


def test_projected_record_carries_platform_created_at():
    mblog = {"id": "n1", "text": "正文", "created_at": "Sat Jun 14 12:00:00 +0800 2025",
             "user": {"id": "u", "screen_name": "名"}}
    item = weibo_store.update_weibo_note({"mblog": mblog}, source_keyword="青岛", current_timestamp=lambda: 0)
    assert (item["create_time"], item["create_date_time"]) == (1749873600, "2025-06-14 12:00:00+08:00")


@pytest.mark.parametrize(
    "cards,exhausted,notes",
    [
        ([], True, 0),
        (None, True, 0),
        ([{"card_type": 11, "card_group": None}], False, 0),
        ([{"card_type": 11, "card_group": [{"card_type": 4, "desc": "相关用户"}]}], False, 0),
        ([{"card_type": 11, "card_group": [{"card_type": 4, "desc": "暂无内容"}]}], True, 0),
        ([{"card_type": 11, "card_group": [{"card_type": 9, "mblog": {"id": "1"}}]}], False, 1),
        ([{"card_type": 9, "mblog": {"id": "1"}}, "bad", None], False, 1),
    ],
)
def test_search_page_exhaustion_requires_empty_cards_or_explicit_marker(cards, exhausted, notes):
    assert weibo_store.search_page_exhausted(cards) is exhausted
    assert len(weibo_store.filter_search_result_card(cards)) == notes


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
