"""T06 以固定 fork 源码为基线，对照请求、签名输入、写出与终态。"""

from __future__ import annotations

import ast
import asyncio
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from PIL import Image
import pytest

from support.raw_author_identity import use_raw_author_identity
from trippostcollect.application import events
from trippostcollect.core import paths
from trippostcollect.artifacts import jsonl
from trippostcollect.platforms import _fork_bridge, entry
from trippostcollect.platforms.douyin import client, core, login, login_support, parser, signer
from trippostcollect.runtime import image_retry


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/adapter_t06"
def t14_douyin_profiles():
    """旧侧 fork 固定位置与新侧当前（测试中已重定向的）core.paths 位置；调用时求值。"""
    return {
        str(ROOT / "tools/MediaCrawler/browser_data/dy_user_data_dir"),
        str(paths.platform_profile_dir("douyin")),
    }


def t14_slider_paths(node):
    """T14 授权差异：滑块临时图从 fork temp_image（及 cwd 相对路径）改到 DOUYIN_SLIDER_IMAGE_DIR。"""
    source = ast.unparse(node)
    source = source.replace(
        "os.path.join(MEDIACRAWLER_DIR, 'temp_image')", "str(DOUYIN_SLIDER_IMAGE_DIR)",
    ).replace("f'./temp_image/{img_type}.jpg'", "os.path.join(DOUYIN_SLIDER_IMAGE_DIR, f'{img_type}.jpg')")
    return ast.parse(source).body[0]
KEYWORD = "青岛崂山旅游攻略"


@contextmanager
def legacy(tmp_path, monkeypatch):
    """以原模块名加载逐字冻结文件，退出时恢复模块表与包属性。"""
    _fork_bridge.install()
    with monkeypatch.context() as patch:
        module_names = []
        for path in FIXTURES.rglob("*.py.txt"):
            relative = path.relative_to(FIXTURES).with_suffix("")
            if len(relative.parts) == 1:
                continue
            target = tmp_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
            name = ".".join(relative.with_suffix("").parts)
            module_names.append(name.removesuffix(".__init__"))
        for name in ("tools", "store", "media_platform", "model", "cache"):
            package = importlib.import_module(name)
            patch.setattr(package, "__path__", [str(tmp_path / name), *package.__path__])
        # utils 的旧相对导入应取得本次冻结的 slider；其他共享依赖使用已迁入实现。
        module_names.append("tools.utils")
        for name in sorted(module_names, key=len, reverse=True):
            patch.delitem(sys.modules, name, raising=False)
            parent, _, child = name.rpartition(".")
            if parent in sys.modules:
                patch.delattr(sys.modules[parent], child, raising=False)
        old_core = importlib.import_module("media_platform.douyin.core")
        old_client = importlib.import_module("media_platform.douyin.client")
        old_store = importlib.import_module("store.douyin")
        # #49：根实现保存作者原始 ID 与昵称，对照只替换旧 store 的身份转换。
        use_raw_author_identity(patch, old_store)
        repair_path = tmp_path / "repair.py"
        repair_path.write_bytes((FIXTURES / "mediacrawler_export_entrypoint.py.txt").read_bytes())
        spec = importlib.util.spec_from_file_location("t06_legacy_repair", repair_path)
        repair = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(repair)
        yield SimpleNamespace(
            core=old_core, client=old_client, store=old_store, repair=repair,
            signer=importlib.import_module("media_platform.douyin.help"),
            login=importlib.import_module("media_platform.douyin.login"),
        )
        for name in module_names:
            sys.modules.pop(name, None)


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


