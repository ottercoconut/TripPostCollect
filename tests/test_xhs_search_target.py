"""小红书目标搜索定义：目标会话谓词、筛选签名与查询指纹参数（父层与 child 共用，只依赖标准库）。"""

from __future__ import annotations

import copy
import json

import pytest

from trippostcollect.xhs.search_target import (
    XHS_FILTER_DEFAULT_TAGS,
    XHS_TARGET_FILTER_LABEL,
    is_target_search,
    search_filter_signature,
    target_search_fingerprint_params,
)

# V16 实测的回车会话与点选“图文”后的目标会话请求体（只保留判定相关字段）。
ENTER_REQUEST = {"keyword": "青岛旅行", "page": 1, "search_id": "2f1a", "sort": "general", "note_type": 0}
TARGET_REQUEST = {
    **ENTER_REQUEST,
    "search_id": "2f1b",
    "filters": [
        {"type": "sort_type", "tags": ["general"]},
        {"type": "filter_note_type", "tags": ["普通笔记"]},
        {"type": "filter_note_time", "tags": ["不限"]},
        {"type": "filter_note_range", "tags": ["不限"]},
        {"type": "filter_pos_distance", "tags": ["不限"]},
    ],
}


def test_enter_session_without_filters_is_not_target_and_signs_as_none():
    assert is_target_search(ENTER_REQUEST) is False
    assert search_filter_signature(ENTER_REQUEST) == "none"


def test_measured_target_request_is_the_target_session():
    assert is_target_search(TARGET_REQUEST) is True
    assert XHS_TARGET_FILTER_LABEL == "图文"


def test_target_needs_only_the_note_type_filter():
    minimal = {**ENTER_REQUEST, "filters": [{"type": "filter_note_type", "tags": ["普通笔记"]}]}
    assert is_target_search(minimal) is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update(sort="time_descending"),
        lambda body: body["filters"][1].update(tags=["视频笔记"]),
        lambda body: body["filters"][2].update(tags=["一天内"]),
        lambda body: body["filters"].append({"type": "filter_unknown", "tags": ["不限"]}),
        lambda body: body["filters"].append({"type": "filter_note_time", "tags": ["不限"]}),
        lambda body: body["filters"].pop(1),
        lambda body: body.update(filters={"type": "filter_note_type"}),
        lambda body: body["filters"].insert(0, "filter_note_type"),
        lambda body: body["filters"][1].update(tags="普通笔记"),
    ],
    ids=[
        "sort", "video_type", "non_default_time", "unknown_type", "duplicate_type",
        "no_note_type", "filters_not_list", "item_not_object", "tags_not_list",
    ],
)
def test_unknown_or_non_default_values_are_never_target(mutate):
    body = copy.deepcopy(TARGET_REQUEST)
    mutate(body)
    assert is_target_search(body) is False


def test_top_level_note_type_is_not_a_criterion():
    assert is_target_search({**TARGET_REQUEST, "note_type": 2}) is True


def test_signature_is_order_independent_and_excludes_keyword_and_search_id():
    reordered = {**TARGET_REQUEST, "filters": list(reversed(TARGET_REQUEST["filters"]))}
    other = {**TARGET_REQUEST, "keyword": "崂山", "search_id": "other", "sort": "time_descending"}
    signature = search_filter_signature(TARGET_REQUEST)
    assert search_filter_signature(reordered) == signature == search_filter_signature(other)
    assert "青岛" not in signature and "2f1b" not in signature
    assert json.loads(signature)[0] == ["filter_note_range", ["不限"]]
    assert search_filter_signature(ENTER_REQUEST) != signature


def test_fingerprint_parameters_are_semantic_labels_not_request_values():
    params = target_search_fingerprint_params()
    assert params == {"search_sort": "general", "search_note_type": "image"}
    assert "普通笔记" not in json.dumps(params, ensure_ascii=False)
    assert set(XHS_FILTER_DEFAULT_TAGS) == {"sort_type", "filter_note_time", "filter_note_range", "filter_pos_distance"}
