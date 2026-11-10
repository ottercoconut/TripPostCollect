"""#163：抖音作者简介取自作者资料 user 节点的 signature；认证两列按主页“认证信息”的实测对照取值。"""

from __future__ import annotations

from typing import Any

import pytest

from trippostcollect.platforms.douyin import parser
from trippostcollect.records import formal

SEARCH_SIGNATURE = "搜索作者对象里的签名"
PROFILE_SIGNATURE = "青岛文旅官方账号\n带你看青岛"


def _aweme(profile_user: dict[str, Any] | None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "aweme_id": "7300000000000000001",
        "aweme_type": 68,
        "desc": "青岛栈桥的海鸥",
        "create_time": 1700000000,
        "author": {"uid": "123", "nickname": "青岛文旅", "signature": SEARCH_SIGNATURE},
        "statistics": {"digg_count": 1, "collect_count": 2, "comment_count": 3, "share_count": 4},
        "images": [{"uri": "img-1", "url_list": ["https://p3.douyinpic.com/img-1.jpeg"]}],
    }
    if profile_user is not None:
        item["creator_profile"] = {"status_code": 0, "user": profile_user}
    return item


def _record(item: dict[str, Any], save_data_option: str = "jsonl") -> dict[str, Any]:
    return parser.update_douyin_aweme(
        item,
        source_keyword="青岛旅游",
        current_timestamp=lambda: 1700000001,
        save_data_option=save_data_option,
        enable_get_medias=True,
        content_detail_source=parser.DOUYIN_SEARCH_CONTENT_SOURCE,
        expected_aweme_id=item["aweme_id"],
    )


def _row(record: dict[str, Any]) -> dict[str, Any]:
    return formal.row_for_record(
        "douyin",
        record,
        artifact_dir="outputs/test",
        captured_at="2026-11-10T00:00:00+08:00",
        keyword="青岛旅游",
        image_items_for_record=lambda platform, rec: [],
        ImagePersistenceError=RuntimeError,
    )


def test_profile_signature_becomes_author_description_verbatim() -> None:
    record = _record(_aweme({"follower_count": 10, "signature": PROFILE_SIGNATURE}))

    assert record["author_desc"] == PROFILE_SIGNATURE
    assert _row(record)["author_description"] == PROFILE_SIGNATURE


@pytest.mark.parametrize("profile_user", [None, {"follower_count": 10}, {"signature": ""}, {"signature": "  "}])
def test_missing_or_blank_profile_signature_leaves_description_empty(profile_user) -> None:
    # 搜索作者对象里的签名不作为简介来源，与 #116 粉丝量只认资料 user 节点一致。
    record = _record(_aweme(profile_user))

    assert "author_desc" not in record
    assert SEARCH_SIGNATURE not in str(record)
    assert _row(record)["author_description"] is None


def test_verify_candidates_are_kept_as_evidence() -> None:
    profile_user = {
        "follower_count": 10,
        "custom_verify": "",
        "enterprise_verify_reason": "青岛市文化和旅游局官方抖音号",
        "verification_type": 0,
        "account_cert_info": '{"label_style":5,"label_text":"青岛市文化和旅游局官方抖音号","is_biz_account":1}',
        "unique_id": "not-evidence",
    }
    record = _record(_aweme(profile_user))

    assert record["author_verify_evidence"] == {
        "custom_verify": "",
        "enterprise_verify_reason": "青岛市文化和旅游局官方抖音号",
        "verification_type": 0,
        "account_cert_info": '{"label_style":5,"label_text":"青岛市文化和旅游局官方抖音号","is_biz_account":1}',
    }
    assert '"author_verify_evidence"' in _row(record)["raw_sample_json"]


# 形态取自 #163 实测：蓝 V 的 label_text 与 enterprise_verify_reason 相同，verification_type 仍为 0；
# 只有 is_biz_account 的账号页面不显示认证。
@pytest.mark.parametrize(
    "profile_user, verified, text",
    [
        pytest.param(
            {"custom_verify": "", "enterprise_verify_reason": "青岛市文化和旅游局官方抖音号", "verification_type": 0,
             "account_cert_info": '{"label_style":5,"label_text":"青岛市文化和旅游局官方抖音号","is_biz_account":1}'},
            1, "青岛市文化和旅游局官方抖音号", id="blue_v_government",
        ),
        pytest.param(
            {"custom_verify": "", "enterprise_verify_reason": "商家认证账号",
             "account_cert_info": '{"label_style":5,"label_text":"商家认证账号","is_biz_account":1}'},
            1, "商家认证账号", id="blue_v_merchant",
        ),
        pytest.param(
            {"custom_verify": "", "enterprise_verify_reason": "", "account_cert_info": '{"is_biz_account":1}'},
            0, None, id="biz_account_without_label",
        ),
        pytest.param(
            {"custom_verify": "", "enterprise_verify_reason": "", "verification_type": 0, "account_cert_info": "{}"},
            0, None, id="unverified",
        ),
        pytest.param(
            {"custom_verify": "旅行领域创作者", "enterprise_verify_reason": "", "account_cert_info": "{}"},
            1, "旅行领域创作者", id="yellow_v_custom_verify",
        ),
        pytest.param(
            {"custom_verify": "", "enterprise_verify_reason": "某公司", "account_cert_info": "{}"},
            1, "某公司", id="enterprise_reason_without_label",
        ),
        pytest.param(
            {"custom_verify": "", "enterprise_verify_reason": ""},
            None, None, id="cert_info_missing",
        ),
        pytest.param(
            {"custom_verify": "", "enterprise_verify_reason": "", "account_cert_info": "not-json"},
            None, None, id="cert_info_unparseable",
        ),
        pytest.param(
            {"enterprise_verify_reason": "", "account_cert_info": "{}"},
            None, None, id="custom_verify_missing",
        ),
        pytest.param(None, None, None, id="no_creator_profile"),
    ],
)
def test_verified_columns_follow_profile_cert_label(profile_user, verified, text) -> None:
    record = _record(_aweme(None if profile_user is None else {"follower_count": 10, **profile_user}))

    assert record.get("author_verified") == verified
    assert record.get("author_verified_text") == text
    row = _row(record)
    assert row["author_verified"] == verified
    assert row["author_verified_text"] == text


def test_no_verify_candidates_writes_no_evidence_key() -> None:
    record = _record(_aweme({"follower_count": 10, "custom_verify": None}))

    assert "author_verify_evidence" not in record


def test_frozen_db_projection_does_not_gain_new_columns() -> None:
    record = _record(
        _aweme({"follower_count": 10, "signature": PROFILE_SIGNATURE, "custom_verify": "x"}),
        save_data_option="db",
    )

    assert "author_desc" not in record
    assert "author_verify_evidence" not in record
    assert "author_verified" not in record
    assert "author_verified_text" not in record
