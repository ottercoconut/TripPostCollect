"""抖音作者字段边界回归。

#49 起作品的 creator_hash 为平台原始 uid、nickname 为原始昵称，sec_uid/short_id/unique_id/头像/IP 归属地
不作为键写出。#163 起作者简介只取作者资料 user 节点的 signature，且只写入 jsonl 记录；jsonl 记录另由 sec_uid
拼出作者主页链接。搜索作者对象里的签名与主页链接不写出。
"""
import asyncio


from support.douyin import douyin_store as ds


# 抖音禁用字段(键):不得作为存储 dict 的 key 出现；作者平台 ID 只经 creator_hash 写出。
FORBIDDEN_KEYS = {
    "user_id", "sec_uid", "short_user_id", "user_unique_id",
    "avatar", "user_signature", "ip_location",
}


# ----------------------------- mock payload -----------------------------

def _build_aweme_item() -> dict:
    """贴近真实抖音结构的 mock aweme_item。
    含会被拍平的用户字段(uid/sec_uid/short_id/unique_id/avatar/signature)与
    ip_label,这些必须不落库;同时含 statistics/video/music/images 等内容字段,
    让 _extract_* 辅助函数返回非空值。"""
    return {
        "aweme_id": "7234567890123456",
        "aweme_type": 0,
        "desc": "这是一个作品描述,同时作为 title",
        "create_time": 1700000000,
        "ip_label": "上海",  # IP 归属地,禁用
        "author": {
            "uid": "9876543210",
            "sec_uid": "MS4wLjABAAAASecretSecUidForTest",
            "short_id": "88877766",
            "unique_id": "creator_unique_abc",
            "nickname": "抖音创作者",
            "avatar_thumb": {"url_list": ["http://x/avatar_thumb.jpg"]},
            "avatar_medium": {"url_list": ["http://x/avatar_medium.jpg"]},
            "signature": "这是创作者个人签名内容",
            "follower_count": 0,  # 搜索作者对象的占位粉丝数，#116 起不再采信
            "following_count": 321,
            "aweme_count": 88,
            "total_favorited": 7654321,
        },
        "creator_profile": {"status_code": 0, "user": {"follower_count": 12345}},
        "statistics": {
            "digg_count": 100,
            "collect_count": 5,
            "comment_count": 20,
            "share_count": 3,
        },
        "video": {
            "raw_cover": {"url_list": ["", "http://x/cover.jpg"]},
            "origin_cover": {"url_list": ["", "http://x/cover2.jpg"]},
            "play_addr_h264": {"url_list": ["", "http://x/video_h264.mp4"]},
            "play_addr_256": {"url_list": ["", "http://x/video_256.mp4"]},
            "play_addr": {"url_list": ["", "http://x/video.mp4"]},
        },
        "music": {
            "play_url": {"uri": "http://x/music.mp3"},
        },
        "images": [
            {"url_list": ["", "http://x/note_img1.jpg"]},
            {"url_list": ["", "http://x/note_img2.jpg"]},
        ],
    }


# ----------------------------- 辅助 -----------------------------

class _FakeStore:
    """捕获存储 dict,不真正落库。"""

    def __init__(self):
        self.contents = []

    async def store_content(self, content_item):
        self.contents.append(dict(content_item))


def _patch_factory(fake):
    """把 DouyinStoreFactory.create_store 替换为返回 fake,返回原方法用于还原。"""
    orig = ds.DouyinStoreFactory.create_store
    ds.DouyinStoreFactory.create_store = staticmethod(lambda: fake)
    return orig


def _assert_no_forbidden(captured: dict, label: str):
    hit = set(captured.keys()) & FORBIDDEN_KEYS
    assert not hit, f"[{label}] 存储 dict 仍含禁用键: {hit}"


def _assert_raw_values_absent(captured: dict, raw_values, label: str):
    """禁用的原始值(sec_uid/short_id/unique_id/头像/签名/IP 等)不得出现在任何存储值里。
    creator_hash(作品为原始 uid)与 author_profile_url(#163 由 sec_uid 拼出)单独断言,不参与子串扫描。"""
    leaked = []
    for rv in raw_values:
        if not rv:
            continue
        for k, v in captured.items():
            if k in {"creator_hash", "author_profile_url"}:
                continue
            if isinstance(v, str) and rv in v:
                leaked.append((k, rv))
    assert not leaked, f"[{label}] 存储 dict 中泄漏了原始敏感值: {leaked}"


# ----------------------------- 测试 -----------------------------

def test_douyin_aweme_masks_user_info():
    aweme = _build_aweme_item()
    raw_uid = aweme["author"]["uid"]
    raw_nick = aweme["author"]["nickname"]
    raw_sensitive = [
        aweme["author"]["sec_uid"],
        aweme["author"]["short_id"],
        aweme["author"]["unique_id"],
        aweme["author"]["avatar_thumb"]["url_list"][0],
        aweme["author"]["signature"],
        aweme["ip_label"],
    ]

    fake = _FakeStore()
    orig = _patch_factory(fake)
    try:
        asyncio.run(ds.update_douyin_aweme(aweme))
    finally:
        ds.DouyinStoreFactory.create_store = orig

    assert len(fake.contents) == 1
    captured = fake.contents[0]

    # 1. 禁用键不出现
    _assert_no_forbidden(captured, "douyin_aweme")
    # 2. 原始敏感值不泄漏到任何存储值
    _assert_raw_values_absent(captured, raw_sensitive, "douyin_aweme")
    # 3. creator_hash 为平台原始 uid
    assert captured.get("creator_hash") == raw_uid
    # 3.1 #163：jsonl 记录的作者主页链接由 sec_uid 拼出，sec_uid 本身不作为键写出
    assert captured.get("author_profile_url") == f"https://www.douyin.com/user/{aweme['author']['sec_uid']}"
    # 4. nickname 为平台原始昵称
    assert captured.get("nickname") == raw_nick
    # 4.1 作者统计字段保留。
    assert captured.get("followers_count") == 12345
    assert captured.get("fans_count") == 12345
    assert captured.get("following_count") == 321
    assert captured.get("aweme_count") == 88
    assert captured.get("author_liked_count") == 7654321
    # 5. 内容字段保留(desc/title 是作品描述,不禁用)
    assert captured.get("desc") == aweme["desc"]
    assert captured.get("title") == aweme["desc"]
    # 6. #116：封面、视频、音乐地址不再写入内容记录；正文图仍来自 images。
    assert not {"cover_url", "video_download_url", "music_download_url"} & set(captured)
    assert "http://x/note_img1.jpg" in captured.get("note_download_url", "")
    assert captured.get("author_followers_source") == "creator_profile"
    # 7. 互动数据拍平
    assert captured.get("liked_count") == "100"
    assert captured.get("collected_count") == "5"
    assert captured.get("comment_count") == "20"
    assert captured.get("share_count") == "3"
    assert captured.get("aweme_url") == f"https://www.douyin.com/video/{aweme['aweme_id']}"


