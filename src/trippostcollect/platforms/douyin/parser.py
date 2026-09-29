
import json
import re
from hashlib import sha256
from typing import Any, Callable, Dict, List
from urllib.parse import urlsplit

from trippostcollect.application.contracts import ImageStagingError
from trippostcollect.records.identity import anonymize_user_id, mask_nickname
from trippostcollect.runtime.helpers import extract_url_params_to_dict, normalize_image_url
from trippostcollect.platforms.douyin.models import SearchResponseError, VideoUrlInfo

DOUYIN_RESULT_LINK_SELECTOR = ', '.join(
    (
        'a[href*="/video/"]:visible',
        'a[href*="/note/"]:visible',
        '[data-e2e*="search-result"]:visible',
        '.search-result-card:visible',
        '[id^="waterfall_item_"]:visible',
    )
)
DOUYIN_NO_RESULT_MARKERS = (
    "暂无搜索结果",
    "没有找到相关结果",
    "没有找到你想要的内容",
    "未搜索到相关内容",
    "搜索结果为空",
    "换个关键词试试",
)


def _find_nested_platform_record(
    value: Any,
    target_id: str,
    *,
    id_keys: tuple[str, ...],
    shape_keys: tuple[str, ...],
    depth: int = 0,
) -> dict[str, Any] | None:
    if depth > 12:
        return None
    if isinstance(value, dict):
        identity = next(
            (
                str(value.get(key) or "")
                for key in id_keys
                if value.get(key) not in (None, "")
            ),
            "",
        )
        if identity == str(target_id) and any(key in value for key in shape_keys):
            return value
        for nested in value.values():
            found = _find_nested_platform_record(
                nested,
                target_id,
                id_keys=id_keys,
                shape_keys=shape_keys,
                depth=depth + 1,
            )
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_nested_platform_record(
                nested,
                target_id,
                id_keys=id_keys,
                shape_keys=shape_keys,
                depth=depth + 1,
            )
            if found is not None:
                return found
    return None


#!/usr/bin/env python
# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
def _find_douyin_detail(value: Any, aweme_id: str) -> dict[str, Any] | None:
    return _find_nested_platform_record(
        value,
        aweme_id,
        id_keys=("aweme_id",),
        shape_keys=("desc", "author", "statistics", "images", "video", "create_time"),
    )


# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/douyin/help.py
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


# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Name: Programmer Ajiang-Relakkes
# @Time    : 2024/6/10 02:24
# @Desc    : Get a_bogus parameter, for learning and communication only, do not use for commercial purposes, contact author to delete if infringement

# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
def parse_video_info_from_url(url: str) -> VideoUrlInfo:
    """
    Parse video ID from Douyin video URL
    Supports the following formats:
    1. Normal video link: https://www.douyin.com/video/7525082444551310602
    2. Link with modal_id parameter:
       - https://www.douyin.com/user/MS4wLjABAAAATJPY7LAlaa5X-c8uNdWkvz0jUGgpw4eeXIwu_8BhvqE?modal_id=7525082444551310602
       - https://www.douyin.com/root/search/python?modal_id=7471165520058862848
    3. Short link: https://v.douyin.com/iF12345ABC/ (requires client parsing)
    4. Pure ID: 7525082444551310602

    Args:
        url: Douyin video link or ID
    Returns:
        VideoUrlInfo: Object containing video ID
    """
    # If it's a pure numeric ID, return directly
    if url.isdigit():
        return VideoUrlInfo(aweme_id=url, url_type="normal")

    # Check if it's a short link (v.douyin.com)
    if "v.douyin.com" in url or url.startswith("http") and len(url) < 50 and "video" not in url:
        return VideoUrlInfo(aweme_id="", url_type="short")  # Requires client parsing

    # Try to extract modal_id from URL parameters
    params = extract_url_params_to_dict(url)
    modal_id = params.get("modal_id")
    if modal_id:
        return VideoUrlInfo(aweme_id=modal_id, url_type="modal")

    # Extract ID from standard video URL: /video/number
    video_pattern = r'/video/(\d+)'
    match = re.search(video_pattern, url)
    if match:
        aweme_id = match.group(1)
        return VideoUrlInfo(aweme_id=aweme_id, url_type="normal")

    raise ValueError(f"Unable to parse video ID from URL: {url}")


# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
def decode_douyin_json_body(body: bytes) -> Dict[str, Any]:
    """Decode normal JSON or Douyin's raw HTTP-chunk-framed stream body."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload_bytes = bytearray()
        raw_chunks: list[bytes] = []
        cursor = 0
        while True:
            line_end = body.find(b"\r\n", cursor)
            if line_end < 0:
                raise SearchResponseError("invalid_stream_chunk_header")
            size_text = body[cursor:line_end].split(b";", maxsplit=1)[0].strip()
            try:
                chunk_size = int(size_text, 16)
            except ValueError as exc:
                raise SearchResponseError("invalid_stream_chunk_size") from exc
            cursor = line_end + 2
            if chunk_size == 0:
                break
            chunk_end = cursor + chunk_size
            if chunk_end > len(body) or body[chunk_end:chunk_end + 2] != b"\r\n":
                raise SearchResponseError("truncated_stream_chunk")
            chunk = body[cursor:chunk_end]
            raw_chunks.append(chunk)
            payload_bytes.extend(chunk)
            cursor = chunk_end + 2
        try:
            payload = json.loads(payload_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            documents = []
            try:
                for chunk in raw_chunks:
                    document = json.loads(chunk)
                    if not isinstance(document, dict):
                        raise TypeError("stream document is not an object")
                    documents.append(document)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as chunk_exc:
                raise SearchResponseError("invalid_stream_json") from chunk_exc
            if not documents:
                raise SearchResponseError("invalid_stream_json") from exc
            payload = dict(documents[-1])
            merged_data = []
            verify_search_nil_info = None
            for document in documents:
                document_data = document.get("data")
                if document_data is None:
                    continue
                if not isinstance(document_data, list):
                    raise SearchResponseError("invalid_stream_json") from exc
                merged_data.extend(document_data)
                if document.get("status_code") not in (None, 0, "0"):
                    payload["status_code"] = document["status_code"]
                search_nil_info = document.get("search_nil_info")
                if (
                    isinstance(search_nil_info, dict)
                    and search_nil_info.get("search_nil_type") == "verify_check"
                ):
                    verify_search_nil_info = search_nil_info
            payload["data"] = merged_data
            if verify_search_nil_info is not None:
                payload["search_nil_info"] = verify_search_nil_info
            for document in reversed(documents):
                if "has_more" in document:
                    payload["has_more"] = document["has_more"]
                    break
            for document in reversed(documents):
                if isinstance(document.get("extra"), dict):
                    payload["extra"] = document["extra"]
                    break
    if not isinstance(payload, dict):
        raise SearchResponseError("search_response_not_object")
    return payload


# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
def validate_douyin_search_response(payload: Any) -> Dict[str, Any]:
    """Reject parseable error envelopes before pagination interprets them as data."""
    if not isinstance(payload, dict):
        raise SearchResponseError("search_response_not_object")

    search_nil_info = payload.get("search_nil_info")
    if isinstance(search_nil_info, dict) and search_nil_info.get("search_nil_type") == "verify_check":
        raise SearchResponseError("search_verify_check")

    status_code = payload.get("status_code")
    if status_code not in (None, 0, "0"):
        raise SearchResponseError("search_business_status_nonzero")

    if "data" not in payload or payload.get("data") is None:
        raise SearchResponseError("missing_data_field")
    if not isinstance(payload.get("data"), list):
        raise SearchResponseError("invalid_data_field")

    if "has_more" not in payload:
        raise SearchResponseError("missing_has_more_field")
    has_more = payload.get("has_more")
    if not isinstance(has_more, (bool, int)) or has_more not in (False, True, 0, 1):
        raise SearchResponseError("invalid_has_more_field")

    extra = payload.get("extra")
    if extra is None:
        extra = {}
    if not isinstance(extra, dict):
        raise SearchResponseError("invalid_extra_field")
    next_search_id = extra.get("logid", "")
    if has_more in (True, 1) and not str(next_search_id or "").strip():
        raise SearchResponseError("missing_next_search_id")
    return payload


# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
def classify_empty_first_page(*, visible_result_count: int, visible_text: str) -> str:
    if visible_result_count > 0:
        return "visible_results"
    if any(marker in visible_text for marker in DOUYIN_NO_RESULT_MARKERS):
        return "explicit_no_results"
    return "ambiguous"


# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/douyin/__init__.py
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

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2024/1/14 18:46
# @Desc    :
# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
def _first_nonempty(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/douyin/__init__.py
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

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2024/1/14 18:46
# @Desc    :
# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
def _nested_value(data: Dict, *paths):
    for path in paths:
        current = data
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current not in (None, ""):
            return current
    return None


# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/douyin/__init__.py
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

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2024/1/14 18:46
# @Desc    :
# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
def _creator_user_profile(creator_profile: Dict) -> Dict:
    user = (
        creator_profile.get("user")
        or creator_profile.get("user_info")
        or creator_profile.get("author")
        or creator_profile.get("profile")
        or creator_profile
        or {}
    )
    return user if isinstance(user, dict) else {}


# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/douyin/__init__.py
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

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2024/1/14 18:46
# @Desc    :
# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
def _author_metric(author: Dict, creator_profile: Dict, *keys: str):
    profile_user = _creator_user_profile(creator_profile)
    candidates = []
    for key in keys:
        value = _first_nonempty(author.get(key), profile_user.get(key), creator_profile.get(key))
        if value not in (None, "") and not isinstance(value, (dict, list)):
            candidates.append(value)
    expected = {key.lower() for key in keys}

    def walk(data):
        if isinstance(data, dict):
            for key, value in data.items():
                if str(key).lower() in expected and value not in (None, "") and not isinstance(value, (dict, list)):
                    candidates.append(value)
                elif isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(data, list):
            for item in data:
                walk(item)

    walk(creator_profile)
    for value in candidates:
        if str(value) != "0":
            return value
    return candidates[0] if candidates else None


# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/douyin/__init__.py
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

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2024/1/14 18:46
# @Desc    :
# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
def _normalized_author_stats(author: Dict, creator_profile: Dict) -> Dict:
    follower_keys = (
        "follower_count",
        "followers_count",
        "mplatform_followers_count",
        "fans_count",
        "fans",
    )
    followers_count = _author_metric(
        author,
        creator_profile,
        *follower_keys,
    )
    expected = {key.lower() for key in follower_keys}

    def observed(data: Any) -> bool:
        if isinstance(data, dict):
            for key, value in data.items():
                if str(key).lower() in expected and value not in (None, "") and not isinstance(value, (dict, list)):
                    return True
                if isinstance(value, (dict, list)) and observed(value):
                    return True
        elif isinstance(data, list):
            return any(observed(item) for item in data)
        return False

    profile_observed = observed(creator_profile)
    search_observed = observed(author)
    followers_observed = profile_observed or search_observed
    return {
        "followers_count": followers_count,
        "fans_count": followers_count,
        "following_count": _author_metric(author, creator_profile, "following_count", "follow_count", "follows"),
        "aweme_count": _author_metric(author, creator_profile, "aweme_count", "post_count", "posts_count", "video_count"),
        "author_liked_count": _author_metric(author, creator_profile, "total_favorited", "favorited_count", "liked_count"),
        "followers_observed": followers_observed,
        "author_followers_source": "creator_profile" if profile_observed else ("search_author" if search_observed else "missing"),
        "author_followers_zero_suspicious": str(followers_count) == "0" and not profile_observed,
    }


# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/douyin/__init__.py
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

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2024/1/14 18:46
# @Desc    :
# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
def _extract_note_image_list(aweme_detail: Dict) -> List[str]:
    """
    Extract note image list

    Args:
        aweme_detail (Dict): Douyin content details

    Returns:
        List[str]: Note image list
    """
    return [asset["url"] for asset in _extract_note_image_assets(aweme_detail)]


# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/douyin/__init__.py
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

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2024/1/14 18:46
# @Desc    :
# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
def _extract_note_image_assets(aweme_detail: Dict) -> List[Dict]:
    """Choose one fresh signed URL per note image and preserve its stable URI."""

    assets: List[Dict] = []
    seen = set()
    images: List[Dict] = aweme_detail.get("images", [])
    if not isinstance(images, list):
        return []
    for image in images:
        if not isinstance(image, dict):
            continue
        image_url_list = image.get("url_list") or []
        if not isinstance(image_url_list, list):
            continue
        url = next((str(value).strip() for value in reversed(image_url_list) if value), "")
        try:
            url = normalize_image_url(url)
        except ImageStagingError:
            continue
        uri = _first_nonempty(
            image.get("uri"),
            image.get("image_uri"),
            _nested_value(image, ("display_image", "uri"), ("origin_image", "uri")),
        )
        asset_key = douyin_source_asset_key(uri, url)
        if asset_key in seen:
            continue
        seen.add(asset_key)
        assets.append(
            {
                "uri": str(uri or ""),
                "url": url,
                "source_index": len(assets),
                "source_asset_key": asset_key,
            }
        )
    return assets


# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/douyin/__init__.py
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

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2024/1/14 18:46
# @Desc    :
# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
def _extract_content_cover_url(aweme_detail: Dict) -> str:
    """
    Extract video cover URL

    Args:
        aweme_detail (Dict): Douyin content details

    Returns:
        str: Video cover URL
    """
    res_cover_url = ""

    video_item = aweme_detail.get("video", {})
    raw_cover_url_list = (video_item.get("raw_cover", {}) or video_item.get("origin_cover", {})).get("url_list", [])
    if raw_cover_url_list and len(raw_cover_url_list) > 1:
        res_cover_url = raw_cover_url_list[1]

    return res_cover_url


# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/douyin/__init__.py
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

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2024/1/14 18:46
# @Desc    :
# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
def _extract_video_download_url(aweme_detail: Dict) -> str:
    """
    Extract video download URL

    Args:
        aweme_detail (Dict): Douyin video

    Returns:
        str: Video download URL
    """
    video_item = aweme_detail.get("video", {})
    url_h264_list = video_item.get("play_addr_h264", {}).get("url_list", [])
    url_256_list = video_item.get("play_addr_256", {}).get("url_list", [])
    url_list = video_item.get("play_addr", {}).get("url_list", [])
    actual_url_list = url_h264_list or url_256_list or url_list
    if not actual_url_list or len(actual_url_list) < 2:
        return ""
    return actual_url_list[-1]


# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/douyin/__init__.py
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

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2024/1/14 18:46
# @Desc    :
# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
def _extract_music_download_url(aweme_detail: Dict) -> str:
    """
    Extract music download URL

    Args:
        aweme_detail (Dict): Douyin video

    Returns:
        str: Music download URL
    """
    music_item = aweme_detail.get("music", {})
    play_url = music_item.get("play_url", {})
    music_url = play_url.get("uri", "")
    return music_url


# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/douyin/__init__.py
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

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2024/1/14 18:46
# @Desc    :
# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
def update_douyin_aweme(aweme_item: Dict, *, source_keyword: str, current_timestamp: Callable[[], int], save_data_option: str, enable_get_medias: bool) -> Dict:
    aweme_id = aweme_item.get("aweme_id")
    user_info = aweme_item.get("author", {})
    creator_profile = aweme_item.get("creator_profile") or {}
    author_stats = _normalized_author_stats(user_info, creator_profile)
    interact_info = aweme_item.get("statistics", {})
    image_assets = _extract_note_image_assets(aweme_item)
    save_content_item = {
        "aweme_id": aweme_id,
        "aweme_type": str(aweme_item.get("aweme_type")),
        "title": aweme_item.get("desc", ""),
        "desc": aweme_item.get("desc", ""),
        "create_time": aweme_item.get("create_time"),
        "creator_hash": anonymize_user_id(user_info.get("uid")),  # 创作者匿名哈希(不存原始 uid)
        "nickname": mask_nickname(user_info.get("nickname")),  # 用户昵称(已脱敏)
        "followers_count": author_stats.get("followers_count"),
        "fans_count": author_stats.get("fans_count"),
        "followers_observed": author_stats.get("followers_observed"),
        "following_count": author_stats.get("following_count"),
        "aweme_count": author_stats.get("aweme_count"),
        "author_liked_count": author_stats.get("author_liked_count"),
        "author_followers_source": author_stats.get("author_followers_source"),
        "author_followers_zero_suspicious": author_stats.get("author_followers_zero_suspicious"),
        "liked_count": str(interact_info.get("digg_count")),
        "collected_count": str(interact_info.get("collect_count")),
        "comment_count": str(interact_info.get("comment_count")),
        "share_count": str(interact_info.get("share_count")),
        "last_modify_ts": current_timestamp(),
        "aweme_url": f"https://www.douyin.com/video/{aweme_id}",
        "cover_url": _extract_content_cover_url(aweme_item),
        "video_download_url": _extract_video_download_url(aweme_item),
        "music_download_url": _extract_music_download_url(aweme_item),
        "note_download_url": ",".join(asset["url"] for asset in image_assets),
        "source_keyword": source_keyword,
    }
    if save_data_option == "jsonl" and enable_get_medias:
        save_content_item["image_assets"] = image_assets
        save_content_item["image_list_source"] = "aweme.images"
    if save_data_option == "jsonl":
        save_content_item["content_detail_status"] = "detail_observed"
        save_content_item["content_detail_source"] = "aweme_detail"
    return save_content_item


# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
def douyin_source_asset_key(uri: str | None, source_url: str) -> str:
    if uri not in (None, ""):
        return f"douyin:uri:{str(uri).strip()}"
    normalized = normalize_image_url(source_url)
    digest = sha256(urlsplit(normalized).path.encode("utf-8")).hexdigest()
    return f"douyin:urlsha256:{digest}"


def _douyin_detail_urls(aweme_id: str) -> tuple[str, str]:
    return (
        f"https://www.douyin.com/note/{aweme_id}",
        f"https://www.douyin.com/video/{aweme_id}",
    )
