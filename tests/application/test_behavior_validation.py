"""行为门禁：target_url_ok 逐层解码，以及访问门禁结果并入 behavior_validation / formal_validation。"""

from __future__ import annotations

from urllib.parse import quote, unquote

import pytest

from trippostcollect.application import reporting
from trippostcollect.application.collection import apply_formal_completion_gates
from trippostcollect.application.reporting import (
    TARGET_URL_MAX_UNQUOTE,
    _url_contains_keyword,
    collect_behavior_validation,
)


KEYWORD = "青岛旅游"


def encoded(times: int) -> str:
    value = KEYWORD
    for _ in range(times):
        value = quote(value, safe="")
    return f"https://www.xiaohongshu.com/search_result_ai?keyword={value}&source=web_explore_feed"


@pytest.mark.parametrize("times,expected", [(0, True), (1, True), (2, True), (3, True), (4, False)])
def test_target_url_decodes_up_to_three_layers(times, expected):
    assert TARGET_URL_MAX_UNQUOTE == 3
    assert _url_contains_keyword(encoded(times), KEYWORD) is expected


@pytest.mark.parametrize(
    "url,keyword",
    [
        ("https://s.weibo.com/weibo?q=%E9%9D%92%E5%B2%9B", "青岛"),
        ("https://www.zhihu.com/search?q=%E9%9D%92%E5%B2%9B&type=content", "青岛"),
        ("https://www.douyin.com/search/%E9%9D%92%E5%B2%9B", "青岛"),
        ("https://search.bilibili.com/all?keyword=%E5%B4%82%E5%B1%B1", "青岛"),
        ("https://www.zhihu.com/search?q=100%25%E9%9D%92%E5%B2%9B", "100%青岛"),
        ("https://www.zhihu.com/search?q=100%E9%9D%92%E5%B2%9B", "100%青岛"),
        ("https://www.zhihu.com/search?q=a%2541", "a%41"),
        ("https://www.zhihu.com/search", ""),
    ],
)
def test_single_decode_results_never_turn_false(url, keyword):
    previous = bool(keyword) and keyword in unquote(url)
    current = _url_contains_keyword(url, keyword)
    assert current >= previous
    if previous:
        assert current is True


def test_keyword_absent_is_false():
    assert _url_contains_keyword("https://www.xiaohongshu.com/explore", KEYWORD) is False
    assert _url_contains_keyword(encoded(2), "") is False


def evidence(platform: str, *, pacing=True, url=None) -> dict:
    return {
        "status": "completed",
        "profile": "xhs_guarded" if platform == "xhs" else "social_high_risk",
        "url": url or encoded(2),
        "events": [{"event": "pause"}, {"event": "mouse_moves"},
                   {"event": "human_scroll_complete", "effective_passes": 1}],
        "page_readiness": {"ready": True},
        "runtime_fingerprint": {"webdriver": None, "languages": ["zh-CN"], "platform": "MacIntel",
                                "user_agent": "test", "visibility_state": "visible",
                                "viewport": {"width": 1280, "height": 900}},
        # 长轮次中被截断的行为证据：早期的必需节奏阶段已不在最后 200 条里。
        "request_pacing_events": (
            [{"stage": stage} for stage in ("search_results", "note_detail", "creator_profile")] if pacing else []
        ),
        "continuity_events": [{"stage": "search_results", "status": "completed"}] if pacing else [],
    }


def platform_record(platform: str, **kwargs) -> dict:
    return {
        "platform": platform,
        "behavior_evidence": evidence(platform, **kwargs),
        "policy_events": [{"allowed": True}],
    }


def visits(*, ok=True, pacing=True, continuity=True) -> dict:
    return {"content_visit_ok": ok, "request_pacing_ok": pacing, "continuity_ok": continuity,
            "problem_counts": {} if ok else {"R3": 1}}


def test_double_encoded_xhs_result_page_passes_target_url():
    result = collect_behavior_validation([platform_record("xhs")], ["xhs"], KEYWORD)
    assert result["platforms"]["xhs"]["target_url_ok"] is True
    assert result["behavior_ok"] is True
    assert result["content_visit_ok"] is None and result["platforms"]["xhs"]["content_visit_ok"] is None


