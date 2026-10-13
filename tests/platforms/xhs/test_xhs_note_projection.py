"""repair 页面状态投影（设计 §9.3）：camelCase → 与 feed note_card 同形、两种来源共用的形状不变式、白名单。"""

from __future__ import annotations

import json

import pytest

from trippostcollect.platforms.xhs.overlay import note_card_failure
from trippostcollect.platforms.xhs.page_scripts import STATE_SCRIPTS
from trippostcollect.platforms.xhs.parser import (
    note_card_problems,
    project_note_state,
    read_note_state,
    xhs_note_state_spec,
)

IMAGE_URL = "https://sns-webpic-qc.xhscdn.com/202601/abc/notes_pre_post/1040g-0!nd_dft_wlteh_webp_3"

# 同一篇合成笔记的两种来源：feed 的 note_card（snake_case）与整页的页面状态 note（camelCase）。
FEED_NOTE_CARD = {
    "note_id": "note-a",
    "type": "normal",
    "title": "青岛 栈桥",
    "desc": "青岛 正文",
    "time": 1_760_000_000_000,
    "last_update_time": 1_760_000_100_000,
    "ip_location": "山东",
    "user": {"user_id": "author-a", "nickname": "作者"},
    "interact_info": {"liked_count": "12", "collected_count": "3", "comment_count": "4", "share_count": "1万"},
    "image_list": [
        {"url_default": IMAGE_URL, "url": "", "url_pre": "https://pre.example/a", "width": 1080, "height": 1440,
         "live_photo": False},
    ],
    "tag_list": [{"id": "t1", "name": "青岛", "type": "topic"}],
}
STATE_NOTE = {
    "noteId": "note-a",
    "type": "normal",
    "title": "青岛 栈桥",
    "desc": "青岛 正文",
    "time": 1_760_000_000_000,
    "lastUpdateTime": 1_760_000_100_000,
    "ipLocation": "山东",
    "xsecToken": "state-token",
    "user": {"userId": "author-a", "nickname": "作者", "avatar": "https://sns-avatar-qc.xhscdn.com/a.jpg",
             "xsecToken": "user-token"},
    "interactInfo": {"likedCount": "12", "collectedCount": "3", "commentCount": "4", "shareCount": "1万",
                     "liked": True, "collected": False, "followed": True, "relation": "fans", "niceCount": 2},
    "imageList": [
        {"urlDefault": IMAGE_URL, "url": "", "urlPre": "https://pre.example/a", "width": 1080, "height": 1440,
         "livePhoto": False, "stream": {"h264": []}, "infoList": [{"url": "x"}], "traceId": "t", "fileId": "f"},
    ],
    "tagList": [{"id": "t1", "name": "青岛", "type": "topic"}],
}


def test_state_projection_matches_feed_note_card_on_all_mapped_fields() -> None:
    assert project_note_state(STATE_NOTE) == FEED_NOTE_CARD


def test_projection_drops_avatar_tokens_interaction_state_and_streams() -> None:
    serialized = json.dumps(project_note_state(STATE_NOTE), ensure_ascii=False)
    for forbidden in ("avatar", "xsec", "token", "liked\"", "collected\"", "followed", "relation", "nice",
                      "stream", "info_list", "trace", "file_id"):
        assert forbidden not in serialized, forbidden
    spec = json.dumps(xhs_note_state_spec("note-a"))
    for forbidden in ("avatar", "xsecToken", "liked\"", "followed", "relation", "niceCount", "stream", "infoList",
                      "traceId", "fileId"):
        assert forbidden not in spec, forbidden


def test_state_script_reads_only_the_target_note_detail() -> None:
    script = STATE_SCRIPTS["note_detail_state"]
    assert "noteDetailMap" in script and "spec.note_id" in script
    assert "JSON.stringify" not in script and "innerHTML" not in script


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ({"status": "missing"}, "state_missing"),
        ({"status": "rejected"}, "state_read_error"),
        ({"status": "ok", "note": []}, "state_read_error"),
        ("not-a-dict", "state_read_error"),
    ],
)
def test_read_note_state_rejects_envelopes(raw, reason) -> None:
    assert read_note_state(raw) == (None, reason)


def test_read_note_state_projects_ok_envelope() -> None:
    assert read_note_state({"status": "ok", "note": STATE_NOTE}) == (FEED_NOTE_CARD, "ok")


def test_both_sources_pass_the_shared_shape_invariant() -> None:
    assert note_card_problems(FEED_NOTE_CARD, "note-a") == []
    assert note_card_problems(project_note_state(STATE_NOTE), "note-a") == []
    assert note_card_failure(FEED_NOTE_CARD, "note-a") == ""


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"note_id": "other"}, "note_id_mismatch"),
        ({"type": "multi"}, "type_invalid"),
        ({"time": 0}, "time_invalid"),
        ({"time": "1760000000000"}, "time_invalid"),
        ({"user": {"user_id": "", "nickname": "作者"}}, "user_id_missing"),
        ({"image_list": []}, "image_list_invalid"),
        ({"image_list": [{"url": IMAGE_URL}]}, "image_list_invalid"),
        ({"interact_info": {"liked_count": "1", "collected_count": "2", "comment_count": "3"}},
         "interact_counts_missing"),
    ],
)
def test_shape_invariant_reports_each_problem(change, problem) -> None:
    assert note_card_problems({**FEED_NOTE_CARD, **change}, "note-a") == [problem]


def test_failure_code_mapping_lets_videos_through_for_the_caller_to_skip() -> None:
    assert note_card_failure(None, "note-a") == "detail_parse_failed"
    assert note_card_failure({**FEED_NOTE_CARD, "note_id": "other"}, "note-a") == "note_id_mismatch"
    assert note_card_failure({**FEED_NOTE_CARD, "image_list": []}, "note-a") == "detail_parse_failed"
    # 视频笔记的形状问题不判失败，由调用方按视频跳过；ID 不符仍判失败。
    assert note_card_failure({**FEED_NOTE_CARD, "type": "video", "image_list": []}, "note-a") == ""
