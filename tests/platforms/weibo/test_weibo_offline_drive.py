"""微博离线边界：依赖方向、移动详情修复链、worker 装配与配置读取。

#211 起整轮搜索驱动不再收集。见 tests/support/weibo_issue211_deviation.py。
"""

from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from playwright.async_api import Error as PlaywrightError
from tenacity import RetryError

from support import worker_scenarios
from support.weibo_adapter import ROOT, settings
from trippostcollect.platforms import entry
from trippostcollect.platforms.weibo import client


def exception_chain(exc):
    result = []
    while exc is not None:
        # RetryError 的 repr 含 Future 内存地址；比较实际终次异常与尝试次数。
        detail = (exc.last_attempt.attempt_number, type(exc.last_attempt.exception()).__name__, str(exc.last_attempt.exception())) if isinstance(exc, RetryError) else str(exc)
        result.append((type(exc).__name__, detail, getattr(exc, "code", None)))
        exc = exc.__cause__
    return result




def test_dependency_direction():
    for path in (ROOT / "src/trippostcollect/platforms/weibo").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = [node.module or ""] if isinstance(node, ast.ImportFrom) else [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
            for module in modules:
                assert module.split(".")[0] not in {"scripts", "config", "tools", "media_platform", "store", "base", "var", "model", "proxy"}, (path, module)
                assert not module.startswith(("trippostcollect.artifacts", "trippostcollect.db", "trippostcollect.scheduler")), (path, module)
                if module.startswith("trippostcollect.application"):
                    assert module == "trippostcollect.application.contracts"
                if module.startswith("trippostcollect.runtime"):
                    assert module in {
                        "trippostcollect.runtime.helpers",
                        "trippostcollect.runtime.page_capture",
                        "trippostcollect.runtime.visit_ledger",
                        "trippostcollect.runtime.human_flow",
                    }, (path, module)
                if module.startswith("trippostcollect.platforms."):
                    assert module.startswith("trippostcollect.platforms.weibo")


# 详情页导航返回的 HTTP 状态由 test_root_repair_page_status_is_classified 覆盖。
REPAIR_SCENARIOS = ["empty", "navigation", "no_page"]


async def repair_chain(patch, scenario, *, page_status=200):
    """同一 fake 下根实现单次修复请求的尝试轨迹与异常链。"""
    trace = []

    async def sleep(_):
        return None

    class HTTP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, **kwargs):
            trace.append((method, url, kwargs))
            return httpx.Response(200, text="missing render data", request=httpx.Request(method, url))

    class Page:
        async def goto(self, url, **kwargs):
            trace.append(("goto", url, kwargs))
            if scenario == "navigation":
                raise PlaywrightError("离线导航失败")
            return SimpleNamespace(status=page_status)

        async def wait_for_timeout(self, timeout):
            trace.append(("wait", timeout))

        async def evaluate(self, script, note_id):
            trace.append(("evaluate", script, note_id))
            return {"id": "wrong", "text": "其他帖子"}

    page = None if scenario == "no_page" else Page()
    patch.setenv("TRIPPOSTCOLLECT_POST_REPAIR", "1")
    patch.setenv("TRIPPOSTCOLLECT_WEIBO_BROWSER_DETAIL_TIMEOUT_MS", "100")
    ports = entry.weibo_dependencies(settings())[1].client
    ports = replace(ports, make_async_client=lambda **kwargs: HTTP())
    patch.setattr(client.WeiboClient._get_note_info_direct.retry, "sleep", sleep)
    instance = client.WeiboClient(headers={"Cookie": "SUB=t05"}, playwright_page=page, cookie_dict={}, ports=ports, post_repair=True)
    with pytest.raises(Exception) as error:
        await instance.get_note_info_by_id("123")
    return trace, exception_chain(error.value)


