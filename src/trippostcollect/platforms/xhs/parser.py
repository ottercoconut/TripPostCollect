# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/help.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

# TripPostCollect：T09 迁入根平台；来源 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303，许可见 resources/licenses/MediaCrawler-LICENSE。

"""小红书纯解析：URL、INITIAL_STATE、正文图片与作者投影；不含网络或文件 IO。"""

import json
import math
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import humps

from trippostcollect.application.contracts import ImageStagingError
from trippostcollect.platforms.xhs.models import NoteUrlInfo
from trippostcollect.records.formal import parse_int
from trippostcollect.records.identity import platform_nickname, platform_user_id
from trippostcollect.records.sanitization import AUTHOR_AVATAR_KEYS, XHS_SERIALIZED_PROFILE_AVATAR_PATHS
from trippostcollect.runtime.helpers import extract_url_params_to_dict, normalize_image_url


XHS_STABLE_PATH_MARKERS = ("/notes_pre_post/", "/notes_post/", "/notes/")


def base36encode(number, alphabet='0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'):
    """Converts an integer to a base36 string."""
    if not isinstance(number, int):
        raise TypeError('number must be an integer')

    base36 = ''
    sign = ''

    if number < 0:
        sign = '-'
        number = -number

    if 0 <= number < len(alphabet):
        return sign + alphabet[number]

    while number != 0:
        number, i = divmod(number, len(alphabet))
        base36 = alphabet[i] + base36

    return sign + base36


def get_search_id():
    e = int(time.time() * 1000) << 64
    t = int(random.uniform(0, 2147483646))
    return base36encode((e + t))


def parse_note_info_from_note_url(url: str) -> NoteUrlInfo:
    """
    Parse note information from Xiaohongshu note URL
    Args:
        url: "https://www.xiaohongshu.com/explore/66fad51c000000001b0224b8?xsec_token=AB3rO-QopW5sgrJ41GwN01WCXh6yWPxjSoFI9D5JIMgKw=&xsec_source=pc_search"
    Returns:

    """
    note_id = url.split("/")[-1].split("?")[0]
    params = extract_url_params_to_dict(url)
    xsec_token = params.get("xsec_token", "")
    xsec_source = params.get("xsec_source", "")
    return NoteUrlInfo(note_id=note_id, xsec_token=xsec_token, xsec_source=xsec_source)


class XiaoHongShuExtractor:
    def __init__(self):
        pass

    def extract_note_detail_from_html(self, note_id: str, html: str) -> Optional[Dict]:
        """Extract note details from HTML

        Args:
            html (str): HTML string

        Returns:
            Dict: Note details dictionary
        """
        if "noteDetailMap" not in html:
            # Either a CAPTCHA appeared or the note doesn't exist
            return None

        state = re.findall(r"window.__INITIAL_STATE__=({.*})</script>", html)[
            0
        ].replace("undefined", '""')
        if state != "{}":
            note_dict = humps.decamelize(json.loads(state))
            return note_dict["note"]["note_detail_map"][note_id]["note"]
        return None

    def extract_creator_info_from_html(self, html: str) -> Optional[Dict]:
        """Extract user information from HTML

        Args:
            html (str): HTML string

        Returns:
            Dict: User information dictionary
        """
        creator_info, _reason = self.extract_creator_info_with_reason(html)
        return creator_info

    def extract_creator_info_with_reason(self, html: str) -> Tuple[Optional[Dict], str]:
        """与 extract_creator_info_from_html 同一严格解码，另返回只含类别的原因供诊断。

        原因取值：``ok``、``state_script_missing``、``state_decode_failed:<类别>``、
        ``state_null``、``user_missing``、``user_page_data_missing``、``user_page_data_empty``；
        不含 HTML、作者 ID 或请求参数。
        """
        match = re.search(
            r"<script[^>]*>\s*window\.__INITIAL_STATE__\s*=\s*", html, re.M
        )
        if match is None:
            return None, "state_script_missing"
        state_source = html[match.end() :].replace(":undefined", ":null").lstrip()
        try:
            info, _ = json.JSONDecoder(strict=False).raw_decode(state_source)
        except json.JSONDecodeError as exc:
            return None, f"state_decode_failed:{_state_decode_failure_kind(state_source, exc.pos)}"
        if info is None:
            return None, "state_null"
        user_info = info.get("user")
        if not isinstance(user_info, dict):
            return None, "user_missing"
        creator_info = user_info.get("userPageData")
        if not isinstance(creator_info, dict):
            return None, "user_page_data_missing"
        return creator_info, "ok" if creator_info else "user_page_data_empty"