async def drive(modules, root, patch, scenario, fallback):
    """替换网络、浏览器、随机源与时钟；解析、重试和文件写出保持真实。"""
    root.mkdir()
    trace = []
    counters = Counter()
    config = importlib.import_module("config")
    detail_mode = scenario.startswith("detail")
    for key, value in {
        "PLATFORM": "dy", "LOGIN_TYPE": "cookie", "COOKIES": "sessionid=fixture",
        "CRAWLER_TYPE": "detail" if detail_mode else "search", "KEYWORDS": KEYWORD,
        "START_PAGE": 1, "PUBLISH_TIME_TYPE": 0, "MAX_CONCURRENCY_NUM": 1,
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
        "TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES": "0",
        "TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED": "0",
        "TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_FALLBACK": str(fallback),
        "TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_TIMEOUT_MS": "1200",
        "TRIPPOSTCOLLECT_EXECUTION_STATE_PATH": str(root / "state.json"),
    }.items():
        patch.setenv(key, value)
    for key in ("TRIPPOSTCOLLECT_DB_PATH", "TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH",
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET", "TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR"):
        patch.delenv(key, raising=False)
    (root / "state.json").write_text('{"events":[]}')
    image_output = io.BytesIO()
    Image.new("RGB", (8, 6), "orange").save(image_output, format="PNG")

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
            if scenario == "browser_pages" and counters["wheel"] == 1:
                page.emit(10, "stable-id")

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

        def emit(self, offset, search_id):
            url = "https://www.douyin.com/aweme/v1/web/general/search/stream/?" + urlencode({
                "keyword": KEYWORD, "offset": offset, "search_id": search_id,
            })
            self.callback(Response(search_payload(offset), url))

        async def goto(self, url, **kwargs):
            trace.append(("goto", url, kwargs))
            self.url = url
            if "/search/" in url:
                assert any(item[0] == "listen" for item in trace)
                if scenario == "browser_pages":
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

        async def add_init_script(self, *, path):
            trace.append(("init_script", sha256(Path(path).read_bytes()).hexdigest()))

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
            # T14 授权差异：持久 profile 从 fork browser_data 迁到 core.paths 定义的新位置；两侧归一为同一记号。
            if kwargs.get("user_data_dir") in t14_douyin_profiles():
                kwargs = {**kwargs, "user_data_dir": "<douyin persistent profile>"}
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
            if "/general/search/" in path:
                assert query["count"] == "10" and query["list_type"] == "single"
                assert query["from_group_id"] == ""
                assert "a_bogus" not in query
                offset = int(query["offset"])
                assert path.endswith("/stream/" if offset == 0 else "/single/")
                payload = search_payload(offset)
                return httpx.Response(200, content=chunked(payload), request=request)
            if "/aweme/detail/" in path:
                payload = {"aweme_detail": {} if scenario == "detail_shell" else aweme(query["aweme_id"])}
                return httpx.Response(200, json=payload, request=request)
            if "/user/profile/other/" in path:
                if scenario == "creator_blocked":
                    return httpx.Response(200, content=b"blocked", request=request)
                if scenario == "creator_retry" and counters[path] < 3:
                    return httpx.Response(200, content=b"not-json", request=request)
                payload = {} if scenario == "creator_empty" else {"user": {"follower_count": 42}}
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
    new = modules is None
    login_class = login.DouYinLogin if new else modules.login.DouYinLogin
    patch.setattr(login_class.check_login_state.retry, "sleep", sleep)
    if new:
        patch.setattr(signer, "douyin_sign_obj", Signer())
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
    else:
        patch.setattr(modules.signer, "douyin_sign_obj", Signer())
        patch.setattr(modules.core, "async_playwright", Playwright)
        patch.setattr(modules.core, "run_required_human_behavior", behavior)
        patch.setattr(modules.core, "project_browser_args", lambda: [])
        patch.setattr(modules.client, "make_async_client", lambda **kw: HttpClient())
        patch.setattr(modules.core.utils, "get_current_timestamp", lambda: 1700000000123)
        writer_module = importlib.import_module("tools.async_file_writer")
        patch.setattr(writer_module.utils, "get_current_date", lambda: "2026-09-30")
        if fallback:
            modules.repair.install_douyin_browser_detail_fallback()
        crawler = modules.core.DouYinCrawler()
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


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [0, 1])
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_old_new_requests_records_images_events(tmp_path, monkeypatch, scenario, fallback):
    with legacy(tmp_path / "legacy", monkeypatch) as old:
        with monkeypatch.context() as patch:
            before = await drive(old, tmp_path / "old", patch, scenario, fallback)
    with monkeypatch.context() as patch:
        after = await drive(None, tmp_path / "new", patch, scenario, fallback)
    assert after == before
    assert after["error"] is None or scenario == "login_expired", after["error"]
    trace = after["trace"]
    assert next(i for i, row in enumerate(trace) if row[0] == "listen") < next(
        i for i, row in enumerate(trace) if row[0] == "goto" and "/search/" in row[1]
    ) if scenario != "login_expired" else True
    requests = [row for row in trace if row[0] == "request"]
    assert all("avatar.test" not in row[2] and "never.mp" not in row[2] for row in requests)
    assert all("/videos/" not in path and not path.endswith((".mp4", ".mp3")) for path in after["files"])
    if scenario == "search_pages":
        searches = [row for row in requests if "/general/search/" in row[2]]
        assert [parse_qs(urlsplit(row[2]).query).get("search_id", [""])[0] for row in searches] == ["", "stable-id", "stable-id"]
        assert sum(row[0] == "wheel" for row in trace) == 16
    if scenario == "browser_pages":
        searches = [row for row in requests if "/general/search/" in row[2]]
        assert len(searches) == 1 and "offset=20" in searches[0][2]
        assert sum(row[0] == "wheel" for row in trace) == 9
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