def test_gated_platform_takes_pacing_and_continuity_from_the_ledger():
    record = platform_record("xhs", pacing=False)
    without = collect_behavior_validation([record], ["xhs"], KEYWORD)
    assert without["platforms"]["xhs"]["request_pacing_ok"] is False and without["behavior_ok"] is False
    gated = collect_behavior_validation([record], ["xhs"], KEYWORD, content_visits={"xhs": visits()})
    platform = gated["platforms"]["xhs"]
    assert platform["request_pacing_ok"] is True and platform["continuity_ok"] is True
    assert platform["content_visit_ok"] is True and platform["content_visit_validation"] == visits()
    assert gated["behavior_ok"] is True and gated["ok"] is True and gated["content_visit_ok"] is True


@pytest.mark.parametrize(
    "result",
    [visits(ok=False), visits(pacing=False), visits(continuity=False)],
    ids=["content_visit", "pacing", "continuity"],
)
def test_gated_platform_failures_fail_behavior(result):
    gated = collect_behavior_validation([platform_record("xhs")], ["xhs"], KEYWORD, content_visits={"xhs": result})
    assert gated["behavior_ok"] is False and gated["ok"] is False
    assert gated["content_visit_ok"] is result["content_visit_ok"]


def test_gated_platform_without_visit_results_fails_closed(monkeypatch):
    monkeypatch.setattr(reporting, "VISIT_GATED_PLATFORMS", frozenset({"xhs"}))
    records = [platform_record("xhs"), platform_record("zhihu", url=encoded(1))]
    result = collect_behavior_validation(records, ["xhs", "zhihu"], KEYWORD)
    xhs = result["platforms"]["xhs"]
    assert xhs["content_visit_ok"] is False
    assert xhs["content_visit_validation"] == {"content_visit_ok": False, "problem": "visit_validation_missing"}
    assert xhs["request_pacing_ok"] is False and xhs["behavior_ok"] is False
    assert result["platforms"]["zhihu"]["content_visit_ok"] is None
    assert result["platforms"]["zhihu"]["behavior_ok"] is True
    assert result["content_visit_ok"] is False and result["behavior_ok"] is False and result["ok"] is False
    passed = collect_behavior_validation(records, ["xhs", "zhihu"], KEYWORD, content_visits={"xhs": visits()})
    assert passed["content_visit_ok"] is True and passed["ok"] is True


def test_ungated_platforms_are_unchanged():
    records = [platform_record("zhihu", url="https://www.zhihu.com/search?q=%E9%9D%92%E5%B2%9B%E6%97%85%E6%B8%B8")]
    result = collect_behavior_validation(records, ["zhihu"], KEYWORD, content_visits={"xhs": visits(ok=False)})
    assert result["platforms"]["zhihu"]["content_visit_ok"] is None
    assert result["behavior_ok"] is True and result["content_visit_ok"] is None


def test_formal_validation_records_content_visit_ok():
    base = {"completion_met": True, "repair_import_met": False, "stop_reason": "source_exhausted"}
    content = {"completion_met": True}
    images = {"complete": True}
    failed = collect_behavior_validation([platform_record("xhs")], ["xhs"], KEYWORD,
                                         content_visits={"xhs": visits(ok=False)})
    gated = apply_formal_completion_gates(base, content_validation=content, image_materialization=images,
                                          behavior_validation=failed, download_images=True)
    assert gated["content_visit_ok"] is False and gated["behavior_evidence_ok"] is False
    assert gated["completion_met"] is False and gated["stop_reason"] == "behavior_evidence_failed"
    plain = collect_behavior_validation([platform_record("zhihu", url=encoded(1))], ["zhihu"], KEYWORD)
    gated = apply_formal_completion_gates(base, content_validation=content, image_materialization=images,
                                          behavior_validation=plain, download_images=True)
    assert gated["content_visit_ok"] is None and gated["completion_met"] is True
