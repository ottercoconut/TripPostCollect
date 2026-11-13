"""#185 有意偏离：知乎搜索改为页面截获，每轮从第一页滚到 paging.is_end。

固化文件不改、不重新生成（见 ``support.legacy_expectations``）。根侧比较对已经过 #113/#127/#133/#182
变换的预期再做“钉住旧值 → 单向替换”：

1. ``/api/v4/me`` 与 ``/api/v4/search_v3`` 不再由 httpx 发出。删除这些 request，以及紧挨在前的
   ``http_client``、首次请求前的 ``sign``，和 search_v3 重试之间的 ``sleep 1.0``。
2. ``compile`` 只为第一次签名发生。它原先紧挨在 ``/me`` 的 sign 之前；若后面还有详情签名，就挪到
   第一条剩余 sign 之前，否则连同 ``/me`` 一起删除（登录失败、空页、搜索阻断、搜索正文已完整）。
3. 产物、停止事件和详情/图片请求保持：页面自己给出的成功响应与旧成功路径的载荷相同；429 等状态改由
   截获响应分类，不再出现在 httpx 轨迹里。

T11 冻结执行器里，知乎与抖音一样拒绝深层恢复参数。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


CAPTURED_PATHS = {"/api/v4/me", "/api/v4/search_v3"}

# 冻结执行器经 #124 变换后与现行 collection.py 相同的抖音拒绝块，再纳入知乎。
T11_REPLACEMENTS = (
    (
        '''    if "douyin" in platforms and (
        args.start_page != 1
        or args.top_refresh_max_pages > 0
        or args.discovery_source_exhausted
    ):
        # #124：抖音每轮从首页全量翻到 has_more=false，旧的深层恢复与顶部刷新参数直接视为错误。
        raise SystemExit("douyin_full_scan_rejects_resume_args")
''',
        '''    for platform_key in ("douyin", "zhihu"):
        if platform_key in platforms and (
            args.start_page != 1
            or args.top_refresh_max_pages > 0
            or args.discovery_source_exhausted
        ):
            # #124/#185：抖音与知乎每轮从首页全量扫描，旧的深层恢复与顶部刷新参数直接视为错误。
            raise SystemExit(f"{platform_key}_full_scan_rejects_resume_args")
''',
    ),
)


def _path(row) -> str:
    return urlsplit(row[2]).path


def _drop_captured_http(trace: list) -> list:
    """1/2：删除 /me 与 search_v3 的 httpx 组，并把首次 compile 挪到剩余的第一条 sign 前。"""

    drop: set[int] = set()
    compile_index = None
    for index, row in enumerate(trace):
        if row[0] != "request" or _path(row) not in CAPTURED_PATHS:
            continue
        drop.add(index)
        client = index - 1
        assert trace[client][0] == "http_client", trace[client]
        drop.add(client)
        previous = client - 1
        if trace[previous][0] == "sign":
            drop.add(previous)
            if previous >= 1 and trace[previous - 1][0] == "compile":
                assert compile_index is None
                compile_index = previous - 1
                drop.add(compile_index)
        elif trace[previous][0] == "sleep" and trace[previous][1] == 1.0:
            drop.add(previous)
        else:
            raise AssertionError(trace[previous])
    kept = [row for index, row in enumerate(trace) if index not in drop]
    if compile_index is None:
        return kept
    assert not any(row[0] == "compile" for row in kept)
    for index, row in enumerate(kept):
        if row[0] == "sign":
            return [*kept[:index], trace[compile_index], *kept[index:]]
    return kept


def zhihu_drive(expected: dict[str, Any], scenario: str) -> dict[str, Any]:
    """T07 一个场景：搜索与登录复验不再出现在 httpx 轨迹里。"""

    assert expected["counts"].get("/api/v4/me") == 1, scenario
    trace = _drop_captured_http(expected["trace"])
    assert trace != expected["trace"], scenario
    assert not any(row[0] == "request" and _path(row) in CAPTURED_PATHS for row in trace)
    counts = {
        key: value for key, value in expected["counts"].items() if key not in CAPTURED_PATHS
    }
    return {**expected, "trace": trace, "counts": counts}


def t11_source(text: str) -> str:
    """冻结执行器源码的 #185 变换：知乎与抖音同样拒绝深层恢复参数。"""

    for old, new in T11_REPLACEMENTS:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text
