"""抖音适配器在离线边界上的请求、签名输入、写出与终态：浏览器截获搜索、详情兜底、作者页、正文图片与入口装配。"""

from __future__ import annotations

import ast
import asyncio
from collections import Counter
from dataclasses import replace
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest

from support.stable_png import solid_png
from trippostcollect.application import events
from trippostcollect.application.worker_inputs import worker_config
from trippostcollect.artifacts import jsonl
from trippostcollect.platforms import entry
from trippostcollect.platforms.douyin import client, core, login, login_support, parser, signer
from trippostcollect.runtime import browser_runtime, image_retry


ROOT = Path(__file__).resolve().parents[3]
KEYWORD = "青岛崂山旅游攻略"


def aweme(post_id):
    return {
        "aweme_id": str(post_id), "aweme_type": 68,
        "desc": "青岛崂山旅游攻略正文", "create_time": 1700000000,
        "author": {"uid": "author", "sec_uid": "sec-author", "nickname": "旅行作者",
                   "avatar_thumb": {"url_list": ["https://avatar.test/never.png"]}},
        "statistics": {"digg_count": 1, "collect_count": 2, "comment_count": 3, "share_count": 4},
        "images": [{"uri": f"uri-{post_id}", "url_list": [
            f"https://image.test/{post_id}.jpg?signature=old",
            f"https://image.test/{post_id}.jpg?signature=fresh",
        ]}],
        "video": {"play_addr": {"url_list": ["", "https://media.test/never.mp4"]}},
        "music": {"play_url": {"uri": "https://media.test/never.mp3"}},
    }


