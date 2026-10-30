"""#124 有意偏离：抖音每轮从首页全量翻到 has_more=false，搜索只经浏览器截获。

固化文件不改、不重新生成（见 ``support.legacy_expectations``）。与 ``douyin_116_deviation`` 同样，根侧比较对
加载的预期做“钉住旧值 → 单向替换为新值”的变换，其余部分仍整体 ``==`` 比较。T06 drive 的页面替身同步改为
“页面自身请求”：进入搜索页即发出首页 /stream/ 响应，每次可见滚轮让页面加载下一页（真实浏览器的行为）；
搜索载荷、作者资料、详情与图片替身不变。

1. 首页不再由客户端直接请求：旧预期中紧随 ``cookies`` 的 ``/general/search/stream/`` 请求行删除，
   首页改由进入搜索页时截获的浏览器响应提供。
2. 后续页不再在 8 次滚轮后回退到直接请求：旧预期中每个 ``/general/search/single/`` 请求行之前恰有 8 组
   相同的 ``move → wheel → wait``；连续翻页时页面一屏就加载下一页，变换为 1 组滚轮，请求行删除。
3. 事件、内容 JSONL、图片字节与 manifest 不变：截获响应与旧直接请求的载荷逐字节相同。
4. 首页可见卡片重建（``_build_visible_first_page_fallback``）随直接请求退出：首页截获不到时一律
   ``browser_search_response_missing``，不请求任何详情。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit


STREAM_PATH = "/aweme/v1/web/general/search/stream/"
SINGLE_PATH = "/aweme/v1/web/general/search/single/"
SCROLL_KINDS = ("move", "wheel", "wait")
LEGACY_SCROLL_BUDGET = 8
MISSING = "browser_search_response_missing"


def _path(row) -> str:
    return urlsplit(row[2]).path


def _search_trace(trace: list) -> list:
    """1/2：删除首页直接请求；每个后续页直接请求连同前 7 组滚轮删除，只留一组。"""
    result: list = []
    for row in trace:
        if row[0] != "request" or "/general/search/" not in row[2]:
            result.append(row)
            continue
        path = _path(row)
        query = parse_qs(urlsplit(row[2]).query)
        if path == STREAM_PATH:
            assert query["offset"] == ["0"] and query.get("search_id", [""]) == [""], row
            assert result and result[-1][0] == "cookies", result[-1:]
            continue
        assert path == SINGLE_PATH and int(query["offset"][0]) > 0, row
        width = len(SCROLL_KINDS) * LEGACY_SCROLL_BUDGET
        scrolls = result[-width:]
        assert [item[0] for item in scrolls] == list(SCROLL_KINDS) * LEGACY_SCROLL_BUDGET, scrolls
        groups = [tuple(scrolls[index:index + 3]) for index in range(0, width, 3)]
        assert all(group == groups[0] for group in groups), groups
        del result[-width:]
        result.extend(groups[0])
    return result


def douyin_drive(expected: dict[str, Any], scenario: str) -> dict[str, Any]:
    """T06 drive 预期：只变换搜索请求与其滚轮；detail 与登录失效场景本无搜索请求，必须原样返回。"""
    trace = _search_trace(expected["trace"])
    if scenario.startswith("detail") or scenario == "login_expired":
        assert trace == expected["trace"], scenario
    else:
        assert trace != expected["trace"], scenario
    return {**expected, "trace": trace}


def first_page_rebuild_retired(expected: Any) -> tuple[str, list]:
    """4：旧预期只有完整 10 张可见卡片 + 健康后页时重建首页，其余为 None；新行为一律截获缺失且不请求详情。"""
    result, calls = expected
    if result is None:
        assert calls == [], expected
    else:
        assert result["has_more"] == 1 and len(result["data"]) == len(calls) == 10, expected
    return (MISSING, [])


# ---------------------------------------------------------------- T11 冻结执行器
#
# 5. child 删除 ``--start-offset``/``--start-cursor``（只服务抖音三元组续跑）与对应两个 env；抖音收到
#    ``--start-page``≠1、顶部刷新或耗尽参数直接报 ``douyin_full_scan_rejects_resume_args``；小红书拒绝条件去掉
#    已不存在的 ``start_cursor``。
# 6. checkpoint 只记录本轮最后位置供审计：刷新分支的抖音保存位置与 ``stable_douyin_search_id`` 纠正、
#    ``verified_empty_first_page`` 的旧耗尽保留分支删除；耗尽后 reseed 证据（``frontier_reseeds``）删除。
# 7. 看门狗停止事件在首个批次前没有保存的 offset/search ID（每轮从首页开始）。
# 冻结源码 tests/fixtures/adapter_t11/mediacrawler_crawl.py.txt 不改；执行与 AST 对照前做同样替换，每处恰好一次。
T11_REPLACEMENTS = (
    (
        '''DISCOVERY_RESEED_EVENT_FIELDS = (
    "platform",
    "reason",
    "saved_resume_page",
    "saved_resume_offset",
    "saved_resume_cursor",
    "resume_page",
    "resume_offset",
    "resume_cursor",
    "refresh_new_candidate_count",
)


''',
        "",
    ),
    (
        '''    refresh_only = event.get("discovery_phase") == "refresh"
    if refresh_only:
        resume_page = int(args.start_page)
        resume_offset = (
            int(args.start_offset)
            if platform_key == "douyin" and args.start_offset is not None
            else None
        )
        resume_cursor = (
            (str(args.start_cursor or "") or None)
            if platform_key == "douyin"
            else None
        )
''',
        '''    refresh_only = event.get("discovery_phase") == "refresh"
    if refresh_only:
        # 顶部刷新只属于按页恢复的通用平台；抖音（#124）每轮全量扫描，没有刷新阶段。
        resume_page = int(args.start_page)
        resume_offset = None
        resume_cursor = None
''',
    ),
    (
        '''        resume_cursor = str(event.get("resume_cursor") or "") or None
        if platform_key == "douyin":
            resume_cursor = stable_douyin_search_id(pagination_evidence) or resume_cursor
''',
        '''        # 抖音（#124）的 page/offset/search ID 只记录本轮最后位置供审计，runner 不再拿它续跑。
        resume_cursor = str(event.get("resume_cursor") or "") or None
''',
    ),
    (
        '''        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        existing_checkpoint = load_checkpoint(
            conn,
            job_id=int(args.discovery_job_id),
            query_fingerprint_value=str(args.discovery_query_fingerprint),
        )
        if (
            platform_key == "douyin"
            and stop_detail == "saved_source_exhausted"
            and source_has_more is False
            and resume_page == 1
            and resume_offset in (None, 0)
            and not resume_cursor
            and existing_checkpoint
            and existing_checkpoint.get("last_stop_detail")
            == "verified_empty_first_page"
        ):
            stop_detail = "verified_empty_first_page"
        save_checkpoint(''',
        '''        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        save_checkpoint(''',
    ),
    ('''    frontier_reseeds: list[dict[str, Any]] = []
''', ""),
    (
        '''        elif event.get("type") == "discovery_frontier_reseeded":
            frontier_reseeds.append(
                {
                    key: details.get(key)
                    for key in DISCOVERY_RESEED_EVENT_FIELDS
                    if key in details
                }
            )
''',
        "",
    ),
    ('''        "frontier_reseeds": frontier_reseeds,
''', ""),
    (
        '''                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET": str(args.start_offset),
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR": str(args.start_cursor or ""),
''',
        "",
    ),
    (
        '''            start_offset=(
                int(args.start_offset) if args.start_offset is not None else None
            ),
            start_cursor=str(args.start_cursor or "") or None,
''',
        '''            # #124：offset/search ID 只在抖音链上出现，且每轮从首页开始；首个批次前没有保存位置。
            start_offset=None,
            start_cursor=None,
''',
    ),
    (
        '''    if args.start_offset < 0 or args.top_refresh_max_pages < 0:
        raise SystemExit("--start-offset and --top-refresh-max-pages cannot be negative")
''',
        '''    if args.top_refresh_max_pages < 0:
        raise SystemExit("--top-refresh-max-pages cannot be negative")
''',
    ),
    (
        '''            or args.start_page != 1
            or args.start_offset != 0
            or args.start_cursor
            or args.discovery_job_id is not None
''',
        '''            or args.start_page != 1
            or args.discovery_job_id is not None
''',
    ),
    (
        '''    if "douyin" in platforms and args.start_page > 1 and not args.start_cursor:
        raise SystemExit(
            "Douyin continuation requires --start-cursor together with --start-page"
        )
''',
        '''    if "douyin" in platforms and (
        args.start_page != 1
        or args.top_refresh_max_pages > 0
        or args.discovery_source_exhausted
    ):
        # #124：抖音每轮从首页全量翻到 has_more=false，旧的深层恢复与顶部刷新参数直接视为错误。
        raise SystemExit("douyin_full_scan_rejects_resume_args")
''',
    ),
)
# 5 续：小红书拒绝条件只在 #71 替换之后出现，单独登记、在 #71 之后施加。
T11_AFTER_T71_REPLACEMENTS = (
    (
        '''        if (
            args.start_page != 1
            or args.start_cursor
            or args.top_refresh_max_pages > 0
            or args.discovery_source_exhausted
        ):
            raise SystemExit("xhs_full_scan_rejects_resume_args")''',
        '''        if (
            args.start_page != 1
            or args.top_refresh_max_pages > 0
            or args.discovery_source_exhausted
        ):
            raise SystemExit("xhs_full_scan_rejects_resume_args")''',
    ),
)
# 6 续：只服务抖音三元组续跑的定义随之退出，T11 AST 对照改为断言旧定义确在冻结源码、新侧已不存在。
T11_EXITED_DEFINITIONS = frozenset({"stable_douyin_search_id"})


def _replace(text: str, pairs) -> str:
    for old, new in pairs:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


def t11_source(text: str) -> str:
    """冻结执行器源码的 #124 变换（#71 之前可施加的部分）。"""
    return _replace(text, T11_REPLACEMENTS)


def t11_after_t71_source(text: str) -> str:
    """依赖 #71 替换结果的 #124 变换。"""
    return _replace(text, T11_AFTER_T71_REPLACEMENTS)