def test_frozen_source_hashes_and_signature_resource():
    manifest = (FIXTURES / "sources.json").read_bytes()
    assert sha256(manifest).hexdigest() == "18a932b640c8620d79ccaa1ccffdcaaaad1118fad9ffd8621531529bdea98878"
    sources = json.loads(manifest)
    assert sources["fork_sha"] == "5a68eb5098fcd17308c7fe0b9d53916ae839b303"
    for name, digest in sources["files"].items():
        assert sha256((FIXTURES / name).read_bytes()).hexdigest() == digest, name
    from trippostcollect.core import resources
    assert sha256(resources.read_text("js/douyin.js").encode()).hexdigest() == "ff5cb3133e2717523ffb3f96679e3f5d23bd31a999411ea604cff9a51da9e26e"


def test_unchanged_parser_signer_and_login_support_bodies():
    pairs = {
        "media_platform/douyin/search_safety.py": (parser, [
            "decode_douyin_json_body", "validate_douyin_search_response", "classify_empty_first_page",
        ]),
        "media_platform/douyin/help.py": (signer, ["get_a_bogus", "get_a_bogus_from_js"]),
        "tools/easing.py": (login_support, [
            "ease_in_quad", "ease_out_quad", "ease_out_quart", "ease_out_expo", "ease_out_bounce", "ease_out_elastic",
        ]),
        "cache/local_cache.py": (login_support, ["ExpiringLocalCache"]),
        "cache/abs_cache.py": (login_support, ["AbstractCache"]),
        "tools/slider_util.py": (login_support, ["Slide", "get_track_simple"]),
        "tools/image_manifest.py": (parser, ["douyin_source_asset_key"]),
        "store/douyin/__init__.py": (parser, [
            "_first_nonempty", "_nested_value", "_creator_user_profile", "_author_metric",
            "_normalized_author_stats", "_extract_note_image_list", "_extract_note_image_assets",
            "_extract_content_cover_url", "_extract_video_download_url", "_extract_music_download_url",
        ]),
    }
    for name, (module, names) in pairs.items():
        old = {node.name: node for node in ast.parse((FIXTURES / (name + ".txt")).read_text()).body if hasattr(node, "name")}
        new = {node.name: node for node in ast.parse(Path(module.__file__).read_text()).body if hasattr(node, "name")}
        for symbol in names:
            expected = t14_slider_paths(old[symbol]) if symbol == "Slide" else old[symbol]
            actual = ast.parse(ast.unparse(new[symbol])).body[0] if symbol == "Slide" else new[symbol]
            assert ast.dump(expected) == ast.dump(actual), (name, symbol)


