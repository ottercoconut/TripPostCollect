"""#193：知乎认证只认作者页已通过的机构/个人身份。搜索 verify_bayes 不是认证。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.db.content import row_for_record, upsert_web_post
from trippostcollect.platforms.zhihu.models import ZhihuContent
from trippostcollect.platforms.zhihu.parser import (
    ZhihuExtractor,
    apply_author_page,
    merge_search_content_detail,
    update_zhihu_content,
    user_from_initial_data,
)

ICON_URL = "https://picx.zhimg.com/v2-icon.png"
AVATAR_URL = "https://picx.zhimg.com/avatar.jpg"
_MEDIA_MARKERS = ("icon", "url", "avatar", "image")


def _badge(kind: str, detail_type: str, title: str, description: str, *, status: str = "passed", **extra: object) -> dict:
    item = {
        "type": kind,
        "detailType": detail_type,
        "title": title,
        "description": description,
        "sources": [],
        "badgeStatus": status,
        "icon": ICON_URL,
        "nightIcon": "https://picx.zhimg.com/v2-night.png",
    }
    item.update(extra)
    return item


def _user(badge_v2: object | None = None, *, include_badge_v2: bool = True, **extra: object) -> dict:
    user: dict = {
        "followerCount": 11,
        "followingCount": 5,
        "answerCount": 2,
        "articlesCount": 3,
        "userType": "organization" if extra.get("isOrg") else "people",
        "isOrg": False,
        "vipInfo": {
            "isVip": True,
            "vipType": "vip",
            "vipIcon": {"url": "https://picx.zhimg.com/vip.png"},
        },
        "badge": [{"type": "identity", "description": "旧版认证", "avatar_url": AVATAR_URL}],
        "avatarUrl": AVATAR_URL,
    }
    user.update(extra)
    if include_badge_v2:
        user["badgeV2"] = badge_v2
    return user


def _badge_v2(*items: dict, title: str = "") -> dict:
    return {"title": title, "mergedBadges": [], "detailBadges": list(items)}


def _search_content(content_id: str = "100", **author_extra: object) -> ZhihuContent:
    author = {
        "id": "author-1",
        "url_token": "author",
        "name": "青岛作者",
        "follower_count": 3,
        "verify_bayes": "教育",
        "badge_v2": {"title": "优秀答主"},
    }
    author.update(author_extra)
    payload = {
        "data": [{
            "type": "search_result",
            "object": {
                "id": content_id,
                "type": "answer",
                "question": {"id": "10"},
                "created_time": 1_700_000_000,
                "content": "<p>青岛正文</p>",
                "author": author,
            },
        }]
    }
    (content,) = ZhihuExtractor().extract_contents_from_search(payload)
    return content


def _from_author_page(user: dict, *, content_id: str = "100") -> ZhihuContent:
    state = json.dumps({"initialState": {"entities": {"users": {"author": user}}}})
    parsed = user_from_initial_data(state, "author")
    assert parsed is not None
    search = _search_content(content_id)
    detail = ZhihuContent(content_id=content_id, content_type="answer", content_text="青岛正文")
    apply_author_page(detail, parsed, "author")
    return merge_search_content_detail(search, detail)


def _assert_no_media(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            folded = str(key).casefold()
            assert not any(marker in folded for marker in _MEDIA_MARKERS), key
            _assert_no_media(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_media(item)


def _row(content: ZhihuContent, tmp_path: Path) -> dict:
    record = update_zhihu_content(content, source_keyword="青岛旅游")
    return row_for_record(
        "zhihu",
        record,
        artifact_dir=str(tmp_path),
        captured_at="2026-11-14T00:00:00+08:00",
        keyword="青岛旅游",
    )


def test_search_verify_bayes_is_not_verification() -> None:
    content = _search_content()

    assert content.verified_text == ""
    assert content.author_verified is None
    assert content.author_page_badges == {}


def test_org_and_people_identity_orders_org_first_and_dedupes(tmp_path: Path) -> None:
    content = _from_author_page(_user(_badge_v2(
        _badge("identity", "identity_people", "已认证的个人", "新闻业 新媒体运营"),
        _badge("identity", "identity_org", "已认证机构号", "已认证机构号"),
        _badge("identity", "identity_people", "已认证的个人", "新闻业 新媒体运营"),
        _badge("identity", "identity_people", "已认证的个人", "哈尔滨工业大学 控制工程硕士"),
        _badge("best", "best_answerer", "优秀答主", "优秀答主", sources=[{
            "name": "青岛",
            "url": "https://www.zhihu.com/topic/1",
            "avatarUrl": AVATAR_URL,
        }]),
    )))

    assert content.author_verified == 1
    assert content.verified_text == "已认证机构号、新闻业 新媒体运营、哈尔滨工业大学 控制工程硕士"
    assert content.verified_text != "教育"
    badges = content.author_page_badges
    assert badges["userType"] == "people"
    assert badges["isOrg"] is False
    assert badges["vipInfo"] == {"isVip": True, "vipType": "vip"}
    assert badges["badge"] == [{"type": "identity", "description": "旧版认证"}]
    assert badges["badgeV2"]["detailBadges"][0]["description"] == "新闻业 新媒体运营"
    assert badges["badgeV2"]["detailBadges"][-1]["sources"] == [{"name": "青岛"}]
    assert "avatarUrl" not in badges
    _assert_no_media(badges)
    blob = json.dumps(badges, ensure_ascii=False)
    assert ICON_URL not in blob and AVATAR_URL not in blob
    row = _row(content, tmp_path)
    assert row["author_verified"] == 1
    assert row["author_verified_text"] == content.verified_text
    author = json.loads(row["author_json"])
    raw = json.loads(row["raw_sample_json"])
    assert "author_page_badges" not in author
    assert "badgeV2" not in author
    assert raw["author_page_badges"]["badgeV2"]["detailBadges"][1]["description"] == "已认证机构号"
    _assert_no_media(raw["author_page_badges"])


def test_people_only() -> None:
    content = _from_author_page(_user(_badge_v2(
        _badge("identity", "identity_people", "已认证的个人", "哈尔滨工业大学 控制工程硕士"),
    )))

    assert content.author_verified == 1
    assert content.verified_text == "哈尔滨工业大学 控制工程硕士"


def test_org_only() -> None:
    content = _from_author_page(_user(_badge_v2(
        _badge("identity", "identity_org", "已认证机构号", "已认证机构号"),
    ), isOrg=True))

    assert content.author_verified == 1
    assert content.verified_text == "已认证机构号"
    assert content.author_page_badges["isOrg"] is True
    assert content.author_page_badges["userType"] == "organization"


def test_honors_only_are_unverified_but_kept(tmp_path: Path) -> None:
    content = _from_author_page(_user(_badge_v2(
        _badge("best", "best_answerer", "优秀答主", "优秀答主"),
        _badge("reward", "zhihu_yearly_answerer", "年度新知答主", "2024 年度新知答主"),
        _badge("reward", "super_activity", "知势榜", "知势榜"),
    )))

    assert content.author_verified == 0
    assert content.verified_text == ""
    titles = [item["title"] for item in content.author_page_badges["badgeV2"]["detailBadges"]]
    assert titles == ["优秀答主", "年度新知答主", "知势榜"]
    row = _row(content, tmp_path)
    assert row["author_verified"] == 0
    assert row["author_verified_text"] is None


def test_empty_badge_v2_is_unverified() -> None:
    content = _from_author_page(_user(_badge_v2()))

    assert content.author_verified == 0
    assert content.verified_text == ""
    assert content.author_page_badges["badgeV2"]["detailBadges"] == []


def test_missing_or_malformed_badge_v2_is_unknown() -> None:
    missing = _from_author_page(_user(include_badge_v2=False))
    assert missing.author_verified is None
    assert missing.verified_text == ""
    assert "badgeV2" not in missing.author_page_badges
    assert missing.author_page_badges["userType"] == "people"

    not_dict = _from_author_page(_user("not-a-badge"))
    assert not_dict.author_verified is None
    assert not_dict.author_page_badges["badgeV2"] == "not-a-badge"

    missing_details = _from_author_page(_user({"title": "", "mergedBadges": []}))
    assert missing_details.author_verified is None
    assert missing_details.verified_text == ""

    wrong_details = _from_author_page(_user({"title": "", "detailBadges": {}}))
    assert wrong_details.author_verified is None


def test_badge_status_must_be_passed() -> None:
    content = _from_author_page(_user(_badge_v2(
        _badge("identity", "identity_people", "已认证的个人", "新闻业 新媒体运营", status="reviewing"),
        _badge("identity", "identity_org", "已认证机构号", "已认证机构号", status="passed"),
    )))

    assert content.author_verified == 1
    assert content.verified_text == "已认证机构号"

    rejected = _from_author_page(_user(_badge_v2(
        _badge("identity", "identity_people", "已认证的个人", "新闻业 新媒体运营", status="rejected"),
    )))
    assert rejected.author_verified == 0
    assert rejected.verified_text == ""


def test_description_that_repeats_a_stripped_url_is_not_kept() -> None:
    content = _from_author_page(_user(_badge_v2(
        _badge("identity", "identity_people", "已认证的个人", AVATAR_URL, avatar=AVATAR_URL),
    )))

    assert content.author_verified == 1
    assert content.verified_text == ""
    assert AVATAR_URL not in json.dumps(content.author_page_badges, ensure_ascii=False)
    _assert_no_media(content.author_page_badges)


def test_rows_persist_verified_columns(tmp_path: Path) -> None:
    cases = [
        ("101", _from_author_page(_user(_badge_v2(
            _badge("identity", "identity_people", "已认证的个人", "哈尔滨工业大学 控制工程硕士"),
        )), content_id="101"), 1, "哈尔滨工业大学 控制工程硕士"),
        ("102", _from_author_page(_user(_badge_v2()), content_id="102"), 0, None),
        ("103", _from_author_page(_user(include_badge_v2=False), content_id="103"), None, None),
    ]
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as connection:
        bootstrap_connection(connection, sync_jobs=False)
        for content_id, content, verified, text in cases:
            row = _row(content, tmp_path)
            assert row["platform_post_id"] == content_id
            assert row["author_verified"] == verified
            assert row["author_verified_text"] == text
            upsert_web_post(
                connection,
                row,
                project_root=tmp_path,
                media_root=tmp_path / "data" / "media",
            )
        connection.commit()
        stored = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT platform_post_id, author_verified, author_verified_text, author_json, raw_sample_json "
                "FROM web_posts ORDER BY platform_post_id"
            )
        }
    assert stored["101"][:2] == (1, "哈尔滨工业大学 控制工程硕士")
    assert stored["102"][:2] == (0, None)
    assert stored["103"][:2] == (None, None)
    for content_id, columns in stored.items():
        author = json.loads(columns[2])
        raw = json.loads(columns[3])
        assert "author_page_badges" not in author
        assert "author_page_badges" in raw
        assert raw["verified_text"] != "教育"
        if content_id != "103":
            assert "badgeV2" in raw["author_page_badges"]
        else:
            assert "badgeV2" not in raw["author_page_badges"]
        _assert_no_media(raw["author_page_badges"])