def check_repair_chain(new, scenario):
    trace, errors = new
    assert len([row for row in trace if row[0] == "GET"]) == 3
    assert not any(row[0] == "api" for row in trace)
    if scenario == "empty":
        assert "attempts=['page_state:empty_detail']" in errors[0][1]
        assert errors[1][0] == "DataFetchError"
    if scenario != "no_page":
        assert next(row for row in trace if row[0] == "goto")[2]["timeout"] == 5000


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", REPAIR_SCENARIOS)
async def test_root_repair_attempts_and_exception_chain(scenario):
    with pytest.MonkeyPatch.context() as patch:
        new = await repair_chain(patch, scenario)
    check_repair_chain(new, scenario)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,error,code,attempts",
    [
        (403, "PlatformRuntimeError", "login_required", None),
        (401, "PlatformRuntimeError", "login_required", None),
        (429, "PlatformRuntimeError", "rate_limited", None),
        (432, "PlatformRuntimeError", "blocked_or_forbidden", None),
        (404, "DataFetchError", None, "attempts=['page_state:http_404']"),
    ],
)
async def test_root_repair_page_status_is_classified(status, error, code, attempts):
    """#114：回退只读页面导航响应的状态；401/403/429/风控即阻断，其他非 2xx 视为详情不可用。"""
    with pytest.MonkeyPatch.context() as patch:
        trace, errors = await repair_chain(patch, "page_status", page_status=status)
    assert errors[0][0] == error and errors[0][2] == code
    if attempts:
        assert attempts in errors[0][1]
    assert not any(row[0] in {"evaluate", "wait", "api"} for row in trace)
    assert [row[1] for row in trace if row[0] == "goto"] == ["https://m.weibo.cn/detail/123"]


@pytest.mark.parametrize("post_repair", ["0", "1"])
def test_worker_assembly_uses_root_weibo(tmp_path, post_repair):
    from support.isolated_python import json_tail as _json_tail, run_python as _run_python

    result = _json_tail(_run_python(
        "import os, sys, json\n"
        f"os.environ['TRIPPOSTCOLLECT_POST_REPAIR'] = {post_repair!r}\n"
        "from trippostcollect.platforms.entry import configure, install_hooks, load_crawler\n"
        f"configure({worker_scenarios.worker_argv('weibo_search', tmp_path)!r})\n"
        "install_hooks()\n"
        "cls = load_crawler('wb')\n"
        "instance = cls()\n"
        "assert not any(name == p or name.startswith(p + '.') for name in sys.modules for p in ('media_platform.weibo', 'store.weibo'))\n"
        "assert instance.__class__.__module__ == 'trippostcollect.platforms.weibo.core'\n"
        "others = [load_crawler(code).__name__ for code in ('dy', 'zhihu', 'xhs')]\n"
        "print(json.dumps({'repair': instance.ports.post_repair, 'others': others}))\n",
        cwd=tmp_path,
    ))
    assert result == {"repair": post_repair == "1", "others": ["DouYinCrawler", "ZhihuCrawler", "XiaoHongShuCrawler"]}


@pytest.mark.parametrize("login,uid,expected", [(True, "123", True), (True, "", False), (False, "123", False)])
def test_warmup_requires_mobile_login_and_uid(monkeypatch, login, uid, expected):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
    import mediacrawler_login_warmup as warmup

    class Context:
        async def cookies(self, urls):
            assert urls == ["https://m.weibo.cn"]
            return [{"name": "WBPSESS", "value": "desktop-only"}]

    async def api(_):
        return {"ok": True, "login": login, "uid": uid}

    monkeypatch.setattr("trippostcollect.application.warmup.weibo_api_check", api)
    state = asyncio.run(warmup.current_state(Context(), SimpleNamespace(url="https://m.weibo.cn"), "weibo"))
    assert state["ok"] is expected


def test_configuration_snapshot_and_operation_readers(monkeypatch):
    from trippostcollect.application.worker_inputs import weibo_input_readers

    config = settings()
    snapshot, ports = entry.weibo_dependencies(config)
    config.KEYWORDS = "崂山旅游"
    assert snapshot.KEYWORDS == "青岛旅游"
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "2")
    assert ports.refresh_max_pages() == 2
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "invalid")
    assert ports.refresh_max_pages() == 0
    mapping = {"TRIPPOSTCOLLECT_WEIBO_BROWSER_DETAIL_TIMEOUT_MS": "bad"}
    reader = weibo_input_readers(environ=mapping).detail_timeout
    with pytest.raises(ValueError):
        reader()
    mapping["TRIPPOSTCOLLECT_WEIBO_BROWSER_DETAIL_TIMEOUT_MS"] = "12345"
    assert reader() == 12345