@pytest.mark.asyncio
@pytest.mark.parametrize("visible_count,offset,search_id,verify", [
    (10, 10, "stable", False), (9, 10, "stable", False),
    (10, 10, "", False), (10, 10, "stable", True), (10, 0, "stable", False),
])
async def test_first_page_rebuild_evidence_matches_baseline(tmp_path, monkeypatch, visible_count, offset, search_id, verify):
    async def exercise(client_class, *, ports=None):
        calls = []

        class Page:
            def on(self, *args):
                pass

            def locator(self, selector):
                assert selector == '[id^="waterfall_item_"]:visible'
                return self

            async def evaluate_all(self, script):
                return [str(100 + i) for i in range(visible_count)]

        kwargs = {"ports": ports} if ports else {}
        value = client_class(headers={"User-Agent": "fixture"}, playwright_page=Page(), cookie_dict={}, **kwargs)
        payload = {"data": [{"aweme_info": aweme(110)}], "has_more": 1, "extra": {"logid": "rotating"}}
        if verify:
            payload["search_nil_info"] = {"search_nil_type": "verify_check"}
        value._observed_search_responses.append({"keyword": KEYWORD, "offset": offset, "search_id": search_id, "payload": payload})

        async def detail(post_id):
            calls.append(post_id)
            return aweme(post_id)

        value.get_video_by_id = detail
        result = await value._build_visible_first_page_fallback(keyword=KEYWORD, offset=0, search_id="")
        return result, calls

    with legacy(tmp_path / "legacy", monkeypatch) as old:
        before = await exercise(old.client.DouYinClient)
    ports = entry.douyin_dependencies(importlib.import_module("config"))["ports"].client
    after = await exercise(client.DouYinClient, ports=ports)
    assert after == before
    if (visible_count, offset, search_id, verify) == (10, 10, "stable", False):
        assert len(after[0]["data"]) == 10 and after[0]["extra"]["logid"] == "stable"
    else:
        assert after == (None, [])


@pytest.mark.asyncio
async def test_memory_and_slider_lifecycle_match_baseline(tmp_path, monkeypatch):
    with legacy(tmp_path / "legacy", monkeypatch):
        old_cache = importlib.import_module("cache.local_cache").ExpiringLocalCache
        old_slider = importlib.import_module("tools.slider_util")
        for level in ("easy", "hard"):
            assert old_slider.get_tracks(137, level) == login_support.get_tracks(137, level)
        values = []
        for cache_class in (old_cache, login_support.ExpiringLocalCache):
            value = cache_class()
            value.set("dy_phone", b"123456", 120)
            values.append((value.get("dy_phone"), value.keys("dy_*"), value.get("absent")))
            task = value._cron_task
            value.__del__()
            assert task.cancelling()
        assert values[0] == values[1] == (b"123456", ["dy_phone"], None)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("original_error,page_present", [(False, True), (True, True), (True, False)])
async def test_repair_exception_chain_matches_baseline(tmp_path, monkeypatch, original_error, page_present):
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
    with legacy(tmp_path / "legacy", monkeypatch) as old:
        monkeypatch.setattr(old.client.DouYinClient, "get_video_by_id", original)
        old.repair.install_douyin_browser_detail_fallback()
        before = await capture(old.client.DouYinClient(**kwargs))
    trace.clear()
    ports = entry.douyin_dependencies(importlib.import_module("config"))["ports"].client
    monkeypatch.setattr(client.DouYinClient, "_get_video_by_id", original)
    after = await capture(client.DouYinClient(ports=ports, browser_detail_fallback=True, **kwargs))
    assert after == before
    assert after[2] == ("ValueError" if original_error else "NoneType")


@pytest.mark.asyncio
async def test_post_form_and_signature_inputs_match_baseline(tmp_path, monkeypatch):
    async def exercise(module, signer_module, patch, *, ports=None):
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

    with legacy(tmp_path / "legacy", monkeypatch) as old:
        with monkeypatch.context() as patch:
            before = await exercise(old.client, old.signer, patch)
    ports = entry.douyin_dependencies(importlib.import_module("config"))["ports"].client
    with monkeypatch.context() as patch:
        after = await exercise(client, signer, patch, ports=ports)
    assert after == before
    assert after[0][3] == {}  # 保留旧 post 没有传 request_method 的签名输入。
    assert b"a_bogus=fixed%2Bbogus%2F%3D" in after[1][3]
    assert "x-tt-argus" not in dict(after[1][4])
