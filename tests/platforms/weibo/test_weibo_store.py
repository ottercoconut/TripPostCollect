from __future__ import annotations

from io import BytesIO
import json

from PIL import Image
import pytest

from support.weibo_adapter import settings, crawler as make_crawler
from trippostcollect.platforms import entry
from trippostcollect.platforms.weibo import parser as weibo_store
from trippostcollect.application.contracts import ImageStagingError
from trippostcollect.artifacts.image_candidates import content_image_candidates, image_items_for_record
from trippostcollect.records.formal import row_for_record, validate_formal_record

config = settings()


def WeiboStoreImage():
    return entry.weibo_dependencies(config)[1].image_stager()


def png_bytes(color: str = "red") -> bytes:
    output = BytesIO()
    Image.new("RGB", (7, 5), color=color).save(output, format="PNG")
    return output.getvalue()


def _status(**extra) -> dict:
    payload = {
        "idstr": "525",
        "mid": "525",
        "mblogid": "Abc12",
        "created_at": "Sat Jun 14 12:00:00 +0800 2025",
        "text_raw": "青岛栈桥",
        "attitudes_count": 1,
        "comments_count": 2,
        "reposts_count": 3,
        "pic_ids": ["pid-1"],
        "pic_infos": {
            "pid-1": {
                "type": "pic",
                "largest": {"url": "https://wx1.sinaimg.cn/large/pid-1.jpg"},
            }
        },
        "user": {"id": 88},
    }
    payload.update(extra)
    return payload


def _user(**extra) -> dict:
    payload = {
        "id": 88,
        "screen_name": "海边",
        "followers_count": 10,
        "friends_count": 4,
        "statuses_count": 6,
        "description": "简介",
        "verified": True,
        "verified_type": 1,
        "verified_type_ext": 0,
        "verified_reason": "机构",
        "avatar_hd": "https://wx1.sinaimg.cn/avatar.jpg",
        "gender": "f",
        "location": "山东",
    }
    payload.update(extra)
    return payload


def _record(status=None, user=None) -> dict:
    parsed = weibo_store.parse_weibo_detail(
        status if status is not None else _status(),
        user if user is not None else _user(),
        mblogid="Abc12",
        source_keyword="青岛",
        current_timestamp=lambda: 0,
        visit_refs={"ledger_id": "L", "images": []},
    )
    assert parsed.record is not None
    return parsed.record


def test_long_text_content_wins_over_text_raw():
    status = _status(longText={"content": "青岛长文全文"}, text_raw="青岛短", text="<b>html</b>")
    assert weibo_store.weibo_body_text(status) == "青岛长文全文"
    assert "html" not in _record(status)["content"]


def test_multi_image_post_without_long_text_uses_text_raw():
    status = _status(isLongText=True, text="<span>不要</span>")
    status.pop("longText", None)
    assert weibo_store.weibo_body_text(status) == "青岛栈桥"
    assert _record(status)["content"] == "青岛栈桥"
    assert _record(status)["content_detail_source"] == "desktop_detail"
    assert _record(status)["content_detail_status"] == "detail_observed"


def test_empty_body_is_a_skip():
    status = _status(text_raw="  ", longText={"content": ""}, text="<b>x</b>")
    parsed = weibo_store.parse_weibo_detail(
        status, _user(), mblogid="Abc12", source_keyword="青岛", current_timestamp=lambda: 0,
    )
    assert parsed.record is None
    assert parsed.skip_code == "detail_content_empty"


def test_missing_metric_is_none_and_zero_is_kept():
    status = _status()
    del status["attitudes_count"]
    status["comments_count"] = 0
    record = _record(status)
    assert record["liked_count"] is None
    assert record["comments_count"] == 0
    assert record["shared_count"] == 3


def test_video_without_pic_infos_is_skipped():
    status = _status(page_info={"object_type": "video"})
    del status["pic_infos"]
    del status["pic_ids"]
    parsed = weibo_store.parse_weibo_detail(
        status, _user(), mblogid="Abc12", source_keyword="青岛", current_timestamp=lambda: 0,
    )
    assert parsed.video is True
    assert parsed.record is None


def test_video_with_pic_infos_is_not_a_video_skip():
    status = _status(page_info={"object_type": "video"})
    assert weibo_store.is_weibo_video(status) is False


def test_repost_does_not_merge_original_text_or_pictures():
    status = _status(
        text_raw="青岛转发语",
        retweeted_status={
            "text_raw": "原帖正文",
            "pic_ids": ["pid-rt"],
            "pic_infos": {
                "pid-rt": {"type": "pic", "largest": {"url": "https://wx1.sinaimg.cn/large/rt.jpg"}},
            },
        },
    )
    record = _record(status)
    assert record["content"] == "青岛转发语"
    assert record["image_list"] == ["https://wx1.sinaimg.cn/large/pid-1.jpg"]


def test_created_at_timestamp_and_shanghai_datetime_agree():
    created = weibo_store.weibo_created_at({"created_at": "Sat Jun 14 12:00:00 +0800 2025"})
    assert created == (1749873600, "2025-06-14 12:00:00+08:00")
    from datetime import datetime

    assert datetime.fromisoformat(created[1]).timestamp() == created[0]