def chunked(payload):
    content = json.dumps(payload, ensure_ascii=False).encode()
    chunks = (content[:19], content[19:57], content[57:])
    return b"".join(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n" for chunk in chunks) + b"0\r\n\r\n"


async def drive(root, patch, scenario, fallback):
    """替换网络、浏览器、随机源与时钟；解析、重试和文件写出保持真实。"""
    root.mkdir()
    trace = []
    counters = Counter()
    config = worker_config()
    detail_mode = scenario.startswith("detail")
    for key, value in {
        "PLATFORM": "dy", "LOGIN_TYPE": "cookie", "COOKIES": "sessionid=fixture",
        "CRAWLER_TYPE": "detail" if detail_mode else "search", "KEYWORDS": KEYWORD,
        "START_PAGE": 1, "MAX_CONCURRENCY_NUM": 1,
        "CRAWLER_MAX_SLEEP_SEC": 0, "ENABLE_GET_COMMENTS": False,
        "ENABLE_GET_MEIDAS": True, "ENABLE_IP_PROXY": False, "ENABLE_CDP_MODE": False,
        "SAVE_LOGIN_STATE": True, "HEADLESS": False, "SAVE_DATA_OPTION": "jsonl",
        "SAVE_DATA_PATH": str(root / "batch"), "DY_SPECIFIED_ID_LIST": ["101"],
    }.items():
        patch.setattr(config, key, value)
    for key, value in {
        "TRIPPOSTCOLLECT_DOUYIN_ENRICH_CREATORS": "1",
        "TRIPPOSTCOLLECT_DOUYIN_ENRICH_ONLY_IMAGES": "1",
        "TRIPPOSTCOLLECT_DOUYIN_MAX_CREATOR_ENRICH": "-1",
        "TRIPPOSTCOLLECT_DOUYIN_CREATOR_SLEEP_SECONDS": "0",
        "TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS": "1",
        "TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_FALLBACK": str(fallback),
        "TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_TIMEOUT_MS": "1200",
        "TRIPPOSTCOLLECT_EXECUTION_STATE_PATH": str(root / "state.json"),
    }.items():
        patch.setenv(key, value)
    for key in ("TRIPPOSTCOLLECT_DB_PATH", "TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH"):
        patch.delenv(key, raising=False)
    (root / "state.json").write_text('{"events":[]}')
    # 与主机无关的纯色 PNG（见 support/stable_png.py；Pillow 压缩字节随 CPU 不同）。
    image_output = io.BytesIO(solid_png(8, 6, (255, 165, 0)))

    def search_payload(offset):
        if scenario == "search_blocked":
            return {"status_code": 0, "data": [], "has_more": 0,
                    "search_nil_info": {"search_nil_type": "verify_check"}}
        if scenario.startswith("empty"):
            return {"status_code": 0, "data": [], "has_more": 0, "extra": {"logid": "first-id"}}
        items = [aweme(101 + offset)] if offset < 20 else []
        return {"status_code": 0, "data": [{"aweme_info": item} for item in items],
                "has_more": 1 if offset < 20 else 0,
                "extra": {"logid": "stable-id" if offset == 0 else f"rotating-{offset}"}}

    class Response:
        def __init__(self, payload, url):
            self.payload = payload
            self.url = url

        async def body(self):
            return chunked(self.payload)

    class Mouse:
        async def move(self, *args, **kwargs):
            trace.append(("move", args, kwargs))

        async def wheel(self, *args):
            trace.append(("wheel", args))
            counters["wheel"] += 1
            # #124：真实浏览器每次可见滚轮加载下一页；只在上一页 has_more=1 时发出。
            page.emit_next()

    class Locator:
        async def count(self):
            return 1 if scenario == "empty_visible" else 0

        async def inner_text(self, **kwargs):
            return "暂无搜索结果" if scenario == "empty_verified" else "综合 视频 用户"

    class ExpectResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        @property
        def value(self):
            async def result():
                payload = {} if "/note/" in page.url else {"aweme_detail": aweme(101)}
                return Response(payload, "https://www.douyin.com/aweme/v1/web/aweme/detail/")
            return result()

    class Page:
        url = ""
        viewport_size = {"width": 1920, "height": 1080}
        mouse = Mouse()

        def on(self, event, callback):
            trace.append(("listen", event))
            self.callback = callback

        emitted_offset = None

        def emit(self, offset, search_id):
            path = "stream" if offset == 0 else "single"
            url = f"https://www.douyin.com/aweme/v1/web/general/search/{path}/?" + urlencode({
                "keyword": KEYWORD, "offset": offset, "search_id": search_id,
            })
            self.emitted_offset = offset
            self.callback(Response(search_payload(offset), url))

        def emit_next(self):
            if self.emitted_offset is None or not search_payload(self.emitted_offset).get("has_more"):
                return
            self.emit(self.emitted_offset + 10, "stable-id")

        async def goto(self, url, **kwargs):
            trace.append(("goto", url, kwargs))
            self.url = url
            if "/search/" in url:
                assert any(item[0] == "listen" for item in trace)
                # #124：首页来自进入搜索页时页面自身发出的 /stream/ 请求（detail 模式只取行为证据）。
                if not detail_mode:
                    self.emit(0, "")

        async def evaluate(self, expression, *args):
            if expression == "() => navigator.userAgent":
                return "Mozilla/5.0 Chrome/125.0.0.0"
            if expression == "() => window.localStorage":
                return {"HasUserLogin": "0" if scenario == "login_expired" else "1", "xmst": "token"}
            if "hardwareConcurrency" in expression:
                return {"userAgent": "Mozilla/5.0 Chrome/125.0.0.0", "language": "zh-CN", "platform": "MacIntel"}
            if "targetId" in expression:
                return {"aweme_id": "101"}
            raise AssertionError(expression)

        def locator(self, selector):
            return Locator()

        async def wait_for_timeout(self, timeout):
            trace.append(("wait", timeout))

        async def wait_for_selector(self, selector, **kwargs):
            trace.append(("selector", selector, kwargs))

        async def title(self):
            return "抖音"

        def expect_response(self, predicate, **kwargs):
            assert predicate(SimpleNamespace(url="https://www.douyin.com/aweme/v1/web/aweme/detail/"))
            trace.append(("expect_detail", kwargs))
            return ExpectResponse()

    page = Page()

    class Context:
        pages = [page]

        async def new_page(self):
            trace.append(("new_page",))
            return page

        async def add_init_script(self, *args, **kwargs):
            # #127：抖音不再注入任何 init script；出现即进入轨迹，由 check_drive_result 判失败。
            trace.append(("init_script", args, kwargs))

        async def cookies(self, urls=None):
            trace.append(("cookies", urls))
            return [{"name": "sessionid", "value": "fixture"},
                    {"name": "LOGIN_STATUS", "value": "0" if scenario == "login_expired" else "1"},
                    {"name": "s_v_web_id", "value": "verify-fixture"},
                    {"name": "UIFID_TEMP", "value": "uifid-fixture"}]

        async def add_cookies(self, values):
            trace.append(("add_cookies", values))

        async def close(self):
            trace.append(("close",))

    class Playwright:
        async def __aenter__(self):
            self.chromium = self
            trace.append(("playwright_enter",))
            return self

        async def __aexit__(self, *args):
            trace.append(("playwright_exit",))

        async def launch_persistent_context(self, **kwargs):
            # 记录原始 user_data_dir；#59 的 profile 迁移偏离只在比较时对旧侧预期单向变换。
            trace.append(("launch", kwargs))
            return Context()

    class HttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, **kwargs):
            request = httpx.Request(method, url, params=kwargs.get("params"),
                                    data=kwargs.get("data"), headers=kwargs.get("headers"))
            trace.append(("request", method, str(request.url), request.content.hex(),
                          sorted(request.headers.items()), kwargs.get("timeout"), kwargs.get("follow_redirects")))
            path = request.url.path
            counters[path] += 1
            query = dict(request.url.params)
            # #124：搜索只经浏览器截获，客户端不得再发搜索请求。
            assert "/general/search/" not in path, str(request.url)
            if "/aweme/detail/" in path:
                payload = {"aweme_detail": {} if scenario == "detail_shell" else aweme(query["aweme_id"])}
                return httpx.Response(200, json=payload, request=request)
            if "/user/profile/other/" in path:
                if scenario == "creator_blocked":
                    return httpx.Response(200, content=b"blocked", request=request)
                if scenario == "creator_retry" and counters[path] < 3:
                    return httpx.Response(200, content=b"not-json", request=request)
                # #116：作者资料必须是 status_code=0 且带 user 节点的成功信封（真实平台响应形状）。
                payload = {} if scenario == "creator_empty" else {"status_code": 0, "user": {"follower_count": 42}}
                return httpx.Response(200, json=payload, request=request)
            if request.url.host == "image.test":
                code = 503 if scenario == "image_failed" else 429 if scenario == "image_blocked" else 200
                if scenario == "image_retry" and counters[path] == 1:
                    code = 503
                return httpx.Response(code, content=image_output.getvalue(), request=request)
            raise AssertionError(str(request.url))

    async def sleep(delay):
        trace.append(("sleep", delay))

    async def behavior(page, platform_key):
        trace.append(("behavior", platform_key))
        return {"status": "completed"}

    class Signer:
        def call(self, name, params, user_agent):
            trace.append(("sign", name, params, user_agent))
            return "fixture+signature/="

    patch.setattr(asyncio, "sleep", sleep)
    patch.setattr(core.random, "random", lambda: 0.5)
    patch.setattr(core.random, "uniform", lambda a, b: (a + b) / 2)
    patch.setattr(core.random, "randint", lambda a, b: a)
    patch.setattr(events, "_utc_iso", lambda: "2026-09-30T00:00:00+00:00")
    patch.setattr(jsonl, "get_current_date", lambda: "2026-09-30")
    patch.setattr(image_retry, "IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS", (0.0, 0.0))
    patch.setattr(login.DouYinLogin.check_login_state.retry, "sleep", sleep)
    patch.setattr(signer, "douyin_sign_obj", Signer())
    # #136：共享启动函数的运行时参数与环境取替身值，不在仓库 data/runtime 下建目录、不读真实环境。
    patch.setattr(browser_runtime, "browser_runtime_args", lambda: [])
    patch.setattr(browser_runtime, "browser_launch_environment", lambda: {})
    dependencies = entry.douyin_dependencies(config)
    ports = dependencies["ports"]
    # 日期函数是 writer 的默认参数，冻结两边真实 writer 的日期回调。
    original_sink = ports.content_sink

    def sink(crawler_type):
        value = original_sink(crawler_type)
        value.file_writer._current_date = lambda: "2026-09-30"
        return value

    dependencies["ports"] = replace(
        ports, client=replace(ports.client, make_async_client=lambda **kw: HttpClient()),
        async_playwright=Playwright, run_required_human_behavior=behavior,
        project_browser_args=lambda: [], browser_detail_fallback=bool(fallback),
        current_timestamp=lambda: 1700000000123, content_sink=sink,
    )
    crawler = core.DouYinCrawler(**dependencies)
    crawler.get_aweme_video = lambda *a, **k: pytest.fail("图文路径不得调用视频下载")
    error = None
    try:
        await crawler.start()
    except (Exception, SystemExit) as exc:
        error = (type(exc).__name__, str(exc))
    finally:
        await crawler.close()
    files = {str(path.relative_to(root / "batch")): path.read_bytes()
             for path in (root / "batch").rglob("*") if path.is_file()}
    event_rows = json.loads((root / "state.json").read_text())["events"]
    return {"trace": trace, "files": files, "events": event_rows, "error": error}


