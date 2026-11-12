"""#167：小红书作者认证两列取自作者主页投影的官方认证类型码与职业标签（操作人 2026-11-12 确认）。

类型码语义取自小红书网页前端枚举（RedOfficial=1、BlueOfficial=2，>0 显示认证图标）；网页不显示认证
文字，所以认证文字写枚举名原文。职业标签（tags[].tagType=profession，如“美食博主”）也按认证记。
"""

from __future__ import annotations

from typing import Any

import pytest

from trippostcollect.db.content import row_for_record
from trippostcollect.platforms.xhs import parser


def _note(creator_profile: dict[str, Any] | None) -> dict[str, Any]:
    note: dict[str, Any] = {
        "note_id": "64b95d01000000000c034587",
        "type": "normal",
        "title": "青岛海边",
        "desc": "青岛海边正文",
        "time": 1_700_000_000_000,
        "xsec_token": "token",
        "user": {"user_id": "5f0000000000000001000001", "nickname": "海边的猫"},
        "interact_info": {},
        "image_list": [],
    }
    if creator_profile is not None:
        note["creator_profile"] = creator_profile
    return note


def _record(creator_profile: dict[str, Any] | None) -> dict[str, Any]:
    return parser.update_xhs_note(
        _note(creator_profile), source_keyword="青岛旅游", current_timestamp=lambda: 1, save_data_option="jsonl",
    )


def _row(record: dict[str, Any]) -> dict[str, Any]:
    return row_for_record(
        "xhs", record, artifact_dir="/tmp/artifact",
        captured_at="2026-11-12T00:00:00+08:00", keyword="青岛旅游",
    )


PROFESSION_TAGS = [
    {"name": "26岁", "tagType": "info"},
    {"name": "山东青岛", "tagType": "location"},
    {"name": "美食博主", "tagType": "profession"},
    {"name": "旅行博主", "tagType": "profession"},
]


@pytest.mark.parametrize(
    "creator_profile, verified, text",
    [
        # 形态取自 #167 实测：薯队长类型码 1；蓝 V 账号类型码 2；公主4个胃没有 verifyInfo、有两个职业标签。
        pytest.param({"verifyInfo": {"redOfficialVerifyType": 1}, "tags": [{"name": "上海", "tagType": "location"}]},
                     1, "RedOfficial", id="red_official"),
        pytest.param({"verifyInfo": {"redOfficialVerifyType": 2}}, 1, "BlueOfficial", id="blue_official"),
        pytest.param({"basicInfo": {"nickname": "公主4个胃"}, "tags": PROFESSION_TAGS},
                     1, "美食博主、旅行博主", id="profession_only"),
        pytest.param({"verifyInfo": {"redOfficialVerifyType": 2}, "tags": PROFESSION_TAGS},
                     1, "BlueOfficial；美食博主、旅行博主", id="official_and_profession"),
        pytest.param({"verifyInfo": {"redOfficialVerifyType": 0}, "tags": PROFESSION_TAGS[:2]},
                     0, None, id="type_zero"),
        pytest.param({"basicInfo": {"nickname": "momo"}, "tags": [{"name": "23岁", "tagType": "info"}]},
                     0, None, id="no_verify_info"),
        pytest.param({"verifyInfo": {}}, 0, None, id="empty_verify_info"),
        pytest.param({"verifyInfo": {"redOfficialVerifyType": 7}}, 1, None, id="unregistered_positive_code"),
        pytest.param({"verifyInfo": {"redOfficialVerifyType": "2"}}, None, None, id="string_code_unknown"),
        pytest.param({"verifyInfo": {"redOfficialVerifyType": True}}, None, None, id="bool_code_unknown"),
        pytest.param({"verifyInfo": {"redOfficialVerifyType": -1}}, None, None, id="negative_code_unknown"),
        pytest.param({"verifyInfo": "bad"}, None, None, id="verify_info_not_object"),
        pytest.param({"verifyInfo": {"redOfficialVerifyType": "x"}, "tags": PROFESSION_TAGS},
                     1, "美食博主、旅行博主", id="unknown_code_with_profession"),
        pytest.param(None, None, None, id="no_creator_profile"),
    ],
)
def test_verified_columns_follow_official_code_and_profession_tags(creator_profile, verified, text) -> None:
    record = _record(creator_profile)

    assert record.get("author_verified") == verified
    assert record.get("author_verified_text") == text
    row = _row(record)
    assert row["author_verified"] == verified
    assert row["author_verified_text"] == text


def test_profession_tag_names_are_stripped_and_deduplicated() -> None:
    tags = [
        {"name": " 美食博主 ", "tagType": "profession"},
        {"name": "美食博主", "tagType": "profession"},
        {"name": "", "tagType": "profession"},
        {"name": None, "tagType": "profession"},
        {"tagType": "profession"},
        "not-a-tag",
        {"name": "摄影博主", "tagType": "profession"},
    ]
    record = _record({"verifyInfo": {"redOfficialVerifyType": 0}, "tags": tags})

    assert record["author_verified"] == 1
    assert record["author_verified_text"] == "美食博主、摄影博主"


def test_no_creator_profile_writes_no_verified_keys() -> None:
    record = _record(None)

    assert "author_verified" not in record
    assert "author_verified_text" not in record
