"""抖音离线边界：依赖方向、搜索截获、worker 装配与签名输入。

#222 起整轮搜索驱动不再收集。见 tests/support/douyin_issue222_deviation.py。
"""

from __future__ import annotations

import ast
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from trippostcollect.application.worker_inputs import worker_config
from trippostcollect.platforms import entry
from trippostcollect.platforms.douyin import client, login_support, parser, signer


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


#222 起整轮 crawler.start() 驱动不再收集。见 tests/support/douyin_issue222_deviation.py。


def test_dependency_direction():
    allowed_runtime = {
        "trippostcollect.runtime.helpers": {"extract_url_params_to_dict", "normalize_image_url"},
        "trippostcollect.runtime.cookies": {"convert_cookies", "convert_str_cookie_to_dict"},
        "trippostcollect.runtime.image_retry": {
            "ImageDownloadFetchError", "classified_http_image_error", "is_runtime_blocking_image_error",
        },
        "trippostcollect.runtime.page_capture": {
            "CaptureRule", "PageCaptureChannel", "CapturedResponse",
        },
        "trippostcollect.runtime.visit_ledger": {"VisitLedger", "content_visits_path"},
        "trippostcollect.runtime.human_pointer": {"ElementNotInView", "pointer_for"},
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