SCENARIOS = (
    "search_pages", "browser_pages", "detail", "detail_shell",
    "empty_verified", "empty_visible", "empty_ambiguous", "search_blocked",
    "creator_empty", "creator_blocked", "creator_retry", "image_retry",
    "image_failed", "image_blocked", "login_expired",
)


EXHAUSTED = {("source_exhausted", "empty_page")}


def expected_outcome(scenario, fallback):
    """(停止事件集合, 跳过的候选 [(范围, 错误码)], 落盘正文图数, 内容文件名, 错误)。"""
    search = ["image_manifest.jsonl", "search_contents_2026-09-30.jsonl"]
    detail = ["detail_contents_2026-09-30.jsonl", "image_manifest.jsonl"]
    return {
        "search_pages": (EXHAUSTED, [], 2, search, None),
        "browser_pages": (EXHAUSTED, [], 2, search, None),
        "detail": (set(), [], 1, detail, None),
        # 详情只有外壳：兜底开启时经浏览器详情补全，否则不写出。
        "detail_shell": (set(), [], 1, detail, None) if fallback else (set(), [], 0, [], None),
        "empty_verified": ({("source_exhausted", "verified_empty_first_page")}, [], 0, [], None),
        "empty_visible": ({("runtime_failed", "empty_api_response_with_visible_results")}, [], 0, [], None),
        "empty_ambiguous": ({("runtime_failed", "ambiguous_empty_first_page")}, [], 0, [], None),
        "search_blocked": ({("runtime_failed", "search_verify_check")}, [], 0, [], None),
        "creator_empty": (EXHAUSTED, [("post", "creator_profile_empty")] * 2, 0, [], None),
        "creator_blocked": ({("runtime_failed", "account_blocked")}, [], 0, [], None),
        "creator_retry": (EXHAUSTED, [], 2, search, None),
        "image_retry": (EXHAUSTED, [], 2, search, None),
        "image_failed": (EXHAUSTED, [("image", "image_download_retryable")] * 2, 0, ["image_manifest.jsonl"], None),
        "image_blocked": ({("runtime_failed", "image_rate_limited")}, [], 0, ["image_manifest.jsonl"], None),
        "login_expired": (set(), [], 0, [], ("SystemExit", "")),
    }[scenario]


