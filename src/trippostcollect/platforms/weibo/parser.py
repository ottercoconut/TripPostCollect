"""#211 微博桌面详情投影。

正文只取 ``longText.content``，没有或为空时取 ``text_raw``。图片只取本条 ``pic_infos``，
按 ``pic_ids`` 顺序。作者只取 ``sideUser`` 的 ``data.user``。不读 HTML ``text``，不合并转发原帖。
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit

from trippostcollect.records.identity import platform_nickname, platform_user_id
from trippostcollect.runtime.helpers import (
    normalize_image_url, rfc2822_to_china_datetime, rfc2822_to_timestamp,
)


CONTENT_DETAIL_SOURCE = "desktop_detail"
CONTENT_DETAIL_STATUS = "detail_observed"
FOLLOWERS_SOURCE = "detail_side_user"
IMAGE_LIST_SOURCE = "pic_infos"
ACCEPTED_PIC_TYPES = frozenset({"pic", "gif", "livephoto"})
PIC_SPEC_KEYS = ("largest", "mw2000", "original")
VERIFY_KEYS = ("verified", "verified_type", "verified_type_ext", "verified_reason")
# #213：verified_type 为 0 是个人认证，-1 是未认证；其余值（侦察见过 1、2、3、4、7）一律记为机构，
# 不按类型码细分。机构的认证文字以“机构认证”开头。
PERSONAL_VERIFIED_TYPES = frozenset({0, -1})
INSTITUTION_VERIFIED_PREFIX = "机构认证"

CREATED_AT_UNPARSEABLE = "created_at_unparseable"
DETAIL_CONTENT_EMPTY = "detail_content_empty"
AUTHOR_FOLLOWERS_MISSING = "author_followers_missing"
AUTHOR_PROFILE_MISSING = "author_profile_missing"
MISSING_CONTENT_IMAGE = "missing_content_image"
MISSING_METRICS = "missing_metrics"


@dataclass(frozen=True)
class ParsedWeibo:
    record: dict[str, Any] | None = None
    video: bool = False
    skip_code: str | None = None


def weibo_created_at(status: dict) -> tuple[int, str] | None:
    """平台原始发帖时间（RFC 2822）换算的时间戳与上海时区时间；缺失或格式异常返回 None。"""

    created_at = status.get("created_at") if isinstance(status, dict) else None
    if not isinstance(created_at, str) or not created_at.strip():
        return None
    try:
        return (
            rfc2822_to_timestamp(created_at),
            str(rfc2822_to_china_datetime(created_at)),
        )
    except ValueError:
        return None


def weibo_body_text(status: dict) -> str:
    """``longText.content`` 优先；没有该键或去空白后为空时用 ``text_raw``。HTML ``text`` 不参与。"""

    if not isinstance(status, dict):
        return ""
    long_text = status.get("longText")
    if isinstance(long_text, dict):
        content = long_text.get("content")
        if isinstance(content, str) and content.strip():
            return content
    raw = status.get("text_raw")
    if isinstance(raw, str) and raw.strip():
        return raw
    return ""


def show_identity(payload: Any) -> str:
    """``idstr`` 优先，否则 ``mid``。都没有时为空串。"""

    if not isinstance(payload, dict):
        return ""
    for key in ("idstr", "mid"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def show_matches(payload: Any, mid: str) -> bool:
    """``idstr`` 或 ``mid`` 与卡片 mid 完全一致。"""

    target = str(mid or "").strip()
    if not target or not isinstance(payload, dict):
        return False
    for key in ("idstr", "mid"):
        value = payload.get(key)
        if value not in (None, "") and str(value).strip() == target:
            return True
    return False


def show_user_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    user = payload.get("user")
    if not isinstance(user, dict):
        return ""
    return platform_user_id(user.get("id"))


def side_user_of(payload: Any) -> dict[str, Any] | None:
    """``/ajax/side/cards/sideUser`` 的 ``data.user``。没有用户对象时为 None。"""

    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    user = data.get("user")
    return user if isinstance(user, dict) else None


def side_user_matches(payload: Any, user_id: str) -> bool:
    user = side_user_of(payload)
    expected = str(user_id or "").strip()
    if user is None or not expected:
        return False
    return platform_user_id(user.get("id")) == expected


def is_weibo_video(status: dict) -> bool:
    """``page_info.object_type=video`` 且没有 ``pic_infos``。"""

    if not isinstance(status, dict):
        return False
    page_info = status.get("page_info")
    if not isinstance(page_info, dict) or page_info.get("object_type") != "video":
        return False
    infos = status.get("pic_infos")
    return not isinstance(infos, dict) or len(infos) == 0


def _count(source: dict, key: str) -> int | None:
    """缺键为 None。键上的整数（含 0）照记。布尔值与其他类型视为缺失。"""

    if not isinstance(source, dict) or key not in source:
        return None
    value = source[key]
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _spec_url(info: dict, key: str) -> str:
    spec = info.get(key)
    if isinstance(spec, str):
        return spec.strip()
    if isinstance(spec, dict):
        url = spec.get("url")
        if isinstance(url, str):
            return url.strip()
    return ""


def weibo_image_assets(status: dict) -> list[dict[str, Any]]:
    """按 ``pic_ids`` 取 ``pic_infos``。规格顺序 ``largest``、``mw2000``、``original``。"""

    if not isinstance(status, dict):
        return []
    pic_ids = status.get("pic_ids")
    infos = status.get("pic_infos")
    if not isinstance(pic_ids, list) or not isinstance(infos, dict):
        return []
    assets: list[dict[str, Any]] = []
    for raw_pid in pic_ids:
        pid = str(raw_pid).strip() if raw_pid not in (None, "") else ""
        info = infos.get(raw_pid)
        if not isinstance(info, dict) and pid:
            info = infos.get(pid)
        if not pid or not isinstance(info, dict):
            continue
        if info.get("type") not in ACCEPTED_PIC_TYPES:
            continue
        urls = [url for key in PIC_SPEC_KEYS if (url := _spec_url(info, key))]
        if not urls:
            continue
        assets.append(
            {
                "pid": pid,
                "url": urls[0],
                "urls": urls,
                "source_index": len(assets),
                # 访问门禁 R5 按资产键把记录图片与账本 cdn_direct 登记、manifest 对应。
                "source_asset_key": weibo_source_asset_key(pid, urls[0]),
            }
        )
    return assets


def followers_count_of(user: dict) -> int | None:
    """必须是 int。缺 int 时只认纯数字 ``followers_count_str``；带「万」的字串不换算。"""

    if not isinstance(user, dict):
        return None
    if "followers_count" in user:
        value = user.get("followers_count")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    text = user.get("followers_count_str")
    if isinstance(text, str) and text.strip().isdigit():
        return int(text.strip())
    return None


def weibo_verify_of(user: dict) -> dict[str, Any]:
    """认证原值。缺键不写，不把 None 补进去。"""

    if not isinstance(user, dict):
        return {}
    return {key: user[key] for key in VERIFY_KEYS if key in user and user[key] is not None}


def weibo_author_verification(user: dict) -> tuple[int | None, str | None]:
    """按 #213 口径得出 ``author_verified`` 与 ``author_verified_text``。

    ``verified`` 不是布尔值时两列都为 None，不推断。未认证为 0，文字为 None。
    已认证时文字取 ``verified_reason``；``verified_type`` 是整数且不是 0 或 -1 时记为机构，
    文字为“机构认证、<说明>”，说明为空时只写“机构认证”。
    ``verified_type`` 缺失或不是整数时不判定机构，只写说明。
    """

    if not isinstance(user, dict):
        return None, None
    verified = user.get("verified")
    if not isinstance(verified, bool):
        return None, None
    if not verified:
        return 0, None
    raw_reason = user.get("verified_reason")
    reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
    verified_type = user.get("verified_type")
    institution = (
        isinstance(verified_type, int)
        and not isinstance(verified_type, bool)
        and verified_type not in PERSONAL_VERIFIED_TYPES
    )
    if institution:
        return 1, f"{INSTITUTION_VERIFIED_PREFIX}、{reason}" if reason else INSTITUTION_VERIFIED_PREFIX
    return 1, reason or None


def weibo_source_asset_key(pid: str | None, source_url: str) -> str:
    if pid not in (None, ""):
        return f"weibo:pid:{str(pid).strip()}"
    normalized = normalize_image_url(source_url)
    parsed = urlsplit(normalized)
    digest = sha256(f"{parsed.hostname.lower()}{parsed.path}".encode("utf-8")).hexdigest()
    return f"weibo:urlsha256:{digest}"


def _profile_url(user_id: str) -> str:
    return f"https://weibo.com/u/{user_id}" if user_id else ""


def _note_url(user_id: str, mblogid: str) -> str:
    if user_id and mblogid:
        return f"https://weibo.com/{user_id}/{mblogid}"
    return ""


def structural_skip_code(record: dict[str, Any] | None) -> str | None:
    """投影已经成功、但仍过不了字段 profile 时的跳过码。"""

    if not isinstance(record, dict):
        return AUTHOR_PROFILE_MISSING
    if not str(record.get("content") or "").strip():
        return DETAIL_CONTENT_EMPTY
    if not record.get("author_platform_id") or not record.get("nickname"):
        return AUTHOR_PROFILE_MISSING
    if record.get("followers_observed") is not True:
        return AUTHOR_FOLLOWERS_MISSING
    if not record.get("image_list"):
        return MISSING_CONTENT_IMAGE
    for key in ("liked_count", "comments_count", "shared_count"):
        if record.get(key) is None:
            return MISSING_METRICS
    return None


def parse_weibo_detail(
    status: dict,
    user: dict,
    *,
    mblogid: str,
    source_keyword: str,
    current_timestamp,
    visit_refs: dict[str, Any] | None = None,
) -> ParsedWeibo:
    """把一条 show 与匹配的 sideUser 投影成项目记录。视频与无法解析的发帖时间不带记录。"""

    if not isinstance(status, dict):
        return ParsedWeibo(skip_code=DETAIL_CONTENT_EMPTY)
    if is_weibo_video(status):
        return ParsedWeibo(video=True)
    created = weibo_created_at(status)
    if created is None:
        return ParsedWeibo(skip_code=CREATED_AT_UNPARSEABLE)
    body = weibo_body_text(status)
    if not body.strip():
        return ParsedWeibo(skip_code=DETAIL_CONTENT_EMPTY)
    author = user if isinstance(user, dict) else {}
    user_id = platform_user_id(author.get("id"))
    followers = followers_count_of(author)
    followers_observed = followers is not None
    assets = weibo_image_assets(status)
    verify = weibo_verify_of(author)
    author_verified, author_verified_text = weibo_author_verification(author)
    description = author.get("description") if isinstance(author.get("description"), str) else None
    record = {
        "note_id": show_identity(status),
        "mblogid": str(mblogid or status.get("mblogid") or ""),
        "content": body,
        "create_time": created[0],
        "create_date_time": created[1],
        "liked_count": _count(status, "attitudes_count"),
        "comments_count": _count(status, "comments_count"),
        "shared_count": _count(status, "reposts_count"),
        "last_modify_ts": current_timestamp(),
        "note_url": _note_url(user_id, str(mblogid or status.get("mblogid") or "")),
        "image_list": [asset["url"] for asset in assets],
        "image_count": len(assets),
        "image_list_source": IMAGE_LIST_SOURCE,
        "image_assets": assets,
        "author_platform_id": user_id,
        "nickname": platform_nickname(author.get("screen_name")),
        "followers_count": followers,
        "followers_observed": followers_observed,
        "author_followers_source": FOLLOWERS_SOURCE if followers_observed else "missing",
        "author_following_count": _count(author, "friends_count"),
        "author_posts_count": _count(author, "statuses_count"),
        "author_description": description,
        "author_profile_url": _profile_url(user_id),
        "author_verified": author_verified,
        "author_verified_text": author_verified_text,
        # 空对象也留下键。没有认证原值时，历史刷新靠这个键判断 sideUser 已经读到。
        "author_json": {"weibo_verify": verify},
        "content_detail_status": CONTENT_DETAIL_STATUS,
        "content_detail_source": CONTENT_DETAIL_SOURCE,
        "content_visit_refs": dict(visit_refs or {}),
        "source_keyword": source_keyword,
    }
    return ParsedWeibo(record=record)
