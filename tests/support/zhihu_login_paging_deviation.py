"""#113 有意偏离：知乎登录态只取持久 profile、来源耗尽只认 paging.is_end、阻断码不重试、图片请求不带会话。

固化预期（``tests/fixtures/t14_legacy_expectations/T07``）与 manifest 不改、不用根实现重新生成。根侧比较对加载的
预期逐项做“钉住旧值 → 单向替换为新值”：每条变换先断言旧值确实出现在预期的位置与次数，再换成新值；
其余部分仍按原对照整体 ``==`` 比较。drive 的 search_v3 替身另补 ``paging``（末页 ``is_end=true``），
HTTP 次序与旧预期相同。

1. 不向浏览器注入快照 Cookie：删除首页后的逐个 ``add_cookies`` 与随后的 ``reload``。
2. 401/403/429 是平台确定答复，不再由 Tenacity 重试：同一路径的三次请求收为一次，删除两组
   ``sleep 1.0 → http_client → request``，counts 同步改为 1。
3. 搜索阶段保留 ``PlatformRuntimeError.code``：429 的停止细节由 ``search_request_failed`` 改为 ``rate_limited``。
4. 来源耗尽只认原始 ``paging.is_end``：停止细节 ``empty_page`` 改为 ``paging_is_end``；批次的
   ``source_has_more`` 取自响应（未到末页为 ``True``），停止事件已是 ``False``。
5. zhimg 图片请求只发 UA、Accept 与站点 Referer，不带 Cookie、签名与 API 头。
6. ``login_expired``：/me 复验失败即以 ``login_required`` 运行级阻断结束，不再重新注入 Cookie、不搜索；
   整段业务都已不存在，不做变换，由 ``login_expired_expectation`` 给出字面预期。
7. API 请求的 Referer 由写死的 ``q=python`` 改为本轮实际检索词的搜索页。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

IMAGE_REFERER = "https://www.zhihu.com/"
LEGACY_API_REFERER = "https://www.zhihu.com/search?q=python&time_interval=a_year&type=content"
API_REFERER = "https://www.zhihu.com/search?q=" + quote("青岛旅游") + "&type=content"
# 2：各场景中被阻断码重试的路径与旧预期的请求次数。
NO_RETRY_PATHS = {
    "detail_401": ("/question/10/answer/101", "/p/102"),
    "detail_429": ("/question/10/answer/101", "/p/102"),
    "search_block": ("/api/v4/search_v3",),
    "search_detail_401": ("/question/10/answer/101",),
    "search_detail_429": ("/question/10/answer/101",),
}
SEARCH_CODE = {"search_block": ("search_request_failed", "rate_limited")}


def _drop_cookie_injection(trace: list) -> list:
    """1：旧预期中 add_cookies 恰为 d_c0、z_c0 两次，紧随一次 reload；全部删除。"""
    injected = [index for index, row in enumerate(trace) if row[0] == "add_cookies"]
    assert [trace[index][1][0]["name"] for index in injected] == ["d_c0", "z_c0"], injected
    reloads = [index for index, row in enumerate(trace) if row[0] == "reload"]
    assert reloads == [injected[-1] + 1], reloads
    removed = set(injected) | set(reloads)
    return [row for index, row in enumerate(trace) if index not in removed]


def _request_path(row) -> str:
    from urllib.parse import urlsplit

    return urlsplit(row[2]).path


def _collapse_retries(trace: list, paths: tuple[str, ...]) -> list:
    """2：每个路径恰有三次 request，彼此之间是 sleep 1.0 + http_client；只留第一次。"""
    result = list(trace)
    for path in paths:
        hits = [index for index, row in enumerate(result) if row[0] == "request" and _request_path(row) == path]
        assert len(hits) == 3, (path, hits)
        removed = set()
        for previous, current in zip(hits, hits[1:]):
            assert [row[0] for row in result[previous + 1:current + 1]] == ["sleep", "http_client", "request"], path
            assert result[previous + 1][1] == 1.0, path
            removed.update(range(previous + 1, current + 1))
        result = [row for index, row in enumerate(result) if index not in removed]
    return result


def _image_headers(trace: list) -> list:
    """5：旧图片请求复制了整份 API 头（含 Cookie）；新请求只留白名单三项，Referer 固定为站点首页。"""
    result = []
    for row in trace:
        if row[0] == "image":
            headers = row[3]["headers"]
            assert "cookie" in headers and "x-zse-93" in headers, headers
            row = (*row[:3], {**row[3], "headers": {
                "user-agent": headers["user-agent"], "accept": headers["accept"], "referer": IMAGE_REFERER,
            }}, *row[4:])
        result.append(row)
    return result


def _api_referer(trace: list) -> list:
    """7：每条 API request 的 Referer 恰为旧的 q=python 搜索页，换成本轮检索词。"""
    result = []
    for row in trace:
        if row[0] == "request":
            headers = row[3]["headers"]
            assert headers["referer"] == LEGACY_API_REFERER, headers["referer"]
            row = (*row[:3], {**row[3], "headers": {**headers, "referer": API_REFERER}}, *row[4:])
        result.append(row)
    return result


def _events(events: list, scenario: str) -> list:
    """3/4：停止细节与批次 source_has_more。"""
    result = []
    for event in events:
        details = dict(event["details"])
        if event["type"] == "adaptive_search_stopped" and details.get("stop_reason") == "source_exhausted":
            assert details["stop_detail"] == "empty_page", details
            assert details["source_has_more"] is False, details
            details["stop_detail"] = "paging_is_end"
        if scenario in SEARCH_CODE and event["type"] == "adaptive_search_stopped":
            old, new = SEARCH_CODE[scenario]
            assert details["stop_detail"] == old, details
            details["stop_detail"] = new
        if event["type"] == "adaptive_batch_completed":
            assert details["source_has_more"] is None, details
            details["source_has_more"] = True
        if (event["type"] == "adaptive_search_stopped" and details.get("stop_reason") == "runtime_failed"
                and details.get("raw_batch_count") not in (None, 0)):
            # 批次内运行级阻断：停止事件沿用批次记录的 source_has_more。
            assert details["source_has_more"] is None, details
            details["source_has_more"] = True
        result.append({**event, "details": details})
    return result


def zhihu_login_paging(expected: dict[str, Any], scenario: str) -> dict[str, Any]:
    """对 T07 一个场景的固化预期施加 #113 偏离；login_expired 不经此函数。"""
    assert scenario != "login_expired"
    trace = _drop_cookie_injection(expected["trace"])
    counts = dict(expected["counts"])
    for path in NO_RETRY_PATHS.get(scenario, ()):
        assert counts[path] == 3, (scenario, path, counts)
        counts[path] = 1
    trace = _collapse_retries(trace, NO_RETRY_PATHS.get(scenario, ()))
    trace = _api_referer(_image_headers(trace))
    return {**expected, "trace": trace, "counts": counts, "events": _events(expected["events"], scenario)}