def drive_outcome(after):
    stops = {(row["details"]["stop_reason"], row["details"].get("stop_detail"))
             for row in after["events"] if row["type"] == "adaptive_search_stopped"}
    skips = [(row["details"].get("failure_scope"), row["details"].get("error_code"))
             for row in after["events"] if row["type"] == "candidate_skipped"]
    images = sum("/images/" in name for name in after["files"])
    files = sorted(name.rsplit("/", 1)[-1] for name in after["files"] if "/images/" not in name)
    return stops, skips, images, files, after["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [0, 1])
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_drive_requests_records_images_events(tmp_path, monkeypatch, scenario, fallback):
    with monkeypatch.context() as patch:
        after = await drive(tmp_path / "new", patch, scenario, fallback)
    check_drive_result(after, scenario, fallback)
    assert drive_outcome(after) == expected_outcome(scenario, fallback)


async def drive_entry_assembly(root, patch, scenario, fallback):
    """worker 装配路径：install_hooks 读取兜底开关后由 load_crawler 无参构造。"""
    new_class = entry.load_crawler("dy")
    assert issubclass(new_class, core.DouYinCrawler)

    def construct(**dependencies):
        patch.setattr(entry, "_douyin_browser_detail_fallback", False)
        entry.install_hooks()
        assert entry._douyin_browser_detail_fallback is bool(fallback)
        dependencies["ports"] = replace(
            dependencies["ports"], browser_detail_fallback=entry._douyin_browser_detail_fallback,
        )
        patch.setattr(entry, "douyin_dependencies", lambda config: dependencies)
        crawler = new_class()
        assert type(crawler) is new_class
        return crawler

    patch.setattr(core, "DouYinCrawler", construct)
    return await drive(root, patch, scenario, fallback)


def _scrub(value, tmp):
    return json.loads(json.dumps(value, ensure_ascii=False, default=lambda item: sha256(item).hexdigest()
                                 if isinstance(item, bytes) else str(item)).replace(str(tmp), "<TMP>"))


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,fallback", [("search_pages", 0), ("detail_shell", 1), ("image_failed", 0)])
async def test_entry_assembly_matches_direct_construction(tmp_path, monkeypatch, scenario, fallback):
    """经 worker 入口装配与直接构造得到相同的请求、产物、事件与错误。"""
    with monkeypatch.context() as patch:
        direct = await drive(tmp_path / "direct", patch, scenario, fallback)
    with monkeypatch.context() as patch:
        assembled = await drive_entry_assembly(tmp_path / "entry", patch, scenario, fallback)
    assert _scrub(assembled, tmp_path / "entry") == _scrub(direct, tmp_path / "direct")


def check_drive_result(after, scenario, fallback):
    assert after["error"] is None or scenario == "login_expired", after["error"]
    trace = after["trace"]
    assert not [row for row in trace if row[0] == "init_script"]
    assert next(i for i, row in enumerate(trace) if row[0] == "listen") < next(
        i for i, row in enumerate(trace) if row[0] == "goto" and "/search/" in row[1]
    ) if scenario != "login_expired" else True
    requests = [row for row in trace if row[0] == "request"]
    assert all("avatar.test" not in row[2] and "never.mp" not in row[2] for row in requests)
    assert all("/videos/" not in path and not path.endswith((".mp4", ".mp3")) for path in after["files"])
    # #124：任何场景都不发搜索请求；连续翻页每页只需一屏滚轮。
    assert not [row for row in requests if "/general/search/" in row[2]]
    if scenario in ("search_pages", "browser_pages"):
        assert sum(row[0] == "wheel" for row in trace) == 2
    if scenario == "detail_shell" and fallback:
        routes = [row[1] for row in trace if row[0] == "goto" and any(part in row[1] for part in ("/note/", "/video/"))]
        assert routes == ["https://www.douyin.com/note/101", "https://www.douyin.com/video/101"]
        assert all(row[1]["timeout"] == 5000 for row in trace if row[0] == "expect_detail")
    if scenario in ("search_pages", "browser_pages", "empty_verified"):
        stops = [row for row in after["events"] if row["type"] == "adaptive_search_stopped"]
        assert stops[-1]["details"]["stop_reason"] == "source_exhausted"


def test_dependency_direction():
    allowed_runtime = {
        "trippostcollect.runtime.helpers": {"extract_url_params_to_dict", "normalize_image_url"},
        "trippostcollect.runtime.cookies": {"convert_cookies", "convert_str_cookie_to_dict"},
        "trippostcollect.runtime.image_retry": {
            "ImageDownloadFetchError", "classified_http_image_error", "is_runtime_blocking_image_error",
        },
    }
    forbidden = {"config", "tools", "media_platform", "store", "base", "var", "model", "proxy", "cache", "scripts"}
    for path in (ROOT / "src/trippostcollect/platforms/douyin").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                continue
            for module in modules:
                assert module.split(".")[0] not in forbidden, (path, module)
                assert not module.startswith(("trippostcollect.artifacts", "trippostcollect.db", "trippostcollect.scheduler"))
                if module.startswith("trippostcollect.application"):
                    assert module == "trippostcollect.application.contracts"
                if module.startswith("trippostcollect.platforms"):
                    assert module.startswith("trippostcollect.platforms.douyin")
                if module.startswith("trippostcollect.runtime"):
                    assert isinstance(node, ast.ImportFrom) and module in allowed_runtime
                    assert {alias.name for alias in node.names} <= allowed_runtime[module]


def test_issue131_search_nil_whitelist():
    """#131：search_nil_type 只放行空值与 service_empty，其他取值按取值命名失败原因。"""
    assert parser.DOUYIN_NORMAL_SEARCH_NIL_TYPES == frozenset({"service_empty"})
    for value, reason in (("unknown", "search_nil_unknown"), ("verify_check", "search_verify_check")):
        with pytest.raises(parser.SearchResponseError) as raised:
            parser.validate_douyin_search_response(
                {"status_code": 0, "data": [], "has_more": 0, "search_nil_info": {"search_nil_type": value}})
        assert raised.value.reason == reason
    service_empty = {"status_code": 0, "data": [], "has_more": 0, "search_nil_info": {"search_nil_type": "service_empty"}}
    assert parser.validate_douyin_search_response(service_empty) is service_empty

FIRST_PAGE_CASES = [
    (10, 10, "stable", False), (9, 10, "stable", False),
    (10, 10, "", False), (10, 10, "stable", True), (10, 0, "stable", False),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("visible_count,offset,search_id,verify", FIRST_PAGE_CASES)
async def test_first_page_without_capture_fails_without_direct_request(visible_count, offset, search_id, verify):
    """#124：首页只经浏览器截获；未截获时以 browser_search_response_missing 失败，不直接请求、不请求详情。"""
    params = (visible_count, offset, search_id, verify)
    ports = entry.douyin_dependencies(worker_config())["ports"].client
    assert await first_page_without_capture(params, ports=ports) == ("browser_search_response_missing", [])


async def first_page_without_capture(params, *, ports):
    """旧对照的同一组输入（可见卡片数、已截获后页、verify 标记），首页未被截获时的根结果。"""
    from trippostcollect.platforms.douyin.models import SearchResponseError

    visible_count, offset, search_id, verify = params
    calls = []

    class Page:
        def on(self, *args):
            pass

        def locator(self, selector):
            return self

        async def evaluate_all(self, script):
            return [str(100 + i) for i in range(visible_count)]

    value = client.DouYinClient(headers={"User-Agent": "fixture"}, playwright_page=Page(), cookie_dict={}, ports=ports)
    payload = {"data": [{"aweme_info": aweme(110)}], "has_more": 1, "extra": {"logid": "rotating"}}
    if verify:
        payload["search_nil_info"] = {"search_nil_type": "verify_check"}
    value._observed_search_responses.append({"keyword": KEYWORD, "offset": offset, "search_id": search_id, "payload": payload})

    async def detail(post_id):
        calls.append(post_id)
        return aweme(post_id)

    async def forbidden(*args, **kwargs):
        raise AssertionError("#124: first page must not be requested directly")

    value.get_video_by_id = detail
    value.get = forbidden
    with pytest.raises(SearchResponseError) as raised:
        await value.search_info_by_keyword(keyword=KEYWORD, offset=0, search_id="")
    return raised.value.reason, calls


def cache_lifecycle(cache_class):
    value = cache_class()
    value.set("dy_phone", b"123456", 120)
    result = (value.get("dy_phone"), value.keys("dy_*"), value.get("absent"))
    task = value._cron_task
    value.__del__()
    assert task.cancelling()
    return result


@pytest.mark.asyncio
async def test_slider_tracks_and_login_cache_lifecycle():
    # 滑块轨迹确定：easy 匀加速小步，hard 先大步后逐步收敛。
    assert login_support.get_tracks(137, "easy") == [
        0, 0, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3,
        4, 4, 4, 4, 4, 4, 5, 5, 5, 5, 5, 5, 6, 6, 6, 6, 6, 6, 6, 5,
    ]
    assert login_support.get_tracks(137, "hard") == [0, 0, 40, 28, 21, 14, 10, 7, 5, 3, 3, 2, 1, 1, 0, 1, 0, 0, 1, 0, 0]
    assert cache_lifecycle(login_support.ExpiringLocalCache) == (b"123456", ["dy_phone"], None)


@pytest.mark.parametrize("fallback", ["0", "1"])
def test_worker_assembly_from_arbitrary_cwd(tmp_path, fallback):
    argv = [
        "--platform", "dy", "--lt", "cookie", "--type", "search", "--keywords", KEYWORD,
        "--get_comment", "false", "--get_sub_comment", "false", "--get_media", "true",
        "--headless", "false", "--save_data_option", "jsonl", "--save_data_path", str(tmp_path / "batch"),
        "--start", "1", "--max_concurrency_num", "1", "--enable_ip_proxy", "false",
    ]
    source = (
        "import sys,json\n"
        "from trippostcollect.platforms.entry import configure,install_hooks,load_crawler\n"
        f"configure({argv!r})\n"
        "assert 'trippostcollect.platforms.douyin.signer' not in sys.modules\n"
        "install_hooks()\n"
        "cls=load_crawler('dy'); crawler=cls()\n"
        "assert cls.__module__ == 'trippostcollect.platforms.douyin.core'\n"
        "assert not any(n.startswith(('media_platform.douyin','store.douyin')) for n in sys.modules)\n"
        f"assert crawler.ports.browser_detail_fallback is {fallback == '1'}\n"
        "print(json.dumps([load_crawler(code).__name__ for code in ('wb','zhihu','xhs')]))\n"
    )
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"), TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_FALLBACK=fallback)
    result = subprocess.run([sys.executable, "-P", "-c", source], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    assert json.loads(result.stdout.splitlines()[-1]) == ["WeiboCrawler", "ZhihuCrawler", "XiaoHongShuCrawler"]


REPAIR_CASES = [(False, True), (True, True), (True, False)]
NAVIGATION_FAILED = ("browser detail response did not contain aweme_detail; attempts=["
                     "'note:reason=navigation_or_response:Error:payload_keys=[]', "
                     "'video:reason=navigation_or_response:Error:payload_keys=[]']")
REPAIR_EXPECTED = {
    (False, True): ("RuntimeError", NAVIGATION_FAILED, "NoneType", "None", [{"timeout": 30000}] * 2),
    (True, True): ("RuntimeError", NAVIGATION_FAILED, "ValueError", "原详情请求失败", [{"timeout": 30000}] * 2),
    (True, False): ("RuntimeError", "Douyin browser detail fallback requires a Playwright page",
                    "ValueError", "原详情请求失败", []),
}


def repair_inputs(monkeypatch, original_error, page_present):
    from playwright.async_api import Error as PlaywrightError

    trace = []

    class Page:
        def on(self, *args):
            pass

        def expect_response(self, *args, **kwargs):
            trace.append(kwargs)
            raise PlaywrightError("详情导航失败")

        async def evaluate(self, *args):
            return None

    async def original(self, aweme_id):
        if original_error:
            raise ValueError("原详情请求失败")
        return {}

    async def capture(value):
        try:
            await value.get_video_by_id("101")
        except Exception as exc:
            return (type(exc).__name__, str(exc), type(exc.__cause__).__name__, str(exc.__cause__), list(trace))
        pytest.fail("证据不足必须保留异常")

    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_FALLBACK", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_TIMEOUT_MS", "30000")
    kwargs = {"headers": {}, "playwright_page": Page() if page_present else None, "cookie_dict": {}}
    return trace, original, capture, kwargs


async def root_repair_chain(monkeypatch, original_error, page_present):
    trace, original, capture, kwargs = repair_inputs(monkeypatch, original_error, page_present)
    ports = entry.douyin_dependencies(worker_config())["ports"].client
    monkeypatch.setattr(client.DouYinClient, "_get_video_by_id", original)
    return await capture(client.DouYinClient(ports=ports, browser_detail_fallback=True, **kwargs))


@pytest.mark.asyncio
@pytest.mark.parametrize("original_error,page_present", REPAIR_CASES)
async def test_root_repair_exception_chain(monkeypatch, original_error, page_present):
    """浏览器详情兜底先 note 后 video；证据不足时保留异常，原详情请求的异常作为 cause。"""
    after = await root_repair_chain(monkeypatch, original_error, page_present)
    assert after == REPAIR_EXPECTED[(original_error, page_present)]


async def post_form(module, signer_module, patch, *, ports=None):
    trace = []

    class Page:
        def on(self, *args):
            pass

        async def evaluate(self, script):
            if script == "() => window.localStorage":
                return {"__tea_cache_tokens_1300": '{"web_id":"123"}', "xmst": "fixture"}
            return {"userAgent": "Chrome/125.0.0.0"}

    async def sign(url, params, post_data, user_agent, page):
        trace.append(("sign", url, params, post_data, user_agent))
        return "fixed+bogus/="

    class Http:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def request(self, method, url, **kwargs):
            request = httpx.Request(method, url, data=kwargs["data"], headers=kwargs["headers"])
            trace.append(("request", method, str(request.url), request.content, sorted(request.headers.items())))
            return httpx.Response(200, json={"ok": True}, request=request)

    patch.setattr(module, "get_a_bogus", sign)
    kwargs = {"headers": {"User-Agent": "Chrome/125.0.0.0", "Cookie": "sessionid=current", "Referer": "https://www.douyin.com/"},
              "playwright_page": Page(), "cookie_dict": {"s_v_web_id": "verify", "UIFID": "current"}}
    if ports:
        kwargs["ports"] = replace(ports, make_async_client=lambda **kw: Http())
    else:
        patch.setattr(module, "make_async_client", lambda **kw: Http())
    value = module.DouYinClient(**kwargs)
    assert await value.post("/aweme/v1/web/comment/reply/", {"id": "101", "text": "青岛"}) == {"ok": True}
    return trace


def check_post_form(after):
    assert after[0][3] == {}  # post 不传 request_method 的签名输入。
    for part in ("webid=123", "msToken=fixture", "verifyFp=verify", "fp=verify", "uifid=current"):
        assert part in after[0][2].split("&"), part
    assert after[0][4] == "Chrome/125.0.0.0"
    assert b"a_bogus=fixed%2Bbogus%2F%3D" in after[1][3]
    assert "x-tt-argus" not in dict(after[1][4])


@pytest.mark.asyncio
async def test_root_post_form_and_signature_inputs(monkeypatch):
    ports = entry.douyin_dependencies(worker_config())["ports"].client
    with monkeypatch.context() as patch:
        after = await post_form(client, signer, patch, ports=ports)
    check_post_form(after)


