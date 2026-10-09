# -*- coding: utf-8 -*-
"""
微博作者字段边界回归:作者平台 ID 与昵称保存原始值,头像等禁用字段仍不写出。

用例名沿用测试台账登记的 node ID(#49 起 note 用例语义为"保留原始身份、清除禁用字段")。

覆盖:
1. test_weibo_note_masks_user_info —— 用贴近真实微博结构的 mock note_item 喂根实现
   update_weibo_note,用 FakeStore 捕获拍平后的存储 dict,断言:
   - 不含任何禁用字段键(user_id/avatar/gender/profile_url/ip_location/desc ...)
   - creator_hash 为平台原始用户 ID(整数 ID 归一为字符串),nickname 为原始昵称
   - 头像、性别、主页、签名、IP 归属地的原始值不出现在任何存储值里
2. 评论属 T12 退出切片,根实现与正式路径不产出评论;原先只验证冻结旧评论投影哈希/脱敏行为的
   test_weibo_comment_masks_user_info 随旧身份函数在 T14-C 删除。
3. test_weibo_store_end_to_end_sqlite —— 端到端:把 note(根实现)产生的真实 dict
   按冻结旧 ORM 的列定义，用 SQLite 内存库走完整写入+查询。
   sqlite_roundtrip(..., "WeiboNote", captured_dict) 会因 旧模型声明字段对未知关键字的校验,
   在 dict 含已删列时直接抛 TypeError —— 以此证明 dict 的 key 与删列后的 ORM 完全对得上,
   且表中无禁用列、有 creator_hash。

说明:微博 note 的正文存于 content 字段,update_weibo_note 不产生 desc 键
(WeiboNote ORM 亦无 desc 列),故不存在用户 description 被持久化的风险。
"""
import asyncio

import pytest
from support.weibo_privacy import config, sqlite_roundtrip

# 原始(明文)测试数据
RAW_USER_ID = 7654321
RAW_NICKNAME = "微博达人"
NOTE_ID = "5123456789"
# 合法 RFC2822 时间串(weekday 与日期已对齐:2025-06-14 是周六)
RFC2822_TIME = "Sat Jun 14 12:00:00 +0800 2025"

# 禁用字段名(键)。作者平台 ID 只经 creator_hash 写出,昵称经 nickname 写出。
FORBIDDEN_KEYS = {
    "user_id", "sec_uid", "short_user_id", "user_unique_id",
    "avatar", "user_avatar", "face", "sign", "profile_url", "user_link",
    "ip_location", "ip_address", "gender", "sex", "desc",
}


# ----------------------------- mock 数据 -----------------------------

def make_mock_note() -> dict:
    """贴近真实 m.weibo.cn 接口结构的 mock note_item(含嵌套 user 信息)。"""
    return {
        "mblog": {
            "id": NOTE_ID,
            "text": "今天天气不错 <a href='#'>@好友</a> 出去玩",
            "created_at": RFC2822_TIME,
            "attitudes_count": 10,
            "comments_count": 2,
            "reposts_count": 1,
            "user": {
                "id": RAW_USER_ID,
                "screen_name": RAW_NICKNAME,
                "avatar_hd": "https://wx avatar.example.com/7654321.jpg",
                "gender": "f",
                "profile_url": "https://m.weibo.cn/profile/7654321",
                "description": "这是一个用户签名",
                "ip_location": "上海",
                "followers_count": 9999,
            },
        }
    }


# ----------------------------- FakeStore 捕获 -----------------------------

class _FakeStore:
    """捕获 store_content 收到的 dict,不触发任何真实存储。"""

    def __init__(self):
        self.captured_content = {}

    async def store_content(self, content_item):
        self.captured_content.update(content_item)


def _patch_factory(fake: "_FakeStore"):
    """把 store.weibo.WeibostoreFactory.create_store 替换为返回 fake 的静态方法,
    返回 (module, orig) 便于 finally 还原。"""
    from support.weibo_privacy import wb
    orig = wb.WeibostoreFactory.create_store
    wb.WeibostoreFactory.create_store = staticmethod(lambda: fake)
    return wb, orig