def login_expired_expectation(expected: dict[str, Any]) -> dict[str, Any]:
    """6：旧预期先断言确曾三次 /me 后重新注入并完成整轮；新行为在首个 /me 401 后即停止。"""
    assert expected["counts"]["/api/v4/me"] == 3
    assert [row[0] for row in expected["trace"]].count("add_cookies") == 4
    head = []
    for row in _api_referer(_drop_cookie_injection(expected["trace"][: _second_injection(expected["trace"])])):
        head.append(row)
        if row[0] == "request":
            break
    return {
        "counts": {"/api/v4/me": 1},
        "events": [{
            "at": expected["events"][-1]["at"],
            "type": "adaptive_search_stopped",
            "details": {
                "platform": "zhihu", "candidate_count": 0, "valid_new_count": 0, "valid_existing_count": 0,
                "stagnant_batches": 0, "stagnation_basis": "valid_new", "stop_reason": "runtime_failed",
                "pages_fetched": 0, "source_page": 1, "source_offset": None, "source_cursor": None,
                "next_cursor": None, "source_has_more": None, "raw_batch_count": None, "resume_page": 1,
                "resume_offset": None, "resume_cursor": None, "batch_complete": False,
                "discovery_phase": "frontier", "stop_detail": "login_required",
                "skipped_candidate_count": 0, "skipped_candidate_failures": [], "candidate_identities": [],
            },
        }],
        "files": {},
        "records": [],
        "trace": [*head, *expected["trace"][-2:]],
    }


def _second_injection(trace: list) -> int:
    injected = [index for index, row in enumerate(trace) if row[0] == "add_cookies"]
    assert len(injected) == 4, injected
    return injected[2]