@pytest.mark.parametrize("value", [None, "", "not a date", 1749873600, "2025-06-14 12:00:00"])
def test_unparseable_created_at_is_not_projected(value):
    parsed = weibo_store.parse_weibo_detail(
        _status(created_at=value), _user(), mblogid="Abc12",
        source_keyword="青岛", current_timestamp=lambda: 0,
    )
    assert parsed.record is None
    assert parsed.skip_code == "created_at_unparseable"


def test_author_fields_and_verify_blob_omit_absent_keys():
    user = _user()
    del user["verified_type_ext"]
    record = _record(user=user)
    assert record["author_platform_id"] == "88"
    assert record["nickname"] == "海边"
    assert record["followers_count"] == 10
    assert record["followers_observed"] is True
    assert record["author_followers_source"] == "detail_side_user"
    assert record["author_following_count"] == 4
    assert record["author_posts_count"] == 6
    assert record["author_description"] == "简介"
    assert record["author_profile_url"] == "https://weibo.com/u/88"
    assert record["note_url"] == "https://weibo.com/88/Abc12"
    assert record["mblogid"] == "Abc12"
    assert record["author_verified"] == 1
    assert record["author_verified_text"] == "机构认证、机构"
    assert record["author_json"]["weibo_verify"] == {
        "verified": True,
        "verified_type": 1,
        "verified_reason": "机构",
    }
    dumped = json.dumps(record)
    assert "avatar" not in dumped
    assert "gender" not in dumped
    assert "山东" not in dumped


def test_wan_follower_string_is_not_converted():
    user = _user()
    del user["followers_count"]
    user["followers_count_str"] = "32.2万"
    record = _record(user=user)
    assert record["followers_count"] is None
    assert record["followers_observed"] is False

    digits = _user()
    del digits["followers_count"]
    digits["followers_count_str"] = "42"
    assert _record(user=digits)["followers_count"] == 42


def test_zero_followers_stay_observed():
    record = _record(user=_user(followers_count=0))
    assert record["followers_count"] == 0
    assert record["followers_observed"] is True


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        # #213：0 为个人，文字取认证说明。
        ({"verified": True, "verified_type": 0, "verified_reason": " 本地资讯博主（青岛） "}, (1, "本地资讯博主（青岛）")),
        ({"verified": True, "verified_type": 0, "verified_reason": ""}, (1, None)),
        # 0 与 -1 以外一律记为机构，不按类型码细分，未见过的值也算机构。
        ({"verified": True, "verified_type": 1, "verified_reason": "青岛市文化和旅游局"}, (1, "机构认证、青岛市文化和旅游局")),
        ({"verified": True, "verified_type": 3, "verified_type_ext": 53, "verified_reason": "媒体"}, (1, "机构认证、媒体")),
        ({"verified": True, "verified_type": 7, "verified_reason": "   "}, (1, "机构认证")),
        ({"verified": True, "verified_type": 6}, (1, "机构认证")),
        ({"verified": True, "verified_type": 200, "verified_reason": "达人"}, (1, "机构认证、达人")),
        # 类型码缺失或不是整数时不判定机构，只写说明。
        ({"verified": True, "verified_reason": "说明"}, (1, "说明")),
        ({"verified": True, "verified_type": "1", "verified_reason": "说明"}, (1, "说明")),
        ({"verified": True, "verified_type": True, "verified_reason": "说明"}, (1, "说明")),
        ({"verified": True, "verified_type": -1, "verified_reason": "说明"}, (1, "说明")),
        # 未认证为 0；verified 缺失或不是布尔值时不推断。
        ({"verified": False, "verified_type": -1, "verified_reason": ""}, (0, None)),
        ({"verified": False, "verified_type": 1, "verified_reason": "机构"}, (0, None)),
        ({"verified_type": 1, "verified_reason": "机构"}, (None, None)),
        ({"verified": 1, "verified_type": 1, "verified_reason": "机构"}, (None, None)),
        ({"verified": "true", "verified_type": 1}, (None, None)),
    ],
)
def test_author_verification_rule(fields, expected):
    user = _user()
    for key in ("verified", "verified_type", "verified_type_ext", "verified_reason"):
        user.pop(key)
    user.update(fields)
    assert weibo_store.weibo_author_verification(user) == expected
    record = _record(user=user)
    assert (record["author_verified"], record["author_verified_text"]) == expected