def _restore(wb, orig):
    wb.WeibostoreFactory.create_store = orig


def _assert_forbidden_values_absent(captured: dict, user: dict, label: str):
    """头像、主页、签名、IP 归属地的原始值不得出现在任何存储值里(性别为单字符,只按键检查)。"""
    raw_values = [user[key] for key in ("avatar_hd", "profile_url", "description", "ip_location") if key in user]
    leaked = [
        (key, raw) for raw in raw_values for key, value in captured.items()
        if isinstance(value, str) and raw in value
    ]
    assert not leaked, f"[{label}] 存储 dict 中出现禁用字段原始值: {leaked}"


# ----------------------------- 测试 -----------------------------

def test_weibo_note_masks_user_info():
    """note 拍平后的存储 dict 不含禁用键,creator_hash 与昵称为平台原始值。"""
    from support.weibo_privacy import wb

    fake = _FakeStore()
    wb_, orig = _patch_factory(fake)
    try:
        asyncio.run(wb.update_weibo_note(make_mock_note()))
    finally:
        _restore(wb_, orig)

    captured = fake.captured_content
    assert captured, "FakeStore 未捕获到 note dict"

    # 1. 不含任何禁用字段键
    hit = set(captured.keys()) & FORBIDDEN_KEYS
    assert not hit, f"note 存储 dict 仍含禁用字段键: {hit}"

    _assert_forbidden_values_absent(captured, make_mock_note()["mblog"]["user"], "weibo_note")

    # 2. creator_hash 为平台原始用户 ID;微博整数 ID 归一为字符串
    assert captured.get("creator_hash") == str(RAW_USER_ID)

    # 3. 昵称为平台原始昵称
    assert captured.get("nickname") == RAW_NICKNAME

    # 4. 内容字段正确(正文存于 content,不是 desc)
    assert "hello" not in captured  # 确认没误存
    assert "今天天气不错" in captured["content"]
    assert captured["note_id"] == NOTE_ID
    assert captured["liked_count"] == "10"
    assert captured["comments_count"] == "2"
    assert captured["shared_count"] == "1"


def test_weibo_store_end_to_end_sqlite(monkeypatch):
    """端到端:捕获 note 的真实 dict,按冻结旧 ORM 的列定义，用 SQLite 内存库走完整 写入+查询。

    关键点:sqlite_roundtrip(..., "WeiboNote", captured_dict) 会触发
    冻结旧模型声明字段的关键字校验——若 dict 含已删列(如 avatar/gender)会直接
    抛 TypeError。此处不抛异常即证明 dict 的 key 与删列后的 ORM 列完全对得上。
    """
    monkeypatch.setattr(config, "SAVE_DATA_OPTION", "db")
    import sqlite3

    from support.weibo_privacy import wb

    # ---- 1. 用 FakeStore 捕获 update_weibo_note 产生的真实 dict ----
    fake = _FakeStore()
    wb_, orig = _patch_factory(fake)
    try:
        asyncio.run(wb.update_weibo_note(make_mock_note()))
    finally:
        _restore(wb_, orig)

    captured_note = dict(fake.captured_content)
    assert captured_note

    # ---- 2. SQLite 内存库,建 WeiboNote 表 ----
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        row, note_cols = sqlite_roundtrip(connection, "WeiboNote", captured_note)
        # 表结构层面无禁用列
        assert not (note_cols & FORBIDDEN_KEYS), \
            f"WeiboNote 表仍含禁用列: {note_cols & FORBIDDEN_KEYS}"
        # 行数据层面:creator_hash 与昵称为原始值、正文保留
        assert row.creator_hash == str(RAW_USER_ID)
        assert row.nickname == RAW_NICKNAME
        assert row.note_id == NOTE_ID
        assert "今天天气不错" in row.content
        # 确认没有 desc 列存任何用户描述
        assert "desc" not in note_cols

    finally:
        connection.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
