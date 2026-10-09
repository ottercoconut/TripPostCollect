"""#49 作者原始身份：四站根解析产物到正式记录保存平台原始用户 ID 与昵称。

作者平台 ID 统一为字符串（整数 ID 转字符串、去首尾空白），昵称按原文保存；头像仍在首次
序列化前清除，不写入正式记录。B站解析本来就保存原始 mid/作者名，不在此重复。
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from trippostcollect.application.collection import validate_formal_record
from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.db.content import row_for_record, upsert_web_post
from trippostcollect.platforms.douyin import parser as douyin_parser
from trippostcollect.platforms.weibo import parser as weibo_parser
from trippostcollect.platforms.xhs import core as xhs_core
from trippostcollect.platforms.xhs import parser as xhs_parser
from trippostcollect.platforms.zhihu.parser import ZhihuExtractor, update_zhihu_content
from trippostcollect.records.formal import merge_repair_fallback_metadata
from trippostcollect.records.identity import platform_nickname, platform_user_id


AVATAR = "https://avatar.invalid/never.jpg"


def _clock() -> int:
    return 1_700_000_000_000


def weibo_record() -> dict:
    note = {
        "mblog": {
            "id": "5100000000000001",
            "text": "青岛栈桥正文",
            "created_at": "Sat Jun 14 12:00:00 +0800 2025",
            "user": {
                "id": 7654321,
                "screen_name": "青岛 旅行者",
                "followers_count": 10,
                "avatar_hd": AVATAR,
            },
        }
    }
    return weibo_parser.update_weibo_note(note, source_keyword="青岛旅游", current_timestamp=_clock)


def douyin_record() -> dict:
    aweme = {
        "aweme_id": "7234567890123456",
        "aweme_type": 68,
        "desc": "青岛崂山正文",
        "create_time": 1_700_000_000,
        "author": {
            "uid": " 9876543210 ",
            "sec_uid": "MS4wLjABAAAA-sec",
            "nickname": "崂山 小王",
            "follower_count": 12,
            "avatar_thumb": {"url_list": [AVATAR]},
        },
        "statistics": {},
    }
    return douyin_parser.update_douyin_aweme(
        aweme, source_keyword="青岛旅游", current_timestamp=_clock,
        save_data_option="jsonl", enable_get_medias=True,
    )


ZHIHU_AUTHOR_ID = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"
ZHIHU_URL_TOKEN = "qingdao-traveler"


def zhihu_record() -> dict:
    answer = {
        "id": "100200300",
        "type": "answer",
        "question": {"id": "10"},
        "content": "<p>青岛八大关正文</p>",
        "created_time": 1_700_000_000,
        "author": {
            "id": ZHIHU_AUTHOR_ID,
            "url_token": ZHIHU_URL_TOKEN,
            "name": "八大关的风",
            "follower_count": 3,
            "avatar_url": AVATAR,
        },
    }
    (content,) = ZhihuExtractor().extract_contents_from_search(
        {"data": [{"type": "search_result", "object": answer}]}
    )
    return update_zhihu_content(content, source_keyword="青岛旅游")


def xhs_note() -> dict:
    return {
        "note_id": "64b95d01000000000c034587",
        "type": "normal",
        "title": "青岛海边",
        "desc": "青岛海边正文",
        "time": 1_700_000_000_000,
        "xsec_token": "token",
        "user": {"user_id": "5f0000000000000001000001", "nickname": "海边 的猫", "avatar": AVATAR},
        "creator_profile": {"fans_count": 10},
        "interact_info": {},
        "image_list": [],
    }


def xhs_record() -> dict:
    return xhs_parser.update_xhs_note(
        xhs_note(), source_keyword="青岛旅游", current_timestamp=_clock, save_data_option="jsonl",
    )


CASES = {
    "weibo": (weibo_record, "7654321", "青岛 旅行者"),
    "douyin": (douyin_record, "9876543210", "崂山 小王"),
    "zhihu": (zhihu_record, ZHIHU_AUTHOR_ID, "八大关的风"),
    "xhs": (xhs_record, "5f0000000000000001000001", "海边 的猫"),
}


def test_identity_normalization_keeps_raw_values() -> None:
    assert platform_user_id(7654321) == "7654321"
    assert platform_user_id(" abc ") == "abc"
    assert platform_user_id(None) == ""
    assert platform_nickname(" 张三 ") == " 张三 "
    assert platform_nickname("") == ""
    assert platform_nickname(None) == ""


@pytest.mark.parametrize("platform_key", sorted(CASES))
def test_parser_to_formal_row_keeps_raw_author_identity(platform_key: str) -> None:
    build, author_id, nickname = CASES[platform_key]
    record = build()
    assert record["creator_hash"] == author_id
    assert record.get("nickname", record.get("user_nickname")) == nickname

    row = row_for_record(
        platform_key, record, artifact_dir="/tmp/artifact",
        captured_at="2026-10-08T00:00:00+08:00", keyword="青岛旅游",
    )
    assert row["author_platform_id"] == author_id
    assert row["author_display_name"] == nickname
    # 头像在正式记录的任何列（含 author_json/raw_sample_json）中都不出现。
    assert AVATAR not in json.dumps(row, ensure_ascii=False, default=str)


def test_zhihu_platform_id_is_raw_id_and_profile_uses_url_token() -> None:
    record = zhihu_record()
    assert record["creator_url_token"] == ZHIHU_URL_TOKEN
    row = row_for_record(
        "zhihu", record, artifact_dir="/tmp/artifact",
        captured_at="2026-10-08T00:00:00+08:00", keyword="青岛旅游",
    )
    assert row["author_platform_id"] == ZHIHU_AUTHOR_ID
    assert row["author_profile_url"] == f"https://www.zhihu.com/people/{ZHIHU_URL_TOKEN}"


@pytest.mark.asyncio
@pytest.mark.parametrize("keep_author_detail", [None, "0", "1"])
async def test_xhs_store_writes_raw_identity_regardless_of_legacy_switch(monkeypatch, keep_author_detail) -> None:
    """旧 TRIPPOSTCOLLECT_XHS_KEEP_AUTHOR_DETAIL 只控制 user_id/昵称；根实现不再读取，恒为原值。"""
    if keep_author_detail is None:
        monkeypatch.delenv("TRIPPOSTCOLLECT_XHS_KEEP_AUTHOR_DETAIL", raising=False)
    else:
        monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_KEEP_AUTHOR_DETAIL", keep_author_detail)
    captured: dict = {}

    class Store:
        async def store_content(self, content_item):
            captured.update(content_item)

    await xhs_core.store_xhs_note(
        xhs_note(), source_keyword="青岛旅游", crawler_type="search", save_data_option="jsonl",
        current_timestamp=_clock, content_sink_factory=lambda _crawler_type: Store(),
    )
    assert captured["creator_hash"] == captured["user_id"] == "5f0000000000000001000001"
    assert captured["nickname"] == "海边 的猫"
    assert captured["author_profile_url"].startswith(
        "https://www.xiaohongshu.com/user/profile/5f0000000000000001000001?"
    )


# #49 之前微博作者 ID 存为 sha256 截断 16 位、昵称首尾留字脱敏；历史行修复见 #50。
LEGACY_HASH = "0123456789abcdef"
LEGACY_MASKED = "青***者"


def _weibo_row(record: dict) -> dict:
    return row_for_record(
        "weibo", record, artifact_dir="/tmp/artifact",
        captured_at="2026-10-08T00:00:00+08:00", keyword="青岛旅游",
    )


def test_reimport_of_same_post_replaces_legacy_hashed_author(tmp_path) -> None:
    """同帖再次导入时按 platform_post_id 整行更新：旧哈希/脱敏值被原始值替换，不新增行。

    正式抓取在详情前跳过库中已知帖子，日常抓取不会走到这里；只有详情修复流程或 #50 的
    数据修复会对历史行再次导入。
    """
    legacy = dict(weibo_record(), creator_hash=LEGACY_HASH, nickname=LEGACY_MASKED)
    with sqlite3.connect(":memory:") as conn:
        bootstrap_connection(conn, sync_jobs=False)
        post_id, inserted = upsert_web_post(conn, _weibo_row(legacy), project_root=tmp_path,
                                            media_root=tmp_path / "data/media")
        assert inserted is True
        updated_id, inserted = upsert_web_post(conn, _weibo_row(weibo_record()), project_root=tmp_path,
                                               media_root=tmp_path / "data/media")
        rows = conn.execute(
            "SELECT id, author_platform_id, author_display_name, author_json FROM web_posts"
        ).fetchall()
    assert (updated_id, inserted) == (post_id, False)
    assert [row[:3] for row in rows] == [(post_id, "7654321", "青岛 旅行者")]
    assert json.loads(rows[0][3])["creator_hash"] == "7654321"


def test_repair_fallback_never_replaces_raw_identity_with_legacy_values() -> None:
    """修复回填只补空值：本轮已有原始 ID/昵称时保留本轮值；本轮缺失时才沿用旧行（含旧哈希）。"""
    fallback = {
        "creator_hash": LEGACY_HASH, "author_platform_id": LEGACY_HASH,
        "user_nickname": LEGACY_MASKED, "author_display_name": LEGACY_MASKED,
    }
    current = zhihu_record()
    merged = merge_repair_fallback_metadata("zhihu", dict(current), fallback)
    assert (merged["creator_hash"], merged["user_nickname"]) == (ZHIHU_AUTHOR_ID, "八大关的风")
    row = row_for_record("zhihu", merged, artifact_dir="/tmp/artifact",
                         captured_at="2026-10-08T00:00:00+08:00", keyword="青岛旅游")
    assert (row["author_platform_id"], row["author_display_name"]) == (ZHIHU_AUTHOR_ID, "八大关的风")

    missing = dict(current, creator_hash="", user_nickname="")
    merged = merge_repair_fallback_metadata("zhihu", missing, fallback)
    assert (merged["creator_hash"], merged["user_nickname"]) == (LEGACY_HASH, LEGACY_MASKED)


LEGACY_IDENTITY_NAMES = ("anonymize_user_id", "mask_nickname")
# 迁移台账规则以冻结账的（文件, 限定名）字符串为键登记这两个旧定义的 T14 退出；台账键必须与冻结行逐字一致，
# 只能是字符串字面量。该文件只豁免字符串字面量，任何代码形式的引用仍判命中。
LEDGER_RULES = "scripts/dev/adapter_ledger_rules.py"


def _legacy_identity_code_references(source: str) -> bool:
    """去掉普通字符串字面量后按子串判定：名字、属性、导入、注释与 f-string 内容仍计为命中。"""
    import io
    import tokenize

    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    code = " ".join(token.string for token in tokens if token.type != tokenize.STRING)
    return any(name in code for name in LEGACY_IDENTITY_NAMES)


def test_root_package_no_longer_calls_legacy_identity_transforms() -> None:
    """哈希/脱敏函数只为 fork 旧导入出口与冻结旧投影测试保留到 T14；根包与 scripts 不得再引用。"""
    root = Path(__file__).resolve().parents[1]
    identity = root / "src" / "trippostcollect" / "records" / "identity.py"
    hits = []
    for base in (root / "src" / "trippostcollect", root / "scripts"):
        for path in base.rglob("*.py"):
            relative = path.relative_to(root).as_posix()
            source = path.read_text(encoding="utf-8")
            if path == identity:
                continue
            if relative == LEDGER_RULES:
                if _legacy_identity_code_references(source):
                    hits.append(relative)
            elif any(name in source for name in LEGACY_IDENTITY_NAMES):
                hits.append(relative)
    assert hits == []


def test_ledger_rules_exemption_covers_only_string_literals() -> None:
    assert not _legacy_identity_code_references('KEY = ("tools/x.py", "anonymize_user_id")\n')
    assert _legacy_identity_code_references("from trippostcollect.records.identity import mask_nickname\n")
    assert _legacy_identity_code_references("value = identity.anonymize_user_id(raw)\n")
    assert _legacy_identity_code_references("# 调用 mask_nickname\nvalue = 1\n")


@pytest.mark.parametrize("missing_name", [None, ""])
def test_missing_nickname_is_rejected_as_missing_author_name(missing_name) -> None:
    """旧脱敏把空昵称变成 "*" 可通过门禁；现在缺昵称如实为空串，按正式契约判 missing_author_name。"""
    note = {
        "mblog": {
            "id": "5100000000000002",
            "text": "青岛正文",
            "created_at": "Sat Jun 14 12:00:00 +0800 2025",
            "user": {"id": 7654321, "screen_name": missing_name, "followers_count": 10},
        }
    }
    if missing_name is None:
        del note["mblog"]["user"]["screen_name"]
    record = weibo_parser.update_weibo_note(note, source_keyword="青岛旅游", current_timestamp=_clock)
    assert (record["creator_hash"], record["nickname"]) == ("7654321", "")
    reasons = validate_formal_record("weibo", record, set())["reasons"]
    assert "missing_author_name" in reasons
    assert "missing_author_id" not in reasons

    present = validate_formal_record("weibo", weibo_record(), set())["reasons"]
    assert "missing_author_name" not in present