def test_image_spec_order_skips_unknown_types_and_missing_pids():
    status = _status(
        pic_ids=["a", "b", "c", "d"],
        pic_infos={
            "a": {
                "type": "gif",
                "thumbnail": {"url": "https://wx1.sinaimg.cn/thumb/a.jpg"},
                "mw2000": {"url": "https://wx1.sinaimg.cn/mw2000/a.jpg"},
                "largest": {"url": "https://wx1.sinaimg.cn/large/a.jpg"},
            },
            "b": {"type": "livephoto", "original": {"url": "https://wx2.sinaimg.cn/orj1080/b.jpg"}},
            "c": {"type": "video", "largest": {"url": "https://wx3.sinaimg.cn/large/c.jpg"}},
            "d": {"type": "pic", "large": {"url": "https://wx4.sinaimg.cn/orj960/d.jpg"}},
        },
    )
    assets = weibo_store.weibo_image_assets(status)
    assert [asset["pid"] for asset in assets] == ["a", "b"]
    assert [asset["source_index"] for asset in assets] == [0, 1]
    assert [asset["source_asset_key"] for asset in assets] == ["weibo:pid:a", "weibo:pid:b"]
    assert assets[0]["urls"] == [
        "https://wx1.sinaimg.cn/large/a.jpg",
        "https://wx1.sinaimg.cn/mw2000/a.jpg",
    ]
    assert assets[0]["url"] == assets[0]["urls"][0]
    assert assets[1]["urls"] == ["https://wx2.sinaimg.cn/orj1080/b.jpg"]


def test_projected_record_passes_formal_validation_and_keeps_verify_blob():
    record = _record()
    validation = validate_formal_record(
        "weibo", record, set(), content_image_candidates=content_image_candidates,
    )
    assert validation["valid"] is True, validation["reasons"]
    row = row_for_record(
        "weibo",
        record,
        artifact_dir="artifacts",
        captured_at="2026-11-19T00:00:00+08:00",
        keyword="青岛",
        image_items_for_record=image_items_for_record,
        ImagePersistenceError=RuntimeError,
    )
    author = json.loads(row["author_json"])
    assert author["weibo_verify"]["verified_type"] == 1
    assert row["author_following_count"] == 4
    assert row["author_posts_count"] == 6
    assert row["author_description"] == "简介"
    assert row["author_verified"] == 1
    assert row["author_verified_text"] == "机构认证、机构"
    assert row["author_profile_url"] == "https://weibo.com/u/88"
    assert row["canonical_url"] == "https://weibo.com/88/Abc12"


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
    from dataclasses import replace
    crawler.source_keyword = "青岛"
    crawler.ports = replace(crawler.ports, store_factory=lambda: FakeStore())
    await crawler.update_weibo_note({"status": _status(), "side_user": _user(), "mblogid": "Abc12"})

    assert captured["image_list"] == ["https://wx1.sinaimg.cn/large/pid-1.jpg"]
    assert captured["image_list_source"] == "pic_infos"
    assert captured["content_detail_source"] == "desktop_detail"
    assert captured["note_url"] == "https://weibo.com/88/Abc12"
    assert "avatar" not in json.dumps(captured)


def _user_without_verify_keys() -> dict:
    user = _user()
    for key in ("verified", "verified_type", "verified_type_ext", "verified_reason"):
        user.pop(key, None)
    return user


def test_unverified_false_still_stores_verify_blob():
    """未认证且带 verified=false 时，原值非空，列和 raw_sample_json 都有 weibo_verify。"""

    user = _user_without_verify_keys()
    user.update({"verified": False, "verified_type": -1, "verified_reason": ""})
    assert weibo_store.weibo_verify_of(user) == {
        "verified": False,
        "verified_type": -1,
        "verified_reason": "",
    }
    record = _record(user=user)
    assert record["author_verified"] == 0
    assert record["author_verified_text"] is None
    assert record["author_json"]["weibo_verify"]["verified"] is False
    row = row_for_record(
        "weibo",
        record,
        artifact_dir="artifacts",
        captured_at="2026-11-19T00:00:00+08:00",
        keyword="青岛",
        image_items_for_record=image_items_for_record,
        ImagePersistenceError=RuntimeError,
    )
    assert json.loads(row["author_json"])["weibo_verify"]["verified"] is False
    raw = json.loads(row["raw_sample_json"])
    assert raw["author_json"]["weibo_verify"]["verified"] is False


def test_side_user_without_verify_keys_still_writes_empty_marker():
    """认证键全缺时 weibo_verify_of 为 {}。投影成功仍要留下空对象，读失败则没有记录。"""

    user = _user_without_verify_keys()
    assert weibo_store.weibo_verify_of(user) == {}
    assert weibo_store.weibo_verify_of({"verified": None, "verified_reason": None}) == {}
    record = _record(user=user)
    assert record["author_verified"] is None
    assert record["author_json"]["weibo_verify"] == {}
    row = row_for_record(
        "weibo",
        record,
        artifact_dir="artifacts",
        captured_at="2026-11-19T00:00:00+08:00",
        keyword="青岛",
        image_items_for_record=image_items_for_record,
        ImagePersistenceError=RuntimeError,
    )
    assert json.loads(row["author_json"])["weibo_verify"] == {}
    raw = json.loads(row["raw_sample_json"])
    assert raw["author_json"]["weibo_verify"] == {}
    skipped = weibo_store.parse_weibo_detail(
        _status(text_raw="  ", longText={"content": ""}),
        user,
        mblogid="Abc12",
        source_keyword="青岛",
        current_timestamp=lambda: 0,
    )
    assert skipped.record is None