def _state_decode_failure_kind(source: str, position: int) -> str:
    """只按失败位置的首个记号归类，便于区分平台状态里的 JS 构造与普通损坏。"""
    token = source[position : position + 32]
    if not token:
        return "truncated"
    if re.match(r"new\s+[A-Za-z_$]", token):
        return "js_new_expression"
    if re.match(r"[A-Za-z_$]", token):
        return "js_identifier"
    return "invalid_json"


def xhs_source_asset_key(source_url: str) -> str:
    normalized = normalize_image_url(source_url)
    parsed = urlsplit(normalized)
    identity = f"{parsed.netloc.lower()}{parsed.path}"
    for marker in XHS_STABLE_PATH_MARKERS:
        if marker in parsed.path:
            identity = f"{marker}{parsed.path.split(marker, 1)[1]}"
            break
    return f"xhs:path:{identity}"


def _xhs_image_assets(note_item: Dict) -> List[Dict]:
    """Choose one authoritative URL per XHS image object and deduplicate assets."""

    assets: List[Dict] = []
    seen = set()
    image_list = note_item.get("image_list") or []
    if not isinstance(image_list, list):
        return assets
    for image in image_list:
        if isinstance(image, str):
            url = image.strip()
        elif isinstance(image, dict):
            url = next(
                (
                    str(image.get(key) or "").strip()
                    for key in ("url_default", "url", "url_pre")
                    if str(image.get(key) or "").strip()
                ),
                "",
            )
        else:
            continue
        if not url:
            continue
        try:
            asset_key = xhs_source_asset_key(url)
        except ImageStagingError:
            continue
        if asset_key in seen:
            continue
        seen.add(asset_key)
        assets.append(
            {"url": url, "source_index": len(assets), "source_asset_key": asset_key}
        )
    return assets


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _nested_value(data: Dict, *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = data
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current not in (None, ""):
            return current
    return None


def _interaction_count(creator: Dict, *names: str) -> Any:
    expected = {name.lower() for name in names}
    interactions = (
        creator.get("interactions")
        or creator.get("interaction")
        or creator.get("interactionList")
        or creator.get("interaction_list")
        or []
    )
    if not isinstance(interactions, list):
        return None
    for item in interactions:
        if not isinstance(item, dict):
            continue
        item_type = str(_first_nonempty(item.get("type"), item.get("name"), item.get("key")) or "").lower()
        if item_type in expected or any(name in item_type for name in expected):
            return _first_nonempty(item.get("count"), item.get("num"), item.get("value"))
    return None


def _creator_basic_info(creator: Dict) -> Dict:
    basic = creator.get("basicInfo") or creator.get("basic_info") or creator.get("basic") or {}
    return basic if isinstance(basic, dict) else {}


def _creator_metric(creator: Dict, *keys: str) -> Any:
    basic = _creator_basic_info(creator)
    for key in keys:
        value = _first_nonempty(creator.get(key), basic.get(key))
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return value
    return _interaction_count(creator, *keys)


def _creator_profile_url(user_id: str, xsec_token: str = "", xsec_source: str = "pc_search") -> str:
    if not user_id:
        return ""
    url = f"https://www.xiaohongshu.com/user/profile/{user_id}"
    if xsec_token:
        url += f"?xsec_token={xsec_token}&xsec_source={xsec_source or 'pc_search'}"
    return url


def _normalized_creator_item(user_id: str, creator: Dict, *, current_timestamp: Callable[[], int]) -> Dict:
    basic = _creator_basic_info(creator)
    return {
        "user_id": user_id or _first_nonempty(basic.get("userId"), basic.get("user_id")),
        "nickname": _first_nonempty(basic.get("nickname"), creator.get("nickname")),
        "avatar_url": _first_nonempty(basic.get("imageb"), basic.get("avatar"), basic.get("avatarUrl"), creator.get("avatar_url")),
        "author_desc": _first_nonempty(basic.get("desc"), basic.get("description"), creator.get("desc")),
        "gender": _first_nonempty(basic.get("gender"), creator.get("gender")),
        "ip_location": _first_nonempty(basic.get("ipLocation"), basic.get("ip_location"), creator.get("ip_location")),
        "fans": _creator_metric(creator, "fans", "粉丝"),
        "fans_count": _creator_metric(creator, "fans", "fansCount", "fans_count", "followerCount", "followers_count", "粉丝"),
        "follows": _creator_metric(creator, "follows", "关注"),
        "following_count": _creator_metric(creator, "follows", "followsCount", "following_count", "follow_count", "关注"),
        "note_count": _creator_metric(creator, "notes", "noteCount", "note_count", "posts_count", "笔记"),
        "interaction_count": _creator_metric(creator, "interaction", "interactions", "获赞与收藏"),
        "creator_profile_json": json.dumps(creator, ensure_ascii=False, sort_keys=True),
        "last_modify_ts": current_timestamp(),
    }


# 运行时作者状态投影白名单。仓库内读取点：_normalized_creator_item、_creator_basic_info、
# _creator_metric、_interaction_count，以及作者匹配读取的 basicInfo 用户 ID。仓库外读取点：同级
# TripPostResearch 从 creator_profile_json 读取的 basicInfo.redId、tags[].tagType/name、
# verifyInfo.redOfficialVerifyType 与 interactions[].i18nCount。头像（imageb、images、avatar*、icon）
# 与任何凭据或 token 字段都不在白名单中，投影只保留标量值。
XHS_CREATOR_METRIC_FIELDS = (
    "fans", "fansCount", "fans_count", "followerCount", "followers_count", "粉丝",
    "follows", "followsCount", "following_count", "follow_count", "关注",
    "notes", "noteCount", "note_count", "posts_count", "笔记",
    "interaction", "interactions", "获赞与收藏",
)
XHS_CREATOR_TOP_FIELDS = ("nickname", "desc", "gender", "ip_location") + XHS_CREATOR_METRIC_FIELDS
XHS_CREATOR_BASIC_INFO_KEYS = ("basicInfo", "basic_info", "basic")
XHS_CREATOR_BASIC_FIELDS = (
    "userId", "user_id", "redId", "nickname", "desc", "description", "gender", "ipLocation", "ip_location",
) + XHS_CREATOR_METRIC_FIELDS
XHS_CREATOR_VERIFY_FIELDS = ("redOfficialVerifyType",)
XHS_CREATOR_INTERACTION_LIST_KEYS = ("interactions", "interaction", "interactionList", "interaction_list")
XHS_CREATOR_INTERACTION_ITEM_FIELDS = ("type", "name", "key", "count", "num", "value", "i18nCount")
XHS_CREATOR_TAG_FIELDS = ("tagType", "name")
# 对象容器与列表容器：(键, 字段白名单)，页面脚本与 Python 再过滤共用。
XHS_CREATOR_OBJECT_FIELDS = tuple(
    (key, XHS_CREATOR_BASIC_FIELDS) for key in XHS_CREATOR_BASIC_INFO_KEYS
) + (("verifyInfo", XHS_CREATOR_VERIFY_FIELDS),)
XHS_CREATOR_LIST_FIELDS = tuple(
    (key, XHS_CREATOR_INTERACTION_ITEM_FIELDS) for key in XHS_CREATOR_INTERACTION_LIST_KEYS
) + (("tags", XHS_CREATOR_TAG_FIELDS),)
# 计数类字段：运行时投影不接受布尔值（静态解析路径不受影响）。
XHS_CREATOR_NUMERIC_FIELDS = frozenset(
    XHS_CREATOR_METRIC_FIELDS + ("count", "num", "value", "i18nCount")
)
XHS_CREATOR_INTERACTION_MAX_ITEMS = 32
# 页面内头像证据检查的上限；超限、循环引用或异常结构时拒绝整个投影。
XHS_CREATOR_PROJECTION_MAX_DEPTH = 16
XHS_CREATOR_PROJECTION_MAX_NODES = 4000
XHS_CREATOR_PROJECTION_REJECTED_REASONS = frozenset({"projection_avatar_check_incomplete"})
# Python str.strip() 去除的全部空白字符（即 str.isspace() 为真的码点）；页面脚本用同一集合去首尾空白，
# 与 records.sanitization 的头像 URL 比较语义一致。
XHS_PYTHON_STRIP_CHARS = (
    "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


def xhs_creator_projection_spec() -> Dict[str, Any]:
    """传给页面固定投影脚本的参数：字段白名单、头像证据键与路径、空白集合与检查上限。

    头像证据键与路径直接取自 records.sanitization，与首次序列化前的头像清理规则同源。
    """
    return {
        "top_fields": list(XHS_CREATOR_TOP_FIELDS),
        "object_fields": [[key, list(fields)] for key, fields in XHS_CREATOR_OBJECT_FIELDS],
        "list_fields": [[key, list(fields)] for key, fields in XHS_CREATOR_LIST_FIELDS],
        "max_items": XHS_CREATOR_INTERACTION_MAX_ITEMS,
        "numeric_fields": sorted(XHS_CREATOR_NUMERIC_FIELDS),
        "avatar_keys": sorted(AUTHOR_AVATAR_KEYS),
        "avatar_paths": sorted(
            list(path)
            for path in XHS_SERIALIZED_PROFILE_AVATAR_PATHS["creator_profile_json"]
        ),
        "strip_chars": XHS_PYTHON_STRIP_CHARS,
        "max_depth": XHS_CREATOR_PROJECTION_MAX_DEPTH,
        "max_nodes": XHS_CREATOR_PROJECTION_MAX_NODES,
    }


def _scalar_fields(source: Dict, fields: tuple[str, ...]) -> Dict:
    projected = {}
    for field in fields:
        value = source.get(field)
        if isinstance(value, bool):
            if field not in XHS_CREATOR_NUMERIC_FIELDS:
                projected[field] = value
        elif isinstance(value, str) or (isinstance(value, (int, float)) and math.isfinite(value)):
            projected[field] = value
    return projected


def _projection_has_fields(projected: Dict) -> bool:
    """按结构判断投影是否为空：有任一标量字段、非空对象容器或含字段的列表条目即非空。"""
    for value in projected.values():
        if isinstance(value, dict):
            if value:
                return True
        elif isinstance(value, list):
            if any(value):
                return True
        else:
            return True
    return False


def read_creator_runtime_projection(result: Any) -> Tuple[Dict, str]:
    """校验页面投影脚本的返回信封，并按白名单再过滤一次结构与类型。

    头像证据检查只在页面内完成；这里不接收证据集合，只保证返回值不超出固定字段。
    """
    if not isinstance(result, dict):
        return {}, "runtime_projection_error"
    status = result.get("status")
    if status == "missing":
        return {}, "runtime_projection_empty"
    if status == "rejected":
        reason = result.get("reason")
        if reason in XHS_CREATOR_PROJECTION_REJECTED_REASONS:
            return {}, reason
        return {}, "runtime_projection_error"
    creator = result.get("creator")
    if status != "ok" or not isinstance(creator, dict):
        return {}, "runtime_projection_error"
    projected = _scalar_fields(creator, XHS_CREATOR_TOP_FIELDS)
    for key, fields in XHS_CREATOR_OBJECT_FIELDS:
        container = creator.get(key)
        if isinstance(container, dict):
            projected[key] = _scalar_fields(container, fields)
    for key, fields in XHS_CREATOR_LIST_FIELDS:
        items = creator.get(key)
        if isinstance(items, list):
            projected[key] = [
                _scalar_fields(item, fields)
                for item in items[:XHS_CREATOR_INTERACTION_MAX_ITEMS]
                if isinstance(item, dict)
            ]
    if not _projection_has_fields(projected):
        return {}, "runtime_projection_empty"
    return projected, "ok"


def creator_profile_user_ids(creator: Dict) -> List[str]:
    """作者资料自带的用户 ID（只看 basicInfo 系键），用于和请求的作者核对。"""
    user_ids = []
    for key in XHS_CREATOR_BASIC_INFO_KEYS:
        basic = creator.get(key)
        if not isinstance(basic, dict):
            continue
        for field in ("userId", "user_id"):
            value = basic.get(field)
            if value not in (None, ""):
                user_ids.append(str(value))
    return user_ids


def creator_runtime_followers_observed(user_id: str, creator: Dict) -> bool:
    """运行时投影的就绪判定：沿用 update_xhs_note 的 fans_count/fans 取值，只认非布尔且可按
    records.formal.parse_int 解析的计数（含“万/亿”单位）；0 是真实观察值。"""
    creator_item = _normalized_creator_item(user_id, creator, current_timestamp=lambda: 0)
    return any(
        not isinstance(value, bool) and parse_int(value) is not None
        for value in (creator_item.get("fans_count"), creator_item.get("fans"))
    )


def update_xhs_note(
    note_item: Dict,
    *,
    source_keyword: str,
    current_timestamp: Callable[[], int],
    save_data_option: str,
) -> Dict:
    """
    Update Xiaohongshu note
    Args:
        note_item:

    Returns:

    """
    note_id = note_item.get("note_id")
    user_info = note_item.get("user", {})
    interact_info = note_item.get("interact_info", {})
    image_assets = _xhs_image_assets(note_item)
    tag_list: List[Dict] = note_item.get("tag_list", [])
    creator_profile: Dict = note_item.get("creator_profile") or {}
    creator_item = _normalized_creator_item(user_info.get("user_id", ""), creator_profile, current_timestamp=current_timestamp) if creator_profile else {}
    followers_observed = any(
        creator_item.get(key) not in (None, "")
        for key in ("fans_count", "fans")
    )

    video_url = ""
    raw_user_id = platform_user_id(_first_nonempty(user_info.get("user_id"), creator_item.get("user_id")))
    raw_nickname = platform_nickname(_first_nonempty(user_info.get("nickname"), creator_item.get("nickname")))

    local_db_item = {
        "note_id": note_item.get("note_id"),  # Note ID
        "type": note_item.get("type"),  # Note type
        "title": note_item.get("title") or note_item.get("desc", "")[:255],  # Note title
        "desc": note_item.get("desc", ""),  # Note description
        "video_url": video_url,  # Note video url
        "time": note_item.get("time"),  # Note publish time
        "last_update_time": note_item.get("last_update_time", 0),  # Note last update time
        "creator_hash": raw_user_id,  # 作者平台原始 user_id
        "user_id": raw_user_id,
        "nickname": raw_nickname,
        "author_profile_url": _creator_profile_url(
            raw_user_id,
            note_item.get("xsec_token", ""),
            note_item.get("xsec_source", "pc_search"),
        ),
        "avatar_url": _first_nonempty(
            user_info.get("avatar"),
            user_info.get("image"),
            user_info.get("imageb"),
            creator_item.get("avatar_url"),
        ),
        "author_desc": creator_item.get("author_desc"),
        "gender": creator_item.get("gender"),
        "ip_location": creator_item.get("ip_location"),
        "fans": creator_item.get("fans"),
        "fans_count": creator_item.get("fans_count"),
        "followers_count": creator_item.get("fans_count") or creator_item.get("fans"),
        "followers_observed": followers_observed,
        "author_followers_source": "creator_profile" if followers_observed else "missing",
        "follows": creator_item.get("follows"),
        "following_count": creator_item.get("following_count") or creator_item.get("follows"),
        "note_count": creator_item.get("note_count"),
        "interaction_count": creator_item.get("interaction_count"),
        "liked_count": interact_info.get("liked_count"),  # Like count
        "collected_count": interact_info.get("collected_count"),  # Collection count
        "comment_count": interact_info.get("comment_count"),  # Comment count
        "share_count": interact_info.get("share_count"),  # Share count
        "image_list": ','.join(asset["url"] for asset in image_assets),  # Image URLs
        "image_assets": image_assets,
        "image_list_source": "note_detail.image_list",
        "tag_list": ','.join([tag.get('name', '') for tag in tag_list if tag.get('type') == 'topic']),  # Tags
        "last_modify_ts": current_timestamp(),  # Last modification timestamp (Generated by MediaCrawler, mainly used to record the latest update time of a record in DB storage)
        "note_url": f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={note_item.get('xsec_token')}&xsec_source=pc_search",  # Note URL
        "source_keyword": source_keyword,  # Search keyword
        "xsec_token": note_item.get("xsec_token"),  # xsec_token
        "creator_profile_json": creator_item.get("creator_profile_json", ""),
    }
    if save_data_option == "jsonl":
        local_db_item.update(
            {
                "content_detail_status": note_item.get(
                    "content_detail_status", "unobserved"
                ),
                "content_detail_source": note_item.get("content_detail_source", ""),
            }
        )
    return local_db_item


class XhsParserMixin:
    """纯判别与诊断摘要；作为 XiaoHongShuCrawler 的 mixin，方法体逐字保留。"""

    @staticmethod
    def is_video_note(note_detail: Dict) -> bool:
        note_type = str(note_detail.get("type") or "").strip().lower()
        return note_type in {"video", "视频"} or "video" in note_type

    @staticmethod
    def note_detail_summaries(note_details: List[Optional[Dict]]) -> List[Dict]:
        summaries: List[Dict] = []
        for note_detail in note_details:
            if not note_detail:
                continue
            user_info = note_detail.get("user") or {}
            interact_info = note_detail.get("interact_info") or {}
            creator_profile = note_detail.get("creator_profile") or {}
            summaries.append(
                {
                    "note_id": note_detail.get("note_id"),
                    "type": note_detail.get("type"),
                    "title": note_detail.get("title"),
                    "desc_preview": str(note_detail.get("desc") or "")[:80],
                    "image_count": len(note_detail.get("image_list") or []),
                    "user_id": user_info.get("user_id"),
                    "nickname": user_info.get("nickname"),
                    "liked_count": interact_info.get("liked_count"),
                    "collected_count": interact_info.get("collected_count"),
                    "comment_count": interact_info.get("comment_count"),
                    "share_count": interact_info.get("share_count"),
                    "creator_profile": bool(creator_profile),
                }
            )
        return summaries
