"""小红书目标搜索的定义：父层查询指纹与 child 扫描共用，只依赖标准库。

目标是前端默认排序“综合”（回车后不点排序）且只看“图文”（在筛选面板点选）。目标会话只经单一谓词
`is_target_search` 识别；请求体取值按页面实测定值（issuecomment-6107337442），未知取值一律判假。
"""

from __future__ import annotations

import json
from typing import Any, Mapping


# 语义标签，并入查询指纹；不是请求体原值。
XHS_TARGET_SORT = "general"  # 前端默认“综合”
XHS_TARGET_NOTE_TYPE = "image"  # “图文”
XHS_TARGET_FILTER_LABEL = "图文"  # 筛选面板中要点选的选项文字
# 请求体中的表示：界面叫“图文”，接口取值是“普通笔记”。只进谓词，不进指纹。
XHS_FILTER_NOTE_TYPE_KEY = "filter_note_type"
XHS_FILTER_NOTE_TYPE_IMAGE_TAG = "普通笔记"
XHS_FILTER_DEFAULT_TAGS: dict[str, tuple[str, ...]] = {
    "sort_type": ("general",),
    "filter_note_time": ("不限",),
    "filter_note_range": ("不限",),
    "filter_pos_distance": ("不限",),
}


def is_target_search(request_json: Any) -> bool:
    """排序为 general，filters 恰有一项“普通笔记”类型，其余各项都是不重复的已知类型且取默认值。

    顶层 note_type 在回车会话与目标会话中都是 0，不作判据；没有 filters 的回车会话判假。
    """
    if not isinstance(request_json, Mapping) or request_json.get("sort") != XHS_TARGET_SORT:
        return False
    filters = request_json.get("filters")
    if not isinstance(filters, list):
        return False
    seen: set[str] = set()
    note_type_selected = False
    for item in filters:
        if not isinstance(item, Mapping):
            return False
        kind = item.get("type")
        tags = item.get("tags")
        if not isinstance(kind, str) or kind in seen or not isinstance(tags, list):
            return False
        seen.add(kind)
        if kind == XHS_FILTER_NOTE_TYPE_KEY:
            if tags != [XHS_FILTER_NOTE_TYPE_IMAGE_TAG]:
                return False
            note_type_selected = True
        elif kind in XHS_FILTER_DEFAULT_TAGS:
            if tuple(tags) != XHS_FILTER_DEFAULT_TAGS[kind]:
                return False
        else:
            return False
    return note_type_selected


def search_filter_signature(request_json: Any) -> str:
    """按 filters 计算的会话签名：各项按 type 排序后的 (type, tags) 规范化 JSON；不含关键词与 search_id。"""
    filters = request_json.get("filters") if isinstance(request_json, Mapping) else None
    if filters is None:
        return "none"
    if not isinstance(filters, list):
        return "invalid"
    pairs = sorted(
        (
            str(item.get("type") or ""),
            [str(tag) for tag in item.get("tags") or []] if isinstance(item.get("tags"), list) else [],
        )
        for item in filters
        if isinstance(item, Mapping)
    )
    return json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))


def target_search_fingerprint_params() -> dict[str, str]:
    """查询指纹只用语义标签：目标排序或类型改变时形成新记忆。"""
    return {"search_sort": XHS_TARGET_SORT, "search_note_type": XHS_TARGET_NOTE_TYPE}
